"""
LLM-based relation extraction using Anthropic or OpenAI-compatible APIs.

Complements the rule-based extractor in stage4 by:
- Extracting complex multi-hop and implicit relations
- Extracting cross-layer relations (concept->mechanism, mechanism->method, etc.)
- Handling Chinese/English mixed text
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a structured knowledge extraction assistant for a network communication ontology.

Your task: given a text segment and a set of ontology node IDs, extract (subject, predicate, object) triples where:
- subject and object MUST be node IDs from the provided candidate list
- predicate: FIRST try to use one from the provided valid relations list
- IMPORTANT: if the text clearly expresses a relationship that does NOT fit any predicate in the valid list, you MUST create a new predicate name (lowercase_with_underscores, e.g. "replaces", "supersedes", "enables", "recovers_from"). Do NOT force-fit into an existing predicate when the semantics don't match. New predicates will be reviewed as candidate relations.
- Only extract triples that are clearly stated or strongly implied by the text

Return ONLY a JSON array. Each element: {"subject": "<node_id>", "predicate": "<relation_id>", "object": "<node_id>"}
Return [] if no valid triples can be extracted.
Do not add explanation or markdown formatting outside the JSON array.
"""

_USER_TEMPLATE = """\
## Ontology nodes (use these IDs only):
{node_list}

## Valid predicates (use these only):
{relation_list}

## Text segment:
{text}

Extract triples as a JSON array:"""


RST_RELATION_TYPES = [
    # ── Causal / logical ──────────────────────────────────────
    "Cause-Result",      # A causes B (retrospective)
    "Result-Cause",      # B is because of A (reverse narrative order)
    "Purpose",           # A is done in order to achieve B (prospective)
    "Means",             # B is the method/path to accomplish A
    # ── Conditional / enablement ──────────────────────────────
    "Condition",         # if A then B
    "Unless",            # unless A, B holds
    "Enablement",        # A makes B possible (prerequisite)
    # ── Elaborative / refinement ──────────────────────────────
    "Elaboration",       # B elaborates or details A
    "Explanation",       # B explains the mechanism/rationale of A
    "Restatement",       # B restates A in different words
    "Summary",           # B summarises A
    # ── Contrastive / concessive ──────────────────────────────
    "Contrast",          # A and B form a parallel comparison
    "Concession",        # despite A, B (acknowledge A, pivot to B)
    # ── Evidential / evaluative ───────────────────────────────
    "Evidence",          # B provides evidence supporting A's claim
    "Evaluation",        # B evaluates or judges A
    "Justification",     # B justifies the action/decision stated in A
    # ── Structural / organisational ───────────────────────────
    "Background",        # A provides background for understanding B
    "Preparation",       # A prepares the reader's attention for B
    "Sequence",          # A precedes B in temporal/logical order
    "Joint",             # A and B are co-enumerated at the same level
    "Problem-Solution",  # A states a problem; B provides solution
]

_RST_SYSTEM_PROMPT = """\
You are a discourse analyst for technical texts.

Given two adjacent text fragments (EDU-A then EDU-B) from the same document,
identify the most appropriate RST (Rhetorical Structure Theory) relation type.

Choose EXACTLY ONE type from this list (grouped by category):

Causal/logical:     Cause-Result, Result-Cause, Purpose, Means
Conditional:        Condition, Unless, Enablement
Elaborative:        Elaboration, Explanation, Restatement, Summary
Contrastive:        Contrast, Concession
Evidential:         Evidence, Evaluation, Justification
Structural:         Background, Preparation, Sequence, Joint, Problem-Solution

Return ONLY a JSON array of relation objects, one per pair.
Each object: {"src_idx": <int>, "relation_type": "<type>"}
No explanation outside the JSON array."""

_RST_USER_TEMPLATE = """\
Analyse the following EDU pairs and return RST relation types:

{pairs_text}

Return JSON array:"""


