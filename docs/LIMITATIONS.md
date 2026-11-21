# Honest limitations

* **Not verified live in the development sandbox:** stock providers, RSS feeds, Ollama, Piper model download, and the real YouTube API (no network allow-list/credentials there). They are covered by mocked tests; your first run is their real test.
* **Quality ceiling:** without a GPU, visuals are procedural/astronomical-artistic (not photoreal) unless you add stock keys, a local image generator or a video-AI command. The black-hole renderer is an *artistic* lensing effect, not a physics simulation. There is no depth-based parallax.
* **Narration quality** depends on the LLM you run. 7–8B local models need the fact-checker and your REVIEW step; do not run AUTO unattended until you trust the output.
* **Subtitle timing** is exact per sentence (from the TTS audio) and *estimated* per word inside a sentence.
* **No guarantees about growth or income.** Analytics are real; the optimizer needs ≥5 videos per category before it changes anything, and it cannot see impressions/CTR (the API does not expose them).
* **Monetization thresholds** are configurable and may be out of date. Eligibility is decided by YouTube.
* **YouTube policy**: automated, repetitive content can be treated as "inauthentic/mass-produced" and may be ineligible for monetization. Keep REVIEW on, add your own value, follow the synthetic-media disclosure rules.
* **Windows SAPI / espeak** adapters are untested in the sandbox.
* A **video-AI generation** adapter is a generic command hook; no specific paid service is bundled.
