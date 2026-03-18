"""
ClaudeReader — Streamlit UI for TTS readback of Claude Code output.

Run:  streamlit run app.py
Then pipe Claude output into reader_cli.py in a second terminal, or
paste text directly into the manual input box here.

Hotkey assignment:
  Click "Assign Hotkey", then press the button on your Bluetooth headset.
  That key will toggle pause/resume from anywhere on the desktop.
"""

import json
import queue
import threading
import time
import re
import concurrent.futures
from pathlib import Path

# ── Kokoro (local TTS) — loaded once at startup ──────────────────────────────
KOKORO_MODEL  = Path(__file__).parent / "kokoro-v1.0.onnx"
KOKORO_VOICES = Path(__file__).parent / "voices-v1.0.bin"
KOKORO_VOICE  = "af_heart"

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
                    _kokoro = False  # mark as unavailable
    return _kokoro if _kokoro else None

CHAT_LOG = Path(__file__).parent / "chat_log.json"


def _load_chat() -> list:
    try:
        return json.loads(CHAT_LOG.read_text()) if CHAT_LOG.exists() else []
    except Exception:
        return []


def _append_claude(text: str):
    """Called when TTS speaks a Claude response — logs it to shared chat file."""
    try:
        log = _load_chat()
        log.append({"role": "claude", "text": text, "time": time.strftime("%H:%M:%S")})
        CHAT_LOG.write_text(json.dumps(log[-200:], indent=2))
    except Exception:
        pass

import streamlit as st
import pyttsx3
import win32com.client
from pynput import keyboard


def _get_sapi_voices() -> list[str]:
    try:
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        voices = speaker.GetVoices()
        return [voices.Item(i).GetDescription() for i in range(voices.Count)]
    except Exception:
        return []


@st.cache_data(ttl=3600)
def _get_edge_voices() -> list[str]:
    """Return edge-tts English voice short names, cached for 1 hour."""
    try:
        import asyncio, edge_tts
        async def _fetch():
            mgr = await edge_tts.list_voices()
            return [v["ShortName"] for v in mgr if v["Locale"].startswith("en-")]
        return sorted(asyncio.run(_fetch()))
    except Exception:
        return []

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_FILE = Path(__file__).parent / "config.json"
LOG_FILE    = Path(__file__).parent / "spoken.log"

# ---------------------------------------------------------------------------
# Shared module-level state (survives Streamlit reruns within a session)
# ---------------------------------------------------------------------------

if "tts_queue"      not in st.session_state: st.session_state.tts_queue      = queue.Queue()
if "log"            not in st.session_state: st.session_state.log            = []
if "paused"         not in st.session_state: st.session_state.paused         = False
if "capturing"      not in st.session_state: st.session_state.capturing      = False
if "captured_key"   not in st.session_state: st.session_state.captured_key   = None
if "threads_up"     not in st.session_state: st.session_state.threads_up     = False
if "hotkey"         not in st.session_state: st.session_state.hotkey         = None
if "hotkey2"        not in st.session_state: st.session_state.hotkey2        = None
if "capturing2"     not in st.session_state: st.session_state.capturing2     = False
if "rate"           not in st.session_state: st.session_state.rate           = 165
if "voice"          not in st.session_state: st.session_state.voice          = None


def _load_config():
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            st.session_state.hotkey  = cfg.get("hotkey")
            st.session_state.hotkey2 = cfg.get("hotkey2")
            st.session_state.rate    = cfg.get("rate", 165)
            st.session_state.voice   = cfg.get("voice")
        except Exception:
            pass


def _save_config():
    cfg = {
        "hotkey":  st.session_state.hotkey,
        "hotkey2": st.session_state.hotkey2,
        "rate":    st.session_state.rate,
        "volume":  0.95,
        "paused":  st.session_state.paused,
        "voice":   st.session_state.voice,
    }
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# TTS thread
# ---------------------------------------------------------------------------

def _play_samples(samples, sr):
    """Play raw audio samples via sounddevice."""
    import sounddevice as sd
    sd.play(samples, sr)
    sd.wait()


