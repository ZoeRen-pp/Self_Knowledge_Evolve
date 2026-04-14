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
You are a structured knowledge extraction assistant for a 5-layer network communication ontology.

The ontology has 5 knowledge layers:
- **Concept** (IP.*): Configurable objects on network devices (interfaces, protocol instances, policies, VPN instances, etc.)
- **Mechanism** (MECH.*): How things work — protocol algorithms, forwarding mechanisms, isolation principles
- **Method** (METHOD.*): How to do it — configuration procedures, verification methods, troubleshooting methods
- **Condition** (COND.*): When to use it — applicability conditions, constraints, risks, decision rules
- **Scenario** (SCENE.*): Real-world use cases — deployment patterns, business scenarios

Cross-layer relationships to look for:
- concept ←explains→ mechanism: "OSPF uses link-state flooding" → (IP.OSPF_INSTANCE, explains, MECH.LinkStateFlooding)
- mechanism ←implemented_by→ method: "configure BGP policy to implement path selection" → (MECH.BestPathSelection, implemented_by, METHOD.BGPPolicyConfigurationMethod)
- method ←applies_to→ condition: "this method applies to large-scale networks" → (METHOD.X, applies_to, COND.LargeScaleApplicability)
- scenario ←composed_of→ method/condition: "DC fabric scenario uses EVPN-VXLAN provisioning" → (SCENE.X, composed_of, METHOD.Y)

Your task: given a text segment and candidate node IDs from ALL layers, extract (subject, predicate, object) triples where:
- subject and object MUST be node IDs from the provided candidate list
- predicate: FIRST try to use one from the provided valid relations list
- IMPORTANT: if the text clearly expresses a relationship that does NOT fit any predicate in the valid list, you MUST create a new predicate name (lowercase_with_underscores). New predicates will be reviewed as candidate relations.
- Extract BOTH intra-layer and cross-layer relationships
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
    # ── Semantic expansion ────────────────────────────────────
    "Elaboration",       # B adds more detail / sub-topics on A's main topic   [NS]
    "Exemplification",   # B gives a concrete example or illustration of A      [NS]
    # ── Logical structure ─────────────────────────────────────
    "Sequence",          # B follows A in strict temporal / procedural order    [NN]
    "Causation",         # A causes B, or B explains why A holds                [NS]
    "Contrast",          # B presents an alternative, opposing option to A      [NN]
    # ── Normative / constraint ────────────────────────────────
    "Constraint",        # B imposes MUST/SHALL/SHOULD rules governing A        [NS]
    "Condition",         # A is the condition; B is the conditioned content     [SN]
    "Prerequisite",      # A must be done/understood before B can be applied    [SN]
    # ── Support / context ─────────────────────────────────────
    "Evidence",          # B provides spec citations / data supporting A        [NS]
    "Background",        # A provides context / motivation needed for B         [SN]
]

_RST_SYSTEM_PROMPT = """\
You are a discourse analyst for technical network engineering documents.

Given two adjacent paragraphs (A then B) from the same document, classify their
rhetorical relation using EXACTLY ONE type from this list and assign nuclearity.

RELATION TYPES AND NUCLEARITY:
- Elaboration:     B adds more detail, sub-topics, or clarification on A.
                   Nuclearity: NS  (A is the main point; B elaborates)
- Exemplification: B gives a concrete example, scenario, or illustration of A.
                   Nuclearity: NS  (A is the main point; B is the example)
- Sequence:        B follows A in strict temporal or procedural order
                   (numbered steps, first/then/next/finally).
                   Nuclearity: NN  (both equal; order is the key signal)
- Causation:       A causes or leads to B, or B explains why A holds.
                   Nuclearity: NS  (A is cause/nucleus; B is effect or explanation)
- Contrast:        B presents an alternative option, opposing behavior, or
                   counterpoint to A. Both are equally important.
                   Nuclearity: NN  (both equal)
- Constraint:      B imposes a normative rule (MUST/SHALL/MUST NOT/is required/
                   is prohibited) that limits or governs the behavior in A.
                   Nuclearity: NS  (A is the described behavior; B constrains it)
- Condition:       B states an action or outcome; A specifies when B applies
                   (if A then B; unless A; when A; only if A).
                   Nuclearity: SN  (B is nucleus — the conditioned content)
- Prerequisite:    A is something that must be understood or completed BEFORE B
                   can be applied. B is the main task or concept.
                   Nuclearity: SN  (B is nucleus — the main task)
- Evidence:        B provides supporting data, RFC/spec citations, or factual
                   justification for the claim made in A.
                   Nuclearity: NS  (A is nucleus — the claim; B supports it)
- Background:      A provides contextual information (history, motivation, overview)
                   needed to understand B. B is the main content.
                   Nuclearity: SN  (B is nucleus — the main content)

DECISION RULES (apply in order, stop at first match):
1. "for example / consider / as an illustration / e.g." in B → Exemplification
2. Numbered steps, or "first … then … next … finally" → Sequence
3. "however / in contrast / alternatively / whereas / on the other hand" → Contrast
4. "MUST / SHALL / MUST NOT / is required / is prohibited" in B → Constraint
5. "if / when / unless / only if / in case" starts or dominates B → Condition
6. "before / requires / assumes / prerequisite / first ensure" in A → Prerequisite
7. RFC §, spec table, benchmark data, measurement result in B → Evidence
8. History, motivation, "background", overview paragraph before technical detail → Background
9. "because / therefore / as a result / this causes / leads to" → Causation
10. B continues the same topic with more detail → Elaboration (default)

Return ONLY a JSON array. Each element:
{"src_idx": <int>, "relation_type": "<type>", "nuclearity": "NS"|"SN"|"NN"}
One element per input pair. No explanation outside the JSON array."""

