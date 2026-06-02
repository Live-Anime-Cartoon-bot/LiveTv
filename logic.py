import os
import json
import time
import logging
import random
import secrets as pysecrets
import re
import shlex
import shutil
import asyncio
from typing import Tuple, Optional
from os.path import join
from datetime import datetime, timedelta

import psutil
import pytz
import yt_dlp
import gdrive
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)

import config
import limit_system

tz = pytz.timezone(config.TIMEZONE)


def tz_time(*args):
    return datetime.now(tz).timetuple()


logging.Formatter.converter = tz_time
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%d-%m-%Y %I:%M:%S %p " + tz.tzname(datetime.now()),
)
LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directory setup
# ---------------------------------------------------------------------------

os.makedirs(config.DATA_DIRECTORY, exist_ok=True)
os.makedirs(config.DOWNLOAD_DIRECTORY, exist_ok=True)
os.makedirs(config.COOKIES_DIRECTORY, exist_ok=True)

RETENTION_SECONDS = max(int(config.RETENTION_HOURS), 0) * 3600

# ---------------------------------------------------------------------------
# Retention helpers
# ---------------------------------------------------------------------------

def _retention_label() -> str:
    h = config.RETENTION_HOURS
    if h <= 0:
        return "immediately"
    if h == 1:
        return "1 hour"
    return f"{h} hours"


def _safe_rmtree(path: str) -> None:
    try:
        if path and os.path.isdir(path):
            shutil.rmtree(path)
            LOG.info(f"Auto-deleted recording directory: {path}")
    except Exception as e:
        LOG.warning(f"Failed to remove {path}: {e}")


async def _schedule_cleanup(path: str, delay_seconds: int) -> None:
    if not path:
        return
    if delay_seconds <= 0:
        _safe_rmtree(path)
        return
    try:
        await asyncio.sleep(delay_seconds)
    except asyncio.CancelledError:
        return
    _safe_rmtree(path)


def schedule_retention_cleanup(path: str) -> None:
    if not path:
        return
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_schedule_cleanup(path, RETENTION_SECONDS))
        LOG.info(
            f"Scheduled cleanup of {path} in {_retention_label()}"
            if RETENTION_SECONDS > 0
            else f"Scheduled immediate cleanup of {path}"
        )
    except RuntimeError:
        _safe_rmtree(path)


def sweep_old_downloads() -> None:
    try:
        if not os.path.isdir(config.DOWNLOAD_DIRECTORY):
            return
        cutoff = time.time() - RETENTION_SECONDS
        removed = 0
        for entry in os.listdir(config.DOWNLOAD_DIRECTORY):
            full = join(config.DOWNLOAD_DIRECTORY, entry)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            if mtime < cutoff:
                if os.path.isdir(full):
                    _safe_rmtree(full)
                else:
                    try:
                        os.remove(full)
                    except OSError as e:
                        LOG.warning(f"Failed to remove {full}: {e}")
                removed += 1
        if removed:
            LOG.info(f"Startup sweep removed {removed} expired recording entries")
    except Exception as e:
        LOG.error(f"sweep_old_downloads failed: {e}")


# ---------------------------------------------------------------------------
# JSON storage helpers
# ---------------------------------------------------------------------------

VERIFIED_FILE  = join(config.DATA_DIRECTORY, "verified.json")
PLANS_FILE     = join(config.DATA_DIRECTORY, "plans.json")
CHANNELS_FILE  = join(config.DATA_DIRECTORY, "channels.json")
ADMIN_FILE     = join(config.DATA_DIRECTORY, "admins.json")


