"""POST /api/v1/copilot — natural language knowledge Q&A powered by query engine + LLM."""

from __future__ import annotations

import json
import logging
import re
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
        llm = getattr(_app, "llm", None)
        llm_ok = llm and llm.is_enabled()

        alias_hits = _alias_match(question, _app)
        if llm_ok:
            search_terms = _llm_extract_terms(question, _app)
            fuzzy_hits = _fuzzy_search(search_terms, alias_hits, _app)
            plan = _llm_plan(question, fuzzy_hits, _app)
        else:
            fuzzy_hits = [{"node_id": nid, "name": nid, "score": 1.0, "source": "alias"} for nid in alias_hits]
            plan = _fallback_plan(question, alias_hits)

        engine = QueryEngine(_app)
        qr = engine.execute(plan)
        result = qr.get("result", {})

        segments = _extract_segments(result)
        node_ids = _collect_node_ids(plan, result, [h["node_id"] for h in fuzzy_hits])
        facts = _collect_facts(node_ids, _app)

        if llm_ok:
            answer = _generate_answer(question, facts, segments, _app)
        else:
            answer = _fallback_answer(question, facts, segments)

        return {
            "answer": answer,
            "facts": facts[:15],
            "segments": segments[:5],
            "query_plan": plan,
            "latency_ms": (time.time() - t0) * 1000,
        }
    except Exception as exc:
        log.error("Copilot error: %s", exc, exc_info=True)
        return {"error": str(exc), "latency_ms": (time.time() - t0) * 1000}


def _alias_match(question: str, app) -> list[str]:
    """Fast alias matching — returns node_ids found in question text."""
    ontology = app.ontology
    text_lower = question.lower()
    found = []
    for surface, node_id in ontology.alias_map.items():
        if len(surface) <= 2:
            if re.search(r"\b" + re.escape(surface) + r"\b", text_lower):
                found.append(node_id)
        elif surface in text_lower:
            found.append(node_id)
    return list(dict.fromkeys(found))[:10]


def _llm_extract_terms(question: str, app) -> list[str]:
    """LLM extracts search keywords from the question."""
    llm = app.llm
    prompt = f"""Extract technical search keywords from this question. Return a JSON array of strings.
Include: protocol names, technology terms, network concepts, Chinese and English variants.
Do NOT return node IDs — return natural language terms that can be searched.

Question: {question}

Return ONLY a JSON array, e.g. ["BGP", "route reflector", "路由反射器", "iBGP"]"""

    try:
        raw = llm.complete(prompt, system="Extract search terms. Return JSON array only.", max_tokens=200)
        if raw:
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            raw = re.sub(r"```json\s*", "", raw)
            raw = re.sub(r"```\s*$", "", raw)
            terms = json.loads(raw)
            if isinstance(terms, list):
                log.info("LLM extracted %d search terms: %s", len(terms), terms[:8])
                return [str(t).strip() for t in terms if t][:15]
    except Exception as exc:
        log.warning("LLM term extraction failed: %s", exc)
    return []


def _fuzzy_search(search_terms: list[str], alias_hits: list[str], app) -> list[dict]:
    """Fuzzy-match search terms against ontology nodes via alias + embedding."""
    ontology = app.ontology
    results: dict[str, dict] = {}

    for nid in alias_hits:
        node = ontology.get_node_dict(nid)
        name = node.get("canonical_name", nid) if node else nid
        results[nid] = {"node_id": nid, "name": name, "score": 1.0, "source": "alias_exact"}

    for term in search_terms:
        term_lower = term.lower().strip()
        if not term_lower:
            continue

        nid = ontology.lookup_alias(term_lower)
        if nid and nid not in results:
            node = ontology.get_node_dict(nid)
            name = node.get("canonical_name", nid) if node else nid
            results[nid] = {"node_id": nid, "name": name, "score": 0.95, "source": "alias_term"}
            continue

        best_score = 0
        best_nid = None
        for surface, nid in ontology.alias_map.items():
            if term_lower in surface or surface in term_lower:
                score = len(min(term_lower, surface, key=len)) / len(max(term_lower, surface, key=len))
                if score > best_score and nid not in results:
                    best_score = score
                    best_nid = nid
        if best_nid and best_score > 0.4:
            node = ontology.get_node_dict(best_nid)
            name = node.get("canonical_name", best_nid) if node else best_nid
            results[best_nid] = {"node_id": best_nid, "name": name, "score": round(best_score, 2), "source": "alias_partial"}

    if len(results) < 3 and search_terms:
        try:
            from src.utils.embedding import get_embeddings
            from src.config.settings import settings
            if getattr(settings, "EMBEDDING_ENABLED", False):
                all_nodes = list(ontology.nodes.items())
                node_texts = [(nid, n.get("canonical_name", nid)) for nid, n in all_nodes if n.get("canonical_name")]
                if node_texts:
                    query_text = " ".join(search_terms)
                    query_vec = get_embeddings([query_text])
                    node_vecs = get_embeddings([t[1] for t in node_texts])
                    if query_vec and node_vecs:
                        import numpy as np
                        qv = np.array(query_vec[0])
                        nv = np.array(node_vecs)
                        sims = np.dot(nv, qv)
                        top_indices = np.argsort(sims)[::-1][:5]
                        for idx in top_indices:
                            sim = float(sims[idx])
                            if sim > 0.5:
                                nid = node_texts[idx][0]
                                if nid not in results:
                                    results[nid] = {"node_id": nid, "name": node_texts[idx][1], "score": round(sim, 3), "source": "embedding"}
        except Exception as exc:
            log.debug("Embedding fuzzy search failed: %s", exc)

    ranked = sorted(results.values(), key=lambda x: -x["score"])
    log.info("Fuzzy search: %d terms → %d nodes matched", len(search_terms), len(ranked))
    return ranked[:15]


