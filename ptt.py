"""
ptt.py — Push-to-talk + TTS readback daemon for Claude Code.

PTT:
  Tap configured hotkey → mic records.
  Tap again → Whisper transcribes → text pasted into active window → Enter sent.

TTS:
  Watches the active Claude Code session JSONL for new assistant messages.
  Filters out code blocks, speaks prose via pyttsx3.

Reads hotkey from config.json (set via the ClaudeReader Streamlit app).
Runs as a standalone background process — launched by ClaudeReader.bat.
"""

import json
import re
import sys
import time
import tempfile
import threading
import wave
import concurrent.futures
from pathlib import Path

import numpy as np
import sounddevice as sd
import pyperclip
from pynput import keyboard
from pynput.keyboard import Key, Controller as KeyboardController

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_FILE = Path(__file__).parent / "config.json"
CHAT_LOG    = Path(__file__).parent / "chat_log.json"
SESSIONS_DIR = Path.home() / ".claude" / "projects" / "C--Users-Admin"

_kb = KeyboardController()


def _append_chat(role: str, text: str):
    try:
        log = json.loads(CHAT_LOG.read_text()) if CHAT_LOG.exists() else []
        log.append({"role": role, "text": text, "time": time.strftime("%H:%M:%S")})
        CHAT_LOG.write_text(json.dumps(log[-200:], indent=2))
    except Exception:
        pass


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


SAMPLE_RATE   = 16000
CHANNELS      = 1
KOKORO_MODEL  = Path(__file__).parent / "kokoro-v1.0.onnx"
KOKORO_VOICES = Path(__file__).parent / "voices-v1.0.bin"
KOKORO_VOICE  = "bf_isabella"

_kokoro      = None
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

def _speak_kokoro(text: str, voice: str, rate: int) -> bool:
    kokoro = _get_kokoro()
    if not kokoro:
        return False
    speed = 0.9 + (rate - 150) / 500
    speed = max(0.7, min(1.4, speed))
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]
    if not sentences:
        return True
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(kokoro.create, s, voice, speed, "en-us") for s in sentences]
        for fut in futures:
            try:
                samples, sr = fut.result()
                sd.play(samples, sr)
                sd.wait()
            except Exception:
                pass
    return True


# ---------------------------------------------------------------------------
# Whisper model — loaded in background so key listener starts immediately
# ---------------------------------------------------------------------------

import winsound
from faster_whisper import WhisperModel

_model = None
_model_ready = threading.Event()


def _load_model():
    global _model
    print("Loading Whisper model (tiny.en) — first run downloads ~40 MB...")
    _model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    print("Whisper ready. PTT fully armed.")
    _model_ready.set()


threading.Thread(target=_load_model, daemon=True).start()


# ---------------------------------------------------------------------------
# TTS engine (shared — serialised via a queue)
# ---------------------------------------------------------------------------

import queue as _queue

_tts_q: _queue.Queue = _queue.Queue()
_tts_paused = False   # set from config on startup
_skip_flag  = threading.Event()

SKIP_FILE = Path(__file__).parent / "skip.flag"


def _do_skip():
    """Drain the TTS queue and signal the worker to stop current utterance."""
    _skip_flag.set()
    while not _tts_q.empty():
        try:
            _tts_q.get_nowait()
            _tts_q.task_done()
        except Exception:
            break


def _skip_watcher():
    """Watch for skip.flag written by the Streamlit UI."""
    while True:
        if SKIP_FILE.exists():
            try:
                SKIP_FILE.unlink()
            except Exception:
                pass
            _do_skip()
        time.sleep(0.1)


threading.Thread(target=_skip_watcher, daemon=True).start()


def _is_edge_voice(voice_name: str) -> bool:
    """Edge-tts voices follow the pattern like 'en-US-AvaNeural'."""
    return bool(voice_name and re.match(r'^[a-z]{2}-[A-Z]{2}-\w+Neural', voice_name))


