#!/usr/bin/env bash
# Run QHYCCD Camera Live on macOS
cd "$(dirname "$0")/.."
pip3 install numpy Pillow 2>/dev/null
python3 live/main.py
