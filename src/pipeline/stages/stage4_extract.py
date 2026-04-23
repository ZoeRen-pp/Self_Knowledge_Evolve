"""Stage 4: Relation extraction + fact construction - rules R1-R4 + LLM."""

from __future__ import annotations

import logging
import re
import uuid

from semcore.core.context import PipelineContext
from semcore.pipeline.base import Stage
from semcore.providers.base import RelationalStore

from src.utils.confidence import score_fact

log = logging.getLogger(__name__)

# Predicate signal patterns loaded from ontology/patterns/predicate_signals.yaml
# (no hardcoded patterns — loaded at runtime via OntologyRegistry)
# NOTE: regex relation extraction removed — LLM-only with co-occurrence fallback


class ExtractStage(Stage):
    name = "extract"

    def __init__(self) -> None:
        self._ontology = None
        self._llm = None
        self._store: RelationalStore | None = None

    def process(self, ctx: PipelineContext, app) -> PipelineContext:  # type: ignore[override]
        self._ontology = app.ontology
        self._llm = app.llm
        self._store = app.store
        self._crawler_store = getattr(app, "crawler_store", None) or app.store
        self._predicate_signals = getattr(app.ontology, "predicate_signal_patterns", [])
        source_doc_id = ctx.doc.source_doc_id if ctx.doc else ctx.source_doc_id
        facts = self._run(source_doc_id)
        self.set_output(ctx, {"facts": facts})
        return ctx

    # RST relation types whose predicate can be derived structurally (no LLM needed).
    # Value: (ontology_predicate, subject_is_src)
    #   subject_is_src=True  → subject = nucleus when NS, i.e. src_seg
    #   subject_is_src=False → subject = nucleus when SN, i.e. dst_seg
    # In all cases subject = nucleus segment; object = satellite segment.
    _RST_DERIVED_PREDICATES: dict[str, tuple[str, bool]] = {
        "Constraint":      ("constrained_by", True),   # NS: src(behavior) constrained_by dst(rule)
        "Prerequisite":    ("requires",       False),  # SN: dst(task) requires src(prereq)
        "Causation":       ("triggers",       True),   # NS: src(cause) triggers dst(effect)
        "Condition":       ("applies_when",   False),  # SN: dst(content) applies_when src(cond)
        "Exemplification": ("has_example",    True),   # NS: src(concept) has_example dst
    }

    def _run(self, source_doc_id: str) -> list[dict]:
        """Extract facts from all segments of a document."""
        store = self._store
        doc = store.fetchall(
            "SELECT source_rank FROM documents WHERE source_doc_id=%s", (source_doc_id,)
        )
        source_rank = doc[0]["source_rank"] if doc else "C"

        segments = store.fetchall(
            "SELECT * FROM segments WHERE source_doc_id=%s AND lifecycle_state='active'",
            (source_doc_id,),
        )
        # Enrich each segment with ALL ontology-linked tags that Stage 3
        # assigned — not just canonical. mechanism_tag / method_tag /
        # condition_tag / scenario_tag point to MECH.* / METHOD.* / COND.* /
        # SCENE.* nodes respectively and are the correct cross-layer anchors.
        # (Field name kept as `canonical_nodes` for backward compatibility
        # with _extract_rst_derived_facts and _walk_rst_chains.)
        for seg in segments:
            tags = store.fetchall(
                "SELECT ontology_node_id FROM segment_tags "
                "WHERE segment_id=%s AND ontology_node_id IS NOT NULL "
                "AND tag_type IN ('canonical','mechanism_tag','method_tag',"
                "'condition_tag','scenario_tag')",
                (seg["segment_id"],),
            )
            seg["canonical_nodes"] = [
                t["ontology_node_id"] for t in tags if t.get("ontology_node_id")
            ]

        log.info("Extract start doc=%s: segments=%d rank=%s llm=%s",
                 source_doc_id, len(segments), source_rank,
                 "enabled" if self._llm.is_enabled() else "disabled")

        all_facts: list[dict] = []

        # P0: RST-derived facts — structural predicate inference, no LLM needed
        rst_derived = self._extract_rst_derived_facts(segments, source_rank, source_doc_id)
        all_facts.extend(rst_derived)

        llm_count = 0
        cooccurrence_count = 0
        merged_count = 0
        covered_ids: set[str] = set()  # segments with successful single-seg LLM extraction

        for i, seg in enumerate(segments):
            # P1: LLM extraction (highest quality, single segment)
            llm_facts = self.extract_facts_llm(seg, source_rank)
            if llm_facts:
                all_facts.extend(llm_facts)
                llm_count += len(llm_facts)
                covered_ids.add(str(seg["segment_id"]))
                log.debug("  seg=%s llm=%d", str(seg["segment_id"])[:12], len(llm_facts))
                continue

            # P2: LLM with merged context + RST discourse hint
            if i > 0:
                merged_facts = self._extract_merged_context(
                    segments[i - 1], seg, source_rank, source_doc_id,
                )
                if merged_facts:
                    all_facts.extend(merged_facts)
                    merged_count += len(merged_facts)
                    log.debug("  seg=%s merged=%d", str(seg["segment_id"])[:12], len(merged_facts))
                    continue

            # P3: Co-occurrence (last resort, low quality)
            cooc_facts = self._extract_cooccurrence(seg, source_rank)
            if cooc_facts:
                all_facts.extend(cooc_facts)
                cooccurrence_count += len(cooc_facts)
                log.debug("  seg=%s cooccurrence=%d", str(seg["segment_id"])[:12], len(cooc_facts))

        # P2-chain: RST chain extraction for chains with uncovered segments
        chain_facts = self._walk_rst_chains(segments, source_rank, source_doc_id, covered_ids)
        all_facts.extend(chain_facts)

        self._save_facts(all_facts, source_doc_id)
        self._crawler_store.execute(
            "INSERT INTO extraction_jobs (job_type, source_doc_id, status, pipeline_version) "
            "VALUES ('dedup',%s,'pending','0.2.0')",
            (source_doc_id,),
        )
        fact_ids = [f["fact_id"] for f in all_facts]
        id_preview = self._preview_ids(fact_ids)
        log.info(
            "Extracted facts doc=%s total=%d llm=%d merged=%d "
            "rst_derived=%d chain=%d cooccurrence=%d fact_ids=%s",
            source_doc_id, len(all_facts), llm_count, merged_count,
            len(rst_derived), len(chain_facts), cooccurrence_count, id_preview,
        )
        return all_facts

    def _extract_rst_derived_facts(
        self, segments: list[dict], source_rank: str, source_doc_id: str,
    ) -> list[dict]:
        """P0: Derive facts directly from RST structure — no LLM needed.

        For 5 RST types the predicate can be read off the discourse relation itself.
        Subject/object assignment follows nuclearity:
          NS → src_seg is nucleus → src_seg is subject
          SN → dst_seg is nucleus → dst_seg is subject
          NN → src_seg as subject (symmetric, arbitrary)
        """
        store = self._store
        facts: list[dict] = []

        seg_index = {str(s["segment_id"]): s for s in segments}
        seg_ids = list(seg_index.keys())
        if len(seg_ids) < 2:
            return facts

        placeholders = ",".join(["%s"] * len(seg_ids))
        rst_rows = store.fetchall(
            f"""SELECT src_edu_id, dst_edu_id, relation_type, nuclearity
                FROM t_rst_relation
                WHERE src_edu_id::text IN ({placeholders})
                  AND dst_edu_id::text IN ({placeholders})""",
            (*seg_ids, *seg_ids),
        )

        for row in rst_rows:
            rel_type = row.get("relation_type", "")
            if rel_type not in self._RST_DERIVED_PREDICATES:
                continue

            predicate, subject_is_src = self._RST_DERIVED_PREDICATES[rel_type]
            nuclearity = row.get("nuclearity") or "NN"

            src_seg = seg_index.get(str(row["src_edu_id"]))
            dst_seg = seg_index.get(str(row["dst_edu_id"]))
            if not src_seg or not dst_seg:
                continue

            # Pick which segment is subject (nucleus) based on nuclearity
            if nuclearity == "NS":
                subj_seg, obj_seg = src_seg, dst_seg
            elif nuclearity == "SN":
                subj_seg, obj_seg = dst_seg, src_seg
            else:  # NN — follow static subject_is_src flag
                subj_seg, obj_seg = (src_seg, dst_seg) if subject_is_src else (dst_seg, src_seg)

            subj_nodes = subj_seg.get("canonical_nodes") or []
            obj_nodes  = obj_seg.get("canonical_nodes") or []

            # Need at least one node on each side
            for sn in subj_nodes[:3]:
                for on in obj_nodes[:3]:
                    if sn == on:
                        continue
                    if not self._ontology.is_valid_relation(predicate):
                        continue
                    facts.append(self._build_fact(
                        sn, predicate, on, subj_seg, source_rank, "rst_derived",
                    ))

        log.debug("RST-derived facts doc=%s: %d facts from %d RST rows",
                  source_doc_id, len(facts), len(rst_rows))
        return facts

    def _extract_merged_context(
        self, prev_seg: dict, curr_seg: dict, source_rank: str, source_doc_id: str,
    ) -> list[dict]:
        """Priority 2: Merge with previous segment and retry LLM.

        Only triggers when the discourse relation between prev and curr is continuative
        (Elaboration, Sequence, Causation, Evidence, Background, Exemplification),
        meaning they form a semantic unit that was split by segmentation.
        Prepends an RST discourse hint so the LLM understands the semantic role split.
        """
        store = self._store
        continuative_types = {"Elaboration", "Sequence", "Causation",
                              "Evidence", "Background", "Exemplification"}

        # Check RST relation between prev and curr — also fetch nuclearity
        rst_row = store.fetchone(
            """SELECT relation_type, nuclearity FROM t_rst_relation
               WHERE src_edu_id = %s AND dst_edu_id = %s LIMIT 1""",
            (str(prev_seg["segment_id"]), str(curr_seg["segment_id"])),
        )
        if not rst_row or rst_row.get("relation_type") not in continuative_types:
            return []

        rel_type   = rst_row["relation_type"]
        nuclearity = rst_row.get("nuclearity") or "NN"

        # Build a human-readable hint for the LLM
        _nuc_hint = {
            "NS": "the first paragraph is the main content, the second is supportive",
            "SN": "the second paragraph is the main content, the first is supportive",
            "NN": "both paragraphs carry equal weight",
        }
        rst_hint = (
            f"[Discourse context: These two paragraphs have a {rel_type} relation "
            f"({_nuc_hint.get(nuclearity, 'both paragraphs carry equal weight')}). "
            f"Extract facts that span or connect both paragraphs.]\n\n"
        )

        # Merge texts with hint prepended
        merged_text = (
            rst_hint
            + (prev_seg.get("raw_text") or "")
            + "\n"
            + (curr_seg.get("raw_text") or "")
        )
        # Build merged segment dict for LLM
        merged_seg = {
            **curr_seg,
            "raw_text": merged_text,
            "normalized_text": merged_text.lower(),
            "canonical_nodes": list(set(
                (prev_seg.get("canonical_nodes") or []) +
                (curr_seg.get("canonical_nodes") or [])
            )),
        }
        facts = self.extract_facts_llm(merged_seg, source_rank)
        if facts:
            log.debug("  merged context (%s/%s): %s + %s → %d facts",
                      rel_type, nuclearity,
                      str(prev_seg["segment_id"])[:8],
                      str(curr_seg["segment_id"])[:8],
                      len(facts))
        return facts

    def _walk_rst_chains(
        self, segments: list[dict], source_rank: str,
        source_doc_id: str, covered_ids: set[str],
    ) -> list[dict]:
        """P2: Multi-hop triple extraction along RST chains.

        Finds chains of 3–4 segments connected by continuative RST relations
        where at least one segment was not already covered by single-segment LLM.
        Sends the whole chain as merged context to LLM for cross-segment triples.
        """
        CHAIN_TYPES = {"Sequence", "Elaboration", "Causation", "Background", "Exemplification"}
        MAX_CHAIN_LEN  = 4
        MAX_CHAIN_TOKENS = 1500

        store = self._store
        seg_index = {str(s["segment_id"]): s for s in segments}
        seg_ids = list(seg_index.keys())
        if len(seg_ids) < 3:
            return []

        # Build directed adjacency: src → [dst] for chain-eligible RST types
        placeholders = ",".join(["%s"] * len(seg_ids))
        rst_rows = store.fetchall(
            f"""SELECT src_edu_id, dst_edu_id, relation_type, nuclearity
                FROM t_rst_relation
                WHERE src_edu_id::text IN ({placeholders})
                  AND dst_edu_id::text IN ({placeholders})
                  AND relation_type IN ({",".join(["%s"] * len(CHAIN_TYPES))})""",
            (*seg_ids, *seg_ids, *CHAIN_TYPES),
        )

        adjacency: dict[str, list[str]] = {}  # src_id → list of dst_id
        reachable: set[str] = set()           # ids that are reachable from some other id
        for row in rst_rows:
            src = str(row["src_edu_id"])
            dst = str(row["dst_edu_id"])
            adjacency.setdefault(src, []).append(dst)
            reachable.add(dst)

        # Chain starts: nodes in the adjacency graph that are NOT reachable from another
        chain_starts = [nid for nid in adjacency if nid not in reachable]

        facts: list[dict] = []

        for start in chain_starts:
            # Walk the chain greedily
            chain: list[str] = [start]
            current = start
            total_tokens = len((seg_index.get(start) or {}).get("raw_text") or "") // 4

            while len(chain) < MAX_CHAIN_LEN:
                nexts = adjacency.get(current, [])
                if not nexts:
                    break
                nxt = nexts[0]  # take the first (usually only) successor
                nxt_tokens = len((seg_index.get(nxt) or {}).get("raw_text") or "") // 4
                if total_tokens + nxt_tokens > MAX_CHAIN_TOKENS:
                    break
                chain.append(nxt)
                total_tokens += nxt_tokens
                current = nxt

            if len(chain) < 3:
                continue

            # Only process chains where at least one segment was not covered
            if all(sid in covered_ids for sid in chain):
                continue

            # Merge chain texts with a structural header
            parts = []
            for idx, sid in enumerate(chain, 1):
                seg = seg_index.get(sid)
                if seg:
                    parts.append(f"[Paragraph {idx}]\n{seg.get('raw_text') or ''}")

            merged_text = "\n\n".join(parts)
            all_nodes: list[str] = []
            for sid in chain:
                seg = seg_index.get(sid)
                if seg:
                    all_nodes.extend(seg.get("canonical_nodes") or [])
            all_nodes = list(dict.fromkeys(all_nodes))  # deduplicate, preserve order

            # Use the first segment as anchor for segment_id / metadata
            anchor_seg = {
                **(seg_index.get(chain[0]) or {}),
                "raw_text": merged_text,
                "normalized_text": merged_text.lower(),
                "canonical_nodes": all_nodes,
            }
            chain_facts = self.extract_facts_llm(anchor_seg, source_rank)
            facts.extend(chain_facts)
            if chain_facts:
                log.debug("  rst_chain len=%d segs=%s → %d facts",
                          len(chain), [s[:8] for s in chain], len(chain_facts))

        return facts

    def _extract_cooccurrence(self, segment: dict, source_rank: str) -> list[dict]:
        """Priority 3 (last resort): Co-occurrence when regex and LLM both returned nothing.

        Strict guards:
        - Only when exactly 2 canonical nodes co-occur (no combinatorial explosion)
        - Only 1 predicate signal (the strongest match)
        - Lower confidence than regex/LLM
        """
        canonical_nodes = segment.get("canonical_nodes") or []
        if len(canonical_nodes) != 2:
            return []

        text = segment.get("normalized_text") or segment.get("raw_text", "")
        detected = self._detect_predicates(text)
        if not detected:
            return []

        ontology = self._ontology
        predicate = detected[0]  # only the single strongest signal
        if not ontology.is_valid_relation(predicate):
            return []

        subj_id, obj_id = canonical_nodes[0], canonical_nodes[1]
        if subj_id == obj_id:
            return []

        return [self._build_fact(
            subj_id, predicate, obj_id, segment, source_rank, "cooccurrence",
        )]

    def _build_fact(
        self, subj_id: str, predicate: str, obj_id: str,
        segment: dict, source_rank: str, extraction_method: str,
    ) -> dict:
        conf = score_fact(
            source_rank=source_rank,
            extraction_method="rule" if extraction_method != "llm" else "llm",
            ontology_fit=0.85 if extraction_method == "rule" else 0.60,
            cross_source_consistency=0.5,
            temporal_validity=1.0,
        )
        return {
            "fact_id":           str(uuid.uuid4()),
            "subject":           subj_id,
            "predicate":         predicate,
            "object":            obj_id,
            "qualifier":         {},
            "domain":            subj_id.split(".")[0] if "." in subj_id else None,
            "confidence":        conf,
            "extraction_method": extraction_method,
            "segment_id":        segment["segment_id"],
            "source_rank":       source_rank,
            "lifecycle_state":   "active",
            "ontology_version":  "v0.2.0",
        }

    def _detect_predicates(self, text: str) -> list[str]:
        """Detect which relation predicates are signaled by keywords in text."""
        predicates = []
        text_sample = text[:3000]
        for pattern, predicate in self._predicate_signals:
            if pattern.search(text_sample):
                predicates.append(predicate)
        return predicates

    def extract_facts_llm(self, segment: dict, source_rank: str) -> list[dict]:
        """LLM-based extraction: uses segment's Stage-3-aligned nodes as context.

        Candidates are strictly what Stage 3 aligned to this segment via
        exact/alias match or (when embedding is enabled) the 0.80 semantic
        fallback. We do NOT dump the full cross-layer node set at the LLM;
        doing so caused hallucinated facts referencing scenarios and
        mechanisms the source text never mentions. If Stage 3 couldn't
        anchor at least 3 nodes on this segment, we skip LLM extraction
        rather than ask the model to guess.
        """
        llm = self._llm
        ontology = self._ontology
        if not llm.is_enabled():
            return []

        text = segment.get("normalized_text") or segment.get("raw_text", "")
        if not text.strip():
            return []

        # Deduplicate anchor list (Stage 3 can produce the same node via
        # multiple tag types — e.g. canonical + mechanism_tag).
        canonical_nodes = segment.get("canonical_nodes") or []
        candidate_ids = list({n for n in canonical_nodes if n})

        # Not enough anchors to ground a relational claim — skip LLM.
        if len(candidate_ids) < 3:
            log.debug(
                "  seg=%s skipped LLM: only %d anchors",
                str(segment.get("segment_id", ""))[:12], len(candidate_ids),
            )
            return []

        valid_relations = list(ontology.relation_ids)

        # Raw text used for quote grounding; prefer raw_text over normalized (normalised
        # lowercases the text, making exact-match harder for mixed-case quotes).
        segment_raw_text = segment.get("raw_text") or text

        raw_triples = llm.extract_triples(text, candidate_ids, valid_relations)
        facts: list[dict] = []
        dropped_no_quote = 0
        for triple in raw_triples:
            subj = triple.get("subject", "")
            pred = triple.get("predicate", "")
            obj = triple.get("object", "")
            quote = triple.get("quote", "")
            if not subj or not pred or not obj or subj == obj:
                continue
            # Quote grounding: drop triples the LLM cannot pin to the source text.
            if not self._quote_supported(quote, segment_raw_text):
                dropped_no_quote += 1
                log.debug(
                    "  quote ungrounded, dropping triple (%s, %s, %s) quote=%r",
                    subj, pred, obj, quote[:80],
                )
                continue
            # Normalize subject/object: try alias → node_id mapping
            subj = ontology.lookup_alias(subj) or subj
            obj = ontology.lookup_alias(obj) or obj
            # Normalize predicate: lowercase, strip, underscores
            pred = pred.strip().lower().replace(" ", "_").replace("-", "_")
            if not ontology.is_valid_relation(pred):
                # Unknown predicate → candidate relation pool
                self._record_relation_candidate(
                    pred, subj, obj, segment, source_doc_id=segment.get("source_doc_id", ""),
                )
                continue
            conf = score_fact(
                source_rank=source_rank,
                extraction_method="llm",
                ontology_fit=0.75,
                cross_source_consistency=0.5,
                temporal_validity=1.0,
            )
            facts.append({
                "fact_id":           str(uuid.uuid4()),
                "subject":           subj,
                "predicate":         pred,
                "object":            obj,
                "qualifier":         {},
                "domain":            subj.split(".")[0] if "." in subj else None,
                "confidence":        conf,
                "extraction_method": "llm",
                "segment_id":        segment["segment_id"],
                "source_rank":       source_rank,
                "lifecycle_state":   "active",
                "ontology_version":  "v0.2.0",
                "exact_span":        quote,
            })
        if dropped_no_quote:
            log.info(
                "  seg=%s: dropped %d ungrounded triple(s), kept %d",
                str(segment.get("segment_id", ""))[:12], dropped_no_quote, len(facts),
            )
        # Self-verify: second independent LLM pass checks direction and predicate sanity
        if facts:
            facts = self._llm_verify_facts(facts, segment_raw_text, llm)
        return facts

    def _llm_verify_facts(self, facts: list[dict], segment_text: str, llm) -> list[dict]:
        """Independent LLM self-verification pass for direction and predicate sanity."""
        if not facts:
            return facts
        import json as _json
        triple_list = [
            {"i": i, "subject": f["subject"], "predicate": f["predicate"],
             "object": f["object"], "quote": f.get("exact_span", "")}
            for i, f in enumerate(facts)
        ]
        system = (
            "You are verifying (subject, predicate, object) triples extracted from telecom "
            "technical text. For each triple, decide if it is CORRECT given the source quote. "
            "Check: (1) is the direction right (A configures B vs B configures A)? "
            "(2) does the predicate accurately describe the relationship in the quote? "
            "Return JSON array of {i: int, valid: bool, reason: str}."
        )
        prompt = (
            f"Source segment:\n{segment_text[:1500]}\n\n"
            f"Triples to verify:\n{_json.dumps(triple_list, ensure_ascii=False)}\n\n"
            f"Return JSON array only."
        )
        try:
            raw = llm._call_llm(system, prompt, max_tokens=1024)
            if not raw:
                return facts
            import re as _re
            raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
            raw = _re.sub(r"```json\s*", "", raw)
            raw = _re.sub(r"```\s*$", "", raw)
            verdicts = _json.loads(raw)
            if not isinstance(verdicts, list):
                return facts
            invalid_indices = {int(v["i"]) for v in verdicts
                               if isinstance(v, dict) and "i" in v and not v.get("valid", True)}
            if invalid_indices:
                log.info(
                    "  LLM self-verify: dropped %d/%d triples (direction/predicate errors)",
                    len(invalid_indices), len(facts),
                )
            return [f for i, f in enumerate(facts) if i not in invalid_indices]
        except Exception as exc:
            log.debug("Fact verification failed: %s", exc)
            return facts

    def _record_relation_candidate(
        self, predicate: str, subject: str, obj: str,
        segment: dict, source_doc_id: str,
    ) -> None:
        """Store an unknown predicate into evolution_candidates (type='relation')."""
        import json
        from src.utils.normalize import normalize_term
        store = self._store
        normalized = normalize_term(predicate)
        example = json.dumps([{
            "subject": subject, "object": obj,
            "segment_id": str(segment.get("segment_id", "")),
            "source_doc_id": source_doc_id,
        }])
        try:
            store.execute(
                """
                INSERT INTO governance.evolution_candidates
                    (surface_forms, normalized_form, candidate_type, examples,
                     source_count, first_seen_at, last_seen_at, review_status)
                VALUES (ARRAY[%s], %s, 'relation', %s::jsonb, 1, NOW(), NOW(), 'discovered')
                ON CONFLICT (normalized_form) DO UPDATE SET
                    source_count = governance.evolution_candidates.source_count + 1,
                    last_seen_at = NOW(),
                    examples = governance.evolution_candidates.examples || %s::jsonb
                """,
                (predicate, normalized, example, example),
            )
            log.debug("  relation candidate: %s (%s → %s)", predicate, subject, obj)
        except Exception as exc:
            log.warning("Failed to record relation candidate %s: %s", predicate, exc)

    @staticmethod
    def _quote_supported(quote: str, segment_text: str) -> bool:
        """Return True if `quote` is grounded in `segment_text`.

        Accepts two forms of grounding:
        1. Exact substring match (normalised whitespace, case-insensitive).
        2. Fuzzy: ≥80% token overlap — handles minor whitespace / encoding
           differences while remaining strict enough to block hallucinations.
        """
        if not quote or len(quote) < 8:
            return False
        norm_quote = re.sub(r"\s+", " ", quote.lower().strip())
        norm_text  = re.sub(r"\s+", " ", segment_text.lower())
        if norm_quote in norm_text:
            return True
        q_tokens = set(norm_quote.split())
        if not q_tokens:
            return False
        overlap = len(q_tokens & set(norm_text.split())) / len(q_tokens)
        return overlap >= 0.80

    def _resolve_term(self, term: str) -> str | None:
        return self._ontology.lookup_alias(term.lower())

    def _save_facts(self, facts: list[dict], source_doc_id: str) -> None:
        if not facts:
            return
        store = self._store
        with store.transaction() as cur:
            for f in facts:
                cur.execute(
                    """
                    INSERT INTO facts (fact_id, subject, predicate, object, qualifier,
                        domain, confidence, lifecycle_state, ontology_version)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        f["fact_id"], f["subject"], f["predicate"], f["object"],
                        "{}", f.get("domain"), f["confidence"],
                        f["lifecycle_state"], f["ontology_version"],
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO evidence (evidence_id, fact_id, source_doc_id, segment_id,
                        source_rank, extraction_method, evidence_score, exact_span)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        str(uuid.uuid4()), f["fact_id"], source_doc_id,
                        f.get("segment_id"), f["source_rank"],
                        f["extraction_method"], f["confidence"],
                        f.get("exact_span") or None,
                    ),
                )
        log.info("Saved facts doc=%s facts=%d evidence=%d", source_doc_id, len(facts), len(facts))

    @staticmethod
    def _preview_ids(values: list[str], limit: int = 8) -> str:
        if not values:
            return "[]"
        if len(values) <= limit:
            return "[" + ", ".join(values) + "]"
        return "[" + ", ".join(values[:limit]) + f", ...(+{len(values) - limit})]"