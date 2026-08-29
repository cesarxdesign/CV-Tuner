#!/bin/bash
# ---------------------------------------------------------------
#  TCV — double-click to run.
#  Starts the server, opens the app, and gets out of the way.
#  Keep the Terminal window open while you use it. Closing it stops TCV.
# ---------------------------------------------------------------

APP="$HOME/Claude/CXD/cv/tcv"

# If this file lives next to server.py, prefer that location.
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SELF_DIR/server.py" ] && APP="$SELF_DIR"

PORT="${TCV_PORT:-8765}"
URL="http://localhost:$PORT"

say() { printf '  %s\n' "$1"; }
hold() { echo; read -r -p "  Press Return to close. " _; }

if [ ! -f "$APP/server.py" ]; then
  echo
  say "Can't find TCV at:"
  say "$APP"
  say "If you moved the folder, update the APP line at the top of this file."
  hold
  exit 1
fi

cd "$APP" || exit 1

# Finder gives a double-clicked script a minimal PATH that usually misses npm's
# global bin, which is where the `claude` CLI lives — and the CLI is the free
# path. Load the login shell's PATH, then add the usual suspects.
if [ -x /usr/libexec/path_helper ]; then eval "$(/usr/libexec/path_helper -s)"; fi
for f in "$HOME/.zprofile" "$HOME/.zshrc" "$HOME/.bash_profile"; do
  # shellcheck disable=SC1090
  [ -f "$f" ] && . "$f" >/dev/null 2>&1
done
PATH="$HOME/.claude/local:/opt/homebrew/bin:/usr/local/bin:$HOME/.npm-global/bin:$HOME/.bun/bin:$HOME/.local/bin:$PATH"
export PATH

# --- already running? just bring it up --------------------------
if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/api/health"; then
  say "TCV is already running."
  open "$URL"
  exit 0
fi

# --- python ------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo
  say "Python 3 isn't installed."
  say "Open Terminal and run:  xcode-select --install"
  hold
  exit 1
fi

# --- start --------------------------------------------------------
python3 server.py &
PID=$!
trap 'kill "$PID" 2>/dev/null' EXIT

for _ in $(seq 1 60); do
  curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/api/health" && break
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.25
done

if ! kill -0 "$PID" 2>/dev/null; then
  echo
  say "TCV failed to start — the reason is above."
  hold
  exit 1
fi

open "$URL"

echo
say "TCV is running at $URL"
say "Keep this window open. Close it to stop."
echo

wait "$PID"