_RST_USER_TEMPLATE = """\
Classify the rhetorical relation between each pair of adjacent paragraphs:

{pairs_text}

Return JSON array:"""


SEGMENT_TYPES = [
    # 知识本质层 — what it IS
    "definition",       # concept/term/protocol field/data format definition
    "mechanism",        # how a protocol/algorithm/process works internally
    "scenario",         # deployment pattern, business use case, topology design context
    # 操作层 — how to DO it
    "config",           # configuration syntax, parameter description, single-step command
    "procedure",        # ordered multi-step workflow with verification/rollback steps
    "prerequisite",     # preconditions required before a task/configuration can start
    # 条件层 — when/under what
    "constraint",       # protocol-internal MUST/SHALL normative rules, limits, boundaries
    "compatibility",    # cross-vendor/version behavior differences, interoperability facts
    # 评估建议层 — how to JUDGE
    "best_practice",    # design recommendations, security guidance, anti-patterns
    "performance",      # metrics, sizing, capacity, timer values, throughput benchmarks
    "comparison",       # technology/option tradeoff analysis for decision-making
    "fault",            # fault symptoms, error states, commissioning failures, alarms
    "troubleshooting",  # diagnostic steps, verification procedures, debug commands
    # 结构层 — non-prose
    "table",            # meaningful data tables (parameter reference, feature matrix)
    "code",             # CLI output, config snippets, code blocks
    # 过滤层
    "noise",            # TOC, boilerplate, navigation, author page → DROPPED from KB
    "unknown",          # substantive but no clear type fits above
]

