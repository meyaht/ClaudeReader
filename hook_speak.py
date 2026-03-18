"""
hook_speak.py — Claude Code Stop hook.
Reads the last assistant response from the session transcript and
appends it to pending_speech.txt for the Streamlit app to speak.

Called automatically by Claude Code's Stop hook.
Receives JSON on stdin: {"session_id": "...", ...}
"""

import json
import sys
import re
import pathlib

PROJECTS_DIR = pathlib.Path.home() / ".claude" / "projects"
PENDING      = pathlib.Path(__file__).parent / "pending_speech.txt"

# ── same prose filter as reader_cli.py ──────────────────────────────────────
_SKIP_RE = [
    re.compile(r"^\s*```"),
    re.compile(r"^\s*\$\s+\S"),
    re.compile(r"^\s*(>>>|\.\.\.)\s"),
    re.compile(r"^\s*[{}\[\]]\s*$"),
    re.compile(r'^\s*"[a-z_]+":\s'),
    re.compile(r"^\s*(import |from \w+ import|def |class )\S"),
    re.compile(r"^\s*#\s"),
    re.compile(r"^[\s\-=_*#]{5,}$"),
    re.compile(r"^\s*\d+\u2192"),
    re.compile(r"^\s*Co-Authored-By:"),
    re.compile(r"^\s*\S+\.\S+\s*=\s"),
    re.compile(r"^\s*[a-z_]+\s*=\s*[{\[\(\"']"),
    re.compile(r"^\s*return\s"),
    re.compile(r"^\s*\w+:\s*$"),
]

def _is_prose(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    for pat in _SKIP_RE:
        if pat.search(s):
            return False
    if len(s.split()) < 2:
        return False
    return True

def _filter_prose(text: str) -> str:
    in_code = False
    buf = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if re.match(r"^\s*```", line):
            in_code = not in_code
            continue
        if in_code:
            continue
        if _is_prose(line):
            buf.append(line.strip())
    return " ".join(buf).strip()

# ── main ─────────────────────────────────────────────────────────────────────

def main():
    try:
        data = json.loads(sys.stdin.read())
        session_id = data.get("session_id", "")
    except Exception:
        return

    if not session_id:
        return

    # Find the transcript file across all project dirs
    transcript = None
    for jsonl in PROJECTS_DIR.rglob(f"{session_id}.jsonl"):
        transcript = jsonl
        break

    if not transcript or not transcript.exists():
        return

    # Read all lines, find the last complete assistant text message
    last_text = None
    try:
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("type") != "assistant":
                continue
            msg = entry.get("message", {})
            if msg.get("stop_reason") != "end_turn":
                continue
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    last_text = block["text"]
                    break
            if last_text is not None:
                break
    except Exception:
        return

    if not last_text:
        return

    prose = _filter_prose(last_text)
    if not prose:
        return

    # Append to pending queue file (app.py polls this)
    try:
        with open(PENDING, "a", encoding="utf-8") as f:
            f.write(prose + "\n---ENTRY---\n")
    except Exception:
        pass

if __name__ == "__main__":
    main()
