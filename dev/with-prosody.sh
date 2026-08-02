#!/usr/bin/env bash
# Run a command against a freshly-started dev Prosody, then shut it down.
#
#   dev/with-prosody.sh uv run python dev/smoke_xmpp.py
#   dev/with-prosody.sh uv run pytest -m integration
#
# Why this exists: the agent sandbox gives each shell invocation its own network
# namespace, so a server backgrounded in one command is unreachable from the next. The
# server and whatever talks to it have to live in a single invocation. It is also just a
# tidy way to get a disposable server locally.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_TEMPLATE="$REPO_ROOT/dev/prosody/prosody.cfg.lua"
CONFIG="$REPO_ROOT/dev/prosody/prosody.generated.cfg.lua"
LOG="$REPO_ROOT/dev/prosody/prosody.log"
PORT=5222

if [[ $# -eq 0 ]]; then
  echo "usage: $0 <command...>" >&2
  exit 2
fi

command -v prosody >/dev/null || { echo "prosody not installed (pacman -S prosody)" >&2; exit 1; }

sed "s|__ROOT__|$REPO_ROOT|g" "$CONFIG_TEMPLATE" >"$CONFIG"

prosody --config "$CONFIG" >"$LOG" 2>&1 &
PROSODY_PID=$!
cleanup() {
  kill "$PROSODY_PID" 2>/dev/null || true
  wait "$PROSODY_PID" 2>/dev/null || true
  rm -f "$CONFIG"
}
trap cleanup EXIT

for _ in $(seq 1 100); do
  if (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null; then
    break
  fi
  if ! kill -0 "$PROSODY_PID" 2>/dev/null; then
    echo "prosody died on startup; log follows:" >&2
    tail -30 "$LOG" >&2
    exit 1
  fi
  sleep 0.1
done

"$@"
