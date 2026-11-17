# repo-kb-ingest

Phase 1 of the repo knowledge base: a **deterministic, no-LLM** pipeline that analyzes one git repo at one commit and
stores evidence-backed facts, file cards, module summaries and a 12-section dossier in append-only Postgres 16. A
minimal MCP server exposes the 12 Phase 2 retrieval tools over that data, and the same tools are the publish gate.

Scope built so far: build-order steps 0-6 (foundation, inventory + git facts, Node/Express/axios/mssql/SQS extractors,
cards + dossier + verify, publish + retrieval self-test, cross-repo links, incremental runs, change reports, a local
webhook trigger) against synthetic fixtures (`orders-api`, `login-api`, `mock-api`, `gaps-api`). Not built (deferred
by design): step 7 embeddings / LLM enrichment; a real Jenkins integration.

## Run
```bash
docker compose up -d
pip install -e ".[dev]"
KB_OWNER_URL=postgresql://kb_owner:kb_owner_dev@localhost:54329/kb alembic upgrade head
kbingest ingest /path/to/repo [--ref HEAD] [--name repo-name]   # verify -> write -> self-test -> publish
kbingest audit-verify        # audit hash chain; export the printed hash outside the DB on a schedule
kbingest serve-mcp           # stdio MCP server, read-only role
kbingest ingest-all tests/fixtures   # ingest every repo in a folder (non-git folders become temp git repos), then link
kbingest link                # re-resolve cross-repo edges for all known repos
kbingest changes <repo>      # change report of the current snapshot (+ downstream affected repos)
kbingest trigger [--port 8765] [--repos-dir D]   # local webhook, 127.0.0.1 only: POST /trigger {"repo","sha"}
kbingest golden-check | golden-update
pytest                       # needs the compose DB (creates a throwaway database per session)
```
Config: DB URLs in `.env.example`; suite, branch->environment and internal host -> repo mapping (`hosts:`) in `config/mappings.yaml`.

## MySQL 8.x backend (alternative to Postgres, no Docker)
```bash
pip install -e ".[dev,mysql]"
# .env: KB_DB=mysql plus the mysql:// URLs from .env.example (WAMP/XAMPP default: root, empty password, 127.0.0.1:3306)
kbingest init-db --check     # read-only preflight: version, max_allowed_packet, binlog settings
kbingest init-db             # creates database `kb`, schema, append-only triggers, view, kb_audit_verify(), kb_writer / kb_reader
KB_DB=mysql kbingest ingest /path/to/repo
python scripts/try_repo.py <repo_path> [<repo_path> ...]   # ingest + readable report (routes, dossier, gaps, sample tool calls, cross-repo edges)
KB_DB=mysql pytest           # throwaway kbtest_* databases
```
Postgres stays the default (`KB_DB` unset). Differences on MySQL: append-only is enforced by BEFORE UPDATE/DELETE triggers
(SQLSTATE 45000, error 1644) plus privileges; TRUNCATE by the owner account cannot be blocked (kb_writer/kb_reader lack DROP).
`init-db` needs an owner account with GRANT OPTION to create kb_writer/kb_reader; without it, it warns and you point
`KB_WRITER_URL`/`KB_READER_URL` at the owner (triggers still apply, the SELECT-only/INSERT-only split does not).
The retrieval self-test runs on the writer connection (no transaction-scoped role switch). Text search (`search_code`,
fuzzy `find_code_for_log`) runs in Python (pg_trgm-style trigram similarity), so ranking is close to but not identical with
Postgres. Audit chain concurrent writers are not serialized. Schema lives in `src/kbingest/mysql_schema.py`, not Alembic.

## Cross-repo links, incremental runs, change reports (build steps 5-6)
- `stages/link.py` matches a repo's verified outbound HTTP calls (host via `hosts:` mapping, path with `:id`/`{}` params
  normalised, method) to other repos' routes, and queue sends to other repos' receivers (same queue name). Results are
  `edges` rows (`http` or `queue`, `origin=derived`, evidence = source and destination `file:line`). External hosts,
  unknown queues and `unverified` URLs produce no edge. Edges belong to the source repo's snapshot and are part of its
  output digest and dossier (`dependencies` section), so they render identically from memory and DB rows.
