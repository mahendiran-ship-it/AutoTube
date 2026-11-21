<div align="center">

# AutoTube AI

**A local-first, honest, configurable pipeline that researches, scripts, voices, renders, quality-checks, uploads and learns from YouTube Shorts — with a human in the loop by default.**

Python 3.10+ · FastAPI dashboard · SQLite · FFmpeg · Ollama / llama.cpp · Piper TTS · YouTube Data + Analytics APIs · MIT license

</div>

> **Default channel: "Cosmic Unknown"** (space & astronomy, English, cinematic documentary Shorts of 30–60 s, one per day at 18:30 IST).
> Nothing about the channel is hardcoded — name, niche, style, schedule and sources all live in `config/` and in the dashboard, so the same
> code can run a different channel.

---

## Table of contents
1. [What it does](#what-it-does)
2. [Honest status — read this first](#honest-status--read-this-first)
3. [Quick start (install, API keys, AI model)](#quick-start)
4. [First-run checklist](#first-run-checklist)
5. [Using the dashboard](#using-the-dashboard)
6. [Command line](#command-line)
7. [Publishing modes](#publishing-modes-and-safety)
8. [The pipeline, stage by stage](#the-pipeline-stage-by-stage)
9. [Configuration](#configuration)
10. [Hardware, speed and cost](#hardware-speed-and-cost)
11. [Analytics, optimizer and monetization tracking](#analytics-optimizer-and-monetization-tracking)
12. [Security](#security)
13. [Project layout](#project-layout)
14. [Testing](#testing)
15. [Docs](#docs)
16. [Responsible use](#responsible-use)
17. [License](#license)

---

## What it does

| Step | What happens | Where |
|---|---|---|
| Discover | Collects topics from RSS feeds (NASA, JPL, ESA, ESO, ScienceDaily, Phys.org, Universe Today, Space.com, Sky & Telescope…) and Wikipedia pageview trends | `app/research` |
| Score | 11 transparent criteria (curiosity, retention potential, visual potential, originality, …) with weights you can edit; duplicate detection against everything you ever made | `topic_selector.py` |
| Research | Pulls the source article, extracts facts into a **local research record** with source links | `research_engine.py` |
| Script | Hook → question → story → escalation → payoff → ending, written by your local LLM | `app/content` |
| Fact-check | Deterministic checks (every number must appear in the research, theories must be hedged, no "proven!" wording, no invented quotes) + optional LLM judge | `fact_checker.py` |
| Plan scenes | 5–8 scenes, each with a visual kind and fallbacks | `scene_planner.py` |
| Voice | Local Piper TTS (free), pronunciation dictionary | `voice_generator.py` |
| Visuals | Per-scene chain: AI video → stock video → stock image → AI image → **procedural** (always works offline) | `app/generation` |
| Edit | FFmpeg: 9:16, H.264/AAC, crossfades on narration boundaries, burned subtitles, generated SFX/ambience, music ducking, loudness normalisation | `app/editing` |
| QC | Measured checks (duration, resolution, silence, black frames, clipping, subtitles, licenses, duplicate content, placeholder voice…) → 0–10 scores | `app/quality` |
| Metadata | SEO title/description/tags/hashtags + thumbnail | `seo_generator.py`, `thumbnail_generator.py` |
| Upload | OAuth upload, thumbnail, scheduling, post-upload read-back verification | `app/publishing` |
| Learn | Real analytics → bounded topic-weight adjustments + recommendations | `app/analytics` |

Everything is orchestrated as a **resumable task system**: if the PC sleeps, crashes or you close the window, the run continues from the last completed stage.

---

## Honest status — read this first

This project follows one rule: **never pretend**. Concretely:

* **Tested for real:** configuration, database/migrations, task manager, scoring, duplicate detection, fact-checker, script/scene logic, TTS plumbing, the procedural renderers, subtitles, audio mixing, the FFmpeg editor, thumbnails, QC, the orchestrator (a full **smoke pipeline renders a real MP4** through every local stage), the uploader and analytics logic (with mocked Google services), the optimizer, the scheduler and the dashboard API. See [Testing](#testing).
* **Mock-tested only (not run live by the author):** Ollama/llama.cpp calls, RSS feeds, Pexels/Pixabay/NASA media APIs, AUTOMATIC1111, the Piper model download, and the real YouTube API. They need your network, keys or credentials. Your first run is their real test — `python main.py doctor` and `check-sources` tell you what works. See [docs/PROVIDERS.md](docs/PROVIDERS.md).
* **Not available / not faked:** YouTube's API does **not** provide impressions or impression-CTR, so the dashboard shows `n/a`. Revenue is shown only if YouTube reports it. There are no fake views, no fake revenue, no invented trend numbers (a topic is called "trending" only when a pageview measurement exists).
* **Quality ceiling:** with no GPU the visuals are procedural/artistic. Add free stock keys or a local image generator for richer footage. See [docs/LIMITATIONS.md](docs/LIMITATIONS.md).
* **No promises** about subscribers, views or income. Monetization thresholds in the config may be out of date; YouTube decides eligibility.

---

## Quick start

### Step 0 — What you need installed first

| Need | Why | How |
|---|---|---|
| **Python 3.10 or newer** | runs the app | <https://www.python.org/downloads/> (Windows: tick **"Add python.exe to PATH"**). Check: `python --version` |
| **Git** | to download the project | <https://git-scm.com/downloads> |
| **FFmpeg** (includes `ffprobe`) | builds the video files | Windows: `winget install Gyan.FFmpeg` · Ubuntu/Debian: `sudo apt install ffmpeg` · macOS: `brew install ffmpeg`. Reopen the terminal, check: `ffmpeg -version` |
| **An AI model** (pick one) | writes the script | **A)** free cloud API, e.g. Groq — no big download, see [Step 4](#step-4--connect-an-ai-model-pick-one) · **B)** local Ollama — free, ~5 GB model |

### Step 1 — Download the project
```bash
git clone https://github.com/mahendiran-ship-it/AutoTube.git
cd AutoTube
```

### Step 2 — Install the Python requirements

**Easy way (recommended)** — one script does everything below for you:

| System | Command |
|---|---|
| Windows (PowerShell) | `powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1` |
| Linux / macOS | `bash scripts/install_linux.sh` |

**Manual way** — the same thing, step by step:

| | Windows (PowerShell) | Linux / macOS |
|---|---|---|
| Create a private Python environment | `python -m venv .venv` | `python3 -m venv .venv` |
| Activate it | `.\.venv\Scripts\activate` | `source .venv/bin/activate` |
| **Install the requirements** | `pip install -r requirements.txt` | `pip install -r requirements.txt` |
| Install the free voice engine | `pip install piper-tts` | `pip install piper-tts` |
| Create folders, `.env` file and database | `python main.py init` | `python main.py init` |

> Every time you open a new terminal, activate the environment again (the "Activate it" row) before running `python main.py ...`.
> `start.bat` / `start.sh` do this for you.

### Step 3 — Download the voice (one time, free)
```bash
python main.py setup-voice
```

### Step 4 — Connect an AI model (pick one)

**Option A — Groq cloud API (no local model, fast, has a free tier)**
```bash
python main.py set-key GROQ_API_KEY     # paste your key when asked (input is hidden)
python main.py use-provider groq        # turns it on and makes it the first choice
```
Get a key at <https://console.groq.com/keys>. Model names change — if `doctor` complains, pick a current one from
<https://console.groq.com/docs/models> and run `python main.py use-provider groq --model THE-MODEL-NAME`.

**Option B — Local Ollama (free, private, needs ~5 GB + a decent PC)**
```bash
# install from https://ollama.com, then:
ollama pull llama3.1:8b
```
No AI at all? The app still runs with a limited, **flagged "extractive" script** that is never auto-published.

Any other OpenAI-compatible service (OpenRouter, Together, LM Studio…) works through the `llamacpp` entry in `config/settings.yaml`
(`base_url`, `model`; key via `python main.py set-key LLM_API_KEY`).

### Step 5 — Check everything
```bash
python main.py doctor
```
Every line should say `OK` (YouTube can wait until later). The message beside any `DOWN`/`MISSING` line tells you the fix.

### Step 6 — Start the dashboard
```bash
python main.py serve        # or double-click start.bat (Windows) / run ./start.sh
```
Open <http://127.0.0.1:8787>.

### API keys — how they are handled

* Keys are stored **only in the `.env` file** in the project folder (git-ignored, permission 600 on Linux/macOS) — never in `config/*.yaml`, the database, logs, the dashboard or GitHub.
* Add or change one: `python main.py set-key NAME` (hidden prompt). See which are set: `python main.py keys` (never prints values).
  You can also edit `.env` by hand (copy of `.env.example`).
* The dashboard (Settings → API keys) only shows **set / not set**; it deliberately cannot read or accept key values.
* Available keys: `GROQ_API_KEY`, `LLM_API_KEY`, `PEXELS_API_KEY` (free), `PIXABAY_API_KEY` (free), `DASHBOARD_TOKEN`, `NOTIFY_WEBHOOK_URL`. All are optional.
* Never paste a key into a chat, issue or commit. If you leak one, revoke it at the provider and create a new one.
* YouTube uses a different mechanism (an OAuth client file, not a key) — see [docs/YOUTUBE_SETUP.md](docs/YOUTUBE_SETUP.md).

---

## First-run checklist

1. `python main.py doctor` — fix anything marked `DOWN`/`MISSING` (FFmpeg, AI provider, voice).
2. `python main.py smoke-test` — renders a fixture video through the real pipeline (no network, no AI, **never uploaded**). Open `output/videos/` to see it.
3. `python main.py check-sources` — shows which RSS feeds respond from your network. Disable dead ones in `config/sources.yaml`.
4. Optional free stock keys: `python main.py set-key PEXELS_API_KEY` and `PIXABAY_API_KEY`. NASA media needs no key.
5. Dashboard → **Settings**: set channel name/niche/style/timezone and your schedule.
6. Connect YouTube — follow [docs/YOUTUBE_SETUP.md](docs/YOUTUBE_SETUP.md) (≈10 minutes, free).
7. Dashboard → **Topics → Discover**, then **Run full pipeline now**. In the default `REVIEW` mode it stops for your approval before anything is uploaded.
8. When happy with the output, switch **Automation → Start** (daily production at 10:00, publish slot 18:30 IST by default).

---

## Using the dashboard

Dark-theme UI served locally at `http://127.0.0.1:8787`. Every button calls a real backend action.

| Page | What you can do |
|---|---|
| **Overview** | system health (AI, voice, FFmpeg, RAM/disk/GPU), problems list with fixes, budget, automation on/off, run pipeline / smoke test / discover |
| **Topics** | discover, score & select, add your own topic, reject, produce a specific topic; see every score and the reason |
| **Pipeline** | live runs with per-stage status, pause/resume, **approval inbox** (approve/reject topic, script, upload) |
| **Videos** | preview the MP4 and thumbnail, read script/metadata, QC table with every check, asset license list, re-run QC, upload manually (private/unlisted/scheduled) |
| **Analytics** | collect now, per-video metrics, channel stats, optimizer weights and history, weekly report, monetization progress |
| **Settings** | channel identity, schedule, publishing mode, connect/disconnect YouTube, provider checks |
| **Logs** | notifications and structured logs |

Settings you save are written to `config/local.yaml` (git-ignored). Secrets are never accepted by the dashboard — they only live in `.env`.

---

## Command line

```text
python main.py init            create folders, .env, database
python main.py doctor          health check with fixes
python main.py setup-voice     download the Piper voice
python main.py keys            show which API keys are set
python main.py set-key NAME    save an API key to .env (hidden prompt)
python main.py use-provider NAME [--model M]   enable an AI provider (groq, ollama, llamacpp)
python main.py check-sources   test every RSS feed
python main.py discover [--select]
python main.py smoke-test      real render, never uploaded
python main.py run [--topic ID] [--mode MANUAL|REVIEW|PRIVATE|AUTO]
python main.py resume RUN_ID
python main.py approve RUN_ID STAGE [--reject]
python main.py youtube-auth    connect YouTube
python main.py analytics       collect analytics now
python main.py report [daily|weekly]
python main.py optimize [--dry-run]
python main.py serve [--host H --port P]     dashboard + scheduler
python main.py scheduler       headless scheduler (no dashboard)
```

---

## Publishing modes and safety

| Mode | Behaviour |
|---|---|
| `MANUAL` | you approve topic, script and upload |
| **`REVIEW`** (default) | automatic until the finished video, then waits for your upload approval |
| `PRIVATE` | uploads automatically, **private only**, no scheduling — you publish in YouTube Studio |
| `AUTO` | uploads and schedules by itself, **except** when the script came from the no-AI fallback or QC/research raised warnings |

Always enforced: QC must pass; smoke/placeholder-voice videos are refused; no double uploads (DB unique index + content hash + crash-safe playlist check);
YouTube's real privacy/schedule is read back after upload and shown truthfully. Details: [docs/YOUTUBE_SETUP.md](docs/YOUTUBE_SETUP.md).

---

## The pipeline, stage by stage

`discover → analyze → select_topic → topic_approval → research → script → script_approval → scenes → voice → visuals → captions → audio → render → metadata → thumbnail → qc → upload_approval → upload`

* Each stage is a **child task** with bounded retries (`pipeline.max_stage_retries`) and its result stored in SQLite.
* Approval stages pause the run (`waiting_for_approval`) until you decide; automation never bypasses a gate required by the mode.
* **QC repair loop**: if QC fails on something fixable (e.g. audio/visual defects) the media stages are re-run once (`pipeline.max_qc_repair_attempts`), never endlessly.
* If output files vanish, the stage is re-run on resume.
* Disk-space guard stops a run before it fills your drive.

---

## Configuration

| File | Purpose |
|---|---|
| `config/settings.yaml` | every tunable: schedule, providers, scoring weights, video presets, audio, QC limits, budget, quota facts… (heavily commented) |
| `config/channel.yaml` | channel identity, categories, blocked terms, hashtags, branding |
| `config/sources.yaml` | RSS feeds (enable/disable, reliability) and the Wikipedia pageview watchlist |
| `config/local.yaml` | **your** overrides (written by the dashboard; git-ignored) |
| `.env` | secrets and keys only (git-ignored) |

To run a different channel: edit `config/channel.yaml` (or Settings in the dashboard), swap feeds in `sources.yaml`, adjust the `scoring.weights`. No code changes.

---

## Hardware, speed and cost

* **Cost: ₹0 by default.** Every provider used by default is local or free. `budget.monthly_limit_inr: 0` blocks any paid provider; paid ones must be explicitly enabled and are gated by the budget controller. Costs are recorded in `api_usage`; if a provider reports no cost the dashboard says "unknown", not zero.
* **Minimum:** 2 CPU cores, 8 GB RAM, ~10 GB free disk (plus the LLM). Measured in development on **2 CPUs**: procedural rendering ≈ 25–90 ms/frame at 540×960 → roughly **1–2 minutes of rendering per 45 s Short**; the LLM and TTS are extra.
* `video.preset: auto` picks low/medium/high from your RAM/CPU/GPU. Force `low` (720×1280) on weak machines.
* A GPU is optional. Local image/video generators are *optional* providers you point the app at; none is required.

---

## Analytics, optimizer and monetization tracking

* `python main.py analytics` (or the daily scheduled job) stores per-video views, watch time, average view duration/percentage, likes, comments, subscribers gained/lost and retention curves from the official YouTube Analytics API, plus channel stats.
* **Optimizer**: scores categories by retention (60 %) and views (40 %) relative to your channel average. It changes **only topic-category weights**, only after ≥ 5 analysed videos in a category, only between 0.8 and 1.25, and by at most 0.05 per run. Every change is logged with its evidence in `strategy_changes` and can be reset in the dashboard. Other suggestions (hooks, length, timing) are recommendations that you apply.
* **Monetization progress** compares your real subscribers / watch hours to the thresholds in `monetization.tiers`. Those numbers can change — verify at <https://support.google.com/youtube/answer/72851>.

---

## Security

* Dashboard binds to `127.0.0.1`; it refuses to bind elsewhere without `DASHBOARD_TOKEN`. Host-header allow-list, same-origin and custom-header checks on every write, strict security headers/CSP, media served only from `output/`.
* Secrets only in `.env`/`data/secrets/` (git-ignored, mode 600); the config saver rejects secret-looking keys; logs redact keys/tokens.
* All downloads go through an SSRF-safe fetcher (https only, public IPs only, redirect re-validation, size caps, XML entity rejection).
* Subprocesses use argument lists, never a shell; filenames are sanitised.

---

## Project layout

```
AutoTube/
├─ main.py                  CLI
├─ config/                  settings.yaml · channel.yaml · sources.yaml
├─ app/
│  ├─ core/                 config, logging, tasks, scheduler, budget, notifications
│  ├─ database/             SQLite + migrations + repository
│  ├─ ai/                   Ollama / OpenAI-compatible providers, prompts
│  ├─ research/  content/   discovery, scoring, research, fact-check, script, scenes, SEO
│  ├─ generation/  editing/ TTS, visuals, subtitles, audio, FFmpeg, thumbnails
│  ├─ quality/              QC engine
│  ├─ publishing/  analytics/  YouTube OAuth/upload, collector, optimizer, reports
│  ├─ pipeline/             orchestrator + service container
│  └─ dashboard/            FastAPI API + static UI
├─ assets/                  music (your licensed files) · fonts
├─ scripts/                 install_linux.sh · install_windows.ps1
├─ docs/                    YOUTUBE_SETUP · PROVIDERS · ARCHITECTURE · LIMITATIONS · TROUBLESHOOTING
├─ tests/                   pytest suite (mocks, no network)
├─ data/  output/           runtime state and results (git-ignored)
```

---

## Testing

```bash
python -m pytest -q
```
The suite needs no network, API keys, GPU or LLM. It includes a full smoke-pipeline render (skipped automatically if FFmpeg is missing).
Google/YouTube services, stock providers and LLMs are mocked, so passing tests prove the logic — **not** that your credentials or network work; use `doctor`.

---

## Docs

* [YouTube setup, quota & publishing safety](docs/YOUTUBE_SETUP.md)
* [Providers: supported vs verified](docs/PROVIDERS.md)
* [Architecture](docs/ARCHITECTURE.md)
* [Honest limitations](docs/LIMITATIONS.md)
* [Troubleshooting](docs/TROUBLESHOOTING.md)
* Music licensing: [assets/music/README.md](assets/music/README.md)

---

## Responsible use

You are the publisher. Keep `REVIEW` on until you trust the output, read the sources, follow YouTube's policies (including disclosure of altered/synthetic content and the rules on repetitive or mass-produced content), respect each media license, and add your own editorial value. This tool is for original, educational content — not for copying other creators.

## License

MIT — see [LICENSE](LICENSE). Third-party media and music keep their own licenses.
