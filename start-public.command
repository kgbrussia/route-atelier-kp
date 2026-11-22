#!/bin/zsh
# Открывает ИИ-сервер этого Mac в интернет для страницы на GitHub Pages.
# 1) запускает server.py с ключом доступа, 2) поднимает туннель Cloudflare,
# 3) пишет адрес туннеля в config.json и пушит его, чтобы Pages узнал новый адрес,
# 4) печатает ссылку для команды. Остановить: Ctrl-C в этом окне.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
PORT=${PORT:-8765}

if [[ ! -s .access-key ]]; then
  python3 -c "import secrets; print(secrets.token_urlsafe(9))" > .access-key
fi
export ACCESS_KEY="$(cat .access-key)"

LOG=$(mktemp -t route-atelier-tunnel)
cleanup() { kill $SERVER_PID $TUNNEL_PID $CAFF_PID 2>/dev/null; rm -f "$LOG"; }
trap cleanup EXIT INT TERM

python3 server.py & SERVER_PID=$!
caffeinate -dimsu & CAFF_PID=$!
# HTTP/2 вместо QUIC: во многих сетях UDP до Cloudflare заблокирован.
cloudflared tunnel --no-autoupdate --protocol http2 --url "http://localhost:$PORT" > "$LOG" 2>&1 & TUNNEL_PID=$!

URL=""
for i in {1..60}; do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1)
  [[ -n "$URL" ]] && grep -q "Registered tunnel connection" "$LOG" && break
  sleep 1
done
if [[ -z "$URL" ]] || ! grep -q "Registered tunnel connection" "$LOG"; then
  echo "Не удалось поднять туннель. Лог:"; tail -20 "$LOG"; exit 1
fi
echo "Жду, пока адрес туннеля заработает…"
for i in {1..45}; do
  curl -s --max-time 5 "$URL/api/health" | grep -q '"ok"' && break
  sleep 2
done

printf '{"api": "%s", "updated": "%s"}\n' "$URL" "$(date '+%Y-%m-%d %H:%M')" > config.json
if git remote get-url origin >/dev/null 2>&1; then
  git add config.json && git commit -q -m "Обновить адрес ИИ-сервера" && git push -q origin HEAD 2>/dev/null \
    && echo "config.json запушен: GitHub Pages подхватит адрес через 1–2 минуты."
  REMOTE=$(git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')
  PAGES="https://${REMOTE%%/*}.github.io/${REMOTE#*/}/"
fi

echo ""
echo "ИИ-сервер в интернете: $URL"
echo "Ключ доступа:          $ACCESS_KEY"
[[ -n "$PAGES" ]] && echo "Ссылка для команды:    ${PAGES}?key=${ACCESS_KEY}"
echo "Прямая ссылка:         ${URL}/?key=${ACCESS_KEY}"
echo ""
echo "Mac не уснёт, пока окно открыто. Остановить: Ctrl-C."
wait $SERVER_PID
