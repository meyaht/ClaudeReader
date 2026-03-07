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
from pathlib import Path

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
if "rate"           not in st.session_state: st.session_state.rate           = 165
if "voice"          not in st.session_state: st.session_state.voice          = None


def _load_config():
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            st.session_state.hotkey = cfg.get("hotkey")
            st.session_state.rate   = cfg.get("rate", 165)
            st.session_state.voice  = cfg.get("voice")
        except Exception:
            pass


def _save_config():
    cfg = {
        "hotkey":  st.session_state.hotkey,
        "rate":    st.session_state.rate,
        "volume":  0.95,
        "paused":  st.session_state.paused,
        "voice":   st.session_state.voice,
    }
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# TTS thread
# ---------------------------------------------------------------------------

def _tts_worker(q: queue.Queue, rate_ref: list):
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
            eng.setProperty("rate", rate_ref[0])
            eng.say(text)
            eng.runAndWait()
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

    tts_t = threading.Thread(
        target=_tts_worker,
        args=(st.session_state.tts_queue, rate_ref),
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

if shared:
    st.session_state.paused = shared.get("paused", st.session_state.paused)

st.title("ClaudeReader")
st.caption("TTS readback for Claude Code — prose only, code skipped.")

col1, col2 = st.columns([1, 1])

# ---- Left column: controls ------------------------------------------------
with col1:
    st.subheader("Controls")

    # Pause / Resume
    pause_label = "Resume" if st.session_state.paused else "Pause"
    if st.button(pause_label, use_container_width=True):
        st.session_state.paused = not st.session_state.paused
        if shared:
            shared["paused"] = st.session_state.paused
        _save_config()
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
    sapi_voices  = _get_sapi_voices()
    edge_voices  = _get_edge_voices()
    all_voices   = sapi_voices + edge_voices
    if all_voices:
        current_voice = st.session_state.voice or "en-US-AvaNeural"
        if current_voice not in all_voices:
            current_voice = all_voices[0]
        selected_voice = st.selectbox(
            "Voice (SAPI or Neural)",
            all_voices,
            index=all_voices.index(current_voice),
            format_func=lambda v: f"Neural: {v}" if "-" in v and "Neural" in v else f"SAPI: {v}",
        )
        if selected_voice != st.session_state.voice:
            st.session_state.voice = selected_voice
            _save_config()
    else:
        st.caption("No voices found.")

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

# Auto-refresh every 3 seconds to pick up new PTT messages
time.sleep(3)
st.rerun()
