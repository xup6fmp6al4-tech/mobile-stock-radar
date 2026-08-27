@echo off
cd /d %~dp0
python -m pip install -r requirements.txt
python -m playwright install chromium
python server.py
pause
