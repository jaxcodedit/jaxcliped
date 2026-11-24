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
    "twice_daily": 43200,      # 2 per day (every 12 hrs)
    "thrice_daily": 28800,     # 3 per day (every 8 hrs)
    "four_daily": 21600,       # 4 per day (every 6 hrs)
    "every_4_hours": 14400,    # 6 per day (every 4 hrs)
    "daily": 86400,            # 1 each day (daily)
    "every_2_days": 86400 * 2, # every 2 days
    "weekly": 86400 * 7,       # once a week
    "monthly": 86400 * 30,     # once a month
}

CADENCE_LABELS = {
    "twice_daily": "2 Posts / Day (Every 12 hrs)",
    "thrice_daily": "3 Posts / Day (Every 8 hrs)",
    "four_daily": "4 Posts / Day (Every 6 hrs)",
    "every_4_hours": "6 Posts / Day (Every 4 hrs)",
    "daily": "1 Post / Day (Every 24 hrs)",
    "every_2_days": "Every 2 Days (48 hrs)",
    "weekly": "Once a Week",
    "monthly": "Once a Month",
}


def resolve_cadence_step(cadence: str = "daily", custom_posts_per_day: float = None, custom_hours: float = None) -> tuple:
    """Returns (step_seconds, readable_label) for any preset or customized cadence."""
    if custom_posts_per_day is not None and float(custom_posts_per_day) > 0:
        ppd = float(custom_posts_per_day)
        sec = round(86400.0 / ppd)
        hrs = round(sec / 3600.0, 1)
        hrs_str = f"{int(hrs)}h" if hrs == int(hrs) else f"{hrs}h"
        return max(300, sec), f"{ppd:g} Posts / Day (every {hrs_str})"

    if custom_hours is not None and float(custom_hours) > 0:
        hrs = float(custom_hours)
        sec = round(hrs * 3600.0)
        return max(300, sec), f"Every {hrs:g} Hours"

    cad = str(cadence or "daily").strip()
    if cad.startswith("custom_per_day:") or cad.startswith("custom_daily:"):
        try:
            ppd = float(cad.split(":", 1)[1])
            sec = round(86400.0 / ppd)
            hrs = round(sec / 3600.0, 1)
            hrs_str = f"{int(hrs)}h" if hrs == int(hrs) else f"{hrs}h"
            return max(300, sec), f"{ppd:g} Posts / Day (every {hrs_str})"
        except Exception:
            pass
    elif cad.startswith("custom_hours:"):
        try:
            hrs = float(cad.split(":", 1)[1])
            sec = round(hrs * 3600.0)
            return max(300, sec), f"Every {hrs:g} Hours"
        except Exception:
            pass
    elif cad.startswith("custom:") or cad.isdigit():
        try:
            sec = int(cad.split(":", 1)[-1])
            hrs = round(sec / 3600.0, 1)
            return max(300, sec), f"Every {hrs:g} Hours"
        except Exception:
            pass

    sec = CADENCE_SECONDS.get(cad, 86400)
    label = CADENCE_LABELS.get(cad, "1 Post / Day (Every 24 hrs)")
    return sec, label


def _default_start_time() -> float:
    """Defaults to tomorrow at 18:00 (6:00 PM) local time."""
    now = datetime.now()
    tomorrow_evening = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    return tomorrow_evening.timestamp()


