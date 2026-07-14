#!/usr/bin/env bash
# Run QHYCCD Camera Single on Linux/macOS
cd "$(dirname "$0")"
pip install numpy Pillow pyside6 2>/dev/null
python single/main.py
