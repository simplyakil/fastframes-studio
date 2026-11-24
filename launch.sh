#!/bin/zsh
# Start FastFrames Studio (if it isn't running) and open it in its own window.
S="${0:A:h}"
PY="${FFS_PYTHON:-$S/.venv/bin/python}"
PORT="${FFS_PORT:-7870}"
URL="http://127.0.0.1:$PORT"
LOG="$HOME/Library/Logs/FastFrames Studio.log"
if ! curl -sf "$URL/api/stats" >/dev/null 2>&1; then
  if [ ! -x "$PY" ]; then
    osascript -e 'display alert "FastFrames Studio" message "Python environment not found. See the README: create .venv in the app folder or set FFS_PYTHON."'
    exit 1
  fi
  cd "$S" && nohup "$PY" -s -E app.py >"$LOG" 2>&1 &
  for i in {1..60}; do curl -sf "$URL/api/stats" >/dev/null 2>&1 && break; sleep 0.25; done
fi
if [ -d "/Applications/Google Chrome.app" ]; then
  open -na "Google Chrome" --args --app="$URL" --window-size=1240,820 \
    --user-data-dir="$HOME/Library/Application Support/FastFrames Studio/browser"
else
  open "$URL"
fi