- Because edges depend on the other repos, every successful ingest re-ingests the other known repos (same commit); a repo whose
  edge set did not change returns `unchanged`, otherwise it gets a new snapshot. Ingest order therefore does not matter.
  `get_dependencies` returns outgoing `edges` plus `dependents` (other repos' current edges into this repo).
- Self-test gate `edge-source` / `edge-target` / `edges-tool-matches-table` checks every edge against the stored call/send
  and the destination repo's current routes/consumers.
- Incremental mode (default; `--full` disables): the previous snapshot's file content hashes decide what is unchanged;
  unchanged files reuse their tree-sitter parse + import/symbol scan from an in-process cache (so the saving applies inside
  a long-lived process: `trigger`, `ingest-all`, re-links), while cross-file extractors, cards and dossier are always
  recomputed so the output is byte-identical to a full run (tested). Falls back to a full run when pipeline/extractor/
  template versions differ from the previous snapshot. The run report carries `incremental` (changed/removed/unchanged files, reused parses).
- `change_log.downstream_affected` lists dependent repos whose edges target a changed route (`GET /x`) or queue consumer
  (`queue:name`); `kbingest changes` and `get_recent_changes` show it.
- Migration `0004` drops the `(repo, branch, commit, digest)` uniqueness so a repo can return to an earlier output at the same
  commit when edges flip; "unchanged" now compares with the latest snapshot only. The tables remain append-only.

## Pipeline
`inventory -> extract -> git facts -> cards/modules -> dossier -> verify -> publish (+ self-test)`

- Everything stored is `source` (parser fact), `derived` (rule) or `document` (quoted text), with `file:line` evidence.
- Secrets are redacted before storage (gitleaks if installed, built-in patterns otherwise); `.env*` values are never kept.
- Verify (blocks publishing): cited file:line exists and matches its line hash; dossier JSON schema; re-render identical;
  dossier equals extractor tables; every file has a card and every directory a summary; framework/extractor coverage;
  coverage ledger terminal states; extractor golden fixtures.
- Publish writes the snapshot and runs the self-test **inside the same transaction as `kb_reader`**; a failure rolls
  everything back, so the previous snapshot stays current. The run is recorded in `ingest_runs` and `audit_log`.
- Determinism: pinned versions stored per snapshot; all rows sorted; output digest excludes timestamps and run ids.
  Re-ingesting the same commit returns `unchanged`.

## Database
Alembic migrations `0001` (schema), `0002` (roles, append-only triggers, audit chain, `current_snapshots` view),
`0003` (manifests), `0004` (relink: non-unique snapshot digests, edges index). Roles: `kb_owner` (migrations), `kb_writer` (INSERT/SELECT), `kb_reader` (SELECT). UPDATE/DELETE/
TRUNCATE are rejected by privileges and by triggers (even for the owner). "Current" is a view over the latest snapshot
per branch.

## Known gaps (also surfaced per snapshot in the "Unknowns and gaps" section)
- Only JavaScript is parsed (`tree-sitter-javascript`); `.ts/.tsx` files are recorded as `failed: no parser grammar`.
- Edges are matched by host mapping + route/queue name only; calls through dynamic URLs, shared client wrappers or non-mapped hosts stay unlinked. The trigger is a local stand-in for Jenkins (no auth, loopback only).
- No scheduler-registration extractor, so the Entry points section does not list scheduled jobs.
- Frameworks without an extractor (fastify, koa, nestjs, sequelize, ...) are reported as extraction gaps.
- Dynamic URLs / string-built SQL are stored as `unverified` with the expression text.
- Provenance: every fact row carries `snapshot_id` and `run_id`; commit SHA, pipeline and extractor versions live on the snapshot.
- GxP validation documentation (IQ/OQ/PQ) is out of scope; the migrations and tests are the starting evidence.