def _get_ontology_context(app) -> str:
    """Build a concise ontology summary for LLM context. Reads live from registry."""
    ontology = app.ontology
    layers = {}
    for nid in ontology.all_node_ids():
        layer = ontology.get_node_layer(nid)
        layers.setdefault(layer, []).append(nid)

    lines = ["Knowledge layers and sample node IDs:"]
    for layer in ["concept", "mechanism", "method", "condition", "scenario"]:
        ids = layers.get(layer, [])
        sample = ids[:8]
        lines.append(f"  {layer} ({len(ids)} nodes): {', '.join(sample)}")

    rels = sorted(ontology.relation_ids)
    lines.append(f"\nRelation types ({len(rels)}): {', '.join(rels[:20])}")
    if len(rels) > 20:
        lines.append(f"  ... and {len(rels)-20} more")

    lines.append("\nReserved edges (cross-store): tagged_in, rst_adjacent, evidenced_by")
    return "\n".join(lines)


_PLAN_SYSTEM = """You are a query planner for a telecom knowledge base. Given a user question, you produce a JSON query plan.

The query engine has 5 primitives:
- seed: initialize nodes. Modes: by="alias" (term lookup), by="id" (node IDs), by="layer" (all nodes in a layer), by="embedding" (semantic search on segments)
- expand: graph traversal. Use "any_of" with relation types. Reserved edges: "tagged_in" (node→segments), "rst_adjacent" (segment→segment), "evidenced_by" (fact→segments). Set direction: "outbound"/"inbound"/"both", depth: 1-5
- combine: set ops. method: "union"/"intersect"/"subtract", sets: ["$var1","$var2"]
- aggregate: function: "count"/"rank"/"rerank". For rerank use "query" field with search keywords
- project: select fields. fields: ["node_id","raw_text","confidence",...]

Rules:
- Each step needs "op", "as" (output variable starting with $), and op-specific fields
- "from" references a previous variable
- Use ontology relation types (lowercase) for expand edges, NOT uppercase
- Keep plans under 8 steps
- Always end with a rerank or rank step to get the most relevant results
- For "what is X" questions: seed alias → expand tagged_in → rerank
- For "how does X relate to Y" questions: seed both → expand graph → combine intersect → tagged_in → rerank
- For dependency/impact questions: seed → expand depends_on/requires with depth 2-3 → tagged_in → rerank
- For design/scenario questions: seed matched scenario/concept nodes → expand composed_of/explains/configured_by/applicable_when direction both depth 2 to cover all 5 layers → tagged_in → rerank
- For comparison questions: seed both terms → expand tagged_in separately → combine union → rerank
- IMPORTANT for design questions: expand with BROAD relation types (composed_of, explains, configured_by, applicable_when, constrained_by, depends_on) to traverse across all 5 knowledge layers (scenario↔condition↔method↔mechanism↔concept)

Example plan for "BGP路由反射器的工作原理":
{"intent":"BGP route reflector mechanism","steps":[
  {"op":"seed","by":"alias","target":"node","value":"BGP","as":"$bgp"},
  {"op":"expand","from":"$bgp","any_of":["depends_on","configured_by","explains"],"direction":"both","depth":1,"as":"$graph"},
  {"op":"combine","method":"union","sets":["$bgp","$graph"],"as":"$all"},
  {"op":"expand","from":"$all","any_of":["tagged_in"],"direction":"outbound","as":"$segs"},
  {"op":"aggregate","function":"rerank","from":"$segs","query":"BGP route reflector mechanism","limit":10,"as":"$ranked"}
]}

CRITICAL rules:
- seed MUST have "target":"node" (or "segment"/"fact") AND "value" (a string or array)
- expand MUST have "any_of" (array of relation type strings) — never omit it
- Every step MUST have "as":"$variable_name"

Return ONLY a JSON object with "intent" (string) and "steps" (array). No markdown, no explanation."""


