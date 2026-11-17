"""Extractor golden fixtures: fixture repo -> expected fact tables (JSON). Run in CI and in the verify stage."""
from __future__ import annotations
import json
import tempfile
from pathlib import Path

from .fixtures import make_repo
from .hashing import canon
from .pipeline import analyze

SKIP_TABLES = {"git_facts"}


def snapshot(root: Path, commit: str) -> dict:
    inv, facts, _ = analyze(root, commit)
    out = {t: sorted(rows, key=canon) for t, rows in facts.tables.items() if t not in SKIP_TABLES}
    out["frameworks"] = {k: v["evidence"] for k, v in sorted(inv.frameworks.items())}
    out["file_states"] = {p: [f.cls, f.state, f.reason] for p, f in sorted(inv.files.items())}
    return json.loads(canon(out))


def fixture_names(fixtures_dir: Path) -> list[str]:
    return sorted(d.name for d in fixtures_dir.iterdir() if d.is_dir() and not d.name.startswith("_"))


def check(fixtures_dir: Path, golden_dir: Path) -> list[str]:
    """Returns failure descriptions (empty = all goldens match)."""
    failures = []
    for name in fixture_names(fixtures_dir):
        g = golden_dir / f"{name}.json"
        with tempfile.TemporaryDirectory() as td:
            commit = make_repo(fixtures_dir / name, Path(td) / name)
            got = snapshot(Path(td) / name, commit)
        if not g.exists():
            failures.append(f"{name}: golden file missing")
        elif json.loads(g.read_text(encoding="utf-8")) != got:
            failures.append(f"{name}: output differs from {g.name}")
    return failures


def update(fixtures_dir: Path, golden_dir: Path) -> None:
    golden_dir.mkdir(parents=True, exist_ok=True)
    for name in fixture_names(fixtures_dir):
        with tempfile.TemporaryDirectory() as td:
            commit = make_repo(fixtures_dir / name, Path(td) / name)
            got = snapshot(Path(td) / name, commit)
        (golden_dir / f"{name}.json").write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                                                 encoding="utf-8")
