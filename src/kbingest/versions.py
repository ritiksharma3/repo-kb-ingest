"""Pinned, versioned components. Stored on every snapshot (determinism)."""
from importlib import metadata

PIPELINE_VERSION = "0.2.0"
TEMPLATE_VERSION = "2"
DETECTOR_VERSION = "1"

EXTRACTOR_VERSIONS = {
    "symbols_imports": "1", "express_routes": "1", "axios_calls": "1", "mssql_db": "1",
    "sqs": "1", "logs": "1", "tests_map": "1", "findings": "1", "conventions": "1",
}


def _v(pkg: str) -> str:
    try:
        return metadata.version(pkg)
    except metadata.PackageNotFoundError:
        return "absent"


def all_versions() -> dict:
    return {
        "pipeline": PIPELINE_VERSION,
        "templates": TEMPLATE_VERSION,
        "detectors": DETECTOR_VERSION,
        "extractors": dict(sorted(EXTRACTOR_VERSIONS.items())),
        "tree_sitter": _v("tree-sitter"),
        "tree_sitter_javascript": _v("tree-sitter-javascript"),
        "sqlglot": _v("sqlglot"),
    }
