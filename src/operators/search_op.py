"""Semantic search operators — pgvector similarity over segments and EDUs."""

from __future__ import annotations

from semcore.operators.base import OperatorResult, SemanticOperator


class SemanticSearchOperator(SemanticOperator):
    name = "semantic_search"

    def execute(self, app, **kw) -> OperatorResult:
        from src.config.settings import settings
        if not settings.EMBEDDING_ENABLED:
            return OperatorResult(
                data={"items": [], "note": "Embedding is disabled"},
                ontology_version=app.ontology.version(),
            )
        # Use the domain-specific helper on EmbeddingProvider
        query_vec = app.embedding.embed_query(kw["query"])  # type: ignore[attr-defined]
        if query_vec is None:
            return OperatorResult(
                data={"items": [], "note": "Embedding model unavailable"},
                ontology_version=app.ontology.version(),
            )
        pg_vec = app.embedding.to_pg_literal(query_vec)  # type: ignore[attr-defined]

        layer_filter = kw.get("layer_filter")
        layer_join  = ""
        layer_where = ""
        if layer_filter:
            tag_type   = f"{layer_filter}_tag" if layer_filter != "concept" else "canonical"
            layer_join  = "JOIN segment_tags st ON s.segment_id = st.segment_id"
            layer_where = f"AND st.tag_type = '{tag_type}'"

        sql = f"""
            SELECT s.segment_id, s.segment_type, s.section_title,
                   LEFT(s.normalized_text, 300) AS text_preview,
                   1 - (s.embedding <=> %s::vector) AS similarity
            FROM segments s {layer_join}
            WHERE s.lifecycle_state = 'active' AND s.embedding IS NOT NULL {layer_where}
            ORDER BY s.embedding <=> %s::vector
            LIMIT %s
        """
        top_k   = kw.get("top_k", 5)
        min_sim = kw.get("min_similarity", 0.5)
        rows    = app.store.fetchall(sql, (pg_vec, pg_vec, top_k))
        items   = [r for r in rows if float(r.get("similarity") or 0) >= min_sim]
        return OperatorResult(
            data={"items": items, "total": len(items)},
            ontology_version=app.ontology.version(),
        )


class EduSearchOperator(SemanticOperator):
    name = "edu_search"

    def execute(self, app, **kw) -> OperatorResult:
        from src.config.settings import settings
        if not settings.EMBEDDING_ENABLED:
            return OperatorResult(
                data={"items": [], "note": "Embedding is disabled"},
                ontology_version=app.ontology.version(),
            )
        query_vec = app.embedding.embed_query(kw["query"])  # type: ignore[attr-defined]
        if query_vec is None:
            return OperatorResult(
                data={"items": [], "note": "Embedding model unavailable"},
                ontology_version=app.ontology.version(),
            )
        pg_vec  = app.embedding.to_pg_literal(query_vec)  # type: ignore[attr-defined]
        tw      = max(0.0, min(1.0, float(kw.get("title_weight", 0.3))))
        cw      = 1.0 - tw
        top_k   = kw.get("top_k", 5)
        min_sim = kw.get("min_similarity", 0.5)

        sql = """
            SELECT s.segment_id AS edu_id, s.title,
                   LEFT(s.raw_text, 300) AS content_preview,
                   s.content_source, s.lifecycle_state AS status,
                   CASE
                     WHEN s.title_vec IS NOT NULL AND s.content_vec IS NOT NULL
                     THEN %(tw)s * (1 - (s.title_vec   <=> %(vec)s::vector))
                        + %(cw)s * (1 - (s.content_vec <=> %(vec)s::vector))
                     WHEN s.content_vec IS NOT NULL
                     THEN 1 - (s.content_vec <=> %(vec)s::vector)
                     ELSE NULL
                   END AS similarity
            FROM segments s
            WHERE s.lifecycle_state = 'active' AND s.content_vec IS NOT NULL
            ORDER BY similarity DESC NULLS LAST
            LIMIT %(top_k)s
        """
        rows  = app.store.fetchall(sql, {"vec": pg_vec, "tw": tw, "cw": cw, "top_k": top_k})
        items = [r for r in rows if float(r.get("similarity") or 0) >= min_sim]
        return OperatorResult(
            data={"items": items, "total": len(items)},
            ontology_version=app.ontology.version(),
        )