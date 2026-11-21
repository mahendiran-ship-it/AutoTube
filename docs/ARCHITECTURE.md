# Architecture

```
main.py (CLI) ─┐                              ┌─ dashboard (FastAPI + static JS)
               ├─ build_services(cfg) ────────┤
Scheduler ─────┘   (app/pipeline/services.py) └─ Scheduler (single worker queue, daily jobs)

Pipeline run (parent task) ── stages (child tasks, resumable, bounded retries, approval gates)
 discover → analyze → select_topic → topic_approval → research → script → script_approval → scenes → voice
 → visuals → captions → audio → render → metadata → thumbnail → qc → upload_approval → upload
```

| Package | Responsibility |
|---|---|
| `app/core` | config layers, logging with secret redaction, errors, notifications, budget, resource monitor, task manager, scheduler |
| `app/database` | SQLite (WAL) + versioned migrations; all SQL lives in `repository.py` |
| `app/ai` | provider abstraction (Ollama / OpenAI-compatible), health checks, JSON repair, prompt templates |
| `app/research` | RSS + Wikipedia-pageview discovery, transparent scoring, duplicate detection, research record, fact-checker |
| `app/content` | script writer + validator, 5–8 scene planner, SEO metadata |
| `app/generation` | TTS, procedural renderers, image motion, stock/AI providers, per-scene fallback chain |
| `app/editing` | subtitles (ASS/SRT), audio mix (ducking, loudnorm), FFmpeg editor, thumbnails |
| `app/quality` | QC engine: measured checks, 0–10 scores, critical vs warning, duplicate/placeholder blocking |
| `app/publishing` | OAuth, uploader, publish-slot calculation |
| `app/analytics` | collector, reports, optimizer (bounded topic-weight learning) |
| `app/dashboard` | REST API + dark UI |

## Key design decisions
* **Config**: `settings.yaml` < `channel.yaml` < `sources.yaml` < `local.yaml` (written by the dashboard). Secrets only via env/`.env`; the saver refuses secret-looking keys.
* **Resumability**: every stage result is stored; on restart interrupted runs resume, and a stage whose output files vanished is re-run.
* **Optimizer**: only per-category topic weights are auto-applied (0.8–1.25, max 0.05 change per run, ≥5 videos per category). Everything else is a recommendation.
* **Scoring**: 11 weighted criteria shown per topic. `trend` is shown only when measured (Wikipedia pageviews); otherwise the signal is labelled "research signal".
* **Security**: argv-only subprocesses, SSRF-safe downloader (https, public IPs, redirect re-validation, size caps), dashboard bound to loopback with Host allow-list, same-origin + custom-header checks, optional token, media served only from `output/`.
