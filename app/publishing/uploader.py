"""Publisher: uploads a QC-passed video to YouTube and (optionally) schedules it.

Safety properties:
- never uploads smoke-test videos, videos whose QC did not pass, or missing files
- idempotent: a video that already has a successful upload is never uploaded again
- content-hash duplicate refusal (same rendered file already uploaded under another video id)
- crash-safe: the YouTube id is persisted the moment the upload finishes; before a retry the uploads
  playlist is checked for a video with the same title so a crash mid-flow cannot create duplicates
- after upload, the real privacyStatus/publishAt are read back and reported truthfully
"""
from __future__ import annotations

import time
from pathlib import Path

from app.core import notifications as N
from app.core.errors import AutoTubeError, ConfigurationError, NonRetryableError, QuotaExceeded
from app.core.logger import get_logger
from app.publishing.scheduler import describe_slot, next_publish_slot, parse_rfc3339, to_rfc3339_utc

log = get_logger("youtube.upload")

_RETRY_STATUS = {500, 502, 503, 504}


class PublishRefused(NonRetryableError):
    """A precondition failed; nothing was sent to YouTube."""


def _http_error_info(e) -> tuple[int | None, str, str]:
    status = getattr(getattr(e, "resp", None), "status", None)
    reason, msg = "", str(e)
    try:
        import json
        body = json.loads(e.content.decode("utf-8", "replace"))
        err = body.get("error", {})
        msg = err.get("message", msg)
        errs = err.get("errors") or [{}]
        reason = errs[0].get("reason", "")
    except Exception:
        pass
    return status, reason, msg[:300]