def plan_episodes(duration: float, title: str = "", mode: str = "duration",
                  duration_sec: float = 60.0, parts_count: int = None,
                  cliffhanger: bool = True, cadence: str = "daily",
                  start_time: float = None, account_ids: list = None,
                  words: list = None, custom_posts_per_day: float = None,
                  custom_hours: float = None, max_series_duration: float = None) -> dict:
    """Calculates episode boundaries, cliffhanger hooks, and the schedule calendar."""
    duration = max(5.0, float(duration))
    if max_series_duration and float(max_series_duration) > 0:
        duration = min(duration, float(max_series_duration))

    step, cadence_label = resolve_cadence_step(cadence, custom_posts_per_day, custom_hours)
    t_start = float(start_time) if start_time else _default_start_time()

    # Determine part count and target duration (supports any part count up to 500)
    has_custom_parts = parts_count is not None and int(parts_count) > 0
    if mode == "parts" or (has_custom_parts and mode != "duration"):
        n_parts = max(2, min(500, int(parts_count or 4)))
        target_len = duration / n_parts
    elif mode == "duration":
        target_len = max(10.0, float(duration_sec or 60.0))
        if has_custom_parts:
            n_parts = max(2, min(500, int(parts_count)))
            target_len = duration / n_parts
        else:
            n_parts = max(2, min(500, int(math.ceil(duration / target_len))))
    elif mode == "cliffhanger":
        target_len = max(15.0, float(duration_sec or 60.0))
        if has_custom_parts:
            n_parts = max(2, min(500, int(parts_count)))
            target_len = duration / n_parts
        else:
            n_parts = max(2, min(500, int(math.ceil(duration / target_len))))
    else:
        target_len = 60.0
        n_parts = max(2, min(500, int(math.ceil(duration / target_len))))

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
        "cadence_label": cadence_label,
        "cadence_step": step,
        "start_time": t_start,
        "start_formatted": datetime.fromtimestamp(t_start).strftime("%a, %b %d · %I:%M %p"),
        "account_ids": account_ids or [],
        "episodes": episodes
    }


def create_series(url: str, title: str, mode: str, duration_sec: float,
                  parts_count: int, cliffhanger: bool, auto_post: bool,
                  cadence: str, start_time: float, account_ids: list,
                  layout: str = "crop", captions_style: str = "phonk",
                  custom_posts_per_day: float = None, custom_hours: float = None,
                  max_series_duration: float = None) -> dict:
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
        account_ids=account_ids,
        custom_posts_per_day=custom_posts_per_day,
        custom_hours=custom_hours,
        max_series_duration=max_series_duration
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
        "cadence_label": plan.get("cadence_label"),
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


def restart_series_job(sid: str, user_id: str = None) -> dict:
    """Re-creates and enqueues a background rendering job for an existing series if its job was lost or interrupted."""
    uid = user_id or ctx.uid_or_none()
    if not uid:
        raise ValueError("No user context for restarting series")
    with ctx.as_user(uid):
        s = get_series(sid, uid)
        if not s:
            raise ValueError("Series not found")
        url = s.get("url")
        title = s.get("title") or "Series"
        auto_post = s.get("auto_post", True)
        account_ids = s.get("account_ids", [])

        # Cancel previous job if any
        old_jid = s.get("job_id")
        if old_jid:
            try:
                jobs.delete(old_jid)
            except Exception:
                pass

        # Create underlying jobs.py job with kind="series"
        job_opts = {
            "style": "phonk",
            "layout": "crop",
            "series_id": sid,
            "auto_post": bool(auto_post),
            "account_ids": account_ids
        }
        job = jobs.create({"type": "url", "url": url, "name": title}, job_opts, enqueue=False, kind="series")
        jid = job["id"]

        # Pre-populate job clips matching the existing planned episodes
        job_clips = []
        for ep in s.get("episodes", []):
            job_clips.append({
                "id": ep["cid"],
                "title": ep["title"],
                "hook": ep.get("hook", f"Part {ep.get('part')}"),
                "start": ep["start"],
                "end": ep["end"],
                "status": "queued",
                "score": 95,
                "scheduled_due_at": ep.get("due_at") if auto_post else None,
                "target_accounts": account_ids if auto_post else [],
                "cliffhanger": ep.get("cliffhanger", True)
            })
        job["clips"] = job_clips
        job["series_id"] = sid
        jobs.save(job)
        jobs.enqueue(job)

        update_series(sid, {"job_id": jid, "status": "processing"}, uid)
        s["job_id"] = jid
        s["status"] = "processing"
        return s


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