def _speak_kokoro(text: str, rate_wpm: int, voice: str = KOKORO_VOICE):
    """Split text into sentences, generate all in parallel, play in order."""
    kokoro = _get_kokoro()
    if not kokoro:
        return False

    speed = 0.9 + (rate_wpm - 150) / 500  # ~150wpm=0.9, 250wpm=1.1
    speed = max(0.7, min(1.4, speed))

    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]
    if not sentences:
        return True

    results = [None] * len(sentences)
    def _gen(i, s):
        try:
            samples, sr = kokoro.create(s, voice=voice, speed=speed, lang="en-us")
            results[i] = (samples, sr)
        except Exception:
            results[i] = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(_gen, i, s) for i, s in enumerate(sentences)]
        concurrent.futures.wait(futs)

    for r in results:
        if r is not None:
            _play_samples(*r)
    return True


def _speak_edge_fallback(text: str, voice: str, rate_wpm: int):
    import asyncio, edge_tts, tempfile, os
    import pygame

    rate_pct = int((rate_wpm - 150) / 150 * 100)
    rate_str = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"

    async def _run():
        communicate = edge_tts.Communicate(text, voice, rate=rate_str)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            tmp = f.name
        await communicate.save(tmp)
        return tmp

    tmp = asyncio.run(_run())
    try:
        if not pygame.mixer.get_init():
            pygame.mixer.init()
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


def _tts_worker(q: queue.Queue, rate_ref: list, voice_ref: list):
    # Pre-warm Kokoro on a background thread so first utterance is faster
    threading.Thread(target=_get_kokoro, daemon=True).start()

    eng = pyttsx3.init()
    eng.setProperty("volume", 0.95)
    while True:
        item = q.get()
        if item is None:
            break
        text, is_paused = item
        if is_paused:
            q.task_done()
            continue
        try:
            if not _speak_kokoro(text, rate_ref[0], voice_ref[0] or KOKORO_VOICE):
                # Kokoro unavailable — fall back to edge-tts
                voice = voice_ref[0] or "en-US-AvaNeural"
                _speak_edge_fallback(text, voice, rate_ref[0])
        except Exception:
            pass
        q.task_done()


# ---------------------------------------------------------------------------
# Hotkey listener thread
# ---------------------------------------------------------------------------

def _hotkey_worker(state: dict):
    """Listens for the assigned hotkey and toggles pause, or captures a new key."""

    def on_press(key):
        # Capture mode: grab the next key and store it
        if state["capturing"]:
            try:
                key_name = key.char if hasattr(key, "char") and key.char else str(key)
            except Exception:
                key_name = str(key)
            state["captured_key"] = key_name
            state["capturing"] = False
            return  # keep listener running

        # Normal mode: check if it matches the assigned hotkey
        if state["hotkey"]:
            try:
                pressed = key.char if hasattr(key, "char") and key.char else str(key)
            except Exception:
                pressed = str(key)
            if pressed == state["hotkey"]:
                state["paused"] = not state["paused"]

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()


# ---------------------------------------------------------------------------
# Start background threads once per session
# ---------------------------------------------------------------------------

def _ensure_threads():
    if st.session_state.threads_up:
        return

    _load_config()

    # Shared mutable state dict (for hotkey thread to read/write)
    shared = {
        "paused":       st.session_state.paused,
        "capturing":    False,
        "captured_key": None,
        "hotkey":       st.session_state.hotkey,
    }
    st.session_state._shared = shared

    rate_ref = [st.session_state.rate]
    st.session_state._rate_ref = rate_ref

    voice_ref = [st.session_state.voice or "en-US-AvaNeural"]
    st.session_state._voice_ref = voice_ref

    tts_t = threading.Thread(
        target=_tts_worker,
        args=(st.session_state.tts_queue, rate_ref, voice_ref),
        daemon=True,
    )
    tts_t.start()

    hk_t = threading.Thread(
        target=_hotkey_worker,
        args=(shared,),
        daemon=True,
    )
    hk_t.start()

    st.session_state.threads_up = True


# ---------------------------------------------------------------------------
# Speak helper
# ---------------------------------------------------------------------------

