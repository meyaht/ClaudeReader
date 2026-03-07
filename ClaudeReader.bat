@echo off
cd /d C:\Users\zkrep\ClaudeReader

echo Starting ClaudeReader...

REM Launch push-to-talk daemon in its own window (stays open, shows transcription log)
start "PTT" "C:\Users\zkrep\AppData\Local\Programs\Python\Python312\python.exe" ptt.py

REM Small delay so PTT window appears first
timeout /t 2 /nobreak >nul

REM Launch Streamlit UI (opens browser automatically)
start "ClaudeReader UI" "C:\Users\zkrep\AppData\Local\Programs\Python\Python312\Scripts\streamlit.exe" run app.py --server.port 8052 --server.headless false

exit