def reschedule_series(sid: str, cadence: str = None, start_time: float = None,
                      auto_post: bool = None, account_ids: list = None,
                      episodes_patch: list = None, user_id: str = None,
                      custom_posts_per_day: float = None, custom_hours: float = None) -> dict:
    """Updates an ongoing series schedule: adjusts future drop times, cadence, accounts, and auto-post settings."""
    uid = user_id or ctx.uid()
    s = get_series(sid, uid)
    if not s:
        raise ValueError("Series not found")

    job = jobs.get(s.get("job_id")) if s.get("job_id") else None

    # 1. Update auto-post if provided
    if auto_post is not None:
        s["auto_post"] = bool(auto_post)

    # 2. Update accounts if provided
    if account_ids is not None:
        s["account_ids"] = list(account_ids)
        acc_labels = []
        for aid in s["account_ids"]:
            a = accounts.get(aid)
            if a:
                acc_labels.append(f"{a['name']} ({a['platform'].title()})")
        s["account_labels"] = acc_labels

    # 3. Update cadence if provided
    step, cad_label = resolve_cadence_step(cadence or s.get("cadence", "daily"), custom_posts_per_day, custom_hours)
    if cadence or custom_posts_per_day is not None or custom_hours is not None:
        s["cadence"] = cadence or s.get("cadence", "daily")
        s["cadence_label"] = cad_label

    # 4. Handle bulk recalculation of unposted episodes if cadence or start_time changed
    if start_time is not None or cadence or custom_posts_per_day is not None or custom_hours is not None:
        cur_t = float(start_time) if start_time is not None else float(s.get("start_time") or time.time())
        if cur_t < time.time() - 300:
            cur_t = time.time() + 60
        s["start_time"] = cur_t

        for ep in s.get("episodes", []):
            p_list = list(ep.get("posts", {}).values())
            is_posted = any(p.get("status") == "posted" for p in p_list)
            if not is_posted:
                ep["due_at"] = cur_t
                ep["due_formatted"] = datetime.fromtimestamp(cur_t).strftime("%a, %b %d · %I:%M %p")
                cur_t += step

    # 5. Handle fine-grained individual episode patches
    if episodes_patch and isinstance(episodes_patch, list):
        ep_map = {e.get("cid"): e for e in s.get("episodes", [])}
        for patch in episodes_patch:
            cid = patch.get("cid")
            if cid in ep_map:
                ep = ep_map[cid]
                if "due_at" in patch and patch["due_at"]:
                    new_due = float(patch["due_at"])
                    ep["due_at"] = new_due
                    ep["due_formatted"] = datetime.fromtimestamp(new_due).strftime("%a, %b %d · %I:%M %p")
                if "title" in patch and patch["title"]:
                    ep["title"] = str(patch["title"])[:200]

    # 6. Save updated series record
    update_series(sid, {
        "auto_post": s.get("auto_post", True),
        "account_ids": s.get("account_ids", []),
        "account_labels": s.get("account_labels", []),
        "cadence": s.get("cadence", "daily"),
        "cadence_label": s.get("cadence_label"),
        "start_time": s.get("start_time"),
        "episodes": s.get("episodes", [])
    }, uid)

    # 7. Sync with underlying job and posting queue
    if job:
        job_clips_map = {c["id"]: c for c in job.get("clips", [])}
        for ep in s.get("episodes", []):
            c = job_clips_map.get(ep["cid"])
            if c:
                c["scheduled_due_at"] = ep["due_at"] if s.get("auto_post") else None
                c["target_accounts"] = s.get("account_ids", [])
                c["title"] = ep["title"]
        jobs.save(job)

        # Update active scheduled posts in posting.py
        user_posts = posting._mine()
        for p in user_posts:
            if p.get("job") == job["id"]:
                matching_ep = next((e for e in s.get("episodes", []) if e["cid"] == p.get("clip")), None)
                if matching_ep and p.get("status") == "scheduled":
                    if not s.get("auto_post"):
                        try:
                            posting.cancel(p["id"])
                        except Exception:
                            pass
                    else:
                        p["due_at"] = matching_ep["due_at"]
                        posting._save()
                        posting._mirror(p)

    return s


if on_series_clip_done not in jobs.on_clip_ready:
    jobs.on_clip_ready.append(on_series_clip_done)

