# Datavio – Technical Documentation

## Overview
Datavio is a production‑grade product data aggregation engine. It ingests product data from CSV/XLSX files, enriches it by fetching information from authoritative web sources (via SearXNG and LLMs), cleans, standardizes, and unifies attributes, and finally publishes a golden record. The system supports configurable business rules, human‑in‑the‑loop (HITL) approvals, and validation/backfilling workflows.

## Architecture

The backend is built with **FastAPI**, **SQLAlchemy** (async), **PostgreSQL**, and **SearXNG**. Key architectural layers:

- **API Layer** – REST endpoints for all operations.
- **Aggregation Pipeline** – asynchronous workflows that orchestrate search, download, extraction, unification, enrichment, and publishing.
- **LLM Integration** – prompts for extraction, standardization, validation, and enrichment.
- **Task Queue** – in‑memory worker pool for processing multiple products concurrently.
- **Audit & Logging** – tracks all operations and provides status feedback.

All external calls (SearXNG, LLMs, downloads) are asynchronous with retries and concurrency limits.

## API Reference

### Products
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/api/v1/products/` | List products with pagination and filtering. |
| POST   | `/api/v1/products/` | Create a new product (manual). |
| POST   | `/api/v1/products/{product_code}/enrich` | Trigger enrichment for a single product. |

### Authentication
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST   | `/api/v1/auth/login/access-token` | Login (returns JWT). |
| POST   | `/api/v1/auth/register` | Register a new user. |

### Audit
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/api/v1/audit-trail/` | Retrieve audit trail of actions. |

### Golden Records
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/api/v1/golden-records/` | List golden records. |
| POST   | `/api/v1/golden-records/publish/{product_id}` | Publish a golden record. |
| GET    | `/api/v1/golden-records/publishable` | Get records ready for publishing. |

### Dashboard & Metrics
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/api/v1/dashboard/debug/{project_id}` | Debug project products. |
| GET    | `/api/v1/dashboard/metrics` | Global metrics. |
| GET    | `/api/v1/dashboard/metrics/{project_id}` | Project‑specific metrics. |

### Business Rules & Prompts
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST   | `/api/v1/business-rules/` | Create a business rule. |
| GET    | `/api/v1/business-rules/` | List rules. |
| GET    | `/api/v1/business-rules/{rule_id}` | Get a rule. |
| PUT    | `/api/v1/business-rules/{rule_id}` | Update a rule. |
| GET    | `/api/v1/business-rules/{rule_identifier}/prompts` | Get prompts for a rule. |
| POST   | `/api/v1/business-rules/{rule_identifier}/prompts` | Add a prompt to a rule. |
| PATCH  | `/api/v1/business-rules/prompts/{prompt_id}/status` | Enable/disable a prompt. |

### Projects
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/api/v1/projects/` | List projects. |
| POST   | `/api/v1/projects/` | Create a project. |

### Sources & Batch Processing
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/api/v1/sources/` | List all sources. |
| POST   | `/api/v1/sources/` | Upload a file (CSV/XLSX) for extraction. |
| POST   | `/api/v1/sources/batch-aggregate` | Upload and automatically start aggregation. |
| GET    | `/api/v1/sources/batch-status/{batch_id}` | Check batch processing status. |
| GET    | `/api/v1/sources/project/{project_id}` | Get sources for a project. |
| POST   | `/api/v1/sources/aggregate/{source_id}` | Trigger aggregation for a specific source. |
| GET    | `/api/v1/sources/{source_id}/metrics` | Get processing metrics for a source. |

### Cleansing & Standardization
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/api/v1/cleansing/issues` | List all issues (validation errors). |
| POST   | `/api/v1/cleansing/resolve/{issue_id}` | Mark an issue as resolved. |
| POST   | `/api/v1/cleansing/projects/{project_id}/clean` | Start a cleaning job for a project. |
| GET    | `/api/v1/cleansing/tasks/{task_id}` | Get status of a cleaning task. |
| GET    | `/api/v1/cleansing/projects/{project_id}/download` | Download cleaned project data. |
| GET    | `/api/v1/standardization/{product_id}` | Get standardized attributes for a product. |
| POST   | `/api/v1/standardization/run/{product_id}` | Run standardization on a product. |

### Aggregation (Core)
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST   | `/api/v1/aggregation/project/{project_id}` | Start aggregation for all products in a project. |
| GET    | `/api/v1/aggregation/project/{project_id}/status` | Check aggregation progress. |
| POST   | `/api/v1/aggregation/project/{project_id}/cancel` | Cancel running aggregation. |
| POST   | `/api/v1/aggregation/run/{product_id}` | Aggregate a single product (manual). |
| GET    | `/api/v1/aggregation/attributes/{product_id}` | Get aggregated attributes (raw). |
| DELETE | `/api/v1/aggregation/jobs/cleanup` | Clean up old job records. |

### Enrichment
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/api/v1/enrichment/{product_id}` | Get enrichment data for a product. |
| POST   | `/api/v1/enrichment/run/{product_id}` | Run enrichment on a product. |

