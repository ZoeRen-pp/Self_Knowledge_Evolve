"""POST /api/v1/copilot — natural language knowledge Q&A powered by query engine + LLM."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.app_factory import get_app
from src.query.engine import QueryEngine

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["copilot"])


class CopilotRequest(BaseModel):
    question: str


@router.post("/copilot")
def copilot(body: CopilotRequest, _app=Depends(get_app)):
    t0 = time.time()
    question = body.question.strip()
    if not question:
        return {"error": "empty question"}

    try:
        terms = _extract_terms(question, _app)
        plan = _build_plan(question, terms)
        engine = QueryEngine(_app)
        qr = engine.execute(plan)
        result = qr.get("result", {})

        facts_raw = _collect_facts(terms, _app)
        segments = _extract_segments(result)
        answer = _generate_answer(question, facts_raw, segments, _app)

        return {
            "answer": answer,
            "facts": facts_raw[:15],
            "segments": segments[:5],
            "query_plan": plan,
            "latency_ms": (time.time() - t0) * 1000,
        }
    except Exception as exc:
        log.error("Copilot error: %s", exc, exc_info=True)
        return {"error": str(exc), "latency_ms": (time.time() - t0) * 1000}


def _extract_terms(question: str, app) -> list[str]:
    """Use ontology alias matching to find known terms in the question."""
    ontology = app.ontology
    text_lower = question.lower()
    found = []
    for surface, node_id in ontology.alias_map.items():
        if len(surface) <= 2:
            import re
            if re.search(r"\b" + re.escape(surface) + r"\b", text_lower):
                found.append(node_id)
        elif surface in text_lower:
            found.append(node_id)
    return list(dict.fromkeys(found))[:10]


def _build_plan(question: str, term_node_ids: list[str]) -> dict:
    if not term_node_ids:
        return {"intent": question, "steps": [
            {"op": "seed", "by": "layer", "target": "node", "value": "concept", "as": "$all"},
            {"op": "expand", "from": "$all", "any_of": ["tagged_in"], "direction": "outbound", "as": "$segs"},
            {"op": "aggregate", "function": "rerank", "from": "$segs", "query": question, "limit": 10, "as": "$ranked"},
        ]}

    return {"intent": question, "steps": [
        {"op": "seed", "by": "id", "target": "node", "value": term_node_ids, "as": "$nodes"},
        {"op": "expand", "from": "$nodes", "any_of": ["depends_on", "configured_by", "contains", "explains", "composed_of"],
         "direction": "both", "depth": 1, "as": "$graph"},
        {"op": "combine", "method": "union", "sets": ["$nodes", "$graph"], "as": "$all"},
        {"op": "expand", "from": "$all", "any_of": ["tagged_in"], "direction": "outbound", "as": "$segs"},
        {"op": "aggregate", "function": "rerank", "from": "$segs", "query": question, "limit": 10, "as": "$ranked"},
    ]}


def _collect_facts(term_node_ids: list[str], app) -> list[dict]:
    if not term_node_ids:
        return []
    store = app.store
    conditions = " OR ".join(
        f"(subject = '{nid}' OR object = '{nid}')" for nid in term_node_ids[:5]
    )
    rows = store.fetchall(
        f"SELECT subject, predicate, object, confidence FROM facts "
        f"WHERE lifecycle_state='active' AND ({conditions}) "
        f"ORDER BY confidence DESC LIMIT 15"
    )
    return [dict(r) for r in rows]


def _extract_segments(result: dict) -> list[dict]:
    ranked = result.get("$ranked", [])
    if not isinstance(ranked, list):
        return []
    out = []
    for n in ranked:
        props = n.get("properties") or n
        out.append({
            "raw_text": props.get("raw_text", ""),
            "segment_type": props.get("segment_type", ""),
            "section_title": props.get("section_title", ""),
            "confidence": props.get("confidence", 0),
        })
    return out


def _generate_answer(question: str, facts: list[dict], segments: list[dict], app) -> str:
    """Use LLM to synthesize answer from retrieved knowledge, or fall back to structured summary."""
    llm = getattr(app, "llm", None)
    if not llm or not llm.is_enabled():
        return _fallback_answer(question, facts, segments)

    facts_text = "\n".join(
        f"- {f['subject']} {f['predicate']} {f['object']} (conf={f['confidence']:.2f})"
        for f in facts[:10]
    )
    segs_text = "\n\n".join(
        f"[{s['segment_type']}] {s['raw_text'][:400]}"
        for s in segments[:5]
    )

    prompt = f"""Based on the following knowledge retrieved from a telecom knowledge base, answer the user's question concisely in the same language as the question.

Question: {question}

Knowledge triples:
{facts_text or '(none)'}

Source text segments:
{segs_text or '(none)'}

Instructions:
- Answer based ONLY on the provided knowledge. If the knowledge is insufficient, say so.
- Be specific and technical. Reference the facts and source text.
- Keep the answer under 300 words."""

    try:
        response = llm.complete(prompt, system="You are a telecom network knowledge assistant.", max_tokens=800)
        if response and isinstance(response, str):
            return response.strip()
    except Exception as exc:
        log.warning("LLM answer generation failed: %s", exc)

    return _fallback_answer(question, facts, segments)


def _fallback_answer(question: str, facts: list[dict], segments: list[dict]) -> str:
    lines = []
    if facts:
        lines.append(f"找到 {len(facts)} 条相关知识条目：")
        for f in facts[:5]:
            lines.append(f"  {f['subject']} —[{f['predicate']}]→ {f['object']}")
    if segments:
        lines.append(f"\n找到 {len(segments)} 段相关原文。")
    if not facts and not segments:
        lines.append("未找到与该问题直接相关的知识。可能需要更多文档被处理后才能回答。")
    return "\n".join(lines)