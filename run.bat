@echo off
REM Run QHYCCD Camera Live on Windows
cd /d "%~dp0"
pip install numpy Pillow >nul 2>&1
python live/main.py