_SEGTYPE_SYSTEM_PROMPT = """\
You are a technical document analyst for a network engineering knowledge base.

Classify each text segment into EXACTLY ONE type from this list:

KNOWLEDGE TYPES:
- definition: What something IS — defines a concept, term, data format, protocol field,
  or object. Usually contains "is defined as", "refers to", "is a", field/value descriptions.
- mechanism: How something WORKS internally — protocol algorithm, state machine, message
  exchange, forwarding logic, timer behaviour. Explains causality or internal process.
- scenario: WHERE/WHEN to use something — deployment topology, business use case, network
  design context, integration pattern. Describes real-world application, not the protocol itself.

OPERATION TYPES:
- config: Single-step CLI syntax, parameter descriptions, command reference, configuration
  knobs for ONE thing. Does NOT include sequential steps or verification.
- procedure: Ordered multi-step workflow — "Step 1 ... Step 2 ... verify ..." — with
  sequencing words (first/then/next/finally) or numbered steps. Includes rollback or
  verification as part of the workflow. DISTINCT from config (config = syntax, procedure = workflow).
- prerequisite: Conditions that MUST be satisfied BEFORE a task starts — "Before configuring X,
  ensure Y is enabled", requirements for the operator's environment, not the protocol's own rules.
  DISTINCT from constraint (constraint = protocol rule, prerequisite = task precondition).

CONDITION TYPES:
- constraint: Protocol-internal normative rules — MUST/SHALL/MUST NOT from an RFC or spec,
  protocol-defined limits (max prefix, timer range, packet size). Applies regardless of operator
  choice. DISTINCT from prerequisite (which is about what the operator must set up first).
- compatibility: Cross-vendor/version behavioral differences, interoperability facts, known
  incompatibilities between implementations. "Vendor X does Y differently", "RFC-compliant but
  Cisco and Juniper diverge on". DISTINCT from comparison (compatibility = interop fact,
  comparison = decision tradeoff).

EVALUATION TYPES:
- best_practice: Design recommendations, security hardening, anti-patterns, "should" guidance
  that goes beyond normative protocol rules.
- performance: Quantitative metrics — throughput numbers, latency figures, timer values,
  capacity sizing, convergence time benchmarks.
- comparison: Tradeoff analysis between two or more options to support a decision —
  "Option A vs Option B: A is better for X because ..., B for Y because ...".
  DISTINCT from compatibility (comparison = help you choose, compatibility = warn you about interop).
- fault: Observable failure symptoms, error states, alarm conditions, commissioning failures.
  What GOES WRONG and what it looks like.
- troubleshooting: Diagnostic steps to identify root cause, verification commands, debug
  procedures. HOW to investigate a fault. Often follows a fault segment.

STRUCTURAL TYPES:
- table: Meaningful data table — parameter reference, feature matrix, timer value table.
  Pure structure with significant data payload. Boilerplate formatted as table = noise.
- code: CLI command output, running configuration snippet, pseudocode, script block.

FILTER TYPES:
- noise: DOCUMENT STRUCTURE NOISE — table of contents, RFC index listings ("| rfc NNNN | ..."),
  author pages/bios, navigation menus, page headers/footers, copyright boilerplate, bare
  reference lists, "on this page" sidebars, section numbering lists. NOT technical content.
- unknown: Substantive technical content that genuinely does not fit any specific type above.
  Prefer this over forcing an incorrect type.

Decision rules:
1. noise vs table: Is the table technical data or document chrome? RFC index = noise.
2. config vs procedure: Is it one command/parameter, or a numbered/sequenced workflow? Workflow = procedure.
3. constraint vs prerequisite: Is it a protocol rule (MUST in spec) or an operator setup step? Setup step = prerequisite.
4. compatibility vs comparison: Is it reporting how implementations differ (fact) or helping choose between options (decision)? Fact = compatibility.
5. scenario vs mechanism: Does it describe a real deployment context (where/why) or explain internal protocol behaviour (how)? Deployment = scenario.
6. Be decisive about noise — RFC index, TOC, author bio, boilerplate reference lists are always noise.

Return ONLY a JSON array. Each element: {"idx": <int>, "type": "<type>"}.
One element per input segment. No explanation outside the JSON array."""

