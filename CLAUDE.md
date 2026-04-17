# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

**Telecom Semantic Knowledge Base** — a governance-enabled, evolving, source-attributed knowledge infrastructure for **network integration & delivery engineers**. **Not RAG, not a search engine.** Target users cover the full delivery lifecycle: pre-sales survey → greenfield network design → configuration → integration delivery → network change (move/add/change) design & delivery. Core value: cross-vendor term normalization, config dependency chain analysis, change impact assessment, knowledge provenance with 5-dim confidence scoring, design rationale traceability, ontology drift prevention, and vector semantic search.

## Commands

### Local Development (no Docker required)
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run_dev.py                 # → http://127.0.0.1:8000/docs
```
Smoke test: `curl http://localhost:8000/health` and `curl "http://localhost:8000/api/v1/semantic/lookup?term=BGP"`

### Port Note
Port `:8000` is reserved by the BGE-M3 embedding systemd service running in WSL2. This app runs on `:8001` (set via `APP_PORT` in `.env`).

### Production Setup
```bash
cp .env.example .env
psql -h 192.168.3.71 -U postgres -d telecom_kb -f scripts/init_postgres.sql
psql -h 192.168.3.71 -U postgres -d telecom_crawler -f scripts/init_crawler_postgres.sql
python scripts/init_neo4j.py
python scripts/load_ontology.py       # YAML → Neo4j + PG lexicon (cold start)
uvicorn src.app:app --host 0.0.0.0 --port 8001
python worker.py                      # background: 4 threads, 60+ seed URLs, LLM hard dependency
```
Dashboard: `http://localhost:8001/dashboard`

### Complete Reset
```bash
python scripts/reset_and_run.py   # kill processes → clear data → load ontology → start worker → start API
```

### Ontology Changes
```bash
# Edit ontology/domains/*.yaml or ontology/top/relations.yaml or ontology/lexicon/aliases.yaml
python scripts/load_ontology.py       # sync YAML → Neo4j + PG; never edit Neo4j directly
```

### Docker (PostgreSQL + Neo4j only)
```bash
docker-compose up -d
```

## Architecture

### Layer Stack
```
semcore ABCs  (semcore/semcore/)               ← zero-dependency framework, publishable standalone
    ↑ implements
src/ domain implementation                     ← telecom-specific providers, operators, pipeline
    ↑ wired by
src/app_factory.py → build_app() → SemanticApp singleton
    ↑ serves
src/app.py (FastAPI)
    ├─ src/api/query_router.py    → POST /api/v1/query (declarative query engine)
    ├─ src/api/copilot_router.py  → POST /api/v1/copilot (natural language Q&A)
    ├─ src/api/semantic/router.py → 21 semantic operators
    └─ src/api/system/router.py   → monitoring, showcase (uses QueryEngine internally)
```

`semcore` is not installed as a package — it's imported via `sys.path.insert(0, "semcore")` in run scripts, or `pip install -e semcore`.

### Knowledge Persistence
```
ontology/*.yaml  →  source of truth (version-controlled)
      ↓ scripts/load_ontology.py
Neo4j            →  runtime graph projection (5 node label types, graph traversal)
PostgreSQL       →  relational store + governance audit log
OntologyRegistry →  in-memory cache used by pipeline alignment stages
```

### Neo4j Dual-Layer Graph Model
```
Ontology Layer (graph traversal: dependency closure, impact propagation)
  OntologyNode ─[DEPENDS_ON/EXPLAINS/...]─> OntologyNode/MechanismNode/MethodNode/...
  Alias ─[:ALIAS_OF]─> any ontology node
  Edges = aggregated from multiple Facts (fact_count + max confidence)

Provenance Layer (evidence tracing: where did this conclusion come from)
  Fact ─[:SUPPORTED_BY]─> Evidence ─[:EXTRACTED_FROM]─> KnowledgeSegment ─[:BELONGS_TO]─> SourceDocument
  Fact references OntologyNodes by property (f.subject = node_id), NOT by graph edge
```
Fact nodes are intentionally disconnected from OntologyNodes in the graph — this keeps the ontology layer clean for traversal queries, unaffected by Fact lifecycle (active/conflicted/superseded/merged). See `docs/architecture-design-20260406.md` §6.2.1.

