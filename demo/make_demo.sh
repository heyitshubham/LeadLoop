#!/usr/bin/env bash
# Records the LeadLoop demo video end to end: narration -> scripted browser run -> edited MP4 + SRT.
#
# Safe by design: it runs a separate LeadLoop on port 8002 with a COPY of backend/data
# (cached SerpApi results, so searches are free) and mail in sandbox mode, so every email
# goes to Mailpit and the lead's replies are role-played. Your real data is not touched.
#
# Needs: ffmpeg, Node + `playwright` (npm i playwright), internet for the edge-tts voice, Mailpit running
# (docker compose up -d), and an LLM key in backend/.env (Groq free tier is enough). Uses ~25 LLM
# calls and 1 SerpApi search (the Google Maps sample).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BE="$HERE/../backend"
WORK="$HERE/work"
PORT=8002
OUT="${1:-$HERE/leadloop-demo.mp4}"
export WORK

echo "1/4 narration (neural voice via edge-tts; falls back to macOS say)"
"$BE/.venv/bin/python" -c "import edge_tts" 2>/dev/null || "$BE/.venv/bin/pip" install -q edge-tts
"$BE/.venv/bin/python" "$HERE/tts.py" "$HERE/narration.json" "$WORK"

echo "2/4 recording copy of LeadLoop on :$PORT (sandbox mail)"
stop_server() { lsof -ti tcp:$PORT -sTCP:LISTEN | xargs kill 2>/dev/null || true; }
trap stop_server EXIT
lsof -ti tcp:$PORT -sTCP:LISTEN | xargs kill 2>/dev/null || true
rm -rf "$WORK/data" "$WORK/video" "$WORK/marks.json"; mkdir -p "$WORK/data"
cp -R "$BE/data/serp_cache" "$WORK/data/" 2>/dev/null || true
cp "$BE/data/credits.json" "$WORK/data/" 2>/dev/null || true
curl -s -X DELETE localhost:8025/api/v1/messages -o /dev/null || true
(cd "$BE" && LEADLOOP_SEND_MODE=sandbox SANDBOX_SMTP_PORT=1025 LEADLOOP_SCHEDULER=0 LEADLOOP_DATA_DIR="$WORK/data" \
  nohup .venv/bin/uvicorn app.main:app --port $PORT > "$WORK/server.log" 2>&1 < /dev/null &)
curl -s --retry 20 --retry-connrefused --retry-delay 1 -o /dev/null "localhost:$PORT/api/health"

echo "3/4 scripted run"
TESTS="$(cd "$BE" && .venv/bin/python -m pytest -q 2>&1 | grep -oE '[0-9]+ passed' | cut -d' ' -f1)"
echo "   $TESTS tests passing"
TESTS="$TESTS" BASE="http://localhost:$PORT" node "$HERE/record.cjs"
lsof -ti tcp:$PORT -sTCP:LISTEN | xargs kill 2>/dev/null || true
if grep -q "using rules fallback" "$WORK/server.log"; then
  echo "   WARNING: the LLM fell back to rules during the take (quota?). Check $WORK/server.log before using this video."
fi

echo "4/4 editing"
python3 "$HERE/compose.py" "$OUT"
rm -rf "$WORK/frames" "$WORK/raw.mp4"   # ~1 GB of captured frames
