"""Series Studio & Episode Splitter for JaxCliped.

Splits long YouTube videos and live recordings into a sequential series
(Part 1, Part 2, Part 3...) with cliffhanger hook overlays, smart scene/sentence
snapping, and an automated publishing schedule calendar.
"""
import json
import math
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import accounts, ai, config, creators, ctx, jobs, media, posting

_lock = threading.RLock()


def _series_file(uid: str) -> Path:
    p = config.user_dir(uid) / "series.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def list_series(uid: str = None) -> list:
    u = uid or ctx.uid()
    f = _series_file(u)
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_series_list(items: list, uid: str = None):
    u = uid or ctx.uid()
    f = _series_file(u)
    with _lock:
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        tmp.replace(f)


def get_series(sid: str, uid: str = None) -> dict:
    for s in list_series(uid):
        if s.get("id") == sid:
            return s
    return None


def update_series(sid: str, updates: dict, uid: str = None) -> dict:
    u = uid or ctx.uid()
    all_s = list_series(u)
    for i, s in enumerate(all_s):
        if s.get("id") == sid:
            s.update(updates)
            all_s[i] = s
            save_series_list(all_s, u)
            return s
    return None


CADENCE_SECONDS = {
    "daily": 86400,
    "every_2_days": 86400 * 2,
    "weekly": 86400 * 7,
    "monthly": 86400 * 30,
}


def _default_start_time() -> float:
    """Defaults to tomorrow at 18:00 (6:00 PM) local time."""
    now = datetime.now()
    tomorrow_evening = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    return tomorrow_evening.timestamp()


def plan_episodes(duration: float, title: str = "", mode: str = "duration",
                  duration_sec: float = 60.0, parts_count: int = 4,
                  cliffhanger: bool = True, cadence: str = "daily",
                  start_time: float = None, account_ids: list = None,
                  words: list = None) -> dict:
    """Calculates episode boundaries, cliffhanger hooks, and the schedule calendar."""
    duration = max(5.0, float(duration))
    step = CADENCE_SECONDS.get(cadence, 86400)
    t_start = float(start_time) if start_time else _default_start_time()

    # Determine part count and target duration
    if mode == "parts":
        n_parts = max(2, min(50, int(parts_count)))
        target_len = duration / n_parts
    elif mode == "duration":
        target_len = max(15.0, min(180.0, float(duration_sec or 60.0)))
        n_parts = max(2, int(math.ceil(duration / target_len)))
    elif mode == "cliffhanger":
        target_len = max(30.0, min(90.0, float(duration_sec or 60.0)))
        n_parts = max(2, int(math.ceil(duration / target_len)))
    else:
        target_len = 60.0
        n_parts = max(2, int(math.ceil(duration / target_len)))

    # Keep parts reasonable
    if n_parts > 30:
        n_parts = 30
        target_len = duration / n_parts

    episodes = []
    cur_start = 0.0

    cliffhanger_hooks = [
        "WAIT FOR WHAT HAPPENS NEXT 😱",
        "NOBODY SAW THIS COMING 👀",
        "YOU WON'T BELIEVE THIS 🤯",
        "AND THEN THIS HAPPENED... ⚡",
        "WAIT FOR PART {next} 🔥",
        "THE BIGGEST TWIST YET 🚨"
    ]

    for i in range(1, n_parts + 1):
        is_last = (i == n_parts)
        if is_last:
            cur_end = duration
        else:
            raw_end = min(duration - 5.0, cur_start + target_len)
            cur_end = raw_end

            # If transcript words are available, snap to word boundary
            if words:
                window_words = [w for w in words if raw_end - 5.0 <= w.get("e", 0) <= raw_end + 5.0]
                if window_words:
                    if cliffhanger:
                        # find a suspenseful ending word if possible
                        suspense_words = [w for w in window_words if any(w.get("w", "").strip().endswith(p) for p in ("?", "!", "..."))]
                        best = suspense_words[-1] if suspense_words else window_words[-1]
                    else:
                        best = window_words[-1]
                    cur_end = best.get("e", raw_end)

        ep_dur = round(cur_end - cur_start, 1)
        due_at = t_start + (i - 1) * step
        dt = datetime.fromtimestamp(due_at)
        due_formatted = dt.strftime("%a, %b %d · %I:%M %p")

        # Hook banner & Part title
        if is_last:
            hook_text = f"PART {i}/{n_parts} · THE FINALE 🏆"
            ep_title = f"{title or 'Series'} — Part {i}: The Finale"
        elif cliffhanger:
            variant = cliffhanger_hooks[(i - 1) % len(cliffhanger_hooks)].replace("{next}", str(i + 1))
            hook_text = f"PART {i}/{n_parts} · {variant}"
            ep_title = f"{title or 'Series'} — Part {i} [Cliffhanger]"
        else:
            hook_text = f"PART {i}/{n_parts} · FOLLOW FOR PART {i+1}"
            ep_title = f"{title or 'Series'} — Part {i}"

        episodes.append({
            "part": i,
            "cid": f"part{i:02d}",
            "title": ep_title,
            "hook": hook_text,
            "start": round(cur_start, 2),
            "end": round(cur_end, 2),
            "duration": ep_dur,
            "due_at": due_at,
            "due_formatted": due_formatted,
            "cliffhanger": bool(cliffhanger and not is_last),
            "status": "ready"
        })
        cur_start = cur_end

    return {
        "title": title or "Series",
        "duration": duration,
        "n_parts": len(episodes),
        "mode": mode,
        "cliffhanger": cliffhanger,
        "cadence": cadence,
        "cadence_label": {"daily": "1 each day", "every_2_days": "Every 2 days", "weekly": "Once a week", "monthly": "Once a month"}.get(cadence, "Daily"),
        "start_time": t_start,
        "start_formatted": datetime.fromtimestamp(t_start).strftime("%a, %b %d · %I:%M %p"),
        "account_ids": account_ids or [],
        "episodes": episodes
    }


