"""Secret redaction before storage. Uses gitleaks when installed, else built-in patterns.
Line count is always preserved so file:line evidence stays valid."""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

REDACTED = "[REDACTED]"

PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|client[_-]?secret)\b(\s*[:=]\s*)(['\"])([^'\"\n]{4,})\2"),
    re.compile(r"(?i)(://[^:/\s]+:)([^@\s/]{3,})(@)"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}\b"),
]
ENV_LINE = re.compile(r"^(\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_.]*\s*=)(.*)$")


def _sub_keep_lines(m: re.Match) -> str:
    return REDACTED + "\n" * m.group(0).count("\n")


def _regex_redact(text: str) -> str:
    for p in PATTERNS:
        if p.groups >= 3 and p.pattern.startswith("(?i)(://"):
            text = p.sub(lambda m: m.group(1) + REDACTED + m.group(3), text)
        elif p.groups == 3:
            text = p.sub(lambda m: m.group(0).replace(m.group(3), REDACTED), text)
        else:
            text = p.sub(_sub_keep_lines, text)
    return text


def _gitleaks_lines(text: str) -> set[int]:
    exe = shutil.which("gitleaks")
    if not exe:
        return set()
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "f.txt"
        f.write_text(text, encoding="utf-8")
        rep = Path(d) / "r.json"
        subprocess.run([exe, "detect", "--no-git", "--source", str(f), "--report-format", "json",
                        "--report-path", str(rep), "--exit-code", "0", "--no-banner"],
                       capture_output=True)
        try:
            return {x["StartLine"] for x in json.loads(rep.read_text() or "[]")}
        except Exception:
            return set()


def redact(path: str, text: str) -> tuple[str, int]:
    """Returns (redacted_text, number_of_redactions). Config values are never kept for env files."""
    n = 0
    name = path.rsplit("/", 1)[-1]
    if name == ".env" or name.startswith(".env."):
        out = []
        for ln in text.split("\n"):
            m = ENV_LINE.match(ln)
            if m and not ln.lstrip().startswith("#"):
                out.append(m.group(1) + REDACTED)
                n += 1
            else:
                out.append(ln)
        return "\n".join(out), n
    leak = _gitleaks_lines(text)
    lines = text.split("\n")
    for i in leak:
        if 0 < i <= len(lines):
            lines[i - 1] = REDACTED
            n += 1
    text = "\n".join(lines)
    new = _regex_redact(text)
    if new != text:
        n += 1
    return new, n
