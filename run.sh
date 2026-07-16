#!/usr/bin/env bash
# Run QHYCCD Camera Live on Linux/macOS
cd "$(dirname "$0")"
pip install numpy Pillow 2>/dev/null
python live/main.py
