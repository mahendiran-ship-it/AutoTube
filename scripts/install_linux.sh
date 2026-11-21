#!/usr/bin/env bash
# AutoTube AI installer for Linux / macOS. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== AutoTube AI setup =="
command -v python3 >/dev/null || { echo "Python 3.10+ is required (https://www.python.org/downloads/)"; exit 1; }
python3 - <<'PY' || { echo "Python 3.10 or newer is required."; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "FFmpeg not found. Install it, e.g.:"
  echo "  Ubuntu/Debian: sudo apt install ffmpeg      macOS: brew install ffmpeg"
  MISSING=1
fi

[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt
# Optional local TTS (Piper). Failure here is not fatal; the doctor will say what is missing.
python -m pip install piper-tts || echo "(piper-tts could not be installed; see README 'Voice setup')"

python main.py init
echo
echo "Next steps:"
echo "  1. AI model: either  python main.py set-key GROQ_API_KEY && python main.py use-provider groq"
echo "     or install Ollama (https://ollama.com) and run: ollama pull llama3.1:8b"
echo "  2. python main.py setup-voice        # downloads the free Piper voice"
echo "  3. python main.py doctor             # shows exactly what is ready / missing"
echo "  4. ./start.sh                        # opens the dashboard at http://127.0.0.1:8787"
[ "${MISSING:-0}" = 1 ] && echo "!! Install FFmpeg before rendering videos."
exit 0
