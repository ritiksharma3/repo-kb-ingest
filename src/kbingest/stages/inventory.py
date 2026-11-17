"""Stage 1: walk tree, classify files, read manifests, detect frameworks."""
from __future__ import annotations
import json
import re
import subprocess
from pathlib import Path

import yaml

from ..hashing import sha256
from ..model import FileRec, Inventory
from ..redact import redact

SKIP_DIRS = {".git": "vcs", "node_modules": "vendored", "bower_components": "vendored", "vendor": "vendored",
             "dist": "build output", "build": "build output", "coverage": "build output",
             ".next": "build output", "out": "build output", ".nyc_output": "build output"}
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json"}
BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".woff", ".woff2", ".ttf",
               ".eot", ".exe", ".dll", ".so", ".class", ".jar", ".mp4", ".bin"}
JS_EXTS = {".js", ".mjs", ".cjs", ".jsx"}
TS_EXTS = {".ts", ".tsx"}
CONFIG_NAMES = {"package.json", "dockerfile", "jenkinsfile", "makefile", "codeowners", ".gitignore",
                ".eslintrc", ".prettierrc", "tsconfig.json", ".npmrc", ".dockerignore"}
CONFIG_EXTS = {".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".env", ".properties"}
DOC_EXTS = {".md", ".txt", ".rst"}
TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|\.(test|spec)\.[cm]?[jt]sx?$")

# dependency / import name -> framework key
DEP_FRAMEWORKS = {
    "express": "express", "axios": "axios", "mssql": "mssql", "aws-sdk": "aws-sqs",
    "@aws-sdk/client-sqs": "aws-sqs", "sqs-consumer": "aws-sqs",
    "fastify": "fastify", "koa": "koa", "@hapi/hapi": "hapi", "@nestjs/core": "nestjs",
    "sequelize": "sequelize", "typeorm": "typeorm", "prisma": "prisma", "@prisma/client": "prisma",
    "knex": "knex", "mongoose": "mongoose", "pg": "pg", "mysql": "mysql", "mysql2": "mysql",
    "node-fetch": "node-fetch", "got": "got", "superagent": "superagent",
}


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def tracked_files(root: Path, commit: str) -> list[str]:
    out = _git(root, "ls-tree", "-r", "--name-only", "-z", commit)
    return sorted(p for p in out.split("\0") if p)


def read_blob(root: Path, commit: str, path: str) -> bytes:
    r = subprocess.run(["git", "-C", str(root), "show", f"{commit}:{path}"], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"cannot read {path}@{commit}")
    return r.stdout


def classify(path: str) -> tuple[str, str | None, str | None]:
    """-> (class, language, skip_reason)"""
    parts = path.split("/")
    name = parts[-1]
    low = name.lower()
    ext = Path(name).suffix.lower()
    for d in parts[:-1]:
        if d in SKIP_DIRS:
            return ("vendored" if SKIP_DIRS[d] == "vendored" else "generated"), None, SKIP_DIRS[d]
    if name in LOCKFILES:
        return "lockfile", None, "lockfile"
    if ext in BINARY_EXTS:
        return "binary", None, "binary"
    if low.endswith(".min.js") or low.endswith(".map") or low.endswith(".d.ts"):
        return "generated", None, "generated"
    if ext in JS_EXTS | TS_EXTS:
        lang = "javascript" if ext in JS_EXTS else "typescript"
        if low.startswith("ecosystem.config") or low in ("jest.config.js", "babel.config.js"):
            return "config", lang, None
        return ("test" if TEST_RE.search(path) else "source"), lang, None
    if (low in CONFIG_NAMES or low.startswith(".env") or ext in CONFIG_EXTS or path.startswith(".github/")
            or low.startswith("dockerfile")):
        return "config", ext.lstrip(".") or low, None
    if ext in DOC_EXTS or parts[0] == "docs":
        return "docs", "markdown" if ext == ".md" else "text", None
    return "config", ext.lstrip(".") or None, None


def build(root: Path, commit: str) -> Inventory:
    inv = Inventory()
    for path in tracked_files(root, commit):
        cls, lang, reason = classify(path)
        if reason:
            inv.files[path] = FileRec(path, cls, lang, "skipped", reason, None, sha256(read_blob(root, commit, path)))
            continue
        raw = read_blob(root, commit, path)
        if b"\0" in raw[:8000]:
            inv.files[path] = FileRec(path, "binary", None, "skipped", "binary", None, sha256(raw))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            inv.files[path] = FileRec(path, cls, lang, "failed", "not valid utf-8", None, sha256(raw))
            continue
        text = text.replace("\r\n", "\n")
        red, n = redact(path, text)
        inv.files[path] = FileRec(path, cls, lang, "summarized", None, red, sha256(red), n)
    _manifests(inv)
    _frameworks(inv)
    return inv


def _text(inv: Inventory, path: str) -> str | None:
    f = inv.files.get(path)
    return f.content if f and f.content is not None else None


def _fail(inv: Inventory, path: str, reason: str) -> None:
    inv.files[path].state = "failed"
    inv.files[path].reason = reason


def _manifests(inv: Inventory) -> None:
    m = inv.manifests
    pj = _text(inv, "package.json")
    if pj:
        try:
            d = json.loads(pj)
            m["package_json"] = {
                "name": d.get("name"), "version": d.get("version"), "main": d.get("main"),
                "dependencies": dict(sorted((d.get("dependencies") or {}).items())),
                "devDependencies": dict(sorted((d.get("devDependencies") or {}).items())),
                "scripts": dict(sorted((d.get("scripts") or {}).items())),
                "engines": d.get("engines") or {}, "line": 1,
            }
        except ValueError as e:
            _fail(inv, "package.json", f"invalid json: {e}")
    for path in sorted(inv.files):
        low = path.rsplit("/", 1)[-1].lower()
        t = _text(inv, path)
        if t is None:
            continue
        if low == "dockerfile" or low.startswith("dockerfile."):
            m.setdefault("dockerfiles", {})[path] = _dockerfile(t)
        elif low == "jenkinsfile":
            m.setdefault("jenkinsfiles", {})[path] = {
                "stages": [{"name": x.group(1), "line": t[:x.start()].count("\n") + 1}
                           for x in re.finditer(r"stage\s*\(\s*['\"]([^'\"]+)['\"]", t)],
                "commands": [{"cmd": x.group(1).strip(), "line": t[:x.start()].count("\n") + 1}
                             for x in re.finditer(r"\bsh\s+['\"]([^'\"\n]+)['\"]", t)]}
        elif low.startswith("ecosystem.config"):
            apps = [{"name": x.group(1), "line": t[:x.start()].count("\n") + 1}
                    for x in re.finditer(r"\bname\s*:\s*['\"]([^'\"]+)['\"]", t)]
            scripts = [x.group(1) for x in re.finditer(r"\bscript\s*:\s*['\"]([^'\"]+)['\"]", t)]
            m.setdefault("pm2", {})[path] = {"apps": apps, "scripts": scripts}
        elif path.startswith(".github/workflows/") and low.endswith((".yml", ".yaml")):
            try:
                y = yaml.safe_load(t) or {}
                jobs = {}
                for jn, jv in sorted((y.get("jobs") or {}).items()):
                    jobs[jn] = [s.get("run") for s in (jv.get("steps") or []) if isinstance(s, dict) and s.get("run")]
                m.setdefault("ci", {})[path] = {"jobs": jobs}
            except Exception as e:
                _fail(inv, path, f"invalid yaml: {e}")
        elif low in ("openapi.json", "openapi.yaml", "openapi.yml", "swagger.json"):
            try:
                y = json.loads(t) if low.endswith(".json") else yaml.safe_load(t)
                ops = sorted(f"{mt.upper()} {p}" for p, v in (y.get("paths") or {}).items()
                             for mt in v if mt.lower() in ("get", "post", "put", "patch", "delete"))
                m.setdefault("openapi", {})[path] = {"operations": ops}
            except Exception as e:
                _fail(inv, path, f"invalid openapi: {e}")
        elif low == "makefile":
            m.setdefault("makefiles", {})[path] = {
                "targets": sorted(set(re.findall(r"^([A-Za-z0-9_.-]+):(?!=)", t, re.M)))}
        elif low == "codeowners":
            m["codeowners_path"] = path
        elif low == "readme.md" and "/" not in path:
            m["readme"] = _readme(t)
    keys = {}
    for path in sorted(inv.files):
        low = path.rsplit("/", 1)[-1].lower()
        t = _text(inv, path)
        if t is not None and low.startswith(".env"):
            keys[path] = sorted(set(re.findall(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=", t, re.M)))
    if keys:
        m["env_keys"] = keys


def _dockerfile(t: str) -> dict:
    d = {"from": [], "expose": [], "cmd": None, "entrypoint": None, "env_keys": [], "args": []}
    for i, line in enumerate(t.split("\n"), 1):
        s = line.strip()
        u = s.upper()
        if u.startswith("FROM "):
            d["from"].append({"image": s.split()[1], "line": i})
        elif u.startswith("EXPOSE "):
            d["expose"] += s.split()[1:]
        elif u.startswith("CMD "):
            d["cmd"] = {"value": s[4:].strip(), "line": i}
        elif u.startswith("ENTRYPOINT "):
            d["entrypoint"] = {"value": s[11:].strip(), "line": i}
        elif u.startswith("ENV "):
            d["env_keys"].append(s.split()[1].split("=")[0])
        elif u.startswith("ARG "):
            d["args"].append(s.split()[1].split("=")[0])
    d["env_keys"] = sorted(set(d["env_keys"]))
    d["args"] = sorted(set(d["args"]))
    return d


def _readme(t: str) -> dict:
    para, start = [], None
    for i, line in enumerate(t.split("\n"), 1):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("![") or s.startswith("[!["):
            if para:
                break
            continue
        if start is None:
            start = i
        para.append(s)
    return {"first_paragraph": " ".join(para), "line": start or 1}


SPEC_RE = re.compile(r"""(?:require\(\s*|from\s+)['"]([^'"]+)['"]""")


def _frameworks(inv: Inventory) -> None:
    pj = inv.manifests.get("package_json") or {}
    deps = {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}
    for dep in sorted(deps):
        fw = DEP_FRAMEWORKS.get(dep)
        if fw:
            inv.frameworks.setdefault(fw, {"evidence": []})["evidence"].append(f"package.json:1 dependency {dep}")
    for path in sorted(inv.files):
        f = inv.files[path]
        if f.state != "summarized" or f.language not in ("javascript", "typescript") or f.cls == "config":
            continue
        for i, line in enumerate(f.lines, 1):
            for spec in SPEC_RE.findall(line):
                fw = DEP_FRAMEWORKS.get(spec)
                if fw:
                    ev = inv.frameworks.setdefault(fw, {"evidence": []})["evidence"]
                    item = f"{path}:{i} imports {spec}"
                    if len(ev) < 5 and item not in ev:
                        ev.append(item)
    for fw in inv.frameworks.values():
        fw["evidence"] = sorted(fw["evidence"])