### 7-Stage Pipeline (triggered by `source_doc_id`)
```
Stage 1: Ingest    → text extraction, denoise, SHA256 dedup, quality gate, doc_type detection
Stage 2: Segment   → 4-step joint segmentation + typing:
                     structural split → paragraph-level LLM classification (17 types) →
                     merge adjacent same-type paragraphs (same section, ≤1024 tokens) →
                     length control (max 1024 tokens, sliding 512/64 fallback)
                     10 paragraph-level discourse relation types with nuclearity (NS/SN/NN);
                     segment boundaries coincide with communicative role changes
Stage 3: Align     → exact+alias match (word-boundary aware), embedding fuzzy match on miss (threshold 0.80),
                     5-layer tagging, LLM candidate discovery (classified: new_concept/variant/noise)
Stage 3b: Evolve   → 6-dim scoring, 6-gate review, auto-promote (score ≥ 0.85 + all gates + ≥ 7 days)
Stage 4: Extract   → 5-priority extraction:
                     P0 RST-derived facts (Constraint→constrained_by, Prerequisite→requires, etc.)
                     P1 single-segment LLM (≥3 anchor nodes, quote grounding ≥80% token overlap)
                     P2 merged-context LLM (continuative RST: Elaboration/Sequence/Causation/Evidence/Background)
                     P2-chain multi-hop RST chains (3-4 segments, ≤1500 tokens)
                     P3 dual-node co-occurrence fallback (exactly 2 nodes + predicate signal)
Stage 5: Dedup     → SimHash + embedding semantic dedup (cosine > 0.90), fact merging,
                     ontology-driven conflict detection (D4 only fires for cardinality=one predicates)
Stage 6: Index     → confidence gate (segment ≥ 0.5, fact ≥ 0.5), Neo4j ingestion, vector index
```
To feed the pipeline: insert a `documents` row with `status='raw'` and upload the file to MinIO `raw/`. The worker polls automatically with continuous-feed: each worker grabs a new doc immediately on completion, never blocked by slow documents on other workers. Worker auto-seeds 60+ authoritative URLs across 10 sites. LLM is a hard dependency — pipeline blocks until LLM is reachable.

### Declarative Query Engine (`POST /api/v1/query`)
5 algebraic primitives over a heterogeneous information network (Neo4j graph + PostgreSQL relations):
- `seed`: initialize result set (by id/alias/layer/embedding/attribute)
- `expand`: graph traversal (ontology edges + reserved: `tagged_in`, `rst_adjacent`, `evidenced_by`)
- `combine`: set operations (union/intersect/subtract)
- `aggregate`: count/rank/group/score/rerank (keyword relevance + optional cross-encoder)
- `project`: field projection

Planner auto-detects variable dependencies (`from`, `input`, `sets` fields) and groups steps into parallel execution waves. Total query timeout: 30s.

### Knowledge Copilot (`POST /api/v1/copilot`)
Natural language Q&A: question → ontology term extraction (alias matching) → build query plan → execute via QueryEngine → LLM synthesizes answer from retrieved facts + segments. Falls back to structured result listing when LLM is unavailable.

### 21 Semantic Operators (REST `/api/v1/semantic/`)
`lookup`, `resolve`, `expand`, `path`, `dependency_closure`, `impact_propagate`, `filter`, `evidence_rank`, `conflict_detect`, `fact_merge`, `candidate_discover`, `attach_score`, `evolution_gate`, `context_assemble`, `semantic_search`, `ontology_quality`, `stale_knowledge`, `cross_layer_check`, `graph_inspect`, `ontology_inspect`, `edu_search`

All responses follow: `{"meta": {"ontology_version": ..., "latency_ms": ...}, "result": {...}}`

### Dev Mode (in-memory, no external services)
`src/dev/` replaces all external stores:
- `fake_postgres.py` → SQLite `:memory:` for knowledge DB
- `fake_crawler_postgres.py` → SQLite `:memory:` for crawler DB
- `fake_neo4j.py` → dict-based graph store
- `seed.py` → seeds from YAML ontology at startup