_SEGTYPE_USER_TEMPLATE = """\
Classify the following segments:

{segments_text}

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
            self._http_client = httpx.Client(
                timeout=httpx.Timeout(
                    connect=15.0,   # SSL handshake must complete in 15s
                    read=120.0,     # LLM generation can be slow
                    write=30.0,
                    pool=30.0,
                ),
            )
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
        import time as _time

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

        last_exc = None
        for attempt in range(1, 4):  # up to 3 attempts
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
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as exc:
                # Transient network issue — retry after short backoff
                last_exc = exc
                if attempt < 3:
                    log.debug("LLM request attempt %d/3 failed (%s), retrying...", attempt, type(exc).__name__)
                    _time.sleep(2 * attempt)
                    continue
            except Exception as exc:
                # Non-transient (auth error, 4xx, etc.) — fail immediately
                log.warning("LLM request failed: %s", exc)
                self._record_failure()
                return None

        log.warning("LLM request failed after 3 attempts: %s", last_exc)
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

    # Alias used by stage4 and backfill when app.llm is LLMExtractor directly
    extract_triples = extract

    # Max pairs per LLM call. Each pair contributes ~800 chars of prompt;
    # 15 pairs ≈ 12 000 chars — well within context limits and avoids timeouts.
    _RST_BATCH_SIZE = 15

    def extract_rst_relations(
        self,
        edu_pairs: list[tuple],
    ) -> list[dict]:
        """
        Extract RST relation type + nuclearity for a list of adjacent paragraph pairs.

        Each pair is independent — A→B relation depends only on those two paragraphs,
        not on the rest of the document. Pairs are processed in batches of
        _RST_BATCH_SIZE to avoid prompt-size timeouts on large documents.

        Args:
            edu_pairs: List of tuples, each either:
                (src_id, src_text, dst_id, dst_text)  — legacy 4-tuple
                (src_id, src_text, src_type, dst_id, dst_text, dst_type)  — 6-tuple with types

            The segment_type hints (src_type / dst_type) are injected into the
            prompt so the LLM can use structural cues even when text is truncated.

        Returns:
            List of dicts {"relation_type": str, "nuclearity": "NS"|"SN"|"NN"},
            one per input pair. Falls back to Elaboration/NN on failure.
        """
        fallback = {"relation_type": "Elaboration", "nuclearity": "NN"}
        if not self.is_enabled() or not edu_pairs:
            return [fallback] * len(edu_pairs)

        results: list[dict] = []
        batch_size = self._RST_BATCH_SIZE

        for start in range(0, len(edu_pairs), batch_size):
            batch = edu_pairs[start : start + batch_size]
            lines = []
            for i, pair in enumerate(batch):
                if len(pair) == 6:
                    _, src_text, src_type, _, dst_text, dst_type = pair
                    type_hint = f" [type: {src_type}]"
                    type_hint_b = f" [type: {dst_type}]"
                else:
                    _, src_text, _, dst_text = pair
                    type_hint = type_hint_b = ""
                lines.append(
                    f"Pair {i}:\n"
                    f"  Paragraph A{type_hint}: {src_text[:600]}\n"
                    f"  Paragraph B{type_hint_b}: {dst_text[:600]}"
                )
            prompt = _RST_USER_TEMPLATE.format(pairs_text="\n\n".join(lines))
            raw = self._call_llm(_RST_SYSTEM_PROMPT, prompt, 32 + 40 * len(batch))
            if raw is None:
                results.extend([fallback] * len(batch))
            else:
                results.extend(self._parse_rst_response(raw, len(batch)))

        return results

    def classify_segment_types(
        self,
        segments: list[dict],
        batch_size: int = 20,
    ) -> list[str]:
        """Batch-classify segment_type for a list of segments.

        Args:
            segments: List of dicts, each with at least `raw_text` (optionally
                `section_title` to provide context).
            batch_size: How many segments to send per LLM request.

        Returns:
            A list of segment_type strings, parallel to `segments`.
            Falls back to "unknown" for every entry if LLM is unavailable.
        """
        if not segments:
            return []
        if not self.is_enabled():
            return ["unknown"] * len(segments)

        results: list[str] = ["unknown"] * len(segments)
        for start in range(0, len(segments), batch_size):
            batch = segments[start : start + batch_size]
            types = self._classify_segtype_batch(batch)
            for i, t in enumerate(types):
                results[start + i] = t
        return results

    def _classify_segtype_batch(self, batch: list[dict]) -> list[str]:
        lines: list[str] = []
        for i, seg in enumerate(batch):
            title = (seg.get("section_title") or "").strip()
            text = (seg.get("raw_text") or "").strip()[:600]
            block = f"Segment {i}:"
            if title:
                block += f"\n  Title: {title[:120]}"
            block += f"\n  Text: {text}"
            lines.append(block)
        prompt = _SEGTYPE_USER_TEMPLATE.format(segments_text="\n\n".join(lines))

        raw = self._call_llm(
            _SEGTYPE_SYSTEM_PROMPT,
            prompt,
            max_tokens=128 + 24 * len(batch),
        )
        if raw is None:
            return ["unknown"] * len(batch)
        return self._parse_segtype_response(raw, len(batch))

    def _parse_segtype_response(self, raw: str, expected: int) -> list[str]:
        """Parse segment_type JSON response; fill gaps with 'unknown'."""
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                return ["unknown"] * expected
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                return ["unknown"] * expected

        if not isinstance(data, list):
            return ["unknown"] * expected

        result = ["unknown"] * expected
        valid = set(SEGMENT_TYPES)
        for item in data:
            if not isinstance(item, dict):
                continue
            idx = item.get("idx")
            t = item.get("type", "unknown")
            if isinstance(idx, int) and 0 <= idx < expected:
                result[idx] = t if t in valid else "unknown"
        return result

    def _parse_rst_response(self, raw: str, expected: int) -> list[dict]:
        """Parse RST JSON response; fill gaps with Elaboration/NN."""
        _default = {"relation_type": "Elaboration", "nuclearity": "NN"}
        _valid_rel = set(RST_RELATION_TYPES)
        _valid_nuc = {"NS", "SN", "NN"}

        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                return [_default.copy() for _ in range(expected)]
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                return [_default.copy() for _ in range(expected)]

        if not isinstance(data, list):
            return [_default.copy() for _ in range(expected)]

        result = [_default.copy() for _ in range(expected)]
        for item in data:
            if not isinstance(item, dict):
                continue
            idx = item.get("src_idx")
            rel = item.get("relation_type", "Elaboration")
            nuc = item.get("nuclearity", "NN")
            if isinstance(idx, int) and 0 <= idx < expected:
                result[idx] = {
                    "relation_type": rel if rel in _valid_rel else "Elaboration",
                    "nuclearity":    nuc if nuc in _valid_nuc else "NN",
                }
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
        """Extract and classify domain terms from text.

        LLM classifies each term as:
        - new_concept: genuinely new, standalone concept → enters candidate pool
        - variant: qualified form of a known concept → discarded
        - noise: generic/non-domain word → discarded

        Args:
            text: Raw segment text (preserving original case).
            known_terms: List of known ontology canonical names / aliases.

        Returns:
            List of dicts with classification:
            [{"term": "...", "classification": "new_concept|variant|noise",
              "parent_concept": "...", "reason": "..."}]
            Empty list if LLM disabled or no candidates found.
        """
        if not self.is_enabled() or not text.strip():
            return []

        known_sample = ", ".join(known_terms[:80])
        system = (
            "You are a network engineering terminology extractor and classifier.\n"
            "Given a text segment and a list of known ontology concepts, "
            "identify technical terms and CLASSIFY each one.\n\n"
            "The ontology has 5 knowledge layers:\n"
            "- concept: Configurable objects on a device (interfaces, protocol instances, "
            "policies, address objects, VPN instances, QoS profiles, ACL rules)\n"
            "- mechanism: How things work — algorithms, forwarding principles, "
            "isolation mechanisms, selection processes\n"
            "- method: How to do it — configuration procedures, verification steps, "
            "troubleshooting methods, deployment methods\n"
            "- condition: When to use it — applicability conditions, constraints, "
            "risks, decision rules\n"
            "- scenario: Real-world use cases — deployment patterns, business scenarios\n\n"
            "Return ONLY a JSON array. Each element:\n"
            '{"term": "<exact surface form>", '
            '"classification": "new_concept|variant|noise", '
            '"knowledge_layer": "concept|mechanism|method|condition|scenario", '
            '"parent_concept": "<known concept if variant, else null>", '
            '"reason": "<brief explanation>"}\n\n'
            "Classifications:\n"
            "- new_concept: a standalone networking/telecom term that deserves "
            "its own ontology entry. You MUST also specify which knowledge_layer it belongs to.\n"
            "- variant: a qualified/contextual form of a KNOWN concept. "
            'e.g. if "router ID" is known, then "OSPF router ID" is a variant. '
            "Set parent_concept to the known concept name.\n"
            "- noise: generic English words, document structure words, author names, dates\n\n"
            "Rules:\n"
            "- Precision over recall: when in doubt, classify as variant or noise\n"
            "- knowledge_layer is REQUIRED for new_concept. Use these guidelines:\n"
            "  - concept: CLI-configurable objects (e.g. 'DHCP snooping', 'route filter')\n"
            "  - mechanism: protocol algorithms (e.g. 'shortest path first', 'label imposition')\n"
            "  - method: operational procedures (e.g. 'graceful restart procedure')\n"
            "  - condition: constraints/rules (e.g. 'MTU mismatch risk')\n"
            "  - scenario: deployment patterns (e.g. 'hub-spoke VPN scenario')\n"
            "- Do NOT classify as new_concept if the term is just [known concept] + "
            "[context modifier]\n"
            "- Return [] if no terms found at all"
        )
        prompt = (
            f"Known concepts: {known_sample}\n\n"
            f"Text:\n{text[:2000]}\n\n"
            "Extract and classify terms as JSON array:"
        )

        raw = self._call_llm(system, prompt, 768)
        if raw is None:
            return []

        return self._parse_candidate_terms(raw)

    def _parse_candidate_terms(self, raw: str) -> list[dict]:
        """Parse LLM response for candidate terms with classification."""
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

        valid_classifications = {"new_concept", "variant", "noise"}
        results = []
        for item in data:
            if not isinstance(item, dict):
                continue
            term = item.get("term", "").strip()
            if not term or len(term) < 2:
                continue
            classification = item.get("classification", "new_concept")
            if classification not in valid_classifications:
                classification = "new_concept"  # fallback: let downstream filter decide
            results.append({
                "term": term,
                "classification": classification,
                "parent_concept": item.get("parent_concept"),
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
