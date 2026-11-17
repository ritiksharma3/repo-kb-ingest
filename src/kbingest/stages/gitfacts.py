"""Stage 3: git history facts - churn, authors, fix-style commits, refs, CODEOWNERS."""
from __future__ import annotations
import re
import subprocess
from pathlib import Path

from ..model import Facts, Inventory

FIX_RE = re.compile(r"\b(fix|fixes|fixed|bug|bugfix|hotfix|patch|revert)\b", re.I)
REF_RE = re.compile(r"(?:#\d+|\b[A-Z][A-Z0-9]+-\d+\b)")


def current_branch(root: Path, commit: str) -> str:
    r = subprocess.run(["git", "-C", str(root), "branch", "--contains", commit, "--format=%(refname:short)"],
                       capture_output=True, text=True)
    names = sorted(b.strip() for b in r.stdout.splitlines() if b.strip() and "HEAD detached" not in b)
    for pref in ("main", "master"):
        if pref in names:
            return pref
    return names[0] if names else "detached"


def resolve_commit(root: Path, ref: str) -> str:
    r = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cannot resolve {ref!r}: {r.stderr.strip()}")
    return r.stdout.strip()


def parse_codeowners(text: str) -> list[dict]:
    rules = []
    for i, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        rules.append({"pattern": parts[0], "owners": parts[1:], "line": i})
    return rules


def run(root: Path, commit: str, inv: Inventory, facts: Facts) -> None:
    fmt = "%x1e%H%x1f%an%x1f%aI%x1f%s"
    out = subprocess.run(["git", "-C", str(root), "log", commit, f"--format={fmt}", "--name-only"],
                         capture_output=True, text=True, encoding="utf-8").stdout
    per: dict[str, dict] = {}
    commits = []
    for chunk in out.split("\x1e"):
        if not chunk.strip():
            continue
        head, *files = chunk.split("\n")
        sha, author, date, subject = head.split("\x1f", 3)
        files = [f for f in files if f.strip()]
        commits.append({"sha": sha[:12], "date": date, "subject": subject, "files": sorted(files)})
        is_fix = bool(FIX_RE.search(subject))
        refs = REF_RE.findall(subject)
        for f in files:
            d = per.setdefault(f, {"commits": 0, "authors": {}, "last_commit": sha[:12], "last_change": date,
                                   "fix_commits": 0, "refs": set()})
            d["commits"] += 1
            d["authors"][author] = d["authors"].get(author, 0) + 1
            d["fix_commits"] += int(is_fix)
            d["refs"].update(refs)
    for path in sorted(inv.files):
        if inv.files[path].state == "skipped":
            continue
        d = per.get(path)
        if d is None:
            continue
        facts.add("git_facts", kind="file", file_path=path, origin="source", data={
            "commits": d["commits"], "authors": dict(sorted(d["authors"].items())),
            "last_commit": d["last_commit"], "last_change": d["last_change"],
            "fix_commits": d["fix_commits"], "refs": sorted(d["refs"])})
    co_path = inv.manifests.get("codeowners_path")
    co = parse_codeowners(inv.files[co_path].content) if co_path else []
    ranking = sorted(((f, v["commits"]) for f, v in per.items() if f in inv.files and inv.files[f].state != "skipped"),
                     key=lambda kv: (-kv[1], kv[0]))[:10]
    facts.add("git_facts", kind="repo", file_path="", origin="source", data={
        "total_commits": len(commits), "codeowners_path": co_path, "codeowners": co,
        "churn_ranking": [{"file": f, "commits": c} for f, c in ranking],
        "recent_commits": [{"sha": c["sha"], "date": c["date"], "subject": c["subject"],
                            "areas": sorted({f.split("/")[0] if "/" in f else "." for f in c["files"]})}
                           for c in commits[:20]]})
