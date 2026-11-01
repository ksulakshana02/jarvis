#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -d .venv ] || { echo "Run ./setup_rocky.sh first"; exit 1; }
. .venv/bin/activate
export YDOTOOL_SOCKET=/tmp/.ydotool_socket
exec python main.py
