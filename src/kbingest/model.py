from dataclasses import dataclass, field

FACT_TABLES = ["symbols", "symbol_refs", "routes", "outbound_calls", "db_access", "queues",
               "log_sites", "test_map", "findings", "convention_stats", "git_facts",
               "extraction_reports"]


@dataclass
class FileRec:
    path: str
    cls: str                    # source|config|test|docs|generated|vendored|binary|lockfile
    language: str | None
    state: str                  # summarized|skipped|failed
    reason: str | None = None
    content: str | None = None  # redacted text; None for skipped
    content_hash: str = ""
    redactions: int = 0

    @property
    def lines(self) -> list[str]:
        return (self.content or "").split("\n")


@dataclass
class Inventory:
    files: dict[str, FileRec] = field(default_factory=dict)
    manifests: dict = field(default_factory=dict)      # parsed manifest facts
    frameworks: dict[str, dict] = field(default_factory=dict)  # name -> {"evidence": [...]}


@dataclass
class Facts:
    tables: dict[str, list[dict]] = field(default_factory=lambda: {t: [] for t in FACT_TABLES})

    def add(self, table: str, **row):
        self.tables[table].append(row)

    def rows(self, table: str) -> list[dict]:
        return self.tables[table]