def speak(text: str):
    text = text.strip()
    if not text:
        return
    shared = st.session_state.get("_shared", {})
    paused = shared.get("paused", st.session_state.paused)
    st.session_state.tts_queue.put((text, paused))
    st.session_state.log.insert(0, f"{time.strftime('%H:%M:%S')}  {text[:120]}")
    st.session_state.log = st.session_state.log[:50]
    _append_claude(text)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.set_page_config(page_title="ClaudeReader", page_icon=None, layout="wide")
_ensure_threads()

# Sync shared state back into session_state for display
shared = st.session_state.get("_shared", {})
if shared.get("captured_key") and st.session_state.capturing:
    st.session_state.captured_key = shared["captured_key"]
    st.session_state.hotkey       = shared["captured_key"]
    shared["hotkey"]              = shared["captured_key"]
    shared["captured_key"]        = None
    st.session_state.capturing    = False
    _save_config()
elif shared.get("captured_key") and st.session_state.capturing2:
    st.session_state.hotkey2      = shared["captured_key"]
    shared["captured_key"]        = None
    st.session_state.capturing2   = False
    _save_config()

if shared:
    st.session_state.paused = shared.get("paused", st.session_state.paused)

st.title("ClaudeReader")
st.caption("TTS readback for Claude Code — prose only, code skipped.")

col1, col2 = st.columns([1, 1])

