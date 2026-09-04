#!/usr/bin/env bash
# Körs av Terminal.app (öppnas av launchd-supervisorn se.wille.tts-watch med
# `open -a Terminal`). Poängen: vakten blir en process under Terminal och ärver
# Terminals iCloud Drive-behörighet. Startad direkt av launchd hänger den på TCC.
# Startar vakten frikopplat och stänger sitt eget fönster.
PY=/opt/homebrew/Caskroom/miniforge/base/envs/pdf-narrator/bin/python
DIR=/Users/wilhelmjohansson/Repos/pdf-narrator
LOG="$HOME/Library/Logs/tts-watch.log"

if ! pgrep -f "tts_watch.py --loop" >/dev/null; then
  cd "$DIR"
  nohup "$PY" tts_watch.py --loop >> "$LOG" 2>&1 &
  disown
  sleep 1
  echo "tts-vakten startad (pid $(pgrep -f 'tts_watch.py --loop' | head -1))"
else
  echo "tts-vakten kör redan"
fi

# Minimera fönstret. Stänga via skript ger en "avsluta processen?"-ruta som kan
# fastna; minimera frågar aldrig. Fönstret öppnas bara vid inloggning eller när
# supervisorn måste starta om vakten, och får stängas för hand när som helst:
# vakten är frikopplad (nohup + disown) och påverkas inte.
osascript -e 'tell application "Terminal" to set miniaturized of (every window whose name contains "tts-watch.command") to true' >/dev/null 2>&1 || true
exit 0