class LLMExtractor:
    # Circuit breaker: after this many consecutive failures, auto-disable
    _CIRCUIT_BREAKER_THRESHOLD = 3
    # Auto-disable duration in seconds (10 minutes)
    _CIRCUIT_BREAKER_COOLDOWN = 600

    def __init__(self) -> None:
        self._client = None
        self._http_client: httpx.Client | None = None
        self._enabled: bool | None = None
        self._consecutive_failures: int = 0
        self._circuit_open_until: float = 0.0  # monotonic timestamp

    def is_enabled(self) -> bool:
        if self._enabled is None:
            from src.config.settings import settings
            self._enabled = settings.LLM_ENABLED and bool(settings.LLM_API_KEY)
        if not self._enabled:
            return False
        # Circuit breaker: if too many consecutive failures, temporarily disable
        if self._circuit_open_until > 0:
            import time
            if time.monotonic() < self._circuit_open_until:
                return False
            # Cooldown expired — reset and allow retry
            log.info("LLM circuit breaker reset, allowing retry")
            self._circuit_open_until = 0.0
            self._consecutive_failures = 0
        return True

    def _record_success(self) -> None:
        """Reset circuit breaker on successful LLM call."""
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _record_failure(self) -> None:
        """Track consecutive failures; trip circuit breaker if threshold exceeded."""
        import time
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._CIRCUIT_BREAKER_THRESHOLD:
            self._circuit_open_until = time.monotonic() + self._CIRCUIT_BREAKER_COOLDOWN
            log.warning(
                "LLM circuit breaker tripped after %d consecutive failures. "
                "Disabling LLM for %ds.",
                self._consecutive_failures,
                self._CIRCUIT_BREAKER_COOLDOWN,
            )

    def _is_openai_style(self) -> bool:
        from src.config.settings import settings
        base = settings.LLM_BASE_URL.lower().rstrip("/")
        if "anthropic" in base or base.endswith("/messages"):
            return False
        if "deepseek" in base or "openai" in base:
            return True
        if "chat/completions" in base or base.endswith("/v1"):
            return True
        return False

    def _openai_url(self) -> str:
        from src.config.settings import settings
        base = settings.LLM_BASE_URL.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if "chat/completions" in base:
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/chat/completions"

    def _get_http_client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=180.0)
        return self._http_client

    def _get_client(self):
        if self._is_openai_style():
            return None
        if self._client is not None:
            return self._client
        try:
            import anthropic
            from src.config.settings import settings
            self._client = anthropic.Anthropic(
                api_key=settings.LLM_API_KEY,
                base_url=settings.LLM_BASE_URL,
            )
        except ImportError:
            log.warning("anthropic SDK not installed; LLM extraction disabled. "
                        "Run: pip install anthropic")
            self._enabled = False
        return self._client

    def _call_llm(self, system: str, prompt: str, max_tokens: int) -> str | None:
        if self._is_openai_style():
            return self._call_openai(system, prompt, max_tokens)
        client = self._get_client()
        if client is None:
            return None
        from src.config.settings import settings
        try:
            response = client.messages.create(
                model=settings.LLM_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            self._record_success()
            return response.content[0].text.strip()
        except Exception as exc:
            log.warning("LLM request failed: %s", exc)
            self._record_failure()
            return None

    def _call_openai(self, system: str, prompt: str, max_tokens: int) -> str | None:
        from src.config.settings import settings
        url = self._openai_url()
        headers = {
            "Authorization": f"Bearer {settings.LLM_API_KEY}",
            "Content-Type": "application/json",
        }
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": settings.LLM_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        try:
            resp = self._get_http_client().post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return ""
            message = choices[0].get("message") or {}
            self._record_success()
            return (message.get("content") or "").strip()
        except Exception as exc:
            log.warning("LLM request failed: %s", exc)
            self._record_failure()
            return None

    def extract(
        self,
        text: str,
        candidate_node_ids: list[str],
        valid_relations: list[str],
    ) -> list[dict[str, Any]]:
        """
        Extract SPO triples from text.

        Args:
            text: The segment text to analyse.
            candidate_node_ids: Ontology node IDs relevant to this segment.
            valid_relations: Allowed predicate IDs.

        Returns:
            List of dicts with keys: subject, predicate, object.
            Empty list on failure or if LLM is disabled.
        """
        if not self.is_enabled():
            return []
        if not text.strip() or not candidate_node_ids:
            return []

        from src.config.settings import settings

        node_list = "\n".join(f"- {n}" for n in candidate_node_ids[:80])
        relation_list = "\n".join(f"- {r}" for r in valid_relations[:60])
        text_truncated = text[:2000]

        prompt = _USER_TEMPLATE.format(
            node_list=node_list,
            relation_list=relation_list,
            text=text_truncated,
        )

        raw = self._call_llm(_SYSTEM_PROMPT, prompt, settings.LLM_MAX_TOKENS)
        if raw is None:
            return []
        triples = self._parse_response(raw, set(candidate_node_ids), set(valid_relations))
        log.debug("LLM extracted %d triples from segment", len(triples))
        return triples

    def extract_rst_relations(
        self,
        edu_pairs: list[tuple[str, str, str, str]],
    ) -> list[str]:
        """
        Extract RST relation types for a list of adjacent EDU pairs.

        Args:
            edu_pairs: List of (src_id, src_text, dst_id, dst_text).

        Returns:
            List of relation_type strings, one per input pair.
            Falls back to "Sequence" for each pair if LLM is unavailable.
        """
        fallback = ["Sequence"] * len(edu_pairs)
        if not self.is_enabled() or not edu_pairs:
            return fallback

        from src.config.settings import settings

        lines = []
        for i, (_, src_text, _, dst_text) in enumerate(edu_pairs):
            lines.append(
                f"Pair {i}:\n"
                f"  EDU-A: {src_text[:300]}\n"
                f"  EDU-B: {dst_text[:300]}"
            )
        pairs_text = "\n\n".join(lines)

        prompt = _RST_USER_TEMPLATE.format(pairs_text=pairs_text)

        raw = self._call_llm(_RST_SYSTEM_PROMPT, prompt, 256 + 32 * len(edu_pairs))
        if raw is None:
            return fallback
        return self._parse_rst_response(raw, len(edu_pairs))

    def _parse_rst_response(self, raw: str, expected: int) -> list[str]:
        """Parse RST JSON response; fill gaps with 'Sequence'."""
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                return ["Sequence"] * expected
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                return ["Sequence"] * expected

        if not isinstance(data, list):
            return ["Sequence"] * expected

        result = ["Sequence"] * expected
        valid = set(RST_RELATION_TYPES)
        for item in data:
            if not isinstance(item, dict):
                continue
            idx = item.get("src_idx")
            rel = item.get("relation_type", "Sequence")
            if isinstance(idx, int) and 0 <= idx < expected:
                result[idx] = rel if rel in valid else "Sequence"
        return result

    def generate_title(self, text: str) -> str | None:
        """
        Generate a concise title (<=255 chars) for an EDU text fragment.

        Returns None if LLM is disabled, so the caller can fall back to
        extracting the first sentence.
        """
        if not self.is_enabled():
            return None
        system = (
            "Summarise the following technical text in one short phrase "
            "(<=15 words, no punctuation at the end). "
            "Return ONLY the phrase, nothing else."
        )
        raw = self._call_llm(system, text[:800], 64)
        if raw is None:
            return None
        title = raw.strip().rstrip(".")
        return title[:255] if title else None

    def extract_candidate_terms(
        self,
        text: str,
        known_terms: list[str],
    ) -> list[dict]:
        """Extract domain terms from text that are NOT in the known ontology.

        Args:
            text: Raw segment text (preserving original case).
            known_terms: List of known ontology canonical names / aliases.

        Returns:
            List of dicts: [{"term": "...", "reason": "..."}]
            Empty list if LLM disabled or no candidates found.
        """
        if not self.is_enabled() or not text.strip():
            return []

        known_sample = ", ".join(known_terms[:80])
        system = (
            "You are a network engineering terminology extractor.\n"
            "Given a text segment and a list of known ontology concepts, "
            "identify NEW technical terms that are NOT in the known list "
            "but SHOULD be added to a networking knowledge base.\n\n"
            "Return ONLY a JSON array. Each element:\n"
            '{"term": "<exact surface form>", "reason": "<why this is a domain concept>"}\n\n'
            "Rules:\n"
            "- Only return networking/telecom domain-specific terms\n"
            "- Skip generic words, document structure words, author names, dates\n"
            "- Include: protocol names, mechanisms, configuration objects, network functions\n"
            "- Include multi-word terms (e.g. 'route reflector', 'forwarding equivalence class')\n"
            "- Return [] if no new terms found"
        )
        prompt = (
            f"Known concepts: {known_sample}\n\n"
            f"Text:\n{text[:2000]}\n\n"
            "Extract new domain terms as JSON array:"
        )

        raw = self._call_llm(system, prompt, 512)
        if raw is None:
            return []

        return self._parse_candidate_terms(raw)

    def _parse_candidate_terms(self, raw: str) -> list[dict]:
        """Parse LLM response for candidate terms."""
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                return []
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                return []

        if not isinstance(data, list):
            return []

        results = []
        for item in data:
            if not isinstance(item, dict):
                continue
            term = item.get("term", "").strip()
            if term and len(term) >= 2:
                results.append({
                    "term": term,
                    "reason": item.get("reason", ""),
                })
        return results

    def ping(self, timeout: float = 15.0) -> bool:
        """Return True if LLM is reachable when enabled.

        Uses a short timeout (default 15s) so health checks don't block startup.
        """
        from src.config.settings import settings
        if not settings.LLM_ENABLED:
            log.info("LLM disabled; skipping health check.")
            return True
        if not settings.LLM_API_KEY:
            log.error("LLM enabled but LLM_API_KEY is empty.")
            return False

        # Use a dedicated short-timeout client for ping
        saved_client = self._http_client
        try:
            self._http_client = httpx.Client(timeout=timeout)
            raw = self._call_llm("Health check. Reply with 'ok'.", "ping", 1)
        finally:
            if self._http_client is not None:
                self._http_client.close()
            self._http_client = saved_client

        if raw is None:
            log.warning("LLM ping failed (timeout=%.0fs).", timeout)
            return False
        log.info("LLM ping ok.")
        return True

    def _parse_response(
        self,
        raw: str,
        valid_nodes: set[str],
        valid_relations: set[str],
    ) -> list[dict[str, Any]]:
        """Parse and validate the LLM JSON response."""
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
        raw = raw.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                return []
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                return []

        if not isinstance(data, list):
            return []

        results = []
        for item in data:
            if not isinstance(item, dict):
                continue
            subj = item.get("subject", "")
            pred = item.get("predicate", "")
            obj = item.get("object", "")
            if subj in valid_nodes and obj in valid_nodes and pred:
                if subj != obj:
                    results.append({"subject": subj, "predicate": pred, "object": obj})

        return results
