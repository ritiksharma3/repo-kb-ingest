"""Deterministic git repos from fixture directories (golden-fixture tests and the verify stage)."""
from __future__ import annotations
import os
import shutil
import stat
import subprocess
from pathlib import Path

ENV = {"GIT_AUTHOR_NAME": "Fixture Author", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
       "GIT_COMMITTER_NAME": "Fixture Author", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
       "GIT_AUTHOR_DATE": "2024-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00+00:00"}


def git(root: Path, *args: str, env: dict | None = None) -> str:
    e = {**os.environ, **ENV, **(env or {})}
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=e, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def _force_rm(func, path, _exc):
    """Windows: git object files are read-only, so rmtree needs a chmod-and-retry."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def make_repo(src: Path, dest: Path, message: str = "initial import") -> str:
    """Copy `src` into `dest`, git-init and commit with fixed identity/date. Returns the commit sha."""
    if dest.exists():
        shutil.rmtree(dest, onexc=_force_rm)
    shutil.copytree(src, dest)
    git(dest, "init", "-b", "main")
    git(dest, "config", "core.autocrlf", "false")
    git(dest, "add", "-A")
    git(dest, "commit", "-m", message)
    return git(dest, "rev-parse", "HEAD")


def commit_change(root: Path, relpath: str, content: str, message: str, date: str) -> str:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", message, env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    return git(root, "rev-parse", "HEAD")