class Publisher:
    def __init__(self, cfg, repo, auth, notifier, service_factory=None, sleep=time.sleep):
        self.cfg, self.repo, self.auth, self.notifier = cfg, repo, auth, notifier
        self._factory = service_factory or (lambda: auth.service())
        self._sleep = sleep

    # ---- helpers -----------------------------------------------------------
    def _synthetic_flag(self, video_id: int) -> bool:
        setting = self.cfg.get("youtube.contains_synthetic_media", "auto")
        if isinstance(setting, bool):
            return setting
        if str(setting).lower() in ("true", "yes"):
            return True
        if str(setting).lower() in ("false", "no"):
            return False
        # auto: true only when AI-generated image/video visuals were used
        return any(a.get("method") in ("ai_video", "ai_image") for a in self.repo.list_assets(video_id))

    def _check_preconditions(self, v: dict) -> Path:
        if not v:
            raise PublishRefused("video not found")
        if v.get("is_smoke"):
            raise PublishRefused("smoke-test videos are never uploaded")
        qc = v.get("qc_json") or {}
        if not qc.get("passed"):
            raise PublishRefused("QC has not passed for this video; fix the issues and re-run QC")
        if qc.get("critical_failures"):
            raise PublishRefused("QC reports critical failures")
        p = Path(v.get("final_path") or "")
        if not p.is_file():
            raise PublishRefused("rendered video file is missing")
        meta = v.get("metadata_json") or {}
        if not meta.get("title") or not meta.get("description"):
            raise PublishRefused("metadata (title/description) is missing")
        dup = self.repo.video_by_hash(v["content_hash"], exclude_id=v["id"]) if v.get("content_hash") else None
        if dup and self.repo.successful_upload(dup["id"]):
            raise PublishRefused(f"identical video content was already uploaded as video #{dup['id']}")
        return p

    def build_body(self, v: dict, privacy: str, publish_at: str | None) -> dict:
        meta = v["metadata_json"]
        desc = meta["description"]
        body = {
            "snippet": {"title": meta["title"][:100], "description": desc[:5000],
                        "tags": self._fit_tags(meta.get("tags", [])), "categoryId": str(meta.get("category_id") or "28"),
                        "defaultLanguage": self.cfg.get("youtube.default_language", "en"),
                        "defaultAudioLanguage": self.cfg.get("youtube.default_language", "en")},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(meta.get("made_for_kids", False)),
                       "containsSyntheticMedia": self._synthetic_flag(v["id"]), "embeddable": True,
                       "publicStatsViewable": True},
        }
        if publish_at:
            body["status"]["privacyStatus"] = "private"    # publishAt requires private + never published
            body["status"]["publishAt"] = publish_at
        return body

    @staticmethod
    def _fit_tags(tags: list[str], limit: int = 450) -> list[str]:
        out, total = [], 0
        for t in tags:
            t = str(t).strip()
            if not t or "<" in t or ">" in t:
                continue
            if total + len(t) + 1 > limit:
                break
            out.append(t)
            total += len(t) + 1
        return out

    # ---- duplicate check after a crash -------------------------------------------
    def _find_existing_by_title(self, yt, title: str) -> str | None:
        """Look at the newest items of the channel's uploads playlist (1 quota unit per call)."""
        try:
            ch = yt.channels().list(part="contentDetails", mine=True).execute()
            self.repo.add_api_usage("youtube", "channels.list", True)
            uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            items = yt.playlistItems().list(part="snippet,contentDetails", playlistId=uploads, maxResults=15).execute()
            self.repo.add_api_usage("youtube", "playlistItems.list", True)
        except Exception as e:     # best effort only
            log.warning("duplicate pre-check skipped: %s", type(e).__name__)
            return None
        for it in items.get("items", []):
            if it["snippet"].get("title", "").strip().lower() == title.strip().lower():
                return it["contentDetails"]["videoId"]
        return None

    # ---- upload -----------------------------------------------------------------
    def publish(self, video_id: int, privacy: str = "private", schedule: bool = False) -> dict:
        v = self.repo.get_video(video_id)
        done = self.repo.successful_upload(video_id) if v else None
        if done:
            log.info("video %s already uploaded as %s; not uploading again", video_id, done["youtube_video_id"])
            return {"already_uploaded": True, "youtube_video_id": done["youtube_video_id"], "url": done.get("url"),
                    "status": done["status"], "privacy": done.get("privacy"), "publish_at": done.get("publish_at")}
        path = self._check_preconditions(v)
        if privacy not in ("private", "unlisted", "public"):
            raise PublishRefused(f"invalid privacy '{privacy}'")
        publish_at = None
        if schedule:
            publish_at = to_rfc3339_utc(next_publish_slot(self.cfg, self.repo.taken_publish_slots()))
        yt = self._factory()
        title = v["metadata_json"]["title"]
        existing = self._find_existing_by_title(yt, title) if self.repo.uploads_for_video(video_id) else None
        upload_id = self.repo.create_upload(video_id, "private" if publish_at else privacy, publish_at)
        self.repo.update_video(video_id, status="uploading")
        try:
            if existing:
                yt_id = existing
                log.warning("found existing YouTube video %s with identical title; adopting it instead of re-uploading", yt_id)
            else:
                yt_id = self._do_upload(yt, path, self.build_body(v, privacy, publish_at), upload_id, video_id)
            self.repo.update_upload(upload_id, youtube_video_id=yt_id, url=f"https://youtu.be/{yt_id}", status="uploaded")
            thumb_status = self._set_thumbnail(yt, yt_id, v)
            self.repo.update_upload(upload_id, thumbnail_status=thumb_status)
            verified = self._verify(yt, yt_id, publish_at)
        except QuotaExceeded as e:
            self.repo.update_upload(upload_id, status="failed", error=str(e)[:300])
            self.repo.update_video(video_id, status="upload_failed")
            self.notifier.notify(N.VIDEO_FAILED, "YouTube quota exhausted", str(e), "error", video_id)
            raise
        except AutoTubeError as e:
            self.repo.update_upload(upload_id, status="failed", error=str(e)[:300])
            self.repo.update_video(video_id, status="upload_failed")
            raise
        status = "scheduled" if (publish_at and verified.get("publish_at_ok")) else (
            "published" if verified.get("privacy") == "public" else "uploaded")
        self.repo.update_upload(upload_id, status=status, privacy=verified.get("privacy") or privacy,
                                publish_at=verified.get("publish_at") or publish_at,
                                schedule_verified="yes" if verified.get("publish_at_ok") else ("no" if publish_at else "n/a"),
                                response_json=verified)
        self.repo.update_video(video_id, status="scheduled" if status == "scheduled" else status)
        msg = f"https://youtu.be/{yt_id} — {verified.get('privacy', '?')}"
        if status == "scheduled":
            msg += f", goes public {describe_slot(parse_rfc3339(verified['publish_at']).astimezone(self.cfg.tz))}"
        level = "success"
        if verified.get("warnings"):
            msg += " | " + "; ".join(verified["warnings"])
            level = "warning"
        self.notifier.notify(N.VIDEO_UPLOADED, "Video uploaded", msg, level, video_id)
        return {"youtube_video_id": yt_id, "url": f"https://youtu.be/{yt_id}", "status": status,
                "privacy": verified.get("privacy"), "publish_at": verified.get("publish_at") or publish_at,
                "thumbnail": thumb_status, "warnings": verified.get("warnings", [])}

    def _do_upload(self, yt, path: Path, body: dict, upload_id: int, video_id: int) -> str:
        try:
            from googleapiclient.errors import HttpError
            from googleapiclient.http import MediaFileUpload
        except ImportError as e:
            raise ConfigurationError("google-api-python-client is not installed") from e
        chunk = int(self.cfg.get("youtube.upload_chunk_mb", 8)) * 1024 * 1024
        media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=chunk, resumable=True)
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        resp, attempts, max_attempts = None, 0, int(self.cfg.get("pipeline.upload_retries", 5))
        while resp is None:
            try:
                _, resp = req.next_chunk()
            except HttpError as e:
                code, reason, msg = _http_error_info(e)
                if reason in ("quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded", "rateLimitExceeded"):
                    self.repo.add_api_usage("youtube", "videos.insert", False, error=reason, video_id=video_id)
                    raise QuotaExceeded(f"YouTube refused the upload ({reason}). Quota resets at midnight Pacific Time; "
                                        "no retry was attempted.") from e
                if code in (401, 403):
                    from app.publishing.youtube_auth import AuthRequired
                    self.notifier.notify(N.AUTH_REQUIRED, "YouTube authorization problem", msg, "error", video_id)
                    raise AuthRequired(f"YouTube rejected the credentials ({code} {reason}): {msg}") from e
                if code in _RETRY_STATUS and attempts < max_attempts:
                    attempts += 1
                    self._sleep(min(60, 2 ** attempts))
                    continue
                self.repo.add_api_usage("youtube", "videos.insert", False, error=f"{code} {reason}", video_id=video_id)
                raise NonRetryableError(f"YouTube upload failed: {code} {reason} {msg}") from e
            except (ConnectionError, TimeoutError, OSError) as e:
                if attempts >= max_attempts:
                    raise AutoTubeError(f"network error during upload: {type(e).__name__}") from e
                attempts += 1
                self._sleep(min(60, 2 ** attempts))
        yt_id = resp.get("id")
        if not yt_id:
            raise NonRetryableError("YouTube returned no video id")
        self.repo.update_upload(upload_id, youtube_video_id=yt_id, url=f"https://youtu.be/{yt_id}")   # persist at once
        self.repo.add_api_usage("youtube", "videos.insert", True, video_id=video_id)
        return yt_id

    def _set_thumbnail(self, yt, yt_id: str, v: dict) -> str:
        tp = Path(v.get("thumbnail_path") or "")
        if not tp.is_file():
            return "skipped: no thumbnail file"
        try:
            from googleapiclient.http import MediaFileUpload
            yt.thumbnails().set(videoId=yt_id, media_body=MediaFileUpload(str(tp), mimetype="image/jpeg")).execute()
            self.repo.add_api_usage("youtube", "thumbnails.set", True, video_id=v["id"])
            return "set"
        except Exception as e:     # never fail an upload over a thumbnail (e.g. channel not verified for custom thumbnails)
            self.repo.add_api_usage("youtube", "thumbnails.set", False, error=type(e).__name__, video_id=v["id"])
            return f"failed: {type(e).__name__} (set it manually in YouTube Studio)"

    def _verify(self, yt, yt_id: str, publish_at: str | None) -> dict:
        out: dict = {"warnings": []}
        try:
            r = yt.videos().list(part="status", id=yt_id).execute()
            self.repo.add_api_usage("youtube", "videos.list", True)
            items = r.get("items") or []
            if not items:
                out["warnings"].append("could not read the video back to verify its status")
                return out
            st = items[0].get("status", {})
            out["privacy"] = st.get("privacyStatus")
            out["publish_at"] = st.get("publishAt")
            out["upload_status"] = st.get("uploadStatus")
            if publish_at:
                try:
                    out["publish_at_ok"] = bool(out["publish_at"]) and parse_rfc3339(out["publish_at"]) == parse_rfc3339(publish_at)
                except ValueError:
                    out["publish_at_ok"] = False
                if not out["publish_at_ok"]:
                    out["warnings"].append("YouTube did not confirm the scheduled time; check YouTube Studio")
            if out["privacy"] == "private" and not publish_at:
                pass
            if out["privacy"] != "private" and publish_at:
                out["warnings"].append(f"expected private-until-scheduled but YouTube reports '{out['privacy']}'")
        except Exception as e:
            out["warnings"].append(f"verification call failed ({type(e).__name__})")
        return out

    # ---- status refresh -------------------------------------------------------------
    def refresh_statuses(self) -> dict:
        """Mark scheduled uploads as published once their time has passed and YouTube says they are public."""
        changed = {}
        pending = [u for u in self.repo.list_uploads(200) if u["status"] == "scheduled" and u.get("youtube_video_id")]
        if not pending:
            return changed
        yt = self._factory()
        for u in pending:
            try:
                r = yt.videos().list(part="status", id=u["youtube_video_id"]).execute()
                self.repo.add_api_usage("youtube", "videos.list", True)
            except Exception as e:
                log.warning("status refresh failed: %s", type(e).__name__)
                continue
            items = r.get("items") or []
            if items and items[0]["status"].get("privacyStatus") == "public":
                self.repo.update_upload(u["id"], status="published", privacy="public")
                self.repo.update_video(u["video_id"], status="published")
                self.notifier.notify(N.VIDEO_PUBLISHED, "Video is live", f"https://youtu.be/{u['youtube_video_id']}",
                                     "success", u["video_id"])
                changed[u["video_id"]] = "published"
        return changed
