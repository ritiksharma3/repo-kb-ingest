import hashlib
import json


def sha256(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def line_hash(line_text: str) -> str:
    return sha256(line_text.strip())[:16]


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest_rows(tables: dict[str, list[dict]]) -> str:
    """Hash over all rows of all tables. Rows sorted canonically; no timestamps/run ids."""
    h = hashlib.sha256()
    for name in sorted(tables):
        rows = sorted(canon(r) for r in tables[name])
        h.update(name.encode())
        for r in rows:
            h.update(b"\x00" + r.encode())
    return h.hexdigest()
