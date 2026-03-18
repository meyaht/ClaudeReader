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
import concurrent.futures
from pathlib import Path

KOKORO_MODEL  = Path(__file__).parent / "kokoro-v1.0.onnx"
KOKORO_VOICES = Path(__file__).parent / "voices-v1.0.bin"
KOKORO_VOICE  = "bf_isabella"
EDGE_VOICE    = "en-US-AvaNeural"

# ---------------------------------------------------------------------------
# Kokoro — loaded once
# ---------------------------------------------------------------------------

_kokoro = None
_kokoro_lock = threading.Lock()

def _get_kokoro():
    global _kokoro
    if _kokoro is None:
        with _kokoro_lock:
            if _kokoro is None:
                try:
                    from kokoro_onnx import Kokoro
                    _kokoro = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
                except Exception:
                    _kokoro = False
    return _kokoro if _kokoro else None

# ---------------------------------------------------------------------------
# TTS — dedicated speaker thread with a queue so speech is serialised
# ---------------------------------------------------------------------------

_q = queue.Queue()

def _play_samples(samples, sr):
    import sounddevice as sd
    sd.play(samples, sr)
    sd.wait()

def _speak_kokoro(text: str):
    kokoro = _get_kokoro()
    if not kokoro:
        return False
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]
    if not sentences:
        return True
    results = [None] * len(sentences)
    def _gen(i, s):
        try:
            samples, sr = kokoro.create(s, voice=KOKORO_VOICE, speed=1.0, lang="en-us")
            results[i] = (samples, sr)
        except Exception:
            results[i] = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        concurrent.futures.wait([ex.submit(_gen, i, s) for i, s in enumerate(sentences)])
    for r in results:
        if r is not None:
            _play_samples(*r)
    return True

def _speak_edge(text: str):
    import edge_tts, pygame
    if not pygame.mixer.get_init():
        pygame.mixer.init()
    async def _run():
        communicate = edge_tts.Communicate(text, EDGE_VOICE, rate="+0%")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            tmp = f.name
        await communicate.save(tmp)
        return tmp
    tmp = asyncio.run(_run())
    try:
        pygame.mixer.music.load(tmp)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.wait(50)
        pygame.mixer.music.unload()
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass

def _speaker_thread():
    threading.Thread(target=_get_kokoro, daemon=True).start()
    while True:
        text = _q.get()
        if text is None:
            break
        try:
            if not _speak_kokoro(text):
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
    if len(s.split()) < 2:
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