def _speak_edge(text: str, voice: str, rate: int):
    import asyncio, edge_tts, os
    from playsound import playsound
    # Map wpm rate to edge-tts percentage: 150 wpm = +0%
    rate_pct = round((rate - 150) / 1.5)
    rate_str = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"
    async def _run():
        communicate = edge_tts.Communicate(text, voice=voice, rate=rate_str)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp = f.name
        await communicate.save(tmp)
        return tmp
    tmp = asyncio.run(_run())
    try:
        playsound(tmp)
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def _tts_worker():
    # Pre-warm Kokoro
    threading.Thread(target=_get_kokoro, daemon=True).start()

    while True:
        item = _tts_q.get()
        if item is None:
            break
        text, rate = item

        if _skip_flag.is_set():
            _tts_q.task_done()
            continue

        try:
            cfg_voice = load_config().get("voice") or KOKORO_VOICE
            if not _speak_kokoro(text, cfg_voice, rate):
                # Fallback: edge-tts if Kokoro unavailable
                if _is_edge_voice(cfg_voice):
                    _speak_edge(text, cfg_voice, rate)
            _skip_flag.clear()
        except Exception as e:
            print(f"[TTS] error: {e}")
        _tts_q.task_done()


threading.Thread(target=_tts_worker, daemon=True).start()


def _speak(text: str):
    if _tts_paused:
        return
    cfg = load_config()
    rate = cfg.get("rate", 165)
    _tts_q.put((text, rate))
    _append_chat("claude", text)


# ---------------------------------------------------------------------------
# Claude session JSONL watcher
# ---------------------------------------------------------------------------

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _filter_prose(text: str) -> str:
    """Remove code blocks and inline code, collapse whitespace."""
    text = _CODE_BLOCK_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    # Remove tool-call-like lines (lines starting with < or purely symbolic)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("<") and stripped.endswith(">"):
            continue
        lines.append(line)
    text = "\n".join(lines)
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _latest_jsonl() -> Path | None:
    """Return the most-recently-modified session JSONL (not a subagent log)."""
    candidates = [
        p for p in SESSIONS_DIR.glob("*.jsonl")
        if "subagent" not in p.name and "subagents" not in str(p)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _watch_claude_responses():
    """
    Tail the active session JSONL.
    Re-discovers the latest file every 30 s (handles new sessions).
    Speaks new assistant text chunks as they arrive.
    Only processes complete lines (ending with newline) to avoid partial-JSON reads.
    """
    current_file: Path | None = None
    file_pos = 0
    last_discovery = 0.0
    leftover = ""

    print("[TTS watcher] starting...")

    while True:
        now = time.time()

        # Re-discover latest session file periodically
        if now - last_discovery > 30:
            new_file = _latest_jsonl()
            if new_file != current_file:
                current_file = new_file
                leftover = ""
                # Seek to end so we only speak NEW messages
                if current_file and current_file.exists():
                    file_pos = current_file.stat().st_size
                print(f"[TTS watcher] watching {current_file.name if current_file else 'None'}")
            last_discovery = now

        if not current_file or not current_file.exists():
            time.sleep(2)
            continue

        try:
            size = current_file.stat().st_size
            if size <= file_pos:
                time.sleep(0.5)
                continue

            with current_file.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(file_pos)
                new_data = f.read()
                file_pos = f.tell()

            # Prepend any leftover partial line from last read
            chunk = leftover + new_data
            lines = chunk.split("\n")
            # Last element may be incomplete — save it for next read
            leftover = lines[-1]
            complete_lines = lines[:-1]

            for line in complete_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    print(f"[TTS watcher] bad JSON (skipped): {line[:60]}")
                    continue

                msg = obj.get("message", {})
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") != "assistant":
                    continue

                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "text":
                        continue
                    raw = block.get("text", "").strip()
                    if not raw:
                        continue
                    prose = _filter_prose(raw)
                    if prose:
                        print(f"[TTS] {prose[:100]}")
                        _speak(prose)

        except Exception as e:
            print(f"[TTS watcher] error: {e}")
            time.sleep(2)
            continue

        time.sleep(0.5)


# Response watching handled by hook_speak.py + Streamlit app
# threading.Thread(target=_watch_claude_responses, daemon=True).start()


# ---------------------------------------------------------------------------
# Audio recording state
# ---------------------------------------------------------------------------

_recording: list[np.ndarray] = []
_is_recording = False
_stream: sd.InputStream | None = None


def _audio_callback(indata, frames, t, status):
    if _is_recording:
        _recording.append(indata.copy())


def _start_stream():
    global _stream
    if _stream is None or not _stream.active:
        _stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=_audio_callback,
            blocksize=1024,
        )
        _stream.start()


