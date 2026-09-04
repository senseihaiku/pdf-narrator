#!/usr/bin/env bash
# Startar (eller startar om) tts-vakten som bakgrundsprocess.
# MÅSTE köras från en terminal (Terminal/iTerm/Claude Code): processen ärver då
# terminalens iCloud Drive-behörighet. Via launchd hänger den på TCC.
# Överlever att fönstret stängs (nohup + disown). Dör vid omstart/utloggning: kör igen.
set -euo pipefail
PY=/opt/homebrew/Caskroom/miniforge/base/envs/pdf-narrator/bin/python
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/Library/Logs/tts-watch.log"

if pgrep -f "tts_watch.py --loop" >/dev/null; then
  echo "vakten kör redan (pid $(pgrep -f 'tts_watch.py --loop' | head -1)), startar om"
  pkill -f "tts_watch.py --loop"; sleep 1
fi
cd "$DIR"
nohup "$PY" tts_watch.py --loop >> "$LOG" 2>&1 &
disown
sleep 1
echo "vakten startad, pid $(pgrep -f 'tts_watch.py --loop' | head -1)"
echo "mapp:  ~/Library/Mobile Documents/com~apple~CloudDocs/tts"
echo "logg:  $LOG"