### Human‑in‑the‑Loop (HITL)
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/api/v1/hitl/pending` | List pending approval items. |
| GET    | `/api/v1/hitl/stats/{project_id}` | HITL statistics for a project. |
| POST   | `/api/v1/hitl/approve` | Approve a pending item. |
| POST   | `/api/v1/hitl/override` | Override a value manually. |
| POST   | `/api/v1/hitl/reject` | Reject a pending item. |

### Publishing
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/api/v1/publishing/targets/{project_id}` | Get publishing targets for a project. |
| POST   | `/api/v1/publishing/targets` | Create a publishing target. |
| GET    | `/api/v1/publishing/export/{project_id}` | Export catalog to CSV. |

### Health
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/health` | Service health check. |

## Data Models (Key Schemas)

- **ProductResponse** – product identifier, MPN, brand, taxonomy, current attributes.
- **ProductAggregationResponse** – golden record with unified attributes, descriptions, features, source URLs, and confidence.
- **AggregationJobResponse** – job status (pending, running, completed, failed), progress, error messages.
- **RulePromptResponse** – prompt text, type (extraction, unification, enrichment, validation), status.
- **SourceResponse** – source file metadata, project association, processing status.
- **ValidationError** – attribute name, expected vs actual value, recommendation.
- **DashboardMetricsResponse** – counts of products, sources, success rates, etc.

## Core Workflows

### 1. Aggregation (Single Product)
1. **Search** – smart search using base + targeted LLM queries, filtered by domain and relevance.
2. **Download** – fetch HTML/PDF from up to 5 selected URLs.
3. **Extraction** – LLM extracts specifications (primary + additional) and product image.
4. **Unification** – combine all extracted attributes, clean, unify synonyms, and pick highest‑confidence values.
5. **Validation & Backfilling** – compare with existing DB/Excel data; override if use case permits.
6. **Enrichment** – generate short/long description and feature list.
7. **Output** – store golden record, update product.

### 2. Batch Processing
- Upload a CSV/XLSX → validated against project taxonomy.
- Source record created → background worker processes each product row using the same pipeline.
- Progress tracked; results can be exported.

### 3. Cleansing & Standardization
- Independent operations to clean raw product data (e.g., remove duplicates, fix formatting) and standardize attributes according to business rules.

### 4. Enrichment
- Separate step that can be rerun independently to regenerate descriptions/features without re‑extracting specifications.

### 5. Human‑in‑the‑Loop (HITL)
- When validation finds conflicts or low confidence, items are flagged for manual review.
- Users can approve, reject, or override values via the HITL endpoints.

## Deployment

### Docker Compose
```yaml
services:
  api:
    build: .
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql+asyncpg://user:pass@postgres/db
      - SEARXNG_URL=http://searxng:8080
      - OPENAI_API_KEY=...
      - GEMINI_API_KEY=....
    depends_on:
      - postgres
      - searxng

  searxng:
    image: searxng/searxng
    ports:
      - "8080:8080"

  postgres:
    image: postgres:15
    environment:
      - POSTGRES_USER=...
      - POSTGRES_PASSWORD=...
```

### Environment Variables
| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string. |
| `SEARXNG_URL` | SearXNG instance URL (e.g., `http://searxng:8080`). |
| `OPENAI_API_KEY` | API key for OpenAI. |
| `GEMINI_API_KEY` | API key for Gemini API. |
| `MAX_RESULTS` | Maximum URLs selected per product (default 5). |
| `HTTP_TIMEOUT` | Timeout for downloads (seconds). |

## Error Handling & Retries

- **LLM Calls** – retry up to 3 times with exponential backoff (via `tenacity`).
- **Downloads** – retry on 5xx, timeouts, and connection errors; fail after 2 retries.
- **Search** – fallback to base query if targeted query generation fails.
- **Aggregation** – if a product fails, the batch continues; status reflects partial success.

## Glossary

| Term | Description |
|------|-------------|
| **MPN** | Manufacturer Part Number |
| **SKU** | Stock Keeping Unit |
| **Primary Attributes** | List of attributes defined by taxonomy (e.g., "Height", "Weight", "Voltage"). |
| **Golden Record** | Final unified product record after all processing. |
| **Backfilling** | Use case where web‑extracted values override existing Excel/DB values. |
| **Validation** | Use case where differences are recorded but not overridden. |
| **Chunk** | Subset of primary attributes processed in one LLM pass to stay within token limits. |
| **HITL** | Human‑in‑the‑loop – manual review and approval step. |

---

