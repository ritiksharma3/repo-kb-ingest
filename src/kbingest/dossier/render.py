"""Dossier rendering: JSON validated against a schema, markdown from Jinja templates (pure functions)."""
from __future__ import annotations
import json
from pathlib import Path

import jinja2
from jsonschema import Draft202012Validator

from ..hashing import canon
from . import sections as S

_DIR = Path(__file__).parent
_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(str(_DIR / "templates")), autoescape=False,
                          keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True,
                          undefined=jinja2.StrictUndefined)
_ENV.filters["j"] = lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False)
_SCHEMA = json.loads((_DIR / "section.schema.json").read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(_SCHEMA)


def validate(section: dict) -> list[str]:
    return sorted(f"{'/'.join(map(str, e.path))}: {e.message}" for e in _VALIDATOR.iter_errors(section))


def markdown(section: dict) -> str:
    return _ENV.get_template("section.md.j2").render(s=section)


def render_all(bundle: dict, coverage: dict) -> dict:
    secs = S.build_all(bundle)
    return {"sections": secs, "markdown": {k: markdown(v) for k, v in secs.items()},
            "card": S.repo_card(bundle, secs, coverage),
            "section_index": [{"key": k, "title": S.TITLES[k], "status": secs[k]["status"]} for k in S.SECTION_KEYS]}


def fingerprint(rendered: dict) -> str:
    from ..hashing import sha256
    return sha256(canon({k: rendered[k] for k in ("sections", "markdown", "card", "section_index")}))
