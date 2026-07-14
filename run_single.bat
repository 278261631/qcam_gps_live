@echo off
REM Run QHYCCD Camera Single on Windows
cd /d "%~dp0"
pip install numpy Pillow pyside6 >nul 2>&1
python single/main.py
