# Progress

| Build step | Status |
|---|---|
| 0 Foundation (compose, Alembic, roles, append-only triggers, audit chain, CI) | done |
| 1 Inventory + git facts | done |
| 2 Extractors (Express, axios/fetch, mssql+sqlglot, SQS, logs, tests, findings, conventions) + golden fixtures | done |
| 3 Cards, modules, 12-section dossier, verify | done |
| 4 Publish + retrieval self-test + MCP server (12 tools) | done |
| 5 Cross-repo links (`edges`, host mapping, `log_source_map` for login-api/mock-api, `kbingest link`) | done (local mock repos) |
| 6 Incremental runs, change reports (`downstream_affected`), `ingest-all`, `changes`, local `trigger` webhook | done (local; no real Jenkins) |
| 7 Optional enrichment (embeddings / LLM) | deferred: contradicts the deterministic no-LLM design |

Verified 2026-11-14: 16 pytest tests pass; orders-api publishes with 80 self-test gates; gaps-api publishes with
2 reported extraction gaps; re-ingest returns `unchanged`; audit chain intact.

Note: the spec's tool table lists 12 tools (the intro text says 11); all 12 are implemented.
