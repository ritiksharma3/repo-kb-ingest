"""Core schema: identity, knowledge, facts, links, history, operations."""
from alembic import op

revision = "0001"
down_revision = None

# Provenance columns carried by every versioned fact row.
P = "snapshot_id bigint NOT NULL REFERENCES repo_snapshots(id), run_id uuid NOT NULL"

SQL = f"""
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- identifier tokenization: camelCase / snake_case / paths -> lowercase words
CREATE FUNCTION kb_tokenize(t text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT lower(regexp_replace(regexp_replace(coalesce(t,''), '([a-z0-9])([A-Z])', '\\1 \\2', 'g'),
                              '[_./\\\\-]+', ' ', 'g'))
$$;

-- ---------------- Identity ----------------
CREATE TABLE repos (
  id bigserial PRIMARY KEY,
  name text NOT NULL UNIQUE
);
CREATE TABLE repo_snapshots (
  id bigserial PRIMARY KEY,
  repo_id bigint NOT NULL REFERENCES repos(id),
  branch text NOT NULL,
  commit_sha text NOT NULL,
  run_id uuid NOT NULL,
  pipeline_version text NOT NULL,
  versions jsonb NOT NULL,
  output_digest text NOT NULL,
  coverage jsonb NOT NULL,
  status text NOT NULL CHECK (status = 'published'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (repo_id, branch, commit_sha, output_digest)
);
CREATE TABLE files (
  {P},
  path text NOT NULL,
  content text,
  content_hash text NOT NULL,
  class text NOT NULL,
  language text,
  coverage_state text NOT NULL CHECK (coverage_state IN ('summarized','skipped','failed')),
  state_reason text,
  line_hashes jsonb,
  PRIMARY KEY (snapshot_id, path)
);

-- ---------------- Facts ----------------
CREATE TABLE symbols (
  {P}, file_path text NOT NULL, name text NOT NULL, kind text NOT NULL,
  line_start int NOT NULL, line_end int NOT NULL, exported boolean NOT NULL, parent text,
  line_hash text NOT NULL, origin text NOT NULL, extractor text NOT NULL,
  PRIMARY KEY (snapshot_id, file_path, name, line_start)
);
CREATE TABLE symbol_refs (
  {P}, src_file text NOT NULL, src_symbol text NOT NULL, src_line int NOT NULL,
  dst_file text, dst_symbol text NOT NULL, kind text NOT NULL, status text NOT NULL,
  line_hash text NOT NULL, origin text NOT NULL, extractor text NOT NULL,
  PRIMARY KEY (snapshot_id, src_file, src_line, dst_symbol, kind)
);
CREATE TABLE routes (
  {P}, method text NOT NULL, path text NOT NULL, route_file text NOT NULL, route_line int NOT NULL,
  handler_file text, handler_symbol text, handler_line int, middleware jsonb NOT NULL DEFAULT '[]',
  framework text NOT NULL, status text NOT NULL, expression text, line_hash text NOT NULL,
  origin text NOT NULL, extractor text NOT NULL,
  PRIMARY KEY (snapshot_id, method, path, route_file, route_line)
);
CREATE TABLE outbound_calls (
  {P}, file_path text NOT NULL, line int NOT NULL, client text NOT NULL, method text,
  url text, expression text NOT NULL, status text NOT NULL, has_timeout boolean NOT NULL,
  enclosing_symbol text, line_hash text NOT NULL, origin text NOT NULL, extractor text NOT NULL,
  PRIMARY KEY (snapshot_id, file_path, line, expression)
);
CREATE TABLE db_access (
  {P}, file_path text NOT NULL, line int NOT NULL, library text NOT NULL, operation text NOT NULL,
  schema_name text NOT NULL DEFAULT '', table_name text NOT NULL DEFAULT '', procedure text NOT NULL DEFAULT '',
  status text NOT NULL, expression text NOT NULL,
  enclosing_symbol text, line_hash text NOT NULL, origin text NOT NULL, extractor text NOT NULL,
  PRIMARY KEY (snapshot_id, file_path, line, operation, table_name, procedure, expression)
);
CREATE TABLE queues (
  {P}, file_path text NOT NULL, line int NOT NULL, direction text NOT NULL, queue_name text,
  library text NOT NULL, status text NOT NULL, expression text NOT NULL, enclosing_symbol text,
  line_hash text NOT NULL, origin text NOT NULL, extractor text NOT NULL,
  PRIMARY KEY (snapshot_id, file_path, line, direction)
);
CREATE TABLE log_sites (
  {P}, file_path text NOT NULL, line int NOT NULL, kind text NOT NULL, level text,
  template text NOT NULL, enclosing_symbol text, line_hash text NOT NULL,
  origin text NOT NULL, extractor text NOT NULL,
  PRIMARY KEY (snapshot_id, file_path, line, template)
);
CREATE TABLE test_map (
  {P}, test_file text NOT NULL, source_file text NOT NULL, how text NOT NULL,
  origin text NOT NULL, extractor text NOT NULL,
  PRIMARY KEY (snapshot_id, test_file, source_file)
);
CREATE TABLE findings (
  {P}, file_path text NOT NULL, line int NOT NULL, rule_id text NOT NULL, detail text NOT NULL,
  enclosing_symbol text, line_hash text NOT NULL, origin text NOT NULL, extractor text NOT NULL,
  PRIMARY KEY (snapshot_id, file_path, line, rule_id)
);
CREATE TABLE convention_stats (
  {P}, pattern text NOT NULL, majority text, counts jsonb NOT NULL,
  representative_file text, representative_line int, outliers jsonb NOT NULL,
  origin text NOT NULL, extractor text NOT NULL,
  PRIMARY KEY (snapshot_id, pattern)
);
CREATE TABLE git_facts (
  {P}, kind text NOT NULL CHECK (kind IN ('file','repo')), file_path text NOT NULL, data jsonb NOT NULL,
  origin text NOT NULL,
  PRIMARY KEY (snapshot_id, kind, file_path)
);
CREATE TABLE extraction_reports (
  {P}, kind text NOT NULL, subject text NOT NULL, detail text NOT NULL,
  file_path text NOT NULL DEFAULT '', line int NOT NULL DEFAULT 0,
  PRIMARY KEY (snapshot_id, kind, subject, file_path, line)
);

-- ---------------- Knowledge ----------------
CREATE TABLE file_cards (
  {P}, file_path text NOT NULL, card jsonb NOT NULL, status text NOT NULL, search_text text NOT NULL,
  PRIMARY KEY (snapshot_id, file_path)
);
CREATE TABLE module_summaries (
  {P}, module_path text NOT NULL, summary jsonb NOT NULL,
  PRIMARY KEY (snapshot_id, module_path)
);
CREATE TABLE repo_dossiers (
  {P}, card jsonb NOT NULL, section_index jsonb NOT NULL, status text NOT NULL,
  PRIMARY KEY (snapshot_id)
);
CREATE TABLE dossier_sections (
  {P}, section_key text NOT NULL, content jsonb NOT NULL, markdown text NOT NULL,
  evidence jsonb NOT NULL, origin text NOT NULL, status text NOT NULL,
  PRIMARY KEY (snapshot_id, section_key)
);

-- ---------------- Links (populated in later build steps) ----------------
CREATE TABLE edges (
  {P}, src_repo text NOT NULL, src_ref text NOT NULL, dst_repo text NOT NULL, dst_ref text NOT NULL,
  kind text NOT NULL, evidence jsonb NOT NULL, origin text NOT NULL,
  PRIMARY KEY (snapshot_id, src_ref, dst_repo, dst_ref, kind)
);
CREATE TABLE log_source_map (
  {P}, log_source text NOT NULL, source_kind text NOT NULL, repo_name text NOT NULL,
  environment text, branch text, origin text NOT NULL,
  PRIMARY KEY (snapshot_id, log_source, source_kind)
);

-- ---------------- History ----------------
CREATE TABLE change_log (
  {P}, prev_snapshot_id bigint, section text NOT NULL, change jsonb NOT NULL,
  PRIMARY KEY (snapshot_id, section)
);

-- ---------------- Operations ----------------
CREATE TABLE ingest_runs (
  run_id uuid PRIMARY KEY,
  repo_name text NOT NULL, branch text, commit_sha text,
  status text NOT NULL CHECK (status IN ('published','blocked','failed')),
  report jsonb NOT NULL,
  started_at timestamptz NOT NULL, finished_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE audit_log (
  id bigserial PRIMARY KEY,
  event_type text NOT NULL,
  actor text NOT NULL DEFAULT current_user,
  payload jsonb NOT NULL,
  prev_hash text NOT NULL,
  row_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- search indexes
CREATE INDEX file_cards_tsv ON file_cards USING gin (to_tsvector('simple', kb_tokenize(search_text)));
CREATE INDEX file_cards_trgm ON file_cards USING gin (kb_tokenize(search_text) gin_trgm_ops);
CREATE INDEX symbols_tsv ON symbols USING gin (to_tsvector('simple', kb_tokenize(name)));
CREATE INDEX symbols_trgm ON symbols USING gin (kb_tokenize(name) gin_trgm_ops);
CREATE INDEX log_sites_trgm ON log_sites USING gin (template gin_trgm_ops);
CREATE INDEX routes_lookup ON routes (snapshot_id, method, path);
CREATE INDEX symbol_refs_dst ON symbol_refs (snapshot_id, dst_file, dst_symbol);
"""


def upgrade():
    op.execute(SQL)


def downgrade():
    raise RuntimeError("append-only knowledge base: downgrade is not supported")
