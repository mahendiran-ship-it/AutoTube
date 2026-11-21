# Troubleshooting

Start with `python main.py doctor` — it reports FFmpeg, AI providers, voice, YouTube and config problems with the fix.

| Symptom | Cause / fix |
|---|---|
| "No AI provider is reachable" | start Ollama (`ollama serve`, `ollama pull llama3.1:8b`) or enable `ai.providers.llamacpp`. Without an LLM you only get a flagged extractive script. |
| "No real TTS voice available" | `pip install piper-tts` then `python main.py setup-voice`. |
| QC fails "placeholder narration" | only the test tone is available; install a real voice. |
| FFmpeg not found | install it and make sure `ffmpeg` and `ffprobe` are on PATH (`winget install Gyan.FFmpeg`, `apt install ffmpeg`, `brew install ffmpeg`). |
| "YouTube token expired or was revoked" | reconnect (Settings → Connect YouTube). OAuth apps in *Testing* mode expire tokens in 7 days. |
| Upload stays private | YouTube decided so (unverified API project policy, or channel restriction). The dashboard shows the real status; check YouTube Studio. |
| `quotaExceeded` | wait for the midnight-PT reset; see `docs/YOUTUBE_SETUP.md`. |
| Render is slow | set `video.preset: low` or leave the default `auto` on weak machines (about 1–2 min per Short on 2 CPUs at medium). |
| Dashboard 403 "host not allowed" | you opened it through a hostname not in `dashboard.allowed_hosts`. |
| A feed fails | `python main.py check-sources` lists each feed; disable dead ones in `config/sources.yaml`. |
