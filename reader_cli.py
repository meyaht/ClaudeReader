"""
claude_reader.py — reads Claude's conversational responses aloud, skips code.

Usage — pipe Claude Code output into this script:
    claude 2>&1 | python claude_reader.py

Or test with:
    echo "Hello this is a test" | python claude_reader.py

Press Ctrl+C to stop.
"""

import sys
import re
import queue
import threading
import asyncio
import tempfile
import os
import subprocess

VOICE = "en-US-AvaNeural"
RATE  = "+0%"

# ---------------------------------------------------------------------------
# TTS — dedicated speaker thread with a queue so speech is serialised
# ---------------------------------------------------------------------------

_q = queue.Queue()

def _speak_edge(text: str):
    import edge_tts
    async def _run():
        communicate = edge_tts.Communicate(text, VOICE, rate=RATE)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            tmp = f.name
        await communicate.save(tmp)
        return tmp
    tmp = asyncio.run(_run())
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-c",
             f"Add-Type -AssemblyName presentationCore; "
             f"$mp = New-Object System.Windows.Media.MediaPlayer; "
             f"$mp.Open([uri]'{tmp}'); $mp.Play(); "
             f"Start-Sleep -Seconds ([math]::Ceiling((Get-Item '{tmp}').Length / 2500) + 1); "
             f"$mp.Stop(); $mp.Close()"],
            check=False
        )
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass

def _speaker_thread():
    while True:
        text = _q.get()
        if text is None:
            break
        try:
            _speak_edge(text)
        except Exception:
            pass
        _q.task_done()

_t = threading.Thread(target=_speaker_thread, daemon=False)
_t.start()


def speak(text: str):
    text = text.strip()
    if text:
        _q.put(text)


def stop():
    _q.put(None)
    _t.join(timeout=5)


# ---------------------------------------------------------------------------
# Filter — strip non-conversational lines
# ---------------------------------------------------------------------------

_SKIP_RE = [
    re.compile(r"^\s*```"),                              # code fence
    re.compile(r"^\s*\$\s+\S"),                          # shell prompt
    re.compile(r"^\s*(>>>|\.\.\.)\s"),                   # Python REPL
    re.compile(r"^\s*[{}\[\]]\s*$"),                     # lone brace/bracket
    re.compile(r'^\s*"[a-z_]+":\s'),                     # JSON key
    re.compile(r"^\s*(import |from \w+ import|def |class )\S"),  # Python
    re.compile(r"^\s*#\s"),                              # comment
    re.compile(r"^[\s\-=_*#]{5,}$"),                    # divider
    re.compile(r"^\s*\d+\u2192"),                        # Read tool line (→)
    re.compile(r"^\s*Co-Authored-By:"),                  # git trailer
    re.compile(r"^\s*\S+\.\S+\s*=\s"),                  # attribute assignment
    re.compile(r"^\s*(Output|Input|State)\("),           # Dash decorators
    re.compile(r"^\s*@callback"),                        # Dash callback decorator
    re.compile(r"^\s*[a-z_]+\s*=\s*[{\[\(\"']"),        # variable = data structure
    re.compile(r"^\s*return\s"),                         # return statement
    re.compile(r"^\s*\w+:\s*$"),                         # lone label (e.g. "Error:")
]


def _is_prose(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    for pat in _SKIP_RE:
        if pat.search(s):
            return False
    # Need at least 4 words to be worth speaking
    if len(s.split()) < 4:
        return False
    return True


# ---------------------------------------------------------------------------
# Stream processor
# ---------------------------------------------------------------------------

def process_stream():
    in_code_block = False
    buffer = []

    for raw_line in sys.stdin:
        line = raw_line.rstrip("\n")

        # Track code fences
        if re.match(r"^\s*```", line):
            in_code_block = not in_code_block
            if buffer:
                speak(" ".join(buffer))
                buffer.clear()
            continue

        if in_code_block:
            continue

        if _is_prose(line):
            buffer.append(line.strip())
        else:
            if buffer:
                speak(" ".join(buffer))
                buffer.clear()

    if buffer:
        speak(" ".join(buffer))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("Claude Reader active. Prose will be spoken; code will be silent.")
    print("Ctrl+C to stop.\n")
    try:
        process_stream()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop()