def create_series(url: str, title: str, mode: str, duration_sec: float,
                  parts_count: int, cliffhanger: bool, auto_post: bool,
                  cadence: str, start_time: float, account_ids: list,
                  layout: str = "crop", captions_style: str = "phonk") -> dict:
    """Creates a series job, computes episodes, and kicks off rendering and scheduling."""
    uid = ctx.uid()
    sid = "ser_" + time.strftime("%Y%m%d_%H%M%S_") + secrets.token_hex(4)

    # 1. Fetch metadata preview if URL
    meta = media.info(url) if media.site_allowed(url) else {"title": title, "duration": 300, "uploader": "Creator"}
    vid_dur = float(meta.get("duration") or 300.0)
    final_title = title or meta.get("title") or "Untitled Series"

    # 2. Plan episodes
    plan = plan_episodes(
        duration=vid_dur,
        title=final_title,
        mode=mode,
        duration_sec=duration_sec,
        parts_count=parts_count,
        cliffhanger=cliffhanger,
        cadence=cadence,
        start_time=start_time,
        account_ids=account_ids
    )

    # 3. Create underlying jobs.py job with kind="series"
    job_opts = {
        "style": captions_style or "phonk",
        "layout": layout or "crop",
        "series_id": sid,
        "auto_post": bool(auto_post),
        "account_ids": account_ids or []
    }
    job = jobs.create({"type": "url", "url": url, "name": final_title}, job_opts, enqueue=False, kind="series")
    jid = job["id"]

    # Pre-populate job clips matching the planned episodes
    job_clips = []
    for ep in plan["episodes"]:
        job_clips.append({
            "id": ep["cid"],
            "title": ep["title"],
            "hook": ep["hook"],
            "start": ep["start"],
            "end": ep["end"],
            "status": "queued",
            "score": 95,
            "scheduled_due_at": ep["due_at"] if auto_post else None,
            "target_accounts": account_ids if auto_post else [],
            "cliffhanger": ep["cliffhanger"]
        })
    job["clips"] = job_clips
    job["series_id"] = sid
    jobs.save(job)
    jobs.enqueue(job)

    # 4. Resolve account display names
    acc_labels = []
    for aid in (account_ids or []):
        a = accounts.get(aid)
        if a:
            acc_labels.append(f"{a['name']} ({a['platform'].title()})")

    # 5. Save Series record
    record = {
        "id": sid,
        "user": uid,
        "job_id": jid,
        "title": final_title,
        "url": url,
        "duration": vid_dur,
        "mode": mode,
        "cliffhanger": cliffhanger,
        "auto_post": auto_post,
        "cadence": cadence,
        "start_time": plan["start_time"],
        "account_ids": account_ids or [],
        "account_labels": acc_labels,
        "created_at": time.time(),
        "status": "processing",
        "episodes": plan["episodes"]
    }

    all_s = list_series(uid)
    all_s.insert(0, record)
    save_series_list(all_s, uid)
    return record


def on_series_clip_done(job: dict, clip: dict):
    """Called when an episode clip finishes rendering. If auto_post is enabled, schedules the post."""
    sid = job.get("series_id")
    if not sid:
        return
    uid = job.get("user")
    s = get_series(sid, uid)
    if not s:
        return

    # Update episode in series record
    for ep in s.get("episodes", []):
        if ep["cid"] == clip["id"]:
            ep["status"] = "ready"
            ep["video_file"] = clip.get("file")
            ep["thumb_file"] = clip.get("thumb")
            break

    # If auto-post enabled and not yet posted/scheduled in posting.py
    target_accounts = clip.get("target_accounts") or s.get("account_ids", [])
    due_at = clip.get("scheduled_due_at")
    if s.get("auto_post") and target_accounts and due_at:
        try:
            with ctx.as_user(uid):
                posting.enqueue(job, clip, target_accounts, due_at=due_at)
        except Exception as e:
            print(f"[Series AutoPost] Failed to enqueue clip {clip['id']}: {e}", flush=True)

    update_series(sid, {"episodes": s.get("episodes", []), "status": "active"}, uid)


if on_series_clip_done not in jobs.on_clip_ready:
    jobs.on_clip_ready.append(on_series_clip_done)

