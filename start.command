#!/bin/zsh
# Двойной клик: запускает локальный сервер Route Atelier и открывает браузер.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$HOME/.nvm/versions/node/$(ls $HOME/.nvm/versions/node 2>/dev/null | tail -1)/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
( sleep 1.5; open "http://localhost:8765" ) &
exec python3 server.py
