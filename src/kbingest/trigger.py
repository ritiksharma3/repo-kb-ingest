"""Local Jenkins-style webhook: POST /trigger {"repo": ..., "sha": ...} re-ingests that repo and replies with the change
report. Standard library only; refuses to bind anything but a loopback address. No real Jenkins is involved."""
from __future__ import annotations
import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db, publish
from .stages import link

MAX_BODY = 64 * 1024


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_root(repo: str, repos_dir: Path | None) -> Path | None:
    if repos_dir and (Path(repos_dir) / repo).is_dir():
        return Path(repos_dir) / repo
    with db.connect("reader") as c:
        info = link.known_roots(c).get(repo)
    return Path(info["root"]) if info and Path(info["root"]).exists() else None


def handle_trigger(payload: dict, mapping_file: Path | None, repos_dir: Path | None) -> tuple[int, dict]:
    repo, sha = payload.get("repo"), payload.get("sha") or "HEAD"
    if not isinstance(repo, str) or not repo or "/" in repo or "\\" in repo or repo.startswith("."):
        return 400, {"error": "payload needs a plain 'repo' name"}
    if not isinstance(sha, str) or not sha or sha.startswith("-"):
        return 400, {"error": "invalid 'sha'"}
    root = resolve_root(repo, repos_dir)
    if root is None:
        return 404, {"error": f"unknown repo {repo!r} (never ingested and not under the repos directory)"}
    rep = publish.ingest(root, sha, repo, mapping_file, None, None)
    ok = rep["status"] in ("published", "unchanged")
    return (200 if ok else 422), {
        "status": rep["status"], "repo": repo, "commit": rep.get("commit"), "snapshot_id": rep.get("snapshot_id"),
        "incremental": rep.get("incremental"), "edges": rep.get("edges"), "relinked": rep.get("relinked", {}),
        "changes": rep.get("change_log", []), "reason": rep.get("reason"), "error": rep.get("error")}


def make_server(host: str = "127.0.0.1", port: int = 8765, mapping_file: Path | None = None,
                repos_dir: Path | None = None) -> ThreadingHTTPServer:
    if not _is_loopback(host):
        raise ValueError(f"refusing to bind {host!r}: the trigger webhook is local-only (use 127.0.0.1)")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict):
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path.rstrip("/") != "/trigger":
                return self._send(404, {"error": "POST /trigger only"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > MAX_BODY:
                    return self._send(413 if n > MAX_BODY else 400, {"error": "body missing or too large"})
                payload = json.loads(self.rfile.read(n))
                if not isinstance(payload, dict):
                    raise ValueError("payload must be a JSON object")
            except ValueError as e:      # JSONDecodeError is a ValueError
                return self._send(400, {"error": f"bad JSON: {e}"})
            code, body = handle_trigger(payload, mapping_file, repos_dir)
            self._send(code, body)

        def do_GET(self):
            if self.path.rstrip("/") == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, *a):
            pass

    return ThreadingHTTPServer((host, port), Handler)
