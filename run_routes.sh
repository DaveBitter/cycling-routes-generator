#!/bin/bash
# Weekly Cycling Route Generator — launcher
# Installs dependencies if needed, then runs the route generator.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$SCRIPT_DIR/run.log"

echo "=== $(date) ===" >> "$LOG"

# Install Python deps if missing
pip3 install requests Pillow --quiet --user >> "$LOG" 2>&1

# Run the generator
python3 "$SCRIPT_DIR/generate_routes.py" >> "$LOG" 2>&1
echo "Exit code: $?" >> "$LOG"