def _llm_plan(question: str, fuzzy_hits: list[dict], app) -> dict:
    """Use LLM to generate a query plan based on fuzzy search results."""
    llm = app.llm
    ontology_ctx = _get_ontology_context(app)

    if fuzzy_hits:
        matched = "\n".join(
            f"  {h['node_id']}: {h['name']} (score={h['score']}, via={h['source']})"
            for h in fuzzy_hits[:12]
        )
        match_info = f"\nOntology nodes matched from the question (USE THESE node IDs in your plan):\n{matched}"
    else:
        match_info = "\nNo ontology nodes matched. Use seed by='layer' or by='embedding' instead of by='id'."

    prompt = f"""{ontology_ctx}
{match_info}

User question: {question}

Generate a query plan (JSON only):"""

    from src.query.validator import QueryValidator, ValidationError

    last_error = ""
    for attempt in range(3):
        retry_hint = ""
        if last_error:
            retry_hint = f"\n\nYour previous plan had validation errors:\n{last_error}\nFix these errors and try again."

        try:
            raw = llm.complete(prompt + retry_hint, system=_PLAN_SYSTEM, max_tokens=600)
            if not raw:
                continue
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            raw = re.sub(r"```json\s*", "", raw)
            raw = re.sub(r"```\s*$", "", raw)
            plan = json.loads(raw)
            if not isinstance(plan, dict) or "steps" not in plan:
                last_error = "Response must be a JSON object with a 'steps' array."
                continue
            plan.setdefault("intent", question)
            errors = QueryValidator(app.ontology.relation_ids).validate(plan)
            if not errors:
                log.info("LLM plan validated on attempt %d: %d steps", attempt + 1, len(plan["steps"]))
                return plan
            else:
                last_error = "; ".join(errors)
                log.info("LLM plan attempt %d failed validation: %s", attempt + 1, last_error)
        except json.JSONDecodeError as exc:
            last_error = f"Invalid JSON: {exc}"
            log.info("LLM plan attempt %d bad JSON: %s", attempt + 1, exc)
        except Exception as exc:
            log.warning("LLM plan attempt %d error: %s", attempt + 1, exc)
            break

    log.warning("LLM plan failed after retries, using fallback")
    return _fallback_plan(question, alias_hits)


def _fallback_plan(question: str, node_ids: list[str]) -> dict:
    """Template-based plan when LLM is unavailable."""
    if not node_ids:
        return {"intent": question, "steps": [
            {"op": "seed", "by": "layer", "target": "node", "value": "concept", "as": "$all"},
            {"op": "expand", "from": "$all", "any_of": ["tagged_in"], "direction": "outbound", "as": "$segs"},
            {"op": "aggregate", "function": "rerank", "from": "$segs", "query": question, "limit": 10, "as": "$ranked"},
        ]}

    return {"intent": question, "steps": [
        {"op": "seed", "by": "id", "target": "node", "value": node_ids, "as": "$nodes"},
        {"op": "expand", "from": "$nodes", "any_of": ["depends_on", "configured_by", "contains", "explains", "composed_of"],
         "direction": "both", "depth": 1, "as": "$graph"},
        {"op": "combine", "method": "union", "sets": ["$nodes", "$graph"], "as": "$all"},
        {"op": "expand", "from": "$all", "any_of": ["tagged_in"], "direction": "outbound", "as": "$segs"},
        {"op": "aggregate", "function": "rerank", "from": "$segs", "query": question, "limit": 10, "as": "$ranked"},
    ]}


