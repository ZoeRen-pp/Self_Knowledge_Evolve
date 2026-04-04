"""ClaudeLLMProvider — LLMProvider implementation backed by src/utils/llm_extract.py."""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

from semcore.providers.base import LLMProvider

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.config.settings import Settings


class ClaudeLLMProvider(LLMProvider):
    """Wraps LLMExtractor for the semcore LLMProvider interface.

    Also exposes domain-specific helpers (extract_triples, extract_rst_relations,
    generate_title) directly on the instance so Stage implementations can call
    them via ``app.llm`` without casting.
    """

    def __init__(self, settings: "Settings | None" = None) -> None:
        # Lazy: LLMExtractor reads settings on first use
        from src.utils.llm_extract import LLMExtractor
        self._extractor = LLMExtractor()

    # ── semcore ABC ───────────────────────────────────────────────────────────

    def complete(self, prompt: str, *, system: str = "", max_tokens: int = 512) -> str:
        if not self._extractor.is_enabled():
            return ""
        raw = self._extractor._call_llm(system, prompt, max_tokens)  # type: ignore[attr-defined]
        return raw or ""

    def extract_structured(
        self, text: str, output_schema: dict[str, Any], *, system: str = ""
    ) -> dict[str, Any]:
        """Best-effort: prompt LLM with schema description, parse JSON response."""
        schema_desc = json.dumps(output_schema, ensure_ascii=False, indent=2)
        prompt = (
            f"Extract data from the following text according to this JSON schema:\n"
            f"{schema_desc}\n\nText:\n{text}\n\nReturn ONLY valid JSON."
        )
        raw = self.complete(prompt, system=system or "You are a structured data extractor.", max_tokens=1024)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            import re
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
        return {}

    # ── Domain-specific extensions (not part of ABC) ──────────────────────────

    def is_enabled(self) -> bool:
        return self._extractor.is_enabled()

    def extract_triples(
        self,
        text: str,
        candidate_node_ids: list[str],
        valid_relations: list[str],
    ) -> list[dict[str, Any]]:
        return self._extractor.extract(text, candidate_node_ids, valid_relations)

    def extract_rst_relations(
        self, edu_pairs: list[tuple[str, str, str, str]]
    ) -> list[str]:
        return self._extractor.extract_rst_relations(edu_pairs)

    def generate_title(self, text: str) -> str | None:
        return self._extractor.generate_title(text)

    def extract_candidate_terms(
        self, text: str, known_terms: list[str],
    ) -> list[dict]:
        return self._extractor.extract_candidate_terms(text, known_terms)
