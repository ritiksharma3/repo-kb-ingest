# repo-kb-ingest: a guide to the files

For a reader who wants to know what each file does. For setup and usage commands, see `README.md`. For the build status of each step, see `PROGRESS.md`.

## What the project does

It reads a code repository and builds a **knowledge base** about it, with **no LLM involved**. The same input always gives the same output, and every fact carries `file:line` evidence.

```
repo (git) -> inventory -> extractors -> git facts -> cards + dossier -> verify
           -> publish (Postgres, append-only) -> self-test -> MCP tools
```

The KB is stored in Postgres 16 and served to AI agents through an MCP server with 12 read-only tools, such as `find_route` and `get_dependencies`. Facts in the database are never updated or deleted. A new snapshot is added on each ingest.

In this local setup there are no real remote repos. The repos in `tests/fixtures/` are mock repos built on the fly.

## Top-level files

| File | Purpose |
|---|---|
| `pyproject.toml` | Package metadata, dependencies (tree-sitter, sqlglot, psycopg, alembic, mcp, typer, jinja2) and the `kbingest` CLI entry point. |
| `docker-compose.yml` | Starts Postgres 16 on host port 54329 for local use and tests. |
| `.env.example` | Database URLs and role passwords. Copy it to `.env`. |
| `alembic.ini` and `alembic/` | Database migrations. |
| `config/mappings.yaml` | Checked-in mapping: repo to business suite, git branch to environment, internal host to repo. The linker uses the host mapping. |
| `.github/workflows/ci.yml` | CI: Postgres service, `golden-check` and `pytest`. |
| `README.md` / `PROGRESS.md` | Usage and known gaps / build-step status. |

## Database (`alembic/versions/`)

| Migration | Purpose |
|---|---|
| `0001_schema.py` | All tables: repos and snapshots, fact tables, `edges` (cross-repo links), `log_source_map`, `change_log`, audit. |
| `0002_roles_triggers.py` | Roles (`kb_owner`, `kb_writer`, `kb_reader`). Triggers that block UPDATE and DELETE. A hash-chained audit log. A `current_snapshots` view. |
| `0003_manifests.py` | Stores parsed manifests so dossiers can be re-rendered from the database alone. |

## Source (`src/kbingest/`)

### Core pipeline
| File | Purpose |
|---|---|
| `cli.py` | Typer commands: `ingest`, `golden-update`, `golden-check`, `audit-verify`, `serve-mcp`. Any extra commands from the current round of work are listed in `README.md`. |
| `pipeline.py` | Runs the analysis stages in order: inventory, extract, git facts. |
| `publish.py` | Writes a snapshot in **one transaction**, writes `change_log` and `log_source_map`, runs the self-test, and commits only if the self-test passes. It returns `unchanged` if the commit has not changed. |
| `model.py` | Dataclasses for facts and inventory. `FACT_TABLES` lists the fact tables. |
| `bundle.py` | The normalised fact bundle. It is built the same way from memory and from database rows, so the two can be compared byte for byte. |
| `db.py` | Postgres connections with the right role. |
| `hashing.py` | SHA-256 and canonical JSON, used for determinism and the audit chain. |
| `redact.py` | Removes secrets before storage (gitleaks if installed, otherwise built-in patterns). The line count is preserved, so line references stay valid. |
| `versions.py` | Pinned pipeline, template and detector versions. They are stored on every snapshot. |

### Stages (`stages/`)
| File | Purpose |
|---|---|
| `inventory.py` | Walks the tree, classifies files, reads manifests (package.json, Jenkinsfile, pm2, Dockerfile) and detects frameworks. |
| `extract.py` | Runs all extractors and builds extraction reports. These record the gaps, such as an unsupported framework or an unresolved URL. |
| `gitfacts.py` | Git history facts: churn, authors, fix-style commits, CODEOWNERS. |
| `cards.py` | Rule-based file cards and module summaries, with no generated prose. |
| `verify.py` | Deterministic checks. Any failure blocks publishing. |
| `link.py` | Cross-repo linking. It matches one repo's outbound calls and queue sends to other repos' routes and queue consumers, and produces `edges`. |

### Extractors (`extractors/`, tree-sitter based)
| File | Extracts |
|---|---|
| `base.py` | Shared parse helpers and per-file context. |
| `express_routes.py` | Express routes, router mounts and prefixes. |
| `axios_calls.py` | Outbound HTTP calls (axios and fetch). |
| `mssql_db.py` | SQL Server access. SQL is parsed with sqlglot to get tables and operations. |
| `sqs.py` | AWS SQS sends and receives. |
| `logs.py` | Log and error strings, so a log line can be traced back to code. |
| `tests_map.py` | Which source files each test covers. |
| `symbols_imports.py` | Symbols, imports, exports and caller/callee links. |
| `findings.py` | Rule-based observations, such as an HTTP call with no timeout. |
| `conventions.py` | Code conventions, with counts and outliers. |

### Dossier (`dossier/`)
| File | Purpose |
|---|---|
| `sections.py` | The 12 dossier sections, as pure functions of the bundle. |
| `render.py` | Validates section JSON against the schema and renders markdown. |
| `section.schema.json` | JSON schema for a section. |
| `templates/section.md.j2` | Markdown template. |

### Retrieval and serving
| File | Purpose |
|---|---|
| `tools.py` | The 12 retrieval functions, such as `find_route`, `search_code`, `get_dependencies` and `get_recent_changes`. They read the database as the read-only role. |
| `selftest.py` | The publish gate. It calls the real tools on the new snapshot, and a failure blocks the publish. |
| `mcp_server/server.py` | Exposes `tools.py` as MCP tools over the read-only role. |

### Test support
| File | Purpose |
|---|---|
| `fixtures.py` | Builds deterministic git repos from fixture folders. |
| `golden.py` | Compares extractor output with the stored expected JSON. |

## Tests (`tests/`)

| Path | Purpose |
|---|---|
| `fixtures/orders-api` | Main mock service: Express routes, axios calls, SQS, mssql, a secret to redact, a Jenkinsfile. |
| `fixtures/login-api` | Mock login and auth service. Other repos call it. |
| `fixtures/mock-api` | Mock gateway service that calls `login-api` and `orders-api`. |
| `fixtures/gaps-api` | Deliberately awkward repo (unsupported framework, dynamic URL, syntax error, TypeScript) to prove gaps are reported rather than hidden. |
| `golden/*.json` | Expected extractor output for each fixture. Regenerate with `kbingest golden-update` and review the diff. |
| `conftest.py` | Creates a throwaway database for each test run and runs the migrations. |
| `test_extractors.py` | Extractors against the golden files. |
| `test_db_and_publish.py` | Publish, append-only enforcement, audit chain, self-test, re-ingest and MCP tools. |

## Design rules to know

1. **Deterministic**: no LLM, and the same commit gives the same snapshot.
2. **Append-only**: database privileges and triggers block UPDATE and DELETE.
3. **Evidence everywhere**: each fact carries `file:line` and an origin.
4. **Gaps are visible**: unsupported or unresolved items are recorded, not dropped.
5. **Publish gate**: a snapshot is committed only if the retrieval self-test passes.
6. **Changing an extractor, template or detector** means updating the goldens and bumping the version in `versions.py`.
