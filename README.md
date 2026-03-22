# Telecom Semantic Knowledge Base

A semantic knowledge infrastructure for the network & telecommunications domain.
Transforms public web corpora into computable, traceable, evolvable knowledge — organized by a domain ontology, stored across multi-modal backends, and exposed through a unified semantic operator API.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Database Initialization](#database-initialization)
- [Ontology](#ontology)
- [Pipeline](#pipeline)
- [Semantic API](#semantic-api)
- [Development Roadmap](#development-roadmap)
- [Design Documents](#design-documents)

---

## Overview

This system is **not** a plain vector search engine or a web scraper.
It is a semantic knowledge infrastructure with five core capabilities:

| Capability | Description |
|---|---|
| **Ontology-anchored organization** | All knowledge is tagged against a versioned domain ontology — stable concepts, controlled relations, alias resolution |
| **Knowledge extraction pipeline** | 6-stage pipeline converts raw HTML → clean segments → ontology-aligned facts + evidence |
| **Multi-modal storage** | PostgreSQL (metadata), Neo4j (graph), pgvector (embeddings), MinIO (raw documents) |
| **Semantic operator API** | 13 operators for lookup, graph traversal, dependency analysis, impact propagation, and ontology evolution |
| **Controlled evolution** | New concepts enter a candidate pool and pass a scored gate before touching the core ontology |

### First-version scope: IP / Data Communication Network

Ethernet · VLAN · STP/RSTP/MSTP · LACP · OSPF · IS-IS · BGP · MPLS · LDP · SR-MPLS · SRv6 · EVPN · VXLAN · L3VPN · VRF · QoS · ACL · NAT · IPsec · BFD · NETCONF · YANG · Telemetry

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        Ontology Core Layer                       │
│   YAML definitions  →  OntologyRegistry  →  Neo4j OntologyNodes  │
└───────────────────────────────┬──────────────────────────────────┘
                                │ semantic skeleton
┌───────────────────────────────▼──────────────────────────────────┐
│                      Corpus Ingestion Layer                      │
│   Spider (robots/rate-limit)  →  Extractor  →  Normalizer        │
└───────────────────────────────┬──────────────────────────────────┘
                                │ clean text
┌───────────────────────────────▼──────────────────────────────────┐
│                     Knowledge Processing Pipeline                │
│  Stage 1 Ingest → Stage 2 Segment → Stage 3 Align               │
│  Stage 4 Extract → Stage 5 Dedup  → Stage 6 Index               │
└──────┬──────────────────┬──────────────────────┬─────────────────┘
       │                  │                      │
┌──────▼──────┐  ┌────────▼────────┐  ┌─────────▼──────────┐
│ PostgreSQL  │  │     Neo4j       │  │   pgvector / MinIO │
│  metadata   │  │  ontology+graph │  │  vectors + raw docs │
└──────┬──────┘  └────────┬────────┘  └─────────┬──────────┘
       └──────────────────┴──────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                    Semantic Operator API  (FastAPI)              │
│  lookup · resolve · expand · filter · path · dependency         │
│  impact_propagate · evidence_rank · conflict_detect             │
│  fact_merge · candidate_discover · attach_score · evolution_gate│
└─────────────────────────────────────────────────────────────────┘
```

### Storage roles

| Store | Role |
|---|---|
| **PostgreSQL** | Source registry, crawl tasks, document metadata, segments, facts, evidence, conflict records, ontology versions, evolution candidates |
| **Neo4j** | Ontology nodes, concept graph, fact nodes, evidence nodes, TAGGED_WITH / SUPPORTED_BY / BELONGS_TO edges |
| **pgvector** | Segment embeddings for semantic similarity search (column on `segments` table) |
| **MinIO / S3** | Raw HTML snapshots, cleaned text, PDF attachments |

---

## Project Structure

```
Self_Knowledge_Evolve/
├── docs/                                   # Design documents
│   ├── telecom-semantic-kb-system-design.md
│   ├── telecom-ontology-design.md
│   └── development-plan-detailed.md
│
├── ontology/                               # Ontology source of truth (YAML)
│   ├── top/
│   │   └── relations.yaml                  # 30 controlled relation types
│   ├── domains/
│   │   └── ip_network.yaml                 # ~60 IP/DC domain nodes
│   ├── lexicon/
│   │   └── aliases.yaml                    # EN/ZH aliases + vendor terms
│   └── governance/
│       └── evolution_policy.yaml           # Anti-drift thresholds
│
├── scripts/
│   ├── init_postgres.sql                   # DDL: 13 tables + pgvector
│   ├── init_neo4j.py                       # Neo4j constraints + indexes
│   └── load_ontology.py                    # YAML → Neo4j + PG lexicon
│
├── src/
│   ├── config/
│   │   └── settings.py                     # Pydantic settings, reads .env
│   ├── db/
│   │   ├── postgres.py                     # PG connection pool + helpers
│   │   └── neo4j_client.py                 # Neo4j driver wrapper
│   ├── utils/
│   │   ├── text.py                         # Normalization, token count, truncate
│   │   ├── hashing.py                      # SHA-256, SimHash, Hamming, Jaccard
│   │   ├── confidence.py                   # Weighted confidence scoring
│   │   └── logging.py                      # Structured logging setup
│   ├── ontology/
│   │   ├── registry.py                     # In-memory ontology registry
│   │   └── validator.py                    # YAML structural validation
│   ├── crawler/
│   │   ├── spider.py                       # HTTP fetch, robots.txt, rate limit
│   │   ├── extractor.py                    # trafilatura + readability extraction
│   │   └── normalizer.py                   # Boilerplate removal, hash computation
│   ├── pipeline/
│   │   ├── runner.py                       # Orchestrates all 6 stages
│   │   └── stages/
│   │       ├── stage1_ingest.py            # Rules C1-C5: fetch, dedup, doc_type
│   │       ├── stage2_segment.py           # Rules S1-S4: structural+semantic split
│   │       ├── stage3_align.py             # Rules A1-A5: ontology tagging
│   │       ├── stage4_extract.py           # Rules R1-R4: relation extraction
│   │       ├── stage5_dedup.py             # Rules D1-D5: SimHash + fact dedup
│   │       └── stage6_index.py             # Rules I1-I3: Neo4j indexing
│   ├── api/
│   │   └── semantic/
│   │       ├── lookup.py                   # Term → ontology node
│   │       ├── resolve.py                  # Alias → canonical node
│   │       ├── expand.py                   # Node neighbourhood traversal
│   │       ├── filter.py                   # Parameterized fact/segment filter
│   │       ├── path.py                     # Shortest path between nodes
│   │       ├── dependency.py               # Dependency closure BFS
│   │       ├── impact.py                   # Fault impact propagation BFS
│   │       ├── evidence.py                 # Evidence rank, conflict detect, fact merge
│   │       ├── evolution.py                # Candidate discover, attach score, gate
│   │       └── router.py                   # FastAPI router wiring all operators
│   └── app.py                              # FastAPI entry point
│
├── tests/
├── .env.example                            # Connection config template
├── requirements.txt
└── README.md
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker containers running **PostgreSQL** and **Neo4j**
- (Optional) MinIO for object storage

### 1. Clone and install

```bash
git clone git@github.com:ZoeRen-pp/Self_Knowledge_Evolve.git
cd Self_Knowledge_Evolve

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure connections

```bash
cp .env.example .env
```

Edit `.env` — minimum required fields:

```dotenv
# PostgreSQL container
POSTGRES_HOST=localhost             # or Docker container name
POSTGRES_PORT=5432
POSTGRES_DB=telecom_kb
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password

# Neo4j container
NEO4J_URI=bolt://localhost:7687    # or bolt://container_name:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_password
```

> **Docker network tip:** If the API container and DB containers share the same Docker network, use container names instead of `localhost`.

### 3. Initialize databases

```bash
# PostgreSQL: create all 13 tables + pgvector extension
psql -h localhost -U postgres -d telecom_kb -f scripts/init_postgres.sql

# Neo4j: create uniqueness constraints and lookup indexes
python scripts/init_neo4j.py
```

### 4. Load the ontology

```bash
# Validate YAML structure first
python -c "from src.ontology.validator import validate_all; from pathlib import Path; validate_all(Path('ontology'))"

# Dry-run (no writes)
python scripts/load_ontology.py --dry-run

# Load all domains + aliases
python scripts/load_ontology.py
```

### 5. Start the API server

```bash
uvicorn src.app:app --host 0.0.0.0 --port 8000 --reload
```

Open **http://localhost:8000/docs** for the interactive Swagger UI.

Health check:
```bash
curl http://localhost:8000/health
# {"postgres": true, "neo4j": true, "status": "ok"}
```

---

## Configuration

All settings are read from `.env` via `src/config/settings.py`.

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_DB` | `telecom_kb` | Database name |
| `POSTGRES_USER` | `postgres` | Username |
| `POSTGRES_PASSWORD` | `changeme` | Password |
| `POSTGRES_POOL_MIN` | `2` | Min pool connections |
| `POSTGRES_POOL_MAX` | `10` | Max pool connections |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j bolt URI |
| `NEO4J_USER` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | `changeme` | Neo4j password |
| `NEO4J_DATABASE` | `neo4j` | Neo4j database name |
| `MINIO_ENDPOINT` | `localhost:9000` | MinIO endpoint |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO access key |
| `MINIO_SECRET_KEY` | `minioadmin` | MinIO secret key |
| `ONTOLOGY_VERSION` | `v0.1.0` | Active ontology version tag |
| `LOG_LEVEL` | `INFO` | Logging level |

---

## Database Initialization

### PostgreSQL tables (13)

| Table | Purpose |
|---|---|
| `source_registry` | Site whitelist, source rank (S/A/B/C), crawl config |
| `crawl_tasks` | URL queue, status tracking, retry count |
| `documents` | Document metadata, content hashes, dedup grouping |
| `segments` | Semantic segments with SimHash and pgvector column |
| `segment_tags` | Canonical / semantic_role / context tags per segment |
| `facts` | Normalized SPO triples with confidence and lifecycle |
| `evidence` | Source evidence linking facts to segments and documents |
| `conflict_records` | Detected contradictions between facts |
| `ontology_versions` | Versioned ontology snapshots and change log |
| `evolution_candidates` | Candidate new concepts with scoring dimensions |
| `review_records` | Audit trail for all human review actions |
| `lexicon_aliases` | Mirror of ontology aliases for SQL-side resolution |
| `extraction_jobs` | Per-document pipeline job tracking |

### Neo4j node types (9)

`OntologyNode` · `Concept` · `Entity` · `Fact` · `KnowledgeSegment` · `SourceDocument` · `Evidence` · `Alias` · `CandidateConcept`

### Neo4j edge types (key)

`SUBCLASS_OF` · `INSTANCE_OF` · `PART_OF` · `RELATED_TO` · `DEPENDS_ON` · `USES` · `IMPACTS` · `CAUSES` · `SUPPORTED_BY` · `DERIVED_FROM` · `EXTRACTED_FROM` · `BELONGS_TO` · `TAGGED_WITH` · `ALIAS_OF` · `CONTRADICTS`

---

## Ontology

The ontology lives in `ontology/` YAML files — **these are the source of truth**, not Neo4j.
Neo4j is the runtime projection; PostgreSQL tracks versions and governance.

### Modification workflow

```
Edit YAML file
     ↓
python scripts/load_ontology.py --dry-run   ← validate
     ↓
Human review (for domain/core changes)
     ↓
python scripts/load_ontology.py             ← write to Neo4j + PG
     ↓
Bump ONTOLOGY_VERSION in .env
```

### Evolution layers

| Layer | Who can change | Change quota |
|---|---|---|
| Core ontology (top-level classes, relation types) | Manual approval only, 2 reviewers | 5 per release |
| Domain ontology (sub-domain concepts) | Semi-auto + 1 reviewer | 20 per release |
| Lexicon / aliases | Auto if confidence ≥ 0.80, 2+ sources | Unlimited |

### Candidate concept admission thresholds

```yaml
min_source_count:       3      # must appear in 3+ documents
min_source_diversity:   0.6    # from 3+ distinct sites
min_temporal_stability: 0.7    # present in 2+ crawl cycles
min_structural_fit:     0.65   # can attach to a clear parent node
min_composite_score:    0.65
synonym_risk_max:       0.4    # must not be a simple synonym
require_human_review:   true
```

---

## Pipeline

The 6-stage pipeline converts a crawled document into indexed graph knowledge.

```
crawl_task
    │
    ▼ Stage 1 — Ingest (rules C1–C5)
    │  robots check · rate limit · content_hash dedup
    │  text extraction · doc_type detection
    │
    ▼ Stage 2 — Segment (rules S1–S4)
    │  structural split: headings / tables / code blocks
    │  semantic role classification: definition / config / fault / …
    │  length control: 30–512 tokens; sliding window for oversized
    │
    ▼ Stage 3 — Align (rules A1–A5)
    │  alias dictionary exact match
    │  ontology node lookup → canonical tags
    │  unmatched terms → evolution_candidates table
    │  semantic_role + context tags
    │
    ▼ Stage 4 — Extract (rules R1–R4)
    │  15 regex relation patterns → (subject, predicate, object)
    │  predicate validation against controlled relation set
    │  both endpoints must resolve to ontology nodes
    │  confidence scoring: source_rank × extraction_method × ontology_fit …
    │
    ▼ Stage 5 — Dedup (rules D1–D5)
    │  segment dedup: SimHash hamming ≤ 3 + Jaccard > 0.85
    │  fact dedup: exact SPO triple match → merge_cluster
    │  conflict detection: same subject+predicate, different object
    │
    ▼ Stage 6 — Index (rules I1–I3)
       gate: segment confidence ≥ 0.5, fact confidence ≥ 0.5
       write PG (already done) → Neo4j nodes + edges
       mark document status = 'indexed'
```

### Run the pipeline

```python
from src.pipeline.runner import PipelineRunner

runner = PipelineRunner()

# Single document
runner.run_document(crawl_task_id=1)

# Batch (picks up pending tasks automatically)
runner.run_pending(limit=50)
```

---

## Semantic API

Base URL: `http://localhost:8000/api/v1/semantic`

All responses include a `meta` envelope:
```json
{
  "meta": { "ontology_version": "v0.1.0", "latency_ms": 12 },
  "result": { ... }
}
```

### Operator reference

#### Lookup & Resolution

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/lookup` | Resolve a term (alias/full name/Chinese) to its ontology node + optional evidence |
| `GET` | `/resolve` | Map an alias or vendor term to the canonical node ID |

```bash
# Example
curl "http://localhost:8000/api/v1/semantic/lookup?term=BGP&include_evidence=true"
curl "http://localhost:8000/api/v1/semantic/resolve?alias=Border+Gateway+Protocol"
curl "http://localhost:8000/api/v1/semantic/resolve?alias=Etherchannel&vendor=Cisco"
```

#### Graph Traversal

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/expand` | Expand a node's neighbourhood (depth 1–3, filter by relation types) |
| `GET` | `/path` | Shortest semantic path between two ontology nodes |
| `GET` | `/dependency_closure` | Full dependency tree via BFS (DEPENDS_ON + REQUIRES) |

```bash
curl "http://localhost:8000/api/v1/semantic/expand?node_id=IP.EVPN&depth=2&include_facts=true"
curl "http://localhost:8000/api/v1/semantic/path?start_node_id=IP.EVPN_VXLAN&end_node_id=IP.BGP"
curl "http://localhost:8000/api/v1/semantic/dependency_closure?node_id=IP.EVPN_VXLAN"
```

#### Impact & Filtering

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/impact_propagate` | BFS fault/change blast-radius from an event node |
| `POST` | `/filter` | Parameterized filter over facts, segments, or concepts |

```bash
curl -X POST "http://localhost:8000/api/v1/semantic/impact_propagate" \
  -H "Content-Type: application/json" \
  -d '{"event_node_id":"IP.BGP","event_type":"fault","relation_policy":"causal","max_depth":3}'
```

#### Evidence & Governance

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/evidence_rank` | Rank evidence supporting a fact by quality |
| `GET` | `/conflict_detect` | Find contradictory facts for a topic node |
| `POST` | `/fact_merge` | Merge duplicate facts into one canonical fact |

#### Ontology Evolution

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/candidate_discover` | Surface new candidate concepts from recent corpus |
| `GET` | `/attach_score` | Score which parent node a candidate best fits |
| `POST` | `/evolution_gate` | Check whether a candidate passes admission thresholds |

```bash
curl "http://localhost:8000/api/v1/semantic/candidate_discover?window_days=30&min_frequency=5"

curl -X POST "http://localhost:8000/api/v1/semantic/evolution_gate" \
  -H "Content-Type: application/json" \
  -d '{"candidate_id":"<uuid>"}'
```

Full interactive documentation: **http://localhost:8000/docs**

---

## Development Roadmap

| Phase | Scope | Status |
|---|---|---|
| **Phase 1** | Ontology definition, relation types, tag system | ✅ Complete |
| **Phase 2** | Crawl pipeline: ingest → segment → align → extract → dedup → index | ✅ Complete |
| **Phase 3** | Knowledge governance: multi-source merge, conflict resolution | ✅ Complete |
| **Phase 4** | Semantic operator API (13 operators) | ✅ Complete |
| **Phase 5** | Ontology evolution loop: discover → score → gate → publish | ✅ Complete |
| **Phase 6** | Application integration: Q&A, config understanding, fault analysis | 🔜 Planned |

### Next steps (Phase 6 candidates)

- [ ] Add site whitelist seeds for IETF RFC, Cisco Docs, Huawei iLearningX
- [ ] Replace stub `_load_raw()` with real MinIO object storage fetch
- [ ] Add LLM-assisted extraction for S/A-rank sources (stage 4 enhancement)
- [ ] Add pgvector ANN index for semantic segment retrieval
- [ ] Add Airflow/Prefect DAG for scheduled batch crawling
- [ ] Add embedding generation step in stage 6 (OpenAI / local model)
- [ ] Add Q&A endpoint that retrieves segments + facts by semantic similarity

---

## Design Documents

Detailed design rationale is in `docs/`:

| Document | Contents |
|---|---|
| `telecom-semantic-kb-system-design.md` | Full system design: 6-layer architecture, storage selection, quality assurance, risk analysis |
| `telecom-ontology-design.md` | Ontology design principles, 12 domain areas, evolution rules, YAML node format |
| `development-plan-detailed.md` | PostgreSQL DDL, Neo4j schema, complete pipeline rules (C1-C5, S1-S4, A1-A5, R1-R4, D1-D5, I1-I3), API specs |

---

## Source Trust Levels

| Rank | Sources | Role in system |
|---|---|---|
| **S** | IETF, 3GPP, ITU-T, IEEE, ETSI, MEF, ONF | Primary facts, high confidence |
| **A** | Cisco, Huawei, Juniper, Nokia, Arista, H3C | Secondary facts, vendor context |
| **B** | Technical whitepapers, open courseware | Supporting evidence |
| **C** | Blogs, forums, Q&A communities | Auxiliary evidence only |

Confidence formula: `0.30×source_authority + 0.20×extraction_quality + 0.20×ontology_fit + 0.20×cross_source_consistency + 0.10×temporal_validity`

---

## License

This project is for research and internal knowledge engineering purposes.
All crawled content remains the property of its original authors.
The system stores knowledge indexes and evidence references, not full-text reproductions.