# ---- Left column: controls ------------------------------------------------
with col1:
    st.subheader("Controls")

    # Pause / Resume + Skip
    pause_label = "Resume" if st.session_state.paused else "Pause"
    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        if st.button(pause_label, use_container_width=True):
            st.session_state.paused = not st.session_state.paused
            if shared:
                shared["paused"] = st.session_state.paused
            _save_config()
            st.rerun()
    with btn_col2:
        if st.button("Skip", use_container_width=True):
            (Path(__file__).parent / "skip.flag").write_text("skip")
            st.rerun()

    st.divider()

    # Hotkey assignment
    st.subheader("Hotkey (Bluetooth call button)")

    current_hk = st.session_state.hotkey or "None assigned"
    st.write(f"Current hotkey: `{current_hk}`")

    if st.session_state.capturing:
        st.info("Press your headset call button now... (this page will update automatically)")
        # Poll every 500ms for the captured key
        time.sleep(0.5)
        if shared.get("captured_key"):
            st.session_state.hotkey    = shared["captured_key"]
            shared["hotkey"]           = shared["captured_key"]
            shared["captured_key"]     = None
            st.session_state.capturing = False
            shared["capturing"]        = False
            _save_config()
        st.rerun()
    else:
        if st.button("Assign Hotkey", use_container_width=True):
            st.session_state.capturing = True
            if shared:
                shared["capturing"] = True
            st.rerun()

        if st.button("Clear Hotkey", use_container_width=True):
            st.session_state.hotkey = None
            if shared:
                shared["hotkey"] = None
            _save_config()
            st.rerun()

    st.divider()

    # Hotkey 2 (keyboard fallback)
    st.subheader("Fallback Hotkey (keyboard)")

    current_hk2 = st.session_state.hotkey2 or "None assigned"
    st.write(f"Current fallback hotkey: `{current_hk2}`")

    if st.session_state.capturing2:
        st.info("Press the fallback key now...")
        time.sleep(0.5)
        if shared.get("captured_key"):
            st.session_state.hotkey2     = shared["captured_key"]
            shared["captured_key"]       = None
            st.session_state.capturing2  = False
            shared["capturing"]          = False
            _save_config()
        st.rerun()
    else:
        if st.button("Assign Fallback Hotkey", use_container_width=True):
            st.session_state.capturing2 = True
            if shared:
                shared["capturing"] = True
            st.rerun()

        if st.button("Clear Fallback Hotkey", use_container_width=True):
            st.session_state.hotkey2 = None
            _save_config()
            st.rerun()

    st.divider()

    # Speed slider
    st.subheader("Voice Speed")
    new_rate = st.slider("Words per minute", 120, 250, st.session_state.rate, 5)
    if new_rate != st.session_state.rate:
        st.session_state.rate = new_rate
        if st.session_state.get("_rate_ref"):
            st.session_state._rate_ref[0] = new_rate
        _save_config()

    st.divider()

    # Voice selector
    st.subheader("Voice")
    kokoro_voices = [
        "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica",
        "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
        "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
        "am_michael", "am_onyx", "am_puck",
        "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
        "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    ]
    current_voice = st.session_state.voice or "af_heart"
    if current_voice not in kokoro_voices:
        current_voice = "af_heart"
    selected_voice = st.selectbox(
        "Kokoro Voice",
        kokoro_voices,
        index=kokoro_voices.index(current_voice),
    )
    if selected_voice != st.session_state.voice:
        st.session_state.voice = selected_voice
        if st.session_state.get("_voice_ref"):
            st.session_state._voice_ref[0] = selected_voice
        _save_config()

    st.divider()

    # Manual text input
    st.subheader("Manual Speak")
    manual_text = st.text_area("Paste text to speak:", height=120, key="manual_input")
    if st.button("Speak Now", use_container_width=True):
        if manual_text.strip():
            speak(manual_text)
            st.success("Queued.")

# ---- Right column: status + log -------------------------------------------
with col2:
    st.subheader("Status")

    status_color = "red" if st.session_state.paused else "green"
    status_text  = "PAUSED" if st.session_state.paused else "ACTIVE"
    st.markdown(
        f"<span style='color:{status_color}; font-size:1.4em; font-weight:bold'>"
        f"{status_text}</span>",
        unsafe_allow_html=True,
    )

    q_size = st.session_state.tts_queue.qsize()
    st.write(f"Queue: {q_size} item(s) pending")

    st.divider()
    st.subheader("Recent Utterances")

    if st.session_state.log:
        for entry in st.session_state.log[:20]:
            st.text(entry)
    else:
        st.caption("Nothing spoken yet.")

    if st.button("Clear Log"):
        st.session_state.log = []
        st.rerun()

    st.divider()
    st.caption(
        "Pipe usage: open a second terminal and run  \n"
        "`claude 2>&1 | python reader_cli.py`  \n"
        "The CLI reader speaks through the same TTS engine."
    )

# ---- Chat history (full width below columns) --------------------------------
st.divider()
st.subheader("Conversation")

chat_col, _ = st.columns([3, 1])
with chat_col:
    chat = _load_chat()
    if not chat:
        st.caption("No messages yet. Chat will appear here as you speak and Claude responds.")
    else:
        for msg in reversed(chat):
            role  = msg.get("role", "?")
            text  = msg.get("text", "")
            ts    = msg.get("time", "")
            if role == "user":
                st.markdown(
                    f"<div style='background:#1a3a1a;padding:8px 12px;border-radius:8px;"
                    f"margin:4px 0;border-left:3px solid #2ecc71'>"
                    f"<span style='color:#888;font-size:0.75em'>{ts} &nbsp; YOU</span><br>"
                    f"{text}</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"<div style='background:#1a1a3a;padding:8px 12px;border-radius:8px;"
                    f"margin:4px 0;border-left:3px solid #3498db'>"
                    f"<span style='color:#888;font-size:0.75em'>{ts} &nbsp; CLAUDE</span><br>"
                    f"{text}</div>",
                    unsafe_allow_html=True,
                )

    if st.button("Clear Conversation"):
        CHAT_LOG.write_text("[]")
        st.rerun()

# ── Poll pending_speech.txt written by hook_speak.py ────────────────────────
PENDING = Path(__file__).parent / "pending_speech.txt"

def _drain_pending():
    if not PENDING.exists():
        return
    try:
        raw = PENDING.read_text(encoding="utf-8", errors="replace")
        PENDING.unlink()
    except Exception:
        return
    entries = [e.strip() for e in raw.split("---ENTRY---") if e.strip()]
    for entry in entries:
        speak(entry)

_drain_pending()

# Auto-refresh every 3 seconds to pick up new PTT messages
time.sleep(3)
st.rerun()
