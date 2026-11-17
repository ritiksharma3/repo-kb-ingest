"""The normalized fact bundle: the single input to cards and dossier rendering.
Built identically from in-memory facts and from DB rows so re-rendering can be compared byte for byte."""
from __future__ import annotations
import json

from .hashing import canon
from .model import FACT_TABLES

PROVENANCE = {"snapshot_id", "run_id"}


def _norm(x):
    return json.loads(canon(x))


def make(snapshot: dict, mapping: dict, files: list[dict], manifests: list[dict], tables: dict[str, list[dict]],
         edges: list[dict] | None = None) -> dict:
    t = {}
    for name in FACT_TABLES:
        rows = [{k: v for k, v in r.items() if k not in PROVENANCE} for r in tables.get(name, [])]
        t[name] = sorted((_norm(r) for r in rows), key=canon)
    return {
        "snapshot": _norm(snapshot),
        "mapping": _norm(mapping),
        "files": sorted((_norm(f) for f in files), key=lambda f: f["path"]),
        "manifests": sorted((_norm({k: v for k, v in m.items() if k not in PROVENANCE}) for m in manifests),
                            key=lambda m: (m["kind"], m["path"])),
        "tables": t,
        "edges": sorted((_norm({k: v for k, v in e.items() if k not in PROVENANCE}) for e in (edges or [])), key=canon),
    }


def manifest(bundle: dict, kind: str) -> dict:
    """path -> data for one manifest kind."""
    return {m["path"]: m["data"] for m in bundle["manifests"] if m["kind"] == kind}


def manifest_rows_from_inventory(inv, mapping: dict, snapshot: dict) -> list[dict]:
    rows = []
    m = inv.manifests
    for kind, key in (("dockerfile", "dockerfiles"), ("jenkinsfile", "jenkinsfiles"), ("pm2", "pm2"), ("ci", "ci"),
                      ("openapi", "openapi"), ("makefile", "makefiles"), ("env_keys", "env_keys")):
        for path, data in sorted((m.get(key) or {}).items()):
            rows.append({"kind": kind, "path": path, "data": data if isinstance(data, dict) else {"keys": data},
                         "origin": "source"})
    if "package_json" in m:
        rows.append({"kind": "package_json", "path": "package.json", "data": m["package_json"], "origin": "source"})
    if "readme" in m:
        rows.append({"kind": "readme", "path": "README.md", "data": m["readme"], "origin": "document"})
    rows.append({"kind": "frameworks", "path": "", "data": inv.frameworks, "origin": "derived"})
    rows.append({"kind": "mapping", "path": "", "data": mapping, "origin": "document"})
    return rows