## Key Conventions

### Adding a New Operator
1. `src/api/semantic/xxx.py` — business logic
2. `src/operators/xxx_op.py` — `SemanticOperator` wrapper
3. `src/operators/__init__.py` — register in `ALL_OPERATORS`
4. `src/api/semantic/router.py` — add FastAPI endpoint

### Database Split
- **`telecom_kb`** (main): `documents`, `segments`, `facts`, `evidence`, plus `governance` schema (`evolution_candidates`, `conflict_records`, `review_records`, `ontology_versions`)
- **`telecom_crawler`** (separate DB): `source_registry`, `crawl_tasks`, `extraction_jobs`
- In pipeline stages: use `app.store` for knowledge DB, `app.crawler_store` for crawler DB — never cross them
- Governance tables require `governance.` schema prefix in SQL; dev mode SQLite strips it automatically

### Confidence Formula
```
score = 0.30×source_authority + 0.20×extraction_method
      + 0.20×ontology_fit + 0.20×cross_source_consistency + 0.10×temporal_validity
```
Source authority tiers: S (IETF/3GPP/ITU-T/IEEE) → 1.0 · A (Cisco/Huawei/Juniper) → 0.85 · B (whitepapers) → 0.65 · C (blogs/forums) → 0.40

### Optional Features
- `LLM_ENABLED=true` — enables OpenAI-compatible LLM for Stage 2 classification, Stage 3 candidate discovery, Stage 4 extraction, and maintenance classification (`LLM_API_KEY` + `LLM_BASE_URL` required; configured for Gemini by default via Google AI API)
- `EMBEDDING_ENABLED=true` — enables vector search via 3-tier fallback: HTTP BGE-M3 service (`EMBEDDING_HTTP_URL`, preferred) → Ollama (`OLLAMA_URL` + `OLLAMA_EMBED_MODEL`) → local sentence-transformers

## Five-Layer Ontology Model

| Layer | YAML file | Neo4j label | Count | Definition |
|-------|-----------|-------------|-------|------------|
| concept | `ip_network.yaml` | `OntologyNode` | 114 | YANG-referenced CLI-configurable objects (interfaces, protocol instances, policies, VPNs, etc.) |
| mechanism | `ip_network_mechanisms.yaml` | `MechanismNode` | 24 | Protocol algorithms and forwarding mechanisms (how things work) |
| method | `ip_network_methods.yaml` | `MethodNode` | 22 | Configuration and troubleshooting procedures (how to operate) |
| condition | `ip_network_conditions.yaml` | `ConditionRuleNode` | 20 | Applicability conditions, constraints, and decision rules (when to use) |
| scenario | `ip_network_scenarios.yaml` | `ScenarioPatternNode` | 13 | Deployment patterns and business scenarios (real-world contexts) |

Relations: 77 types in `ontology/top/relations.yaml` (with optional `cardinality: one` for functional predicates). Aliases: 828 entries in `ontology/lexicon/aliases.yaml` (Chinese/English + vendor variants). Seed relations: 187 (axiom 85 + cross-layer 102).

**Concept layer (v0.3.0):** Redesigned from abstract protocol concepts to YANG-level configurable objects. Protocol-level abstract relations (e.g. "BGP uses TCP") removed; only structural config dependencies retained. Category groupings follow the pattern "Category grouping X configurable objects."

## Design Docs

All architecture decisions are documented before implementation. Key references:
- `docs/architecture-design-20260406.md` — system architecture design
- `docs/development-spec-20260406.md` — full development specification
- `docs/query-engine-design-20260417.md` — declarative query engine design (5 algebraic primitives)
- `docs/product-architecture-4plus1-20260413.md` — 4+1 product architecture view
- `docs/candidate-dedup-design.md` — candidate deduplication design
- `docs/embedding-enhancements-design.md` — embedding enhancement design
- `docs/telecom-ontology-design.md` — 5-layer knowledge model design
- `docs/semcore-framework-design.md` — semcore framework abstraction rationale