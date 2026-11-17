"""Stages 1-3 as one function: inventory -> extract -> git facts."""
from __future__ import annotations
from pathlib import Path

from .model import Facts, Inventory
from .stages import extract, gitfacts, inventory


def analyze(root: Path, commit: str, cache: extract.ParseCache | None = None, reusable: dict[str, str] | None = None):
    """`reusable` = path -> content hash of the previous snapshot (incremental mode); files whose hash is unchanged take
    their parse results from `cache` when it holds them. Output is identical to a full run by construction."""
    inv = inventory.build(root, commit)
    facts, ctxs = extract.run(inv, cache, reusable)
    gitfacts.run(root, commit, inv, facts)
    return inv, facts, ctxs