def _load_json(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        LOG.warning(f"Failed to load {path}: {e}")
        return default


def _save_json(path: str, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        LOG.error(f"Failed to save {path}: {e}")


def load_verified() -> dict:
    return _load_json(VERIFIED_FILE, {"verified": {}, "pending": {}})


def save_verified(data: dict) -> None:
    _save_json(VERIFIED_FILE, data)


def is_verified(user_id: int) -> bool:
    if user_id in config.OWNER_IDS:
        return True
    data = load_verified()
    entry = data.get("verified", {}).get(str(user_id))
    if not entry:
        return False
    expires = entry.get("expires_at")
    if expires:
        try:
            exp_dt = datetime.fromisoformat(expires)
            if datetime.now(tz) > exp_dt:
                return False
        except Exception:
            return True
    return True


def load_plans() -> list:
    default = [
        {"name": "Free Trial",  "price": "Free",          "duration": "3 days",
         "features": ["Up to 3 recordings", "Max 30 minutes per recording", "Standard quality (MKV)"]},
        {"name": "Basic",       "price": "$5 / month",    "duration": "30 days",
         "features": ["Unlimited recordings", "Max 2 hours per recording", "Original quality preserved", "Email support"]},
        {"name": "Pro",         "price": "$12 / month",   "duration": "30 days",
         "features": ["Unlimited recordings", "Max 6 hours per recording", "Original quality + auto-thumbnails", "Priority support", "Early access to new channels"]},
        {"name": "Lifetime",    "price": "$99 one-time",  "duration": "Forever",
         "features": ["Everything in Pro", "Lifetime access", "Custom channel requests", "Direct support line"]},
    ]
    return _load_json(PLANS_FILE, default)


def load_channels() -> dict:
    return _load_json(CHANNELS_FILE, {"categories": {}})


# ---------------------------------------------------------------------------
# Pyrogram client
# ---------------------------------------------------------------------------

app = Client(
    "recorder",
    bot_token=config.BOT_TOKEN,
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    workdir=config.DATA_DIRECTORY,
)

# ---------------------------------------------------------------------------
# Shared runtime state
# ---------------------------------------------------------------------------

user_status:        dict = {}
user_tasks:         dict = {}
rec_setup_sessions: dict = {}   # user_id -> setup dict
_wm_text_pending:   set  = set()
user_ffmpeg_pids:   dict = {}
progress_tasks:     dict = {}
cancelled_users:    set  = set()

MAX_CONCURRENT_REC  = 5
active_recs:        dict = {}   # {user_id: {rec_id: {"status", "ffmpeg_pid", "progress_task", "start"}}}
cancelled_recs:     set  = set()  # set of (user_id, rec_id)
pending_uploads:    dict = {}   # {(user_id, rec_id): upload state dict}
pending_cookies_users: dict = {}
ott_progress:       dict = {}
compress_jobs:      dict = {}
reclink_jobs:       dict = {}
ss_jobs:            dict = {}
merge_sessions:     dict = {}
title_jobs:         dict = {}

# ---------------------------------------------------------------------------
# Auth filter
# ---------------------------------------------------------------------------

def _auth_filter():
    if config.AUTH_USERS:
        return filters.user(config.AUTH_USERS) | filters.user(config.OWNER_IDS or [])
    return filters.all


AUTH = _auth_filter()


def is_owner(user_id: int) -> bool:
    return user_id in config.OWNER_IDS


# ---------------------------------------------------------------------------
# Admin system
# ---------------------------------------------------------------------------

def load_admins() -> list:
    return _load_json(ADMIN_FILE, [])


def save_admins(data: list) -> None:
    _save_json(ADMIN_FILE, data)


def is_admin(user_id: int) -> bool:
    return user_id in load_admins()


def add_admin(user_id: int) -> bool:
    """Add user to admin list. Returns False if already admin."""
    admins = load_admins()
    if user_id in admins:
        return False
    admins.append(user_id)
    save_admins(admins)
    return True


def del_admin(user_id: int) -> bool:
    """Remove user from admin list. Returns False if not found."""
    admins = load_admins()
    if user_id not in admins:
        return False
    admins.remove(user_id)
    save_admins(admins)
    return True


# ---------------------------------------------------------------------------
# Group membership gate
# ---------------------------------------------------------------------------

# In-memory cache: {user_id: (is_member, expires_at)}
_member_cache: dict = {}
_MEMBER_CACHE_TTL = 180  # seconds


async def is_group_member(client, user_id: int) -> bool:
    """
    Return True if user_id is a member of GROUP_CHAT_ID.
    Owners and admins always return True.
    Returns True when GROUP_CHAT_ID is not configured (gate disabled).
    """
    if not config.GROUP_CHAT_ID:
        return True
    if is_owner(user_id) or is_admin(user_id):
        return True

    cached = _member_cache.get(user_id)
    if cached and cached[1] > time.time():
        return cached[0]

    try:
        from pyrogram.enums import ChatMemberStatus
        member = await client.get_chat_member(config.GROUP_CHAT_ID, user_id)
        result = member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except Exception:
        result = False

    _member_cache[user_id] = (result, time.time() + _MEMBER_CACHE_TTL)
    return result


def invalidate_member_cache(user_id: int) -> None:
    """Force re-check on next request (e.g. after admin add/remove)."""
    _member_cache.pop(user_id, None)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def time_to_seconds(time_str: str) -> int:
    try:
        h, m, s = time_str.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except Exception:
        return 0


def TimeFormatter(milliseconds: int) -> str:
    seconds, _ms = divmod(int(milliseconds), 1000)
    minutes, sec = divmod(seconds, 60)
    hours, min_  = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02}:{min_:02}:{sec:02}"
    return f"{min_:02}:{sec:02}"


def _parse_duration_token(tok: str) -> int:
    tok = (tok or "").strip().lower()
    if not tok:
        return 0
    if ":" in tok:
        parts = tok.split(":")
        try:
            parts = [int(p) for p in parts]
        except ValueError:
            return 0
        if len(parts) == 3:
            h, m, s = parts
        elif len(parts) == 2:
            h, m, s = 0, parts[0], parts[1]
        else:
            return 0
        return h * 3600 + m * 60 + s
    m = re.fullmatch(r"(\d+)([smh]?)", tok)
    if not m:
        return 0
    n = int(m.group(1))
    unit = m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


def _seconds_to_hms(sec: int) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

# ---------------------------------------------------------------------------
# Stream probe
# ---------------------------------------------------------------------------

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_M3U8_RE = re.compile(
    r"""(?xi)
    (?P<url>
        (?:https?:)?//[^\s'"<>()\\]+?\.m3u8(?:\?[^\s'"<>()\\]*)?
        |
        /[^\s'"<>()\\]+?\.m3u8(?:\?[^\s'"<>()\\]*)?
    )
    """
)


async def probe_stream(url: str, timeout: float = 8.0, _depth: int = 0) -> dict:
    from urllib.parse import urljoin, urlparse
    from urllib.request import Request, urlopen

    def _fetch(target_url: str, page_referer: str = "") -> dict:
        parsed = urlparse(target_url)
        host_referer = f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme else ""
        req = Request(
            target_url,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": page_referer or host_referer, "Accept": "*/*"},
            method="GET",
        )
        with urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl() or target_url
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body  = resp.read(512 * 1024)
            return {"final_url": final_url, "ctype": ctype, "body": body}

    def _probe(target_url: str) -> dict:
        parsed = urlparse(target_url)
        host_referer = f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme else ""
        result = {"is_hls": False, "final_url": target_url, "referer": host_referer,
                  "user_agent": DEFAULT_USER_AGENT, "extracted_from": None}
        try:
            fetched   = _fetch(target_url)
            final_url = fetched["final_url"]
            ctype     = fetched["ctype"]
            body      = fetched["body"]
            result["final_url"] = final_url
            final_parsed = urlparse(final_url)
            if final_parsed.scheme and final_parsed.netloc:
                result["referer"] = f"{final_parsed.scheme}://{final_parsed.netloc}/"
            head_text = body[:2048].decode("utf-8", errors="ignore").lstrip()
            if "mpegurl" in ctype or "m3u8" in ctype or head_text.startswith("#EXTM3U"):
                result["is_hls"] = True
                return result
            looks_textual = ("html" in ctype or "javascript" in ctype or "json" in ctype
                             or "text" in ctype or not ctype)
            if not looks_textual:
                return result
            text  = body.decode("utf-8", errors="ignore")
            match = _M3U8_RE.search(text)
            if not match:
                return result
            raw = match.group("url")
            if raw.startswith("//"):
                scheme    = final_parsed.scheme or "https"
                extracted = f"{scheme}:{raw}"
            elif raw.startswith("/"):
                extracted = urljoin(final_url, raw)
            else:
                extracted = raw
            LOG.info(f"Extracted m3u8 from page {final_url}: {extracted[:100]}")
            result["extracted_from"]  = final_url
            result["_extracted_url"]  = extracted
            return result
        except Exception as e:
            LOG.warning(f"Stream probe failed for {target_url}: {e}")
            return result

    first     = await asyncio.to_thread(_probe, url)
    extracted = first.pop("_extracted_url", None)
    if extracted and _depth == 0:
        page_url = first["final_url"]
        nested   = await probe_stream(extracted, timeout=timeout, _depth=1)
        if nested["is_hls"]:
            nested["extracted_from"] = page_url
            nested["referer"]        = page_url
            return nested
    return first


# ---------------------------------------------------------------------------
# Shell / FFprobe helpers
# ---------------------------------------------------------------------------

async def runcmd(cmd: str) -> Tuple[int, str, str]:
    args    = shlex.split(cmd)
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode(errors="ignore"), stderr.decode(errors="ignore")


async def get_video_duration(input_file: str) -> int:
    try:
        parser   = createParser(input_file)
        if not parser:
            return 0
        metadata = extractMetadata(parser)
        if not metadata or not metadata.has("duration"):
            return 0
        return int(metadata.get("duration").seconds)
    except Exception as e:
        LOG.warning(f"Hachoir failed: {e}")
        return 0


async def get_duration_ffmpeg(input_file: str) -> int:
    try:
        cmd = (f'ffprobe -v error -show_entries format=duration '
               f'-of default=noprint_wrappers=1:nokey=1 "{input_file}"')
        retcode, out, _err = await runcmd(cmd)
        if retcode == 0 and out.strip():
            return int(float(out.strip()))
    except Exception as e:
        LOG.warning(f"FFprobe failed: {e}")
    return 0


async def _ffprobe_video(path: str) -> dict:
    probe_cmd = (f'ffprobe -v error -hide_banner -print_format json '
                 f'-show_format -show_streams {shlex.quote(path)}')
    rc, out, err = await runcmd(probe_cmd)
    if rc != 0:
        raise Exception(f"ffprobe failed: {err.strip() or 'no stderr'}")
    data         = json.loads(out or "{}")
    duration     = float(data.get("format", {}).get("duration") or 0)
    video_height = 0
    audio_streams: list = []
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not video_height:
            video_height = int(s.get("height") or 0)
        elif s.get("codec_type") == "audio":
            tags = s.get("tags") or {}
            lang = (tags.get("language") or "und").lower()[:3]
            audio_streams.append({
                "index":    s["index"],
                "lang":     lang,
                "codec":    s.get("codec_name", "?"),
                "channels": s.get("channels", 2),
            })
    return {"duration": duration, "video_height": video_height, "audio_streams": audio_streams}

# ---------------------------------------------------------------------------
# Plan / Channel helpers
# ---------------------------------------------------------------------------

def render_plans_text() -> str:
    plans = load_plans()
    out   = ["**Subscription Plans**\n"]
    for p in plans:
        feats = "\n".join([f"  • {f}" for f in p.get("features", [])])
        out.append(f"**{p['name']}** — `{p['price']}`\nDuration: `{p.get('duration', '-')}`\n{feats}")
    out.append(f"\nTo subscribe, contact @{config.SUPPORT_USERNAME}.")
    return "\n\n".join(out)


def _channel_root_kb() -> InlineKeyboardMarkup:
    chans = load_channels()
    cats  = list(chans.get("categories", {}).keys())
    rows, row = [], []
    for i, c in enumerate(cats):
        row.append(InlineKeyboardButton(c, callback_data=f"chcat:{c}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not rows:
        rows = [[InlineKeyboardButton("No channels configured", callback_data="noop")]]
    return InlineKeyboardMarkup(rows)

# ---------------------------------------------------------------------------
# Pre-recording Setup Wizard helpers
# ---------------------------------------------------------------------------

_QUALITY_BITRATE_KBPS = {"480": 600, "576": 820, "640": 1000, "720": 1500, "1080": 2500}


def _est_size_mb(duration_sec: int, quality: str) -> str:
    br = _QUALITY_BITRATE_KBPS.get(quality, 0)
    if not br or duration_sec <= 0:
        return "?"
    return f"~{duration_sec * br / 8 / 1024:.0f} MB"


def _setup_summary(s: dict) -> str:
    q      = s["quality"]
    q_str  = f"{q}p" if q != "original" else "Original"
    q_icon = "🔵" if q == "1080" else ("🔒" if q == "original" else "📺")
    asp    = s["aspect"]
    asp_label = {
        "none": "None (Keep as-is)", "21:9": "21:9 Aspect", "16:9": "16:9 Aspect",
        "4:5": "4:5 Aspect", "bars": "16:9 Black Bars", "zoom": "16:9 Zoom",
        "1280x720": "scale=1280:720",
    }.get(asp, asp)
    wm     = s["watermark_pos"].replace("_", " ").title() if s["watermark_on"] else "OFF"
    at     = s["audio_track"]
    tracks = s.get("detected_audio_tracks", [])
    if at == 0:
        audio_s = "All Tracks"
    elif tracks and at <= len(tracks):
        audio_s = _audio_track_label(tracks[at - 1])
    else:
        audio_s = f"Track {at}"
    auto_s = "✅ On" if s["auto_mode"] else "❌ Off"
    return (
        f"📋 **Recording Setup**\n\n"
        f"⏱ Duration: `{s['timestamp']}`\n"
        f"🔄 Auto Mode: {auto_s}\n"
        f"📁 Filename: `{s['filename']}`\n"
        f"🎙 Audio: `{audio_s}`\n"
        f"💧 Watermark: `{wm}`\n"
        f"{q_icon} Size: `{q_str}`\n"
        f"📐 Aspect: `🔒 {asp_label}`\n\n"
        f"👇 Choose an option:"
    )


def _kb_step1(s: dict) -> InlineKeyboardMarkup:
    uid       = s["user_id"]
    wm_icon   = "✅" if s["watermark_on"] else "🚫"
    wm_label  = "ON" if s["watermark_on"] else "OFF"
    auto_icon = "✅" if s["auto_mode"] else "⏩"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↖️ Top-Left",    callback_data=f"rs:{uid}:wm_pos:top_left"),
         InlineKeyboardButton("↗️ Top-Right",   callback_data=f"rs:{uid}:wm_pos:top_right")],
        [InlineKeyboardButton("⊙ Center",       callback_data=f"rs:{uid}:wm_pos:center")],
        [InlineKeyboardButton("↙️ Bottom-Left", callback_data=f"rs:{uid}:wm_pos:bottom_left"),
         InlineKeyboardButton("↘️ Bottom-Right",callback_data=f"rs:{uid}:wm_pos:bottom_right")],
        [InlineKeyboardButton(f"{wm_icon} 🚫 Watermark {wm_label}", callback_data=f"rs:{uid}:wm_toggle")],
        [InlineKeyboardButton("✏️ Change Watermark Text",            callback_data=f"rs:{uid}:wm_text")],
        [InlineKeyboardButton(f"{auto_icon} Auto: First+Last 1min", callback_data=f"rs:{uid}:auto_toggle")],
        [InlineKeyboardButton("◀️ Back: Audio Track",               callback_data=f"rs:{uid}:back_audio"),
         InlineKeyboardButton("📐 Next: Video Size →",              callback_data=f"rs:{uid}:next_quality")],
        [InlineKeyboardButton("❌ Cancel",                           callback_data=f"rs:{uid}:cancel")],
    ])


def _kb_step2(s: dict) -> InlineKeyboardMarkup:
    uid  = s["user_id"]
    dur  = s["duration_sec"]
    rows = []
    for q, label, icon in [
        ("480", "480p", "🖥️"), ("576", "576p", "🖥️"), ("640", "640p", "🖥️"),
        ("720", "720p", "🖥️"), ("1080", "1080p", "🔵"), ("original", "Original", "🔒"),
    ]:
        sel = "✅ " if s["quality"] == q else ""
        rows.append([InlineKeyboardButton(f"{sel}{icon} {label} ({_est_size_mb(dur, q)})",
                                          callback_data=f"rs:{uid}:quality:{q}")])
    rows.append([InlineKeyboardButton("◀️ Back to Watermark",    callback_data=f"rs:{uid}:back_step1")])
    rows.append([InlineKeyboardButton("📐 Next: Aspect Ratio →", callback_data=f"rs:{uid}:next_aspect"),
                 InlineKeyboardButton("❌ Cancel",               callback_data=f"rs:{uid}:cancel")])
    return InlineKeyboardMarkup(rows)


def _kb_step3(s: dict) -> InlineKeyboardMarkup:
    uid  = s["user_id"]
    rows = []
    for asp, label in [
        ("none",    "🔒 None (Keep as-is)"), ("21:9", "📽 21:9 Aspect"),
        ("16:9",    "🖥️ 16:9 Aspect"),       ("4:5",  "📱 4:5 Aspect"),
        ("bars",    "⬛ 16:9 Black Bars"),    ("zoom", "🔍 16:9 Zoom"),
        ("1280x720","📐 scale=1280:720"),
    ]:
        sel = "✅ " if s["aspect"] == asp else ""
        rows.append([InlineKeyboardButton(f"{sel}{label}", callback_data=f"rs:{uid}:aspect:{asp}")])
    rows.append([InlineKeyboardButton("◀️ Quality/Size",   callback_data=f"rs:{uid}:back_step2")])
    rows.append([InlineKeyboardButton("▶️ Start Recording", callback_data=f"rs:{uid}:start"),
                 InlineKeyboardButton("❌ Cancel",          callback_data=f"rs:{uid}:cancel")])
    return InlineKeyboardMarkup(rows)


def _build_vf_and_codec(setup: dict) -> tuple[list[str], bool]:
    quality  = setup["quality"]
    aspect   = setup["aspect"]
    wm_on    = setup["watermark_on"]
    needs_encode = quality != "original" or aspect != "none" or wm_on
    vf: list[str] = []

    if aspect == "21:9":
        vf.append("crop=ih*21/9:ih")
    elif aspect == "16:9":
        vf.append("crop=min(iw\\,ih*16/9):min(ih\\,iw*9/16)")
    elif aspect == "4:5":
        vf.append("crop=ih*4/5:ih")
    elif aspect == "bars":
        vf += ["scale=-2:720", "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black"]
    elif aspect == "zoom":
        vf += ["scale=1920:1080:force_original_aspect_ratio=increase", "crop=1920:1080"]
    elif aspect == "1280x720":
        vf.append("scale=1280:720")

    res_map = {"480": "-2:480", "576": "-2:576", "640": "-2:640", "720": "-2:720", "1080": "-2:1080"}
    if quality in res_map and aspect not in ("bars", "zoom", "1280x720"):
        vf.append(f"scale={res_map[quality]}")

    if wm_on:
        pos_map = {
            "top_left":    "x=10:y=10",          "top_right":    "x=w-tw-10:y=10",
            "center":      "x=(w-tw)/2:y=(h-th)/2",
            "bottom_left": "x=10:y=h-th-10",     "bottom_right": "x=w-tw-10:y=h-th-10",
        }
        xy   = pos_map.get(setup["watermark_pos"], "x=10:y=10")
        safe = ((setup.get("watermark_text") or config.BRAND_TITLE)
                .replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:"))
        vf.append(f"drawtext=text='{safe}':fontsize=28:fontcolor=white"
                  f":box=1:boxcolor=black@0.4:boxborderw=4:{xy}")

    post: list[str] = []
    at = setup["audio_track"]
    if at == 0:
        post += ["-map", "0:v", "-map", "0:a"]
    else:
        post += ["-map", "0:v", "-map", f"0:a:{at - 1}"]

    if needs_encode:
        if vf:
            post += ["-vf", ",".join(vf)]
        crf = "23" if quality in ("480", "576", "640") else "21"
        abr = "192k" if quality == "1080" else "128k"
        post += ["-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
                 "-c:a", "aac", "-b:a", abr]
    else:
        post += ["-c:v", "copy", "-c:a", "copy"]
    return post, needs_encode

# ---------------------------------------------------------------------------
# Audio track probe (for the wizard's first step)
# ---------------------------------------------------------------------------

async def _probe_audio_tracks(url: str, timeout_sec: int = 15) -> list:
    """Probe a stream URL and return list of audio track dicts."""
    cmd = shlex.split(
        f'ffprobe -v quiet -hide_banner -print_format json '
        f'-show_streams -select_streams a '
        f'-user_agent "{DEFAULT_USER_AGENT}" '
        f'-probesize 5000000 -analyzeduration 5000000 '
        f'-i {shlex.quote(url)}'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        data = json.loads(stdout.decode(errors="ignore") or "{}")
        tracks = []
        for s in data.get("streams", []):
            tags  = s.get("tags") or {}
            lang  = (tags.get("language") or "und").lower()[:3]
            title = tags.get("title") or tags.get("handler_name") or ""
            tracks.append({
                "stream_idx": len(tracks),   # 0-based position among audio streams
                "lang":       lang,
                "title":      title,
                "channels":   s.get("channels", 2),
                "codec":      s.get("codec_name", "?"),
            })
        return tracks
    except asyncio.TimeoutError:
        LOG.warning(f"Audio probe timed out for {url}")
        return []
    except Exception as e:
        LOG.warning(f"Audio probe failed for {url}: {e}")
        return []


def _audio_track_label(track: dict) -> str:
    lang  = track["lang"]
    label = LANG_LABEL.get(lang, lang.upper())
    title = (track.get("title") or "").strip()
    ch    = track.get("channels", 2)
    ch_s  = "stereo" if ch == 2 else ("mono" if ch == 1 else f"{ch}ch")
    if title and title.lower() != label.lower():
        return f"{label} ({title}) [{ch_s}]"
    return f"{label} [{ch_s}]"


def _kb_audio_step(setup: dict) -> InlineKeyboardMarkup:
    uid    = setup["user_id"]
    tracks = setup.get("detected_audio_tracks", [])
    sel    = setup["audio_track"]   # 0 = all, 1 = first track, 2 = second, …
    rows   = []

    all_icon = "✅ " if sel == 0 else ""
    rows.append([InlineKeyboardButton(
        f"{all_icon}🎵 All Tracks",
        callback_data=f"rs:{uid}:audio_select:0"
    )])

    for i, t in enumerate(tracks, 1):
        icon = "✅ " if sel == i else ""
        rows.append([InlineKeyboardButton(
            f"{icon}🎙 {_audio_track_label(t)}",
            callback_data=f"rs:{uid}:audio_select:{i}"
        )])

    rows.append([
        InlineKeyboardButton("📐 Next: Watermark →", callback_data=f"rs:{uid}:next_wm"),
        InlineKeyboardButton("❌ Cancel",             callback_data=f"rs:{uid}:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _audio_step_text(setup: dict) -> str:
    tracks = setup.get("detected_audio_tracks", [])
    url    = setup.get("url", "")
    lines  = [
        "**🎙 Step 1 — Audio Track**\n",
        f"📡 URL: `{url[:80]}{'…' if len(url) > 80 else ''}`",
        f"Duration: `{setup['timestamp']}`  |  File: `{setup['filename']}`\n",
    ]
    if tracks:
        lines.append(f"Found **{len(tracks)}** audio track(s):\n")
        for i, t in enumerate(tracks, 1):
            lines.append(f"`{i}.` {_audio_track_label(t)}")
    else:
        lines.append("_No audio track info (stream will include all audio)._")
    lines.append("\n👇 Choose an audio track, then tap **Next**:")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# handle_record — parse params, probe audio, show pre-recording setup wizard
# ---------------------------------------------------------------------------

async def handle_record(client: Client, message: Message):
    user_id = message.from_user.id
    params  = " ".join(message.command[1:])
    parts   = params.split(" ", 2)
    if len(parts) < 2:
        return await message.reply_text("Bad arguments. Use `/rec <link> HH:MM:SS <filename>`.")
    url          = parts[0]
    timestamp    = parts[1]
    raw_filename = parts[2].strip() if len(parts) > 2 else config.DEFAULT_FILENAME
    for bad in '/\\:*?"<>|':
        raw_filename = raw_filename.replace(bad, "_")

    dur_sec = time_to_seconds(timestamp)
    setup: dict = {
        "user_id":        user_id,
        "chat_id":        message.chat.id,
        "orig_msg":       message,
        "url":            url,
        "timestamp":      timestamp,
        "duration_sec":   dur_sec,
        "filename":       raw_filename,
        "watermark_on":   False,
        "watermark_pos":  "bottom_right",
        "watermark_text": config.BRAND_TITLE,
        "audio_track":    0,
        "auto_mode":      False,
        "quality":        "original",
        "aspect":         "none",
        "step":           0,
        "detected_audio_tracks": [],
    }
    rec_setup_sessions[user_id] = setup

    # Probe audio tracks from the stream before showing the wizard
    probe_msg = await message.reply_text(
        "🔍 **Probing stream for audio tracks…**\n\n"
        f"`{url[:90]}{'…' if len(url) > 90 else ''}`"
    )
    setup["setup_msg_id"] = probe_msg.id

    # Effective URL after redirect/page extraction
    probe = await probe_stream(url)
    effective_url = probe["final_url"]
    tracks = await _probe_audio_tracks(effective_url)
    setup["detected_audio_tracks"] = tracks
    setup["effective_url"]         = effective_url

    await probe_msg.edit_text(_audio_step_text(setup), reply_markup=_kb_audio_step(setup))

# ---------------------------------------------------------------------------
# do_record — actual FFmpeg recording (called after wizard confirmation)
# ---------------------------------------------------------------------------

async def do_record(client: Client, query: CallbackQuery, setup: dict):
    user_id   = setup["user_id"]
    chat_id   = setup["chat_id"]
    url       = setup["url"]
    timestamp = setup["timestamp"]
    filename  = setup["filename"]
    orig_msg  = setup.get("orig_msg")
    rec_id    = int(time.time() * 1000) % 10**9   # unique per-recording slot

    # ── Quota check — non-owners must have Rec credits ───────────────────────
    if not is_owner(user_id):
        ok, quota_msg = limit_system.use_rec(user_id)
        if not ok:
            return await client.send_message(chat_id, quota_msg)

    save_dir: Optional[str]   = None
    video_path: Optional[str] = None

    msg = await client.send_message(chat_id, "⚙️ Initializing recording...")

    try:
        raw_filename = filename
        for bad in '/\\:*?"<>|':
            raw_filename = raw_filename.replace(bad, "_")
        mkv_filename = f"{raw_filename}.mkv"
        save_dir     = join(config.DOWNLOAD_DIRECTORY, str(int(time.time())))
        os.makedirs(save_dir, exist_ok=True)
        video_path   = join(save_dir, mkv_filename)

        recording_start = time.time()
        duration        = time_to_seconds(timestamp)

        rec_entry = {
            "start":         recording_start,
            "status":        {
                "filename": raw_filename, "target": timestamp,
                "progress": "00:00:00", "save_dir": save_dir,
            },
            "ffmpeg_pid":    None,
            "progress_task": None,
        }
        active_recs.setdefault(user_id, {})[rec_id] = rec_entry

        async def update_recording_progress():
            while rec_id in active_recs.get(user_id, {}):
                if (user_id, rec_id) in cancelled_recs:
                    break
                elapsed = time.time() - recording_start
                pct     = min((elapsed / duration) * 100, 100) if duration > 0 else 0
                bar     = "●" * int(20 * pct // 100) + "○" * (20 - int(20 * pct // 100))
                task_id = hex(rec_id)[2:10]
                active_recs[user_id][rec_id]["status"]["progress"] = TimeFormatter(int(elapsed * 1000))
                q_str  = f"{setup['quality']}p" if setup["quality"] != "original" else "Original"
                wm_str = setup["watermark_pos"].replace("_", " ").title() if setup["watermark_on"] else "Off"
                slot_n = list(active_recs.get(user_id, {}).keys()).index(rec_id) + 1
                try:
                    await msg.edit_text(
                        f"🎬 **Recording #{slot_n} in Progress...**\n\n"
                        f"📡 Stream Capture\n"
                        f"[{bar}]  {pct:.1f}%\n"
                        f"⏱ Time  : {TimeFormatter(int(elapsed*1000))} / {TimeFormatter(duration*1000)}\n"
                        f"🆔 Task  : {task_id}\n\n"
                        f"📺 Quality: `{q_str}` | 💧 WM: `{wm_str}`\n"
                        f"⏹ /cancelme"
                    )
                except Exception:
                    pass
                await asyncio.sleep(5)

        progress_task = asyncio.create_task(update_recording_progress())
        rec_entry["progress_task"] = progress_task

        # Re-use probe result from wizard if available (avoids double-probe)
        if setup.get("effective_url"):
            effective_url  = setup["effective_url"]
            is_hls         = effective_url != url or ".m3u8" in effective_url.lower()
            extracted_from = None
            await msg.edit_text("▶️ Starting recording...")
        else:
            await msg.edit_text("🔍 Probing stream...")
            probe          = await probe_stream(url)
            effective_url  = probe["final_url"]
            is_hls         = probe["is_hls"]
            extracted_from = probe.get("extracted_from")
            LOG.info(f"Probe uid={user_id}: hls={is_hls}, changed={'yes' if effective_url!=url else 'no'}")
            if extracted_from:
                await msg.edit_text("Found embedded HLS stream — starting recording...")
            else:
                await msg.edit_text("▶️ Starting recording...")

        referer    = probe["referer"] if not setup.get("effective_url") else ""
        user_agent = probe.get("user_agent", DEFAULT_USER_AGENT) if not setup.get("effective_url") else DEFAULT_USER_AGENT
        args: list[str] = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y",
            "-user_agent", user_agent,
        ]
        if referer:
            args += ["-headers", f"Referer: {referer}\r\n"]
        if is_hls:
            args += ["-f", "hls", "-allowed_extensions", "ALL"]
        args += ["-probesize", "10000000", "-analyzeduration", "15000000", "-i", effective_url]
        extra_post, re_encodes = _build_vf_and_codec(setup)
        args += extra_post
        args += ["-t", str(timestamp), video_path]

        ffmpeg_process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        rec_entry["ffmpeg_pid"] = ffmpeg_process.pid
        LOG.info(f"FFmpeg pid={ffmpeg_process.pid} user={user_id} rec={rec_id} re_encode={re_encodes}")

        _stdout, stderr = await ffmpeg_process.communicate()
        retcode = ffmpeg_process.returncode
        rec_entry.pop("ffmpeg_pid", None)
        pt = rec_entry.pop("progress_task", None)
        if pt:
            pt.cancel()

        was_cancelled = (user_id, rec_id) in cancelled_recs
        if retcode != 0 and not was_cancelled:
            err_tail = stderr.decode(errors="ignore").strip()
            if len(err_tail) > 1500:
                err_tail = "..." + err_tail[-1500:]
            if not err_tail:
                err_tail = f"FFmpeg exited with code {retcode} (no stderr)."
            raise Exception(f"FFmpeg error:\n{err_tail}")

        if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
            if was_cancelled:
                await msg.edit_text("Recording cancelled — no video recorded.")
                return
            raise Exception("No video file created or file is empty.")

        await msg.edit_text("🖼 Generating thumbnail...")
        dur = await get_duration_ffmpeg(video_path) or time_to_seconds(timestamp)

        fixed = join(save_dir, f"fixed_{mkv_filename}")
        rc, _o, err = await runcmd(
            f'ffmpeg -hide_banner -loglevel error -nostats -y '
            f'-i {shlex.quote(video_path)} -map 0 -c copy '
            f'-metadata creation_time="{time.strftime("%Y-%m-%dT%H:%M:%S")}" '
            f'{shlex.quote(fixed)}'
        )
        if rc == 0:
            os.replace(fixed, video_path)
        else:
            LOG.warning(f"Metadata fix failed: {err}")

        rand_sec   = random.randint(5, max(dur - 5, 6))
        thumb_path = join(save_dir, "thumb.jpg")
        await runcmd(
            f'ffmpeg -hide_banner -loglevel error -nostats -y '
            f'-ss {rand_sec} -i {shlex.quote(video_path)} '
            f'-vframes 1 -q:v 2 {shlex.quote(thumb_path)}'
        )
        thumb_ok = os.path.exists(thumb_path)

        retention_note = f"_Auto-deleted from server after {_retention_label()}._"
        q_str = f"{setup['quality']}p" if setup["quality"] != "original" else "Original"
        asp_label = {
            "none": "None", "21:9": "21:9", "16:9": "16:9", "4:5": "4:5",
            "bars": "16:9 Bars", "zoom": "16:9 Zoom", "1280x720": "1280×720",
        }.get(setup["aspect"], setup["aspect"])
        audio_note = "All tracks" if setup["audio_track"] == 0 else f"Track {setup['audio_track']}"
        wm_note    = (f"💧 Watermark: `{setup['watermark_pos'].replace('_',' ').title()}`"
                      if setup["watermark_on"] else "")

        if was_cancelled:
            caption = (f"🎬 **{config.BRAND_TITLE}**\n\n"
                       f"Duration: `{TimeFormatter(dur * 1000)}`\nFormat: `MKV (partial)`\n"
                       f"Channel: @{config.SUPPORT_CHANNEL}\n\n"
                       f"_Recording was cancelled — partial file attached._\n{retention_note}")
        else:
            caption = (f"🎬 **{config.BRAND_TITLE}**\n\n"
                       f"Duration: `{TimeFormatter(dur * 1000)}`\n"
                       f"Quality: `{q_str}` | Aspect: `{asp_label}`\n"
                       f"Audio: `{audio_note}`\n"
                       + (f"{wm_note}\n" if wm_note else "")
                       + f"Channel: @{config.SUPPORT_CHANNEL}\n\n{retention_note}")

        send_target  = orig_msg or (query.message if query else msg)
        size_bytes   = os.path.getsize(video_path)
        size_str     = (f"{size_bytes / (1024**3):.2f} GB" if size_bytes >= 1024**3
                        else f"{size_bytes / (1024**2):.1f} MB")
        partial_note = "\n_⚠️ Partial recording (cancelled)_" if was_cancelled else ""

        pending_uploads[(user_id, rec_id)] = {
            "video_path":    video_path,
            "thumb_path":    thumb_path if thumb_ok else None,
            "caption":       caption,
            "dur":           dur,
            "chat_id":       chat_id,
            "save_dir":      save_dir,
            "was_cancelled": was_cancelled,
            "filename":      mkv_filename,
            "send_target":   send_target,
            "status_msg":    msg,
            "setup":         setup,
        }

        gd_enabled = gdrive._is_enabled() or gdrive.is_user_connected(user_id)
        buttons = [[InlineKeyboardButton(
            "✈️ Upload to Telegram", callback_data=f"upl:{user_id}:{rec_id}:tg"
        )]]
        if gd_enabled:
            buttons[0].append(InlineKeyboardButton(
                "🤖 Google Drive", callback_data=f"upl:{user_id}:{rec_id}:gd"
            ))
            buttons.append([InlineKeyboardButton(
                "📤 Upload to Both", callback_data=f"upl:{user_id}:{rec_id}:both"
            )])
        kb = InlineKeyboardMarkup(buttons)

        await msg.edit_text(
            f"🎉 **Recording Successfully Completed!**\n\n"
            f"🎬 File Name: `{mkv_filename}`\n"
            f"📦 Size: `{size_str}`\n"
            f"⏱ Duration: `{TimeFormatter(dur * 1000)}`"
            f"{partial_note}\n\n"
            "Kripya choose karein aap is file ko kahan upload karna chahte hain:",
            reply_markup=kb,
        )

    except Exception as e:
        LOG.error(f"do_record error uid={user_id}: {e}")
        err_text = str(e)
        if len(err_text) > 3500:
            err_text = "...[truncated]...\n" + err_text[-3500:]
        try:
            if (user_id, rec_id) not in cancelled_recs:
                await msg.edit_text(f"**Recording failed.**\n\n`{err_text}`")
            if (user_id, rec_id) not in cancelled_recs and save_dir and os.path.exists(save_dir):
                _safe_rmtree(save_dir)
        except Exception as exc:
            LOG.error(f"Failed to edit error message: {exc}")
    finally:
        if user_id in active_recs:
            active_recs[user_id].pop(rec_id, None)
            if not active_recs[user_id]:
                del active_recs[user_id]
        cancelled_recs.discard((user_id, rec_id))

# ---------------------------------------------------------------------------
# OTT downloader helpers
# ---------------------------------------------------------------------------

_NETSCAPE_HEADER       = "# Netscape HTTP Cookie File"
_MAX_COOKIE_FILE_BYTES = 2 * 1024 * 1024
_COOKIE_PROMPT_TTL_SEC = 5 * 60


def _user_cookies_path(user_id: int) -> str:
    return join(config.COOKIES_DIRECTORY, f"{user_id}.txt")


def _user_has_cookies(user_id: int) -> bool:
    path = _user_cookies_path(user_id)
    return os.path.exists(path) and os.path.getsize(path) > 0


def _cookies_summary(user_id: int) -> str:
    path = _user_cookies_path(user_id)
    if not os.path.exists(path):
        return "No cookies on file."
    try:
        size  = os.path.getsize(path)
        mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=pytz.timezone(config.TIMEZONE))
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [ln for ln in f if ln.strip() and not ln.startswith("#")]
        hosts = sorted({ln.split("\t", 1)[0].lstrip(".") for ln in lines if "\t" in ln})
        host_preview = ", ".join(hosts[:6]) + ("…" if len(hosts) > 6 else "")
        return (f"Cookies are set.\n• Cookie lines: `{len(lines)}`\n"
                f"• File size: `{size} bytes`\n• Hosts: `{host_preview or 'unknown'}`\n"
                f"• Uploaded: `{mtime.strftime('%Y-%m-%d %H:%M %Z')}`")
    except Exception as e:
        return f"Cookies are set, but couldn't be read ({e})."


def _ott_progress_text(state: dict) -> str:
    pct     = state.get("percent", 0.0)
    bar_len = 20
    filled  = max(0, min(bar_len, int(round(pct / 100 * bar_len))))
    bar     = "●" * filled + "○" * (bar_len - filled)

    def _fmt_bytes(n):
        if n is None: return "?"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024: return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"

    def _fmt_eta(s):
        if s is None or s < 0: return "?"
        s = int(s)
        h, rem = divmod(s, 3600); m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

    speed  = state.get("speed")
    title  = state.get("title") or "Downloading"
    return (f"📡 **{title[:80]}**\n\nStatus: `{state.get('status', '?')}`\n"
            f"`{bar}` `{pct:5.1f}%`\n"
            f"💾 Size: `{_fmt_bytes(state.get('downloaded'))}` / `{_fmt_bytes(state.get('total'))}`\n"
            f"⚡ Speed: `{f'{_fmt_bytes(speed)}/s' if speed else '?'}`\n"
            f"⏳ ETA: `{_fmt_eta(state.get('eta'))}`")


async def handle_ott_download(client: Client, message: Message):
    user_id  = message.from_user.id
    msg      = await message.reply_text("Initializing download...")
    save_dir: Optional[str] = None
    url      = ""
    try:
        parts        = message.text.split(maxsplit=2)
        url          = parts[1].strip()
        raw_filename = parts[2].strip() if len(parts) > 2 else ""
        for bad in '/\\:*?"<>|':
            raw_filename = raw_filename.replace(bad, "_")

        save_dir = join(config.DOWNLOAD_DIRECTORY, f"ott_{int(time.time())}")
        os.makedirs(save_dir, exist_ok=True)
        user_tasks[user_id]  = time.time()
        user_status[user_id] = {"id": int(user_tasks[user_id]), "user_id": user_id,
                                "filename": raw_filename or "(auto)", "duration_str": "—",
                                "channel_name": "OTT", "url": url, "progress": "0%"}
        state: dict = {"status": "starting", "percent": 0.0, "downloaded": 0,
                       "total": None, "speed": None, "eta": None, "title": "Resolving..."}
        ott_progress[user_id] = state

        def _hook(d: dict):
            if user_id in cancelled_users:
                raise yt_dlp.utils.DownloadCancelled("Cancelled by user.")
            st = d.get("status")
            if st == "downloading":
                state["status"]     = "downloading"
                state["downloaded"] = d.get("downloaded_bytes") or 0
                state["total"]      = d.get("total_bytes") or d.get("total_bytes_estimate")
                if state["total"]:
                    state["percent"] = state["downloaded"] * 100 / state["total"]
                state["speed"] = d.get("speed")
                state["eta"]   = d.get("eta")
                info = d.get("info_dict") or {}
                if info.get("title"):
                    state["title"] = info["title"]
            elif st == "finished":
                state["status"]  = "finalizing"
                state["percent"] = 100.0

        async def watcher():
            last_text = ""
            while user_id in user_tasks:
                if user_id in cancelled_users:
                    return
                txt = _ott_progress_text(state)
                if txt != last_text:
                    try:
                        await msg.edit_text(txt)
                        last_text = txt
                    except Exception:
                        pass
                if user_status.get(user_id):
                    user_status[user_id]["progress"] = f"{state['percent']:.1f}%"
                await asyncio.sleep(4)

        watcher_task           = asyncio.create_task(watcher())
        progress_tasks[user_id] = watcher_task

        outtmpl  = join(save_dir, (raw_filename or "%(title).200B") + ".%(ext)s")
        ydl_opts = {
            "outtmpl": outtmpl, "format": "bv*+ba/b", "merge_output_format": "mkv",
            "noplaylist": True, "quiet": True, "no_warnings": True,
            "concurrent_fragment_downloads": 4, "retries": 5, "fragment_retries": 5,
            "progress_hooks": [_hook], "user_agent": DEFAULT_USER_AGENT, "trim_file_name": 200,
            "geo_bypass": True, "geo_bypass_country": "IN", "verbose": False,
            "extractor_args": {
                "hotstar":  {"video_resolution": ["max"]},
                "sonyliv":  {"prefer_subs_lang": ["hi"]},
                "youtube":  {"player_client": ["android", "web"]},
            },
        }
        if _user_has_cookies(user_id):
            ydl_opts["cookiefile"] = _user_cookies_path(user_id)

        def _run_ydl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if "requested_downloads" in info and info["requested_downloads"]:
                    info["_final_filepath"] = info["requested_downloads"][0]["filepath"]
                else:
                    info["_final_filepath"] = ydl.prepare_filename(info)
                return info

        try:
            info = await asyncio.to_thread(_run_ydl)
        except yt_dlp.utils.DownloadCancelled:
            await msg.edit_text("Download cancelled.")
            return

        watcher_task.cancel()
        progress_tasks.pop(user_id, None)

        video_path = info.get("_final_filepath")
        if not video_path or not os.path.exists(video_path):
            raise Exception("yt-dlp finished but the output file is missing.")

        await msg.edit_text("Download finished — preparing upload...")
        title    = info.get("title") or os.path.basename(video_path)
        duration = int(info.get("duration") or 0)

        thumb_path = None
        if duration > 6:
            ts         = random.randint(2, max(duration - 2, 3))
            cand_thumb = join(save_dir, "thumb.jpg")
            rc, _o, _e = await runcmd(
                f'ffmpeg -hide_banner -loglevel error -nostats -y '
                f'-ss {ts} -i "{video_path}" -vframes 1 -q:v 2 "{cand_thumb}"')
            if rc == 0 and os.path.exists(cand_thumb):
                thumb_path = cand_thumb

        retention_note = (f"_The video is automatically deleted from the server after "
                          f"{_retention_label()}._")
        caption = (f"🎬 **{config.BRAND_TITLE}**\n\n"
                   f"Duration: `{TimeFormatter(duration * 1000)}`\n"
                   f"Source: `{(info.get('extractor_key') or info.get('extractor') or 'OTT')}`\n"
                   f"Channel: @{config.SUPPORT_CHANNEL}\n\n{retention_note}")

        start_time = time.time()
        await message.reply_video(
            video=video_path, caption=caption, duration=duration or None,
            thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
            progress=progress_for_pyrogram,
            progress_args=(message, start_time, msg, save_dir, False),
        )
        asyncio.create_task(gdrive.upload_and_notify(
            client, message.chat.id, video_path, os.path.basename(video_path)
        ))
        if save_dir and os.path.exists(save_dir):
            schedule_retention_cleanup(save_dir)

    except Exception as e:
        LOG.error(f"Error in handle_ott_download: {e}")
        try:
            err_text  = str(e)
            err_lower = err_text.lower()
            url_lower = (url or "").lower()
            hints = []
            if any(k in err_lower for k in ("drm", "widevine", "playready", "encrypted")):
                hints.append("🔒 **DRM-protected content**. No tool can download this — try free episodes only.")
            if any(k in err_lower for k in ("login required", "subscription", "premium", "sign in")):
                hints.append("🔑 **Login / subscription needed.** Run /set_cookies with a fresh `cookies.txt`.")
            if any(k in err_lower for k in ("geo", "not available in your", "403", "forbidden")):
                hints.append("🌐 **Geo-blocked** — server IP is outside India.")
            if any(k in err_lower for k in ("expired", "session", "invalid token", "401")):
                hints.append("⏱ **Cookies expired.** Re-export `cookies.txt` and run /set_cookies again.")
            if len(err_text) > 2500:
                err_text = "...[truncated]...\n" + err_text[-2500:]
            hint_block = ("\n\n" + "\n\n".join(hints)) if hints else ""
            await msg.edit_text(f"**Download failed.**\n\n`{err_text}`{hint_block}")
        except Exception:
            pass
        if save_dir and os.path.exists(save_dir):
            _safe_rmtree(save_dir)
    finally:
        ott_progress.pop(user_id, None)
        user_status.pop(user_id, None)
        user_tasks.pop(user_id, None)
        progress_tasks.pop(user_id, None)
        cancelled_users.discard(user_id)

# ---------------------------------------------------------------------------
# Compress helpers
# ---------------------------------------------------------------------------

COMPRESS_SIZE_OPTIONS_MB = [300, 400, 500, 600, 800]
COMPRESS_RES_OPTIONS = [
    ("140p", "h140"), ("240p", "h240"), ("360p", "h360"), ("480p", "h480"),
    ("576p", "h576"), ("640p", "h640"), ("720p", "h720"),
    ("1080p HD", "h1080hevc"), ("1080p", "h1080"), ("HQ", "hq"), ("2K", "h1440"), ("3K", "h2160"),
]
COMPRESS_RES_CONFIG = {
    "h140":      {"height": 140,  "codec": "libx264", "label": "140p"},
    "h240":      {"height": 240,  "codec": "libx264", "label": "240p"},
    "h360":      {"height": 360,  "codec": "libx264", "label": "360p"},
    "h480":      {"height": 480,  "codec": "libx264", "label": "480p"},
    "h576":      {"height": 576,  "codec": "libx264", "label": "576p"},
    "h640":      {"height": 640,  "codec": "libx264", "label": "640p"},
    "h720":      {"height": 720,  "codec": "libx264", "label": "720p"},
    "h1080hevc": {"height": 1080, "codec": "libx265", "label": "1080p HD (HEVC)"},
    "h1080":     {"height": 1080, "codec": "libx264", "label": "1080p"},
    "h1440":     {"height": 1440, "codec": "libx264", "label": "2K"},
    "h2160":     {"height": 2160, "codec": "libx264", "label": "3K"},
    "hq":        {"height": 0,    "codec": "libx264", "label": "HQ (original)"},
}
LANG_LABEL = {
    "hin": "Hindi", "tam": "Tamil", "tel": "Telugu", "mal": "Malayalam",
    "kan": "Kannada", "mar": "Marathi", "ben": "Bengali", "guj": "Gujarati",
    "pan": "Punjabi", "ori": "Odia", "asm": "Assamese", "urd": "Urdu",
    "eng": "English", "und": "Untagged", "multi": "Multi (all)",
}
COMPRESS_LANG_PRESET = ["hin", "tam", "tel", "mal", "kan", "mar", "eng", "multi"]


def _compress_menu(state: dict) -> InlineKeyboardMarkup:
    rows    = []
    sel_size = state.get("size_mb")
    rows.append([InlineKeyboardButton(f"{'✓ ' if sel_size == s else ''}{s} MB",
                                      callback_data=f"cmp:size:{s}")
                 for s in COMPRESS_SIZE_OPTIONS_MB])
    sel_res     = state.get("res_key")
    res_buttons = [InlineKeyboardButton(f"{'✓ ' if sel_res == k else ''}{lbl}",
                                        callback_data=f"cmp:res:{k}")
                   for lbl, k in COMPRESS_RES_OPTIONS]
    for i in range(0, len(res_buttons), 4):
        rows.append(res_buttons[i:i + 4])
    sel_langs = set(state.get("langs", []))
    available = state.get("available_langs", [])
    visible   = [l for l in COMPRESS_LANG_PRESET if l == "multi" or l in available]
    for extra in available:
        if extra not in COMPRESS_LANG_PRESET and extra not in visible:
            visible.append(extra)
    if not visible:
        visible = ["multi"]
    lang_buttons = [InlineKeyboardButton(f"{'✓ ' if l in sel_langs else ''}{LANG_LABEL.get(l, l.upper())}",
                                         callback_data=f"cmp:lang:{l}")
                    for l in visible]
    for i in range(0, len(lang_buttons), 3):
        rows.append(lang_buttons[i:i + 3])
    rows.append([InlineKeyboardButton("▶ Start", callback_data="cmp:start"),
                 InlineKeyboardButton("✖ Cancel", callback_data="cmp:cancel")])
    return InlineKeyboardMarkup(rows)


def _compress_status_text(state: dict) -> str:
    duration   = state.get("duration", 0)
    src_h      = state.get("video_height", 0)
    avail      = state.get("available_langs", [])
    avail_text = (", ".join(LANG_LABEL.get(l, l.upper()) for l in avail)
                  if avail else "(no language tags)")
    sel_size   = state.get("size_mb")
    sel_res    = state.get("res_key")
    res_label  = COMPRESS_RES_CONFIG[sel_res]["label"] if sel_res else "—"
    sel_langs  = state.get("langs") or []
    langs_text = ", ".join(LANG_LABEL.get(l, l.upper()) for l in sel_langs) or "—"
    return (f"**🗜 Video Compressor**\n\nSource: `{TimeFormatter(int(duration * 1000))}`"
            f" • `{src_h}p` • `{len(state.get('audio_streams', []))}` audio track(s)\n"
            f"Available audio langs: {avail_text}\n\n**Choose options:**\n"
            f"• Target size: `{sel_size or '—'} MB`\n• Resolution / codec: `{res_label}`\n"
            f"• Audio: `{langs_text}`\n\n"
            f"_Default audio is **Hindi** when present. Tap **Multi** to keep all tracks._")


def _compress_progress_text(pct, done_sec, dur_sec, size_bytes, target_mb, speed_mult):
    bar_len  = 20
    filled   = max(0, min(bar_len, int(round(pct / 100 * bar_len))))
    bar      = "●" * filled + "○" * (bar_len - filled)
    size_mb  = size_bytes / (1024 * 1024)
    remaining_sec = max(0.0, (dur_sec - done_sec) / max(0.05, speed_mult))
    return (f"📡 **Compressing**\n\nStatus: `encoding`\n`{bar}` `{pct:5.1f}%`\n"
            f"💾 Size: `{size_mb:.1f} MB` / target `{target_mb} MB`\n"
            f"⚡ Speed: `{speed_mult:.2f}x`\n"
            f"⏳ ETA: `{TimeFormatter(int(remaining_sec * 1000))}`")


async def run_compress(client: Client, status_msg: Message, state: dict):
    user_id  = state["user_id"]
    save_dir = state["save_dir"]
    src      = state["src_path"]
    duration = state["duration"]
    target_mb = state["size_mb"]
    res_cfg  = COMPRESS_RES_CONFIG[state["res_key"]]
    langs    = state["langs"]
    out_path = join(save_dir, f"compressed_{int(time.time())}.mkv")

    user_tasks[user_id]  = time.time()
    user_status[user_id] = {"id": int(user_tasks[user_id]), "user_id": user_id,
                            "filename": os.path.basename(out_path),
                            "duration_str": TimeFormatter(int(duration * 1000)),
                            "channel_name": "Compress", "url": "(local)", "progress": "0%"}

    if "multi" in langs:
        kept_audio = list(state["audio_streams"])
    else:
        kept_audio = [s for s in state["audio_streams"] if s["lang"] in langs]
    audio_kbps_per   = 128
    audio_total_kbps = audio_kbps_per * max(1, len(kept_audio) or 1)
    target_total_kbps = (target_mb * 8 * 1024) / max(1, duration)
    video_kbps       = max(80, int(target_total_kbps - audio_total_kbps - 32))

    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1", "-y", "-i", src, "-map", "0:v:0"]
    if "multi" in langs or not kept_audio:
        args += ["-map", "0:a?"]
    else:
        for s in kept_audio:
            args += ["-map", f"0:{s['index']}"]
    if res_cfg["height"] > 0:
        args += ["-vf", f"scale=-2:{res_cfg['height']}"]
    if state["res_key"] == "hq":
        args += ["-c:v", res_cfg["codec"], "-crf", "20", "-preset", "veryfast"]
    elif res_cfg["codec"] == "libx265":
        args += ["-c:v", "libx265", "-b:v", f"{video_kbps}k",
                 "-maxrate", f"{int(video_kbps * 1.4)}k", "-bufsize", f"{int(video_kbps * 2)}k",
                 "-preset", "fast", "-x265-params", "log-level=error", "-tag:v", "hvc1"]
    else:
        args += ["-c:v", "libx264", "-b:v", f"{video_kbps}k",
                 "-maxrate", f"{int(video_kbps * 1.4)}k", "-bufsize", f"{int(video_kbps * 2)}k",
                 "-preset", "veryfast"]
    args += ["-c:a", "aac", "-b:a", f"{audio_kbps_per}k", out_path]

    try:
        await status_msg.edit_text("Compressing — preparing...", reply_markup=None)
    except Exception:
        pass

    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    user_ffmpeg_pids[user_id] = proc.pid
    progress_state = {"out_time_us": 0, "total_size": 0, "speed": 1.0}

    async def read_progress():
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="ignore").strip()
            if "=" not in text:
                continue
            k, v = text.split("=", 1)
            if k == "out_time_us":
                try: progress_state["out_time_us"] = int(v)
                except ValueError: pass
            elif k == "total_size":
                try: progress_state["total_size"] = int(v)
                except ValueError: pass
            elif k == "speed" and v not in ("N/A", ""):
                try: progress_state["speed"] = float(v.rstrip("x"))
                except ValueError: pass

    async def render():
        last = ""
        while proc.returncode is None:
            if user_id in cancelled_users:
                return
            done_sec = progress_state["out_time_us"] / 1_000_000
            pct      = min(100.0, done_sec / max(1, duration) * 100)
            txt      = _compress_progress_text(pct, done_sec, duration,
                                               progress_state["total_size"], target_mb,
                                               progress_state["speed"])
            if txt != last:
                try:
                    await status_msg.edit_text(txt)
                    last = txt
                    if user_status.get(user_id):
                        user_status[user_id]["progress"] = f"{pct:.1f}%"
                except Exception:
                    pass
            await asyncio.sleep(4)

    progress_reader   = asyncio.create_task(read_progress())
    progress_renderer = asyncio.create_task(render())
    progress_tasks[user_id] = progress_renderer

    try:
        rc = await proc.wait()
        progress_reader.cancel()
        progress_renderer.cancel()
        user_ffmpeg_pids.pop(user_id, None)

        if user_id in cancelled_users:
            try: await status_msg.edit_text("Compress cancelled.")
            except Exception: pass
            _safe_rmtree(save_dir)
            return

        if rc != 0:
            err  = (await proc.stderr.read()).decode(errors="ignore")
            tail = err[-1500:] if len(err) > 1500 else err
            raise Exception(f"FFmpeg exit {rc}\n{tail}")
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise Exception("Output file missing or empty.")

        thumb     = join(save_dir, "thumb.jpg")
        thumb_at  = max(1, min(int(duration / 2), int(duration) - 1))
        await runcmd(f'ffmpeg -hide_banner -loglevel error -nostats -y '
                     f'-ss {thumb_at} -i {shlex.quote(out_path)} '
                     f'-vframes 1 -q:v 2 {shlex.quote(thumb)}')
        thumb_path   = thumb if os.path.exists(thumb) else None
        out_size_mb  = os.path.getsize(out_path) / (1024 * 1024)
        retention_note = (f"_The video is automatically deleted from the server after "
                          f"{_retention_label()}._")
        caption = (f"🎬 **{config.BRAND_TITLE}**\n\n"
                   f"Compressed: `{out_size_mb:.1f} MB` (target `{target_mb} MB`)\n"
                   f"Duration: `{TimeFormatter(int(duration * 1000))}`\n"
                   f"Resolution / codec: `{res_cfg['label']}`\n"
                   f"Audio: `{', '.join(LANG_LABEL.get(l, l.upper()) for l in langs)}`\n"
                   f"Channel: @{config.SUPPORT_CHANNEL}\n\n{retention_note}")

        upload_start = time.time()
        await status_msg.reply_video(
            video=out_path, caption=caption, duration=int(duration), thumb=thumb_path,
            progress=progress_for_pyrogram,
            progress_args=(status_msg, upload_start, status_msg, save_dir, False),
        )
        asyncio.create_task(gdrive.upload_and_notify(
            client, status_msg.chat.id, out_path, os.path.basename(out_path)
        ))
        try:
            await status_msg.edit_text(f"Compress done — uploaded `{out_size_mb:.1f} MB`.\n"
                                       f"Server copy auto-deletes in {_retention_label()}.")
        except Exception:
            pass
        if save_dir and os.path.exists(save_dir):
            schedule_retention_cleanup(save_dir)
    except Exception as e:
        LOG.error(f"Compress failed: {e}")
        err_text = str(e)
        if len(err_text) > 3500:
            err_text = "...[truncated]...\n" + err_text[-3500:]
        try: await status_msg.edit_text(f"**Compress failed.**\n\n`{err_text}`")
        except Exception: pass
        if save_dir and os.path.exists(save_dir):
            _safe_rmtree(save_dir)
    finally:
        compress_jobs.pop(user_id, None)
        user_status.pop(user_id, None)
        user_tasks.pop(user_id, None)
        user_ffmpeg_pids.pop(user_id, None)
        progress_tasks.pop(user_id, None)
        cancelled_users.discard(user_id)

# ---------------------------------------------------------------------------
# Reclink (headless Chromium)
# ---------------------------------------------------------------------------

def _resolve_chromium_path() -> Optional[str]:
    env_path = os.environ.get("CHROMIUM_PATH") or os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _looks_like_master_playlist(url: str) -> bool:
    u = url.lower()
    return ".m3u8" in u and any(k in u for k in ("master", "index", "playlist", "manifest"))


async def _extract_streams_with_chromium(page_url: str, timeout_sec: int = 30, log_cb=None) -> dict:
    from playwright.async_api import async_playwright
    log: list = []
    def L(msg: str):
        log.append(msg)
        if log_cb:
            try: log_cb(msg)
            except Exception: pass

    chromium_path = _resolve_chromium_path()
    L(f"Using Chromium: `{chromium_path or 'playwright default'}`")
    seen: dict = {}

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                     "--disable-blink-features=AutomationControlled", "--mute-audio"],
        }
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path
        try:
            browser = await p.chromium.launch(**launch_kwargs)
        except Exception as e:
            raise Exception(f"Could not launch Chromium: {e}")

        context = await browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 720}, ignore_https_errors=True,
        )
        page = await context.new_page()

        def on_request(req):
            try:
                u = req.url
                if ".m3u8" in u.lower() or ".mpd" in u.lower():
                    if u not in seen:
                        seen[u] = (dict(req.headers), _looks_like_master_playlist(u))
                        L(f"📡 captured `{u[:90]}{'…' if len(u) > 90 else ''}`")
            except Exception: pass

        page.on("request", on_request)
        try:
            L(f"Opening page (timeout {timeout_sec}s)...")
            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
            except Exception as nav_err:
                L(f"goto warn: {nav_err}")
            await page.wait_for_timeout(3500)
            for sel in ["button[aria-label*='play' i]", "button[title*='play' i]",
                        ".vjs-big-play-button", ".plyr__control--overlaid", ".jw-icon-display",
                        ".play-button", ".play-btn", "[class*='play' i][class*='button' i]", "video"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click(timeout=1500, force=True)
                        L(f"clicked `{sel}`")
                        await page.wait_for_timeout(1500)
                        if seen: break
                except Exception: pass
            await page.wait_for_timeout(2500)
            page_title = await page.title()
            final_url  = page.url
        finally:
            try: await browser.close()
            except Exception: pass

    streams = [{"url": u, "headers": h, "is_master": m} for u, (h, m) in seen.items()]
    streams.sort(key=lambda s: (not s["is_master"], len(s["url"])))
    L(f"Done. Found {len(streams)} stream(s).")
    return {"streams": streams, "page_title": page_title, "final_url": final_url, "log": log}

# ---------------------------------------------------------------------------
# Screenshot helpers
# ---------------------------------------------------------------------------

SS_MIN, SS_MAX, SS_PER_ROW = 1, 30, 5


def _ss_menu() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(str(n), callback_data=f"ss:n:{n}")
               for n in range(SS_MIN, SS_MAX + 1)]
    rows = [buttons[i:i + SS_PER_ROW] for i in range(0, len(buttons), SS_PER_ROW)]
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data="ss:cancel")])
    return InlineKeyboardMarkup(rows)


def _ss_menu_text(state: dict) -> str:
    duration = state.get("duration", 0)
    h        = state.get("video_height", 0)
    return (f"**📸 Screenshot Generator**\n\nSource: `{TimeFormatter(int(duration * 1000))}` • `{h}p`\n\n"
            f"**Select the number of screenshots**\n\n"
            f"✶ Click the Button of your choice 👇 {SS_MIN} to {SS_MAX}")


async def run_screenshots(client: Client, status_msg: Message, state: dict, n: int):
    user_id  = state["user_id"]
    save_dir = state["save_dir"]
    src      = state["src_path"]
    duration = state["duration"]

    user_tasks[user_id]  = time.time()
    user_status[user_id] = {"id": int(user_tasks[user_id]), "user_id": user_id,
                            "filename": f"screenshots-{n}",
                            "duration_str": TimeFormatter(int(duration * 1000)),
                            "channel_name": "Screenshots", "url": "(local)", "progress": "0%"}
    try:
        try: await status_msg.edit_text(f"📸 Generating **{n}** screenshot{'s' if n != 1 else ''}...",
                                        reply_markup=None)
        except Exception: pass

        edge      = max(1.0, duration * 0.02)
        usable    = max(1.0, duration - 2 * edge)
        timestamps = ([duration / 2] if n == 1
                      else [edge + i * (usable / (n - 1)) for i in range(n)])

        produced: list = []
        for idx, ts in enumerate(timestamps, 1):
            if user_id in cancelled_users: break
            out = join(save_dir, f"shot_{idx:02d}.jpg")
            cmd = (f"ffmpeg -hide_banner -loglevel error -nostats -y "
                   f"-ss {ts:.2f} -i {shlex.quote(src)} -vframes 1 -q:v 2 {shlex.quote(out)}")
            rc, _o, err = await runcmd(cmd)
            if rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                produced.append((out, ts))
            else:
                LOG.warning(f"ss frame {idx} failed: {err.strip()[:200]}")
            pct = idx / n * 100
            if user_status.get(user_id): user_status[user_id]["progress"] = f"{pct:.0f}%"
            if idx % max(1, n // 6) == 0 or idx == n:
                try: await status_msg.edit_text(f"📸 Generating **{n}** screenshot{'s' if n != 1 else ''}...\n"
                                                f"`{idx}` / `{n}` done")
                except Exception: pass

        if user_id in cancelled_users:
            try: await status_msg.edit_text("Screenshot job cancelled.")
            except Exception: pass
            _safe_rmtree(save_dir)
            return
        if not produced:
            raise Exception("FFmpeg produced no images.")

        try: await status_msg.edit_text(f"📤 Uploading {len(produced)} image(s)...")
        except Exception: pass

        first = True
        for chunk_start in range(0, len(produced), 10):
            chunk = produced[chunk_start:chunk_start + 10]
            media = []
            for i, (path, ts) in enumerate(chunk):
                global_idx = chunk_start + i + 1
                cap = (f"🎬 **{config.BRAND_TITLE}**\n\n"
                       f"📸 `{len(produced)}` screenshot{'s' if len(produced) != 1 else ''} • "
                       f"video `{TimeFormatter(int(duration * 1000))}`\n"
                       f"Channel: @{config.SUPPORT_CHANNEL}"
                       if first and i == 0
                       else f"`{global_idx:02d}` • `{TimeFormatter(int(ts * 1000))}`")
                media.append(InputMediaPhoto(media=path, caption=cap))
            await status_msg.reply_media_group(media=media)
            first = False

        try: await status_msg.edit_text(f"✅ Done — sent `{len(produced)}` screenshot"
                                        f"{'s' if len(produced) != 1 else ''}.\n"
                                        f"Server copy auto-deletes in {_retention_label()}.")
        except Exception: pass
        if save_dir and os.path.exists(save_dir): schedule_retention_cleanup(save_dir)
    except Exception as e:
        LOG.error(f"Screenshot job failed: {e}")
        err_text = str(e)
        if len(err_text) > 2500: err_text = "...[truncated]...\n" + err_text[-2500:]
        try: await status_msg.edit_text(f"**Screenshot job failed.**\n\n`{err_text}`")
        except Exception: pass
        if save_dir and os.path.exists(save_dir): _safe_rmtree(save_dir)
    finally:
        ss_jobs.pop(user_id, None)
        user_status.pop(user_id, None)
        user_tasks.pop(user_id, None)
        progress_tasks.pop(user_id, None)
        cancelled_users.discard(user_id)

# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

MERGE_MAX_VIDEOS  = 20
MERGE_SESSION_TTL = 30 * 60


def _merge_session_status(sess: dict) -> str:
    parts     = sess["videos"]
    total_dur = sum(p["duration"] for p in parts)
    lines = [f"🧩 **Merge session active** — `{len(parts)}` / `{MERGE_MAX_VIDEOS}` videos collected.",
             f"Total so far: `{TimeFormatter(int(total_dur * 1000))}`", ""]
    for i, p in enumerate(parts, 1):
        lines.append(f"`{i:02d}.` `{TimeFormatter(int(p['duration'] * 1000))}` • "
                     f"`{p.get('height') or '?'}p` • {p['codec_v']}")
    lines += ["", "Send more videos in order, then `/merge_done` to combine.",
              "Use `/merge_cancel` to discard."]
    return "\n".join(lines)


def _all_streams_compatible(videos: list) -> bool:
    if not videos: return False
    base = videos[0]
    for v in videos[1:]:
        if (v["codec_v"] != base["codec_v"] or v["codec_a"] != base["codec_a"]
                or v["height"] != base["height"] or v["width"] != base["width"]):
            return False
    return True


async def run_merge(client: Client, message: Message, sess: dict):
    user_id   = message.from_user.id
    save_dir  = sess["save_dir"]
    videos    = sess["videos"]
    out_path  = join(save_dir, f"merged_{int(time.time())}.mkv")
    total_dur = sum(v["duration"] for v in videos)

    user_tasks[user_id]  = time.time()
    user_status[user_id] = {"id": int(user_tasks[user_id]), "user_id": user_id,
                            "filename": os.path.basename(out_path),
                            "duration_str": TimeFormatter(int(total_dur * 1000)),
                            "channel_name": "Merge", "url": "(local)", "progress": "0%"}
    status = await message.reply_text(
        f"🧩 **Merging `{len(videos)}` videos** (`{TimeFormatter(int(total_dur * 1000))}` total)..."
    )
    try:
        compatible  = _all_streams_compatible(videos)
        used_method = None

        if compatible:
            list_path = join(save_dir, "concat_list.txt")
            with open(list_path, "w") as f:
                for v in videos:
                    safe = v["path"].replace("'", "'\\''")
                    f.write(f"file '{safe}'\n")
            await status.edit_text("🧩 Streams are compatible — using **fast** concat (lossless)...")
            rc, _o, err = await runcmd(
                f'ffmpeg -hide_banner -loglevel error -nostats -y '
                f'-f concat -safe 0 -i {shlex.quote(list_path)} -c copy {shlex.quote(out_path)}'
            )
            if rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                used_method = "fast (stream copy)"
            else:
                LOG.warning(f"concat demuxer failed, falling back: {err.strip()[:300]}")

        if not used_method:
            await status.edit_text(f"🧩 Re-encoding `{len(videos)}` videos (slower but always works)...")
            inputs = []
            for v in videos: inputs += ["-i", v["path"]]
            n = len(videos)
            filter_complex = ("".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
                              + f"concat=n={n}:v=1:a=1[outv][outa]")
            args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y",
                    *inputs, "-filter_complex", filter_complex,
                    "-map", "[outv]", "-map", "[outa]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                    "-c:a", "aac", "-b:a", "128k", out_path]
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            user_ffmpeg_pids[user_id] = proc.pid
            rc = await proc.wait()
            user_ffmpeg_pids.pop(user_id, None)
            err_out = (await proc.stderr.read()).decode(errors="ignore")
            if rc != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                raise Exception(f"FFmpeg merge (re-encode) failed.\n{err_out[-1500:]}")
            used_method = "re-encode (h264/aac)"

        thumb    = join(save_dir, "thumb.jpg")
        thumb_at = max(1, min(int(total_dur / 2), int(total_dur) - 1))
        await runcmd(f'ffmpeg -hide_banner -loglevel error -nostats -y '
                     f'-ss {thumb_at} -i {shlex.quote(out_path)} '
                     f'-vframes 1 -q:v 2 {shlex.quote(thumb)}')
        thumb_path   = thumb if os.path.exists(thumb) else None
        out_size_mb  = os.path.getsize(out_path) / (1024 * 1024)
        retention_note = (f"_The video is automatically deleted from the server after "
                          f"{_retention_label()}._")
        caption = (f"🎬 **{config.BRAND_TITLE}**\n\n"
                   f"🧩 Merged `{len(videos)}` videos\nDuration: `{TimeFormatter(int(total_dur * 1000))}`\n"
                   f"Size: `{out_size_mb:.1f} MB`\nMethod: `{used_method}`\n"
                   f"Channel: @{config.SUPPORT_CHANNEL}\n\n{retention_note}")

        upload_start = time.time()
        await status.reply_video(
            video=out_path, caption=caption, duration=int(total_dur), thumb=thumb_path,
            progress=progress_for_pyrogram,
            progress_args=(status, upload_start, status, save_dir, False),
        )
        asyncio.create_task(gdrive.upload_and_notify(
            client, status.chat.id, out_path, os.path.basename(out_path)
        ))
        try: await status.edit_text(f"🧩 Merge done — uploaded `{out_size_mb:.1f} MB`.\n"
                                    f"Server copy auto-deletes in {_retention_label()}.")
        except Exception: pass
        if save_dir and os.path.exists(save_dir): schedule_retention_cleanup(save_dir)
    except Exception as e:
        LOG.error(f"Merge failed: {e}")
        err_text = str(e)
        if len(err_text) > 2500: err_text = "...[truncated]...\n" + err_text[-2500:]
        try: await status.edit_text(f"**Merge failed.**\n\n`{err_text}`")
        except Exception: pass
        if save_dir and os.path.exists(save_dir): _safe_rmtree(save_dir)
    finally:
        merge_sessions.pop(user_id, None)
        user_status.pop(user_id, None)
        user_tasks.pop(user_id, None)
        user_ffmpeg_pids.pop(user_id, None)
        progress_tasks.pop(user_id, None)
        cancelled_users.discard(user_id)

# ---------------------------------------------------------------------------
# Upload progress callback (shared by all upload calls)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# /title — burn a text overlay onto a replied video
# ---------------------------------------------------------------------------

TITLE_POS_MAP = {
    "tl": ("↖️ Top-Left",     "x=10:y=10"),
    "tc": ("⬆️ Top-Center",   "x=(w-tw)/2:y=10"),
    "tr": ("↗️ Top-Right",    "x=w-tw-10:y=10"),
    "cc": ("⊙ Center",        "x=(w-tw)/2:y=(h-th)/2"),
    "bl": ("↙️ Bottom-Left",  "x=10:y=h-th-10"),
    "bc": ("⬇️ Bottom-Center","x=(w-tw)/2:y=h-th-10"),
    "br": ("↘️ Bottom-Right", "x=w-tw-10:y=h-th-10"),
}

# Videos >= this many seconds get "no title in last 3 minutes" treatment.
_TITLE_LONG_VIDEO_SEC  = 46 * 60   # 2760 s
_TITLE_FADE_BEFORE_SEC = 3  * 60   # 180 s


def _title_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↖️ Top-Left",     callback_data=f"ti:{uid}:pos:tl"),
         InlineKeyboardButton("⬆️ Top-Center",   callback_data=f"ti:{uid}:pos:tc"),
         InlineKeyboardButton("↗️ Top-Right",    callback_data=f"ti:{uid}:pos:tr")],
        [InlineKeyboardButton("⊙ Center",        callback_data=f"ti:{uid}:pos:cc")],
        [InlineKeyboardButton("↙️ Bottom-Left",  callback_data=f"ti:{uid}:pos:bl"),
         InlineKeyboardButton("⬇️ Bottom-Center",callback_data=f"ti:{uid}:pos:bc"),
         InlineKeyboardButton("↘️ Bottom-Right", callback_data=f"ti:{uid}:pos:br")],
        [InlineKeyboardButton("❌ Cancel",        callback_data=f"ti:{uid}:cancel")],
    ])


def _title_menu_text(state: dict) -> str:
    dur   = state["duration"]
    h     = state.get("video_height", 0)
    text  = state["title_text"]
    note  = ""
    if dur >= _TITLE_LONG_VIDEO_SEC:
        note = (f"\n\n⚠️ Video is `{TimeFormatter(int(dur*1000))}` long — "
                f"title will **not** appear in the last **3 minutes**.")
    return (f"**🔤 Title Overlay**\n\n"
            f"Source: `{TimeFormatter(int(dur*1000))}` • `{h}p`\n"
            f"Text: `{text[:60]}{'…' if len(text)>60 else ''}`{note}\n\n"
            f"**Choose text position:**")


async def run_title(client: Client, status_msg: Message, state: dict, pos_key: str):
    user_id  = state["user_id"]
    save_dir = state["save_dir"]
    src      = state["src_path"]
    duration = state["duration"]
    raw_text = state["title_text"]
    out_path = join(save_dir, f"titled_{int(time.time())}.mkv")

    user_tasks[user_id]  = time.time()
    user_status[user_id] = {
        "id":            int(user_tasks[user_id]),
        "user_id":       user_id,
        "filename":      os.path.basename(out_path),
        "duration_str":  TimeFormatter(int(duration * 1000)),
        "channel_name":  "Title",
        "url":           "(local)",
        "progress":      "0%",
    }

    pos_label, xy = TITLE_POS_MAP[pos_key]

    # Escape text for FFmpeg drawtext (backslash → \\, colon → \:, quote → \')
    safe_text = (raw_text
                 .replace("\\", "\\\\")
                 .replace(":",   "\\:")
                 .replace("'",   "\\'"))

    # For long videos: title disappears 3 minutes before the end
    if duration >= _TITLE_LONG_VIDEO_SEC:
        end_ts     = max(0.0, duration - _TITLE_FADE_BEFORE_SEC)
        enable_str = f":enable='lt(t,{end_ts:.1f})'"
    else:
        enable_str = ""

    vf = (f"drawtext=text='{safe_text}'"
          f":fontsize=36:fontcolor=white"
          f":box=1:boxcolor=black@0.45:boxborderw=5"
          f":{xy}{enable_str}")

    try:
        try:
            await status_msg.edit_text(
                f"🔤 Burning title overlay…\n\n"
                f"Position: {pos_label}\n"
                f"Text: `{raw_text[:60]}`\n"
                f"Re-encoding video — this may take a while.",
                reply_markup=None,
            )
        except Exception:
            pass

        args = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1", "-y",
            "-i", src,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy",
            out_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        user_ffmpeg_pids[user_id] = proc.pid

        progress_state = {"out_time_us": 0, "speed": 1.0}

        async def _read_prog():
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                txt = line.decode(errors="ignore").strip()
                if "=" not in txt:
                    continue
                k, v = txt.split("=", 1)
                if k == "out_time_us":
                    try: progress_state["out_time_us"] = int(v)
                    except ValueError: pass
                elif k == "speed" and v not in ("N/A", ""):
                    try: progress_state["speed"] = float(v.rstrip("x"))
                    except ValueError: pass

        async def _render_prog():
            last = ""
            while proc.returncode is None:
                if user_id in cancelled_users:
                    return
                done_sec  = progress_state["out_time_us"] / 1_000_000
                pct       = min(100.0, done_sec / max(1, duration) * 100)
                bar_len   = 20
                filled    = max(0, min(bar_len, int(round(pct / 100 * bar_len))))
                bar       = "●" * filled + "○" * (bar_len - filled)
                spd       = progress_state["speed"]
                remaining = max(0.0, (duration - done_sec) / max(0.05, spd))
                txt       = (f"🔤 **Burning title…**\n\n"
                             f"`{bar}` `{pct:5.1f}%`\n"
                             f"⚡ Speed: `{spd:.2f}x`\n"
                             f"⏳ ETA: `{TimeFormatter(int(remaining * 1000))}`")
                if txt != last:
                    try:
                        await status_msg.edit_text(txt)
                        last = txt
                        if user_status.get(user_id):
                            user_status[user_id]["progress"] = f"{pct:.1f}%"
                    except Exception:
                        pass
                await asyncio.sleep(4)

        prog_reader   = asyncio.create_task(_read_prog())
        prog_renderer = asyncio.create_task(_render_prog())
        progress_tasks[user_id] = prog_renderer

        rc = await proc.wait()
        prog_reader.cancel()
        prog_renderer.cancel()
        user_ffmpeg_pids.pop(user_id, None)

        if user_id in cancelled_users:
            try: await status_msg.edit_text("Title overlay cancelled.")
            except Exception: pass
            _safe_rmtree(save_dir)
            return

        if rc != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            err = (await proc.stderr.read()).decode(errors="ignore")
            tail = err[-1500:] if len(err) > 1500 else err
            raise Exception(f"FFmpeg exit {rc}\n{tail}")

        # Thumbnail
        thumb    = join(save_dir, "thumb.jpg")
        thumb_at = max(1, min(int(duration / 2), int(duration) - 1))
        await runcmd(f'ffmpeg -hide_banner -loglevel error -nostats -y '
                     f'-ss {thumb_at} -i {shlex.quote(out_path)} '
                     f'-vframes 1 -q:v 2 {shlex.quote(thumb)}')
        thumb_path  = thumb if os.path.exists(thumb) else None
        out_size_mb = os.path.getsize(out_path) / (1024 * 1024)

        long_note = (f"\n_Title disappears in the last 3 min (video > 46 min)._"
                     if duration >= _TITLE_LONG_VIDEO_SEC else "")
        retention_note = (f"_Auto-deleted from server after {_retention_label()}._")
        caption = (f"🎬 **{config.BRAND_TITLE}**\n\n"
                   f"🔤 Title: `{raw_text[:80]}`\n"
                   f"📌 Position: `{pos_label}`\n"
                   f"⏱ Duration: `{TimeFormatter(int(duration * 1000))}`\n"
                   f"💾 Size: `{out_size_mb:.1f} MB`\n"
                   f"Channel: @{config.SUPPORT_CHANNEL}{long_note}\n\n"
                   f"{retention_note}")

        upload_start = time.time()
        await status_msg.reply_video(
            video=out_path, caption=caption, duration=int(duration),
            thumb=thumb_path,
            progress=progress_for_pyrogram,
            progress_args=(status_msg, upload_start, status_msg, save_dir, False),
        )
        asyncio.create_task(gdrive.upload_and_notify(
            client, status_msg.chat.id, out_path, os.path.basename(out_path)
        ))
        try:
            await status_msg.edit_text(
                f"✅ Title overlay done — uploaded `{out_size_mb:.1f} MB`.\n"
                f"Server copy auto-deletes in {_retention_label()}."
            )
        except Exception:
            pass
        if save_dir and os.path.exists(save_dir):
            schedule_retention_cleanup(save_dir)

    except Exception as e:
        LOG.error(f"run_title failed uid={user_id}: {e}")
        err_text = str(e)
        if len(err_text) > 2500:
            err_text = "...[truncated]...\n" + err_text[-2500:]
        try: await status_msg.edit_text(f"**Title overlay failed.**\n\n`{err_text}`")
        except Exception: pass
        if save_dir and os.path.exists(save_dir):
            _safe_rmtree(save_dir)
    finally:
        title_jobs.pop(user_id, None)
        user_status.pop(user_id, None)
        user_tasks.pop(user_id, None)
        user_ffmpeg_pids.pop(user_id, None)
        progress_tasks.pop(user_id, None)
        cancelled_users.discard(user_id)


async def progress_for_pyrogram(current, total, message, start, msg,
                                 save_dir=None, was_cancelled=False):
    now        = time.time()
    diff       = now - start or 1
    percentage = current * 100 / total
    speed      = current / diff
    bar_length = 15
    filled     = int(bar_length * percentage // 100)
    bar        = "█" * filled + "░" * (bar_length - filled)

    if int(percentage) in (0, 10, 25, 50, 75, 90, 95, 99, 100) or current == total:
        eta    = TimeFormatter(int((total - current) / speed * 1000)) if speed > 0 else "00:00:00"
        prefix = "**Uploading partial recording**" if was_cancelled else "**Uploading video**"
        text   = (f"{prefix}\n`[{bar}]` {percentage:.1f}%\n"
                  f"Progress: `{current/(1024*1024):.1f} / {total/(1024*1024):.1f} MB`\n"
                  f"Speed: `{speed/(1024*1024):.1f} MB/s`\nETA: `{eta}`")
        try: await msg.edit_text(text)
        except Exception: pass

        if current == total:
            label = _retention_label()
            final = "Partial recording sent." if was_cancelled else "Upload completed successfully."
            try: await msg.edit_text(f"{final}\nThe server copy will be auto-deleted in {label}.")
            except Exception: pass
