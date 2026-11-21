# YouTube setup, quota and publishing safety

AutoTube AI uses the **official YouTube Data API v3** (upload, thumbnails, read-back) and the
**YouTube Analytics API v2** (reporting). Nothing is scraped and no credentials are stored in the repo.

## 1. Create OAuth credentials (about 10 minutes, free)

1. Open <https://console.cloud.google.com/> and create a project (e.g. "AutoTube").
2. **APIs & Services → Library**: enable **YouTube Data API v3** and **YouTube Analytics API**.
3. **OAuth consent screen**: choose *External*, fill the app name and your email, and add **yourself as a test user**.
4. **Credentials → Create credentials → OAuth client ID → Application type: *Desktop app***. Download the JSON.
5. Save it as `data/secrets/client_secret.json` (the path in `.env.example`; set `YOUTUBE_CLIENT_SECRETS` in `.env` if you use another).
6. Run `python main.py youtube-auth` (or Dashboard → Settings → **Connect YouTube**). A browser opens; approve the requested access.
   The token is saved to `data/secrets/youtube_token.json` (permission 600 on Linux/macOS, git-ignored).

Scopes requested: `youtube.upload`, `youtube.readonly`, `yt-analytics.readonly`. The monetary scope is requested only if you set
`youtube.request_monetary_scope: true` (only useful on a monetized channel).

> **Testing-mode tokens expire after 7 days.** While the consent screen is in *Testing*, Google expires refresh tokens weekly and
> AutoTube AI will show "reconnect your account". Publishing the consent screen to *Production* (it is your own app; you do not need
> Google verification to use it with your own account) avoids this. Verify the current rules in the Cloud Console.

## 2. Quota (verified against Google's documentation on 2026-11-21 — these numbers change)

Always re-check <https://developers.google.com/youtube/v3/determine_quota_cost> and your Cloud Console quota page.

| Call | Cost / limit (as documented at the time of writing) |
|---|---|
| `videos.insert` (upload) | its own bucket, ~100 calls/day, 1 unit each |
| `thumbnails.set` | 50 units from the shared 10,000-unit daily pool |
| `videos.list`, `channels.list`, `playlistItems.list` | 1 unit each |
| `search.list` | own bucket (AutoTube AI does not use it) |
| Reset | midnight Pacific Time |

At 1 video/day the pipeline uses roughly 1 upload + 1 thumbnail (50 units) + a handful of 1-unit reads — a tiny fraction of the pool.
The values live in `youtube.quota` in `config/settings.yaml`. Every API call is recorded in the `api_usage` table.
If YouTube answers with a quota error the upload is **not retried**; it is marked failed and you are notified.

## 3. Publishing modes

| Mode | What happens |
|---|---|
| `MANUAL` | you approve the topic, the script **and** the upload (Dashboard → Pipeline). |
| `REVIEW` (default) | fully automatic up to the finished video, then waits for your approval of the upload; the card lists every QC/research warning. Approved videos are scheduled publicly at the next slot. |
| `PRIVATE` | uploaded as **private**, never scheduled — you press Publish in YouTube Studio. |
| `AUTO` | uploaded and scheduled without asking — **but** still stops for approval if the script was assembled without an AI writer, QC has warnings, or research has warnings. |

Safety rules enforced in code (`app/publishing/uploader.py`):

* Smoke-test videos and videos whose QC did not pass are **refused**.
* A video that already has a successful upload is never uploaded again (also protected by a unique DB index on the YouTube id).
* Identical rendered content (SHA-256) already uploaded under another video id is refused.
* After a crash mid-upload, the channel's uploads playlist is checked for the same title before anything is re-sent.
* Scheduling uses `status.privacyStatus=private` + `status.publishAt` (RFC 3339 UTC). That is the documented way; it only works for never-published private videos.
* After upload the video is **read back** (`videos.list`) and the *actual* privacy/publish time is stored. If YouTube did not accept
  the schedule, or forced the video private, the dashboard shows a warning instead of claiming success.
  (Google's docs have described restrictions for API projects that have not passed an audit — uploads from such projects may be locked
  to private. We do not assume either way; we check every time.)
* `status.containsSyntheticMedia` is set to `true` automatically when AI-generated image/video visuals were used (`youtube.contains_synthetic_media: auto`).
  YouTube's altered/synthetic-content disclosure rules also apply to realistic AI narration — review them and set the value to `true` if needed.
* Custom thumbnails need a verified channel; if `thumbnails.set` fails the upload still succeeds and you are told to set it in Studio.

## 4. What analytics can and cannot tell you

* Collected: views, watch minutes, average view duration/percentage, likes, comments, subscribers gained/lost, retention curve
  (`audienceWatchRatio`, `relativeRetentionPerformance` by `elapsedVideoTimeRatio`).
* **Impressions and impression click-through rate are NOT available** through the Analytics API. They are stored as `NULL` and shown as `n/a`.
  The dashboard never estimates them.
* Revenue is stored only if the API returns it (monetized channel + monetary scope); otherwise it is `unavailable`, never `0`.
* Data lags 1–3 days. Shorts-specific metrics are not exposed separately.