def _collect_node_ids(plan: dict, result: dict, alias_hits: list[str]) -> list[str]:
    """Collect node IDs from plan results and alias hits."""
    ids = set(alias_hits)
    for step in plan.get("steps", []):
        if step.get("op") == "seed" and step.get("by") == "id":
            val = step.get("value", [])
            if isinstance(val, list):
                ids.update(val)
            elif isinstance(val, str):
                ids.add(val)
    for var, data in result.items():
        if isinstance(data, list):
            for node in data[:20]:
                nid = node.get("node_id", "")
                ntype = node.get("node_type", "")
                if ntype == "node" and nid and not nid.startswith(("$", "{")):
                    ids.add(nid)
    return list(ids)[:15]


def _collect_facts(node_ids: list[str], app) -> list[dict]:
    if not node_ids:
        return []
    store = app.store
    safe_ids = [nid.replace("'", "''") for nid in node_ids[:10]]
    conditions = " OR ".join(
        f"(subject = '{nid}' OR object = '{nid}')" for nid in safe_ids
    )
    rows = store.fetchall(
        f"SELECT subject, predicate, object, confidence FROM facts "
        f"WHERE lifecycle_state='active' AND ({conditions}) "
        f"ORDER BY confidence DESC LIMIT 15"
    )
    return [dict(r) for r in rows]


def _extract_segments(result: dict) -> list[dict]:
    for var in ("$ranked", "$segs_out", "$result", "$segs"):
        data = result.get(var)
        if isinstance(data, list) and data:
            out = []
            for n in data:
                props = n.get("properties") or n
                out.append({
                    "raw_text": props.get("raw_text", ""),
                    "segment_type": props.get("segment_type", ""),
                    "section_title": props.get("section_title", ""),
                    "confidence": props.get("confidence", 0),
                })
            return out
    return []


def _generate_answer(question: str, facts: list[dict], segments: list[dict], app) -> str:
    llm = getattr(app, "llm", None)
    if not llm or not llm.is_enabled():
        return _fallback_answer(question, facts, segments)

    layered_facts = _group_facts_by_layer(facts, app)
    segs_text = "\n\n".join(
        f"[{s['segment_type']}] {s['raw_text'][:400]}"
        for s in segments[:5]
    )

    prompt = f"""Based on the following knowledge retrieved from a telecom knowledge base, answer the user's question in the same language as the question.

Question: {question}

Knowledge triples (grouped by ontology layer):
{layered_facts or '(none)'}

Source text segments:
{segs_text or '(none)'}

Instructions:
- Answer based ONLY on the provided knowledge. If the knowledge is insufficient, say so.
- Organize your answer following the 5-layer knowledge model, from top to bottom:
  1. Scenario (业务场景): What real-world deployment pattern applies?
  2. Condition (适用条件): Under what constraints or prerequisites?
  3. Method (操作方法): What configuration/deployment procedures?
  4. Mechanism (协议机制): How does the underlying protocol work?
  5. Concept (可配置对象): What specific objects need to be configured?
- Skip layers that have no relevant knowledge. Only include layers with actual evidence.
- Be specific and technical. Reference the facts and source text.
- Keep the answer under 400 words."""

    try:
        response = llm.complete(prompt, system="You are a telecom network knowledge assistant. Organize answers using the 5-layer model: scenario→condition→method→mechanism→concept.", max_tokens=1000)
        if response and isinstance(response, str):
            response = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
            return response
    except Exception as exc:
        log.warning("LLM answer generation failed: %s", exc)

    return _fallback_answer(question, facts, segments)


def _group_facts_by_layer(facts: list[dict], app) -> str:
    """Group facts by their ontology layer for structured answer generation."""
    ontology = app.ontology
    layer_names = {
        "scenario": "Scenario (业务场景)",
        "condition": "Condition (适用条件)",
        "method": "Method (操作方法)",
        "mechanism": "Mechanism (协议机制)",
        "concept": "Concept (可配置对象)",
    }
    layer_order = ["scenario", "condition", "method", "mechanism", "concept"]
    buckets: dict[str, list[str]] = {l: [] for l in layer_order}

    for f in facts[:15]:
        subj_layer = ontology.get_node_layer(f["subject"])
        obj_layer = ontology.get_node_layer(f["object"])
        top_layer = subj_layer if layer_order.index(subj_layer) < layer_order.index(obj_layer) else obj_layer
        line = f"  {f['subject']} —[{f['predicate']}]→ {f['object']} (conf={f['confidence']:.2f})"
        buckets[top_layer].append(line)

    lines = []
    for layer in layer_order:
        items = buckets[layer]
        if items:
            lines.append(f"\n[{layer_names[layer]}]")
            lines.extend(items)
    return "\n".join(lines) if lines else ""


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