# ---------------------------------------------------------------------------
# Beep helpers
# ---------------------------------------------------------------------------

def _beep_start():
    threading.Thread(target=lambda: winsound.Beep(880, 120), daemon=True).start()

def _beep_stop():
    threading.Thread(target=lambda: winsound.Beep(660, 100), daemon=True).start()

def _beep_error():
    threading.Thread(target=lambda: winsound.Beep(300, 300), daemon=True).start()


# ---------------------------------------------------------------------------
# Send to Claude Code window (works even when window is not focused)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Transcription + send
# ---------------------------------------------------------------------------

def _transcribe_and_send(audio_data: np.ndarray):
    if len(audio_data) < SAMPLE_RATE * 0.3:
        print("Too short — ignoring.")
        return

    if not _model_ready.is_set():
        print("Whisper still loading — waiting...")
        _model_ready.wait(timeout=120)

    import wave as _wave
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name

    try:
        with _wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            pcm = (audio_data * 32767).astype(np.int16)
            wf.writeframes(pcm.tobytes())

        segments, _ = _model.transcribe(
            tmp_path,
            language="en",
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt="Software engineering conversation with Claude Code. Python, terminal, file paths, git.",
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()

        if not text:
            print("No speech detected.")
            _beep_error()
            return

        print(f"Transcribed: {text}")
        _append_chat("user", text)

        # Paste into active window and send Enter
        pyperclip.copy(text)
        time.sleep(0.05)
        _kb.press(Key.ctrl)
        _kb.press('v')
        _kb.release('v')
        _kb.release(Key.ctrl)
        time.sleep(0.15)
        _kb.press(Key.enter)
        _kb.release(Key.enter)

    except Exception as e:
        print(f"Transcription error: {e}")
        _beep_error()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Hotkey listener
# ---------------------------------------------------------------------------

def _key_name(key) -> str:
    try:
        return key.char if hasattr(key, "char") and key.char else str(key)
    except Exception:
        return str(key)


def run(hotkeys: list):
    global _is_recording, _recording

    print(f"PTT listening. Hotkeys: {hotkeys}")
    print("Tap once to start recording, tap again to send.")
    _start_stream()
    _beep_start()   # startup confirmation beep

    def on_press(key):
        global _is_recording, _recording
        name = _key_name(key)
        if name not in hotkeys:
            return

        if not _is_recording:
            _recording = []
            _is_recording = True
            _beep_start()
            print("Recording... (tap again to send)")
        else:
            _is_recording = False
            _beep_stop()
            print("Processing...")
            if _recording:
                audio = np.concatenate(_recording, axis=0).flatten()
                threading.Thread(
                    target=_transcribe_and_send,
                    args=(audio,),
                    daemon=True,
                ).start()

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg    = load_config()
    hotkey  = cfg.get("hotkey")
    hotkey2 = cfg.get("hotkey2")

    hotkeys = [k for k in [hotkey, hotkey2] if k]
    if not hotkeys:
        print("No hotkey assigned. Open ClaudeReader (localhost:8052) and assign one first.")
        sys.exit(1)

    try:
        run(hotkeys)
    except KeyboardInterrupt:
        print("\nPTT stopped.")
    finally:
        if _stream and _stream.active:
            _stream.stop()
            _stream.close()
