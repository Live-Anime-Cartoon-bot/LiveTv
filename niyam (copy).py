"""
niyam.py  —  Bot Schedule & Rage-Mode Rules
Works on any server: Railway, Replit, VPS, local, etc.
Timezone: Asia/Kolkata (IST)

Rules summary
─────────────
Normal days  : Bot runs 08:00 AM – 07:00 PM IST.

Evening zones (IST):
  08:00–18:40  → NORMAL  — everything allowed
  18:40–18:50  → CAUTION — ongoing recordings allowed, no cancellation
  18:50–18:58  → DANGER  — new recording triggers Rage Mode
                            (culprit's ALL recordings cancelled)
  18:58–19:00  → EXTREME — same + immediate offline; next-day schedule shifts
  19:00+       → OFFLINE — bot sleeps

Rage Mode (12 hours):
  Owner    : 09:00 AM – 06:00 PM
  Verified : 10:00 AM – 04:00 PM
  Normal   : 10:00 AM – 04:00 PM  (same as verified)
  On startup: culprit @username announced once in group/owner chat.
  Auto-reset after 12 h → normal schedule resumes for everyone.

Manual State Commands (owner-only, defined in handlers.py):
  /rage_reset                    — Force reset rage mode immediately
  /rage_on USER_ID [hrs]         — Manually activate rage mode for N hours (default 12)
  /niyam_state                   — View raw niyam_state.json contents
  /niyam_edit KEY VALUE          — Directly set any key in niyam_state.json
  /handlersfree <OwnerID>        — 4-minute temporary bypass for owner (no full reset)
  /handlersUser All <N> minus    — Sabka rage time N ghante kam karo
  /handlersUser All <N> plus     — Sabka rage time N ghante aur badha do
  /unlock restart only <OwnerID> — Owner ke liye aaj ka shutdown 10 PM tak extend karo
                                   (baaki sab ke liye 7 PM normal rehta hai)
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytz

IST = pytz.timezone("Asia/Kolkata")

# ── Persistent state file (same dir as this script) ───────────────────────
_STATE = Path(__file__).parent / "niyam_state.json"

# ── Normal schedule ────────────────────────────────────────────────────────
OPEN_H  = 8    # 08:00 AM
CLOSE_H = 19   # 07:00 PM

# ── Evening danger thresholds (minutes since midnight) ────────────────────
_CAUTION_MIN = 18 * 60 + 40   # 6:40 PM
_DANGER_MIN  = 18 * 60 + 50   # 6:50 PM
_EXTREME_MIN = 18 * 60 + 58   # 6:58 PM
_CLOSE_MIN   = 19 * 60        # 7:00 PM
_OPEN_MIN    = 8  * 60        # 8:00 AM

# ── Rage-mode access windows ───────────────────────────────────────────────
OWNER_START    = 9    # 9:00 AM
OWNER_END      = 18   # 6:00 PM
VERIFIED_START = 10   # 10:00 AM
VERIFIED_END   = 16   # 4:00 PM
RAGE_HOURS     = 12

# ── Owner extended-close hour (/unlock restart only) ──────────────────────
# Normal close = 07:00 PM (CLOSE_H = 19)
# Extended     = 10:00 PM — sirf owner ke liye, sirf aaj ke liye
EXTENDED_CLOSE_H = 22   # 10:00 PM

# ── Zone constants ─────────────────────────────────────────────────────────
OFFLINE = "offline"
NORMAL  = "normal"
CAUTION = "caution"
DANGER  = "danger"
EXTREME = "extreme"


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(IST)


def _load() -> dict:
    try:
        with open(_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(state: dict) -> None:
    with open(_STATE, "w") as f:
        json.dump(state, f, indent=2)


def _fmt_time(h: int) -> str:
    suffix = "AM" if h < 12 else "PM"
    h12    = h if h <= 12 else h - 12
    return f"{h12:02d}:00 {suffix}"


# ═══════════════════════════════════════════════════════════════════════════
# Zone detection
# ═══════════════════════════════════════════════════════════════════════════

def current_zone() -> str:
    """Return one of: offline | normal | caution | danger | extreme."""
    now = _now()
    m   = now.hour * 60 + now.minute
    if m < _OPEN_MIN or m >= _CLOSE_MIN:
        return OFFLINE
    if m >= _EXTREME_MIN:
        return EXTREME
    if m >= _DANGER_MIN:
        return DANGER
    if m >= _CAUTION_MIN:
        return CAUTION
    return NORMAL


# ═══════════════════════════════════════════════════════════════════════════
# Rage mode
# ═══════════════════════════════════════════════════════════════════════════

def is_rage_active() -> bool:
    """True if rage mode is active and not yet expired."""
    state = _load()
    ts    = state.get("rage_until")
    if not ts:
        return False
    if _now().timestamp() < float(ts):
        return True
    # Auto-reset
    _save({})
    return False


def activate_rage(culprit_id: int, culprit_username: str) -> None:
    """Start rage mode for RAGE_HOURS hours."""
    until = (_now() + timedelta(hours=RAGE_HOURS)).timestamp()
    _save({
        "rage_mode":          True,
        "rage_until":         until,
        "culprit":            culprit_id,
        "culprit_username":   culprit_username or str(culprit_id),
        "culprit_announced":  False,
    })


def rage_until_str() -> str:
    """Human-readable reset time, e.g. '08:50 AM IST'."""
    state = _load()
    ts    = state.get("rage_until")
    if not ts:
        return ""
    dt = datetime.fromtimestamp(float(ts), tz=IST)
    return dt.strftime("%I:%M %p IST")


def rage_remaining_str() -> str:
    """Human-readable time left in rage mode, e.g. '11h 42m'."""
    state = _load()
    ts    = state.get("rage_until")
    if not ts:
        return ""
    secs = float(ts) - _now().timestamp()
    if secs <= 0:
        return ""
    h, rem = divmod(int(secs), 3600)
    m      = rem // 60
    return f"{h}h {m}m"


def pop_culprit_announcement() -> str | None:
    """
    Returns a one-time announcement string the FIRST time this is called
    after rage mode activates (e.g. on bot startup).
    Returns None on all subsequent calls.
    """
    state = _load()
    if not state.get("rage_mode"):
        return None
    if state.get("culprit_announced"):
        return None
    state["culprit_announced"] = True
    _save(state)
    uname    = state.get("culprit_username", "unknown")
    until    = rage_until_str()
    return (
        "🔥 **RAGE MODE IS ACTIVE**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **Culprit:** @{uname}\n"
        f"⏳ **Resets at:** {until}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👑 Owner access : {_fmt_time(OWNER_START)} – {_fmt_time(OWNER_END)}\n"
        f"✅ Verified     : {_fmt_time(VERIFIED_START)} – {_fmt_time(VERIFIED_END)}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ Bot is online but access is restricted until rage resets."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Owner temporary bypass  (/handlersfree)
# ═══════════════════════════════════════════════════════════════════════════
#
# niyam_state.json mein ek extra key store hoti hai:
#   "owner_bypass": { "<owner_id>": <unix_timestamp_expiry> }
#
# Ye rage mode ya schedule ko delete nahi karta — sirf owner ko
# ek short window deta hai bina kisi restriction ke.

_BYPASS_MINUTES = 4   # Default bypass duration


def _is_owner_bypassed(owner_id: int) -> bool:
    """Return True if owner has an active temporary bypass."""
    state   = _load()
    bypasses = state.get("owner_bypass", {})
    expiry   = bypasses.get(str(owner_id))
    if not expiry:
        return False
    if _now().timestamp() < float(expiry):
        return True
    # Expired — clean up this entry
    bypasses.pop(str(owner_id), None)
    state["owner_bypass"] = bypasses
    _save(state)
    return False


def _owner_bypass_remaining_secs(owner_id: int) -> float:
    """Seconds left in owner bypass window (0 if not active)."""
    state   = _load()
    bypasses = state.get("owner_bypass", {})
    expiry   = bypasses.get(str(owner_id))
    if not expiry:
        return 0.0
    remaining = float(expiry) - _now().timestamp()
    return max(remaining, 0.0)


def set_owner_bypass(owner_id: int, minutes: float = _BYPASS_MINUTES) -> str:
    """
    Grant owner a temporary bypass for `minutes` minutes.
    Rage mode and schedule rules stay unchanged — only this owner
    gets unconditional access for the bypass window.
    Returns a confirmation message string.
    """
    expiry_dt = _now() + timedelta(minutes=minutes)
    expiry_ts = expiry_dt.timestamp()

    state = _load()
    bypasses = state.get("owner_bypass", {})
    bypasses[str(owner_id)] = expiry_ts
    state["owner_bypass"] = bypasses
    _save(state)

    reset_str = expiry_dt.strftime("%I:%M:%S %p IST")
    rage_note = ""
    if is_rage_active():
        culprit  = state.get("culprit_username", "?")
        rage_rem = rage_remaining_str()
        rage_note = (
            f"\n\n⚠️ Rage mode still active (@{culprit}, {rage_rem} left).\n"
            "Users are still restricted — only YOU are bypassed."
        )

    return (
        "🔓 **Owner Bypass Activated!**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Owner ID : `{owner_id}`\n"
        f"⏱ Duration : **{minutes:.0f} minutes**\n"
        f"🔁 Expires  : **{reset_str}**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Aap ab bina kisi restriction ke recording kar sakte hain.\n"
        f"Bypass automatically expire hoga **{minutes:.0f} min** baad."
        f"{rage_note}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Owner extended close  (/unlock restart only)
# ═══════════════════════════════════════════════════════════════════════════
#
# niyam_state.json mein store hota hai:
#   "owner_extended_close": { "<owner_id>": "<YYYY-MM-DD>" }
#
# Sirf aaj ki date match hone par extension active maani jaati hai.
# Kal automatically expire — koi manual cleanup nahi chahiye.

def _is_owner_extended(owner_id: int) -> bool:
    """True if owner has extended-close active for today."""
    state = _load()
    ext   = state.get("owner_extended_close", {})
    stored_date = ext.get(str(owner_id))
    if not stored_date:
        return False
    today = _now().strftime("%Y-%m-%d")
    return stored_date == today


def _get_owner_close_hour(owner_id: int) -> int:
    """
    Return the effective closing hour for this owner.
    Extended: 22 (10 PM) if /unlock is active today.
    Normal  : CLOSE_H (19 = 7 PM) otherwise.
    """
    return EXTENDED_CLOSE_H if _is_owner_extended(owner_id) else CLOSE_H


def set_owner_extended_close(owner_id: int) -> str:
    """
    Extend today's shutdown to 10 PM for this owner only.
    Other users are unaffected — their 7 PM cutoff stays.
    Extension is date-scoped: auto-expires at midnight, no cleanup needed.
    Returns a confirmation message string.
    """
    today = _now().strftime("%Y-%m-%d")

    state = _load()
    ext   = state.get("owner_extended_close", {})
    ext[str(owner_id)] = today
    state["owner_extended_close"] = ext
    _save(state)

    return (
        "🔓 **Owner Unlock — Extended Hours Activated!**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Owner ID  : `{owner_id}`\n"
        f"📅 Valid for : Today ({today})\n"
        f"🕐 New close : **{_fmt_time(EXTENDED_CLOSE_H)}** (10:00 PM IST)\n"
        f"🕖 Others    : Normal **{_fmt_time(CLOSE_H)}** (7:00 PM IST)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Aap aaj raat 10 PM tak bot use kar sakte hain.\n"
        "⚠️ Baaki sabhi users 7 PM par offline ho jaayenge.\n"
        "🔁 Kal se automatically normal schedule resume hoga."
    )


def cancel_owner_extended_close(owner_id: int) -> str:
    """Remove extended-close for this owner (revert to 7 PM)."""
    state = _load()
    ext   = state.get("owner_extended_close", {})
    if str(owner_id) not in ext:
        return "ℹ️ Koi active extension nahi mili is owner ke liye."
    ext.pop(str(owner_id))
    state["owner_extended_close"] = ext
    _save(state)
    return (
        "✅ **Extension cancelled.**\n"
        f"👤 Owner `{owner_id}` ab normal **{_fmt_time(CLOSE_H)}** schedule par wapas aa gaya."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Rage time adjustment  (/handlersUser All N minus/plus)
# ═══════════════════════════════════════════════════════════════════════════

def adjust_rage_time(hours: float, direction: str) -> str:
    """
    Shift the global rage_until timestamp by `hours` hours.
    direction: "minus" → time kam karo (faster expiry)
               "plus"  → time badha do (longer punishment)

    Returns a confirmation or error message string.
    """
    if not is_rage_active():
        return (
            "ℹ️ **Rage mode active nahi hai.**\n"
            "Adjust karne ke liye pehle rage mode on hona chahiye."
        )
    if hours <= 0:
        return "❌ Hours must be > 0."

    state = _load()
    current_ts = float(state["rage_until"])
    delta      = timedelta(hours=hours)

    if direction == "minus":
        new_ts = current_ts - delta.total_seconds()
        # Agar time pehle se guzar gaya, turant reset karo
        if new_ts <= _now().timestamp():
            culprit = state.get("culprit_username", "unknown")
            _save({})
            return (
                "✅ **Rage Mode expired!** (time ghata ke zero pe aa gaya)\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Culprit cleared: @{culprit}\n"
                "🟢 Bot ab normal schedule par wapas aa gaya."
            )
        arrow = "⬇️ kam kiya"
    elif direction == "plus":
        new_ts = current_ts + delta.total_seconds()
        arrow  = "⬆️ badha diya"
    else:
        return "❌ direction must be 'minus' or 'plus'."

    state["rage_until"] = new_ts
    _save(state)

    old_dt = datetime.fromtimestamp(current_ts, tz=IST)
    new_dt = datetime.fromtimestamp(new_ts,     tz=IST)
    culprit = state.get("culprit_username", "?")

    # Recalculate remaining after update
    secs = new_ts - _now().timestamp()
    h_left, rem = divmod(int(secs), 3600)
    m_left = rem // 60

    return (
        f"{'🕊' if direction == 'minus' else '🔒'} **Rage Time {arrow}!**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Culprit   : @{culprit}\n"
        f"🔧 Changed by: **{hours:.1f} hours** ({arrow})\n"
        f"📤 Old expiry: {old_dt.strftime('%I:%M %p IST')}\n"
        f"📥 New expiry: {new_dt.strftime('%I:%M %p IST')}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ Ab remaining: **{h_left}h {m_left}m**"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Access gate
# ═══════════════════════════════════════════════════════════════════════════

def check_access(user_id: int,
                 *,
                 is_owner: bool = False,
                 is_verified: bool = False) -> tuple[bool, str]:
    """
    Returns (allowed: bool, denial_message: str).
    denial_message is "" when allowed is True.

    Priority order:
      1. /handlersfree bypass  — short window, ignores everything
      2. Rage-mode window      — restricted hours for all
      3. Normal schedule       — owners may have extended close via /unlock
    """
    now = _now()
    h   = now.hour

    # ── 1. Owner temporary bypass (/handlersfree) ───────────────────────────
    if is_owner and _is_owner_bypassed(user_id):
        remaining_s = _owner_bypass_remaining_secs(user_id)
        m, s = divmod(int(remaining_s), 60)
        return True, f"🔓 bypass active ({m}m {s}s left)"

    # ── 2. Rage-mode restrictions ───────────────────────────────────────────
    if is_rage_active():
        state   = _load()
        culprit = state.get("culprit_username", "someone")
        until   = rage_until_str()

        s, e = (OWNER_START, OWNER_END) if is_owner else (VERIFIED_START, VERIFIED_END)

        if not (s <= h < e):
            remaining = rage_remaining_str()
            return False, (
                "🚫 **ACCESS DENIED — Rage Mode Active**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Culprit: @{culprit}\n"
                f"⏳ Resets in: **{remaining}** (at {until})\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🕐 Your access window: **{_fmt_time(s)} – {_fmt_time(e)}**\n"
                "Come back during your access window."
            )
        return True, ""

    # ── 3. Normal schedule ──────────────────────────────────────────────────
    # Owner may have extended close (10 PM) via /unlock restart only
    effective_close = _get_owner_close_hour(user_id) if is_owner else CLOSE_H

    if not (OPEN_H <= h < effective_close):
        if is_owner and effective_close == EXTENDED_CLOSE_H and h >= CLOSE_H:
            # Owner is in extended window (7 PM – 10 PM) — allow
            return True, f"🔓 extended hours active (until {_fmt_time(EXTENDED_CLOSE_H)})"
        return False, (
            "💤 **Bot is Sleeping!**\n"
            f"⏰ Active hours: **{_fmt_time(OPEN_H)} – {_fmt_time(CLOSE_H)} IST**\n"
            "Please come back during active hours."
        )

    return True, ""


# ═══════════════════════════════════════════════════════════════════════════
# New-recording guard
# ═══════════════════════════════════════════════════════════════════════════

def guard_new_recording(user_id: int,
                        username: str) -> tuple[str, bool]:
    """
    Call before accepting any new recording/download request.

    Returns (action, rage_triggered):
      action values:
        "allow"   — proceed normally
        "caution" — within safe window; allow (ongoing recordings continue)
        "block"   — offline or danger zone; deny the new request
        "extreme" — extreme zone; deny + immediate offline
      rage_triggered:
        True  — rage mode was just activated (caller must cancel ALL jobs)
        False — no change in rage state
    """
    # Access check first (rage, offline)
    allowed, _ = check_access(user_id)
    if not allowed:
        return "block", False

    zone = current_zone()

    if zone in (NORMAL, CAUTION):
        return "caution" if zone == CAUTION else "allow", False

    if zone == DANGER:
        activate_rage(user_id, username)
        return "block", True

    if zone == EXTREME:
        activate_rage(user_id, username)
        return "extreme", True

    # OFFLINE (belt-and-suspenders)
    return "block", False


def guard_message(action: str, zone: str | None = None) -> str:
    """Human-readable denial string for guard_new_recording results."""
    z = zone or current_zone()
    if action == "extreme":
        return (
            "🔥 **EXTREME DANGER ZONE (6:58 PM)**\n\n"
            "Ab toh had ho gayi!\n"
            "Bot **turant offline** ja raha hai.\n"
            "Teri aur baaki sabki recordings cancel ho gayi.\n\n"
            "🔥 Rage Mode activated for 12 hours.\n"
            f"👑 Owner: {_fmt_time(OWNER_START)}–{_fmt_time(OWNER_END)}\n"
            f"✅ Verified: {_fmt_time(VERIFIED_START)}–{_fmt_time(VERIFIED_END)}"
        )
    if action == "block" and z == DANGER:
        return (
            "🔴 **DANGER ZONE (6:50 PM)**\n\n"
            "Shaam ko masti?\n"
            "Teri nayi + purani DONO recordings cancel ho gayi!\n\n"
            "🔥 **Rage Mode** activated for 12 hours.\n"
            f"👑 Owner: {_fmt_time(OWNER_START)}–{_fmt_time(OWNER_END)}\n"
            f"✅ Verified: {_fmt_time(VERIFIED_START)}–{_fmt_time(VERIFIED_END)}"
        )
    if action == "block":
        return (
            "💤 **Bot is offline right now.**\n"
            f"⏰ Active: {_fmt_time(OPEN_H)} – {_fmt_time(CLOSE_H)} IST"
        )
    return "❌ Request blocked."


# ═══════════════════════════════════════════════════════════════════════════
# Status helpers
# ═══════════════════════════════════════════════════════════════════════════

def status_line() -> str:
    """One-line status badge for /status or startup messages."""
    zone = current_zone()
    rage = is_rage_active()
    now  = _now()

    emoji = {
        NORMAL:  "🟢",
        CAUTION: "🟡",
        DANGER:  "🔴",
        EXTREME: "🔥",
        OFFLINE: "💤",
    }.get(zone, "⚪")

    time_str = now.strftime("%I:%M %p IST")

    if rage:
        remaining = rage_remaining_str()
        culprit   = _load().get("culprit_username", "?")
        return f"🔥 RAGE MODE — @{culprit} | resets in {remaining}"
    if zone == OFFLINE:
        return f"💤 Offline | Back at {_fmt_time(OPEN_H)} IST"
    if zone == CAUTION:
        return f"🟡 Caution Zone | {time_str} | Closing at {_fmt_time(CLOSE_H)}"
    if zone == DANGER:
        return f"🔴 DANGER ZONE | {time_str} — New recordings blocked!"
    return f"{emoji} Online | {time_str}"


# ═══════════════════════════════════════════════════════════════════════════
# Manual state editing (owner-only — called from handlers.py)
# ═══════════════════════════════════════════════════════════════════════════

def manual_reset_rage() -> str:
    """
    Force-reset rage mode immediately, regardless of time.
    Returns a confirmation message string.
    """
    state = _load()
    if not state:
        return "ℹ️ Rage mode pehle se inactive hai — koi state nahi mili."

    culprit = state.get("culprit_username", "unknown")
    _save({})
    return (
        "✅ **Rage Mode manually reset!**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Culprit cleared: @{culprit}\n"
        "🟢 Bot ab normal schedule par wapas aa gaya."
    )


def manual_activate_rage(culprit_id: int,
                         culprit_username: str,
                         hours: float = RAGE_HOURS) -> str:
    """
    Manually activate rage mode for `hours` hours.
    Returns a confirmation message string.
    """
    until_dt = _now() + timedelta(hours=hours)
    until_ts = until_dt.timestamp()
    uname    = culprit_username.lstrip("@") or str(culprit_id)

    _save({
        "rage_mode":         True,
        "rage_until":        until_ts,
        "culprit":           culprit_id,
        "culprit_username":  uname,
        "culprit_announced": False,
    })

    reset_str = until_dt.strftime("%I:%M %p IST")
    return (
        "🔥 **Rage Mode manually activated!**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **Culprit:** @{uname}\n"
        f"⏳ **Duration:** {hours:.1f} hours\n"
        f"🔁 **Resets at:** {reset_str}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👑 Owner access : {_fmt_time(OWNER_START)} – {_fmt_time(OWNER_END)}\n"
        f"✅ Verified     : {_fmt_time(VERIFIED_START)} – {_fmt_time(VERIFIED_END)}"
    )


def get_full_state() -> str:
    """
    Return a formatted string of the current niyam_state.json contents.
    Safe to send as a Telegram message.
    """
    state = _load()
    if not state:
        return (
            "📂 **niyam_state.json**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "_(empty — rage mode inactive)_"
        )

    lines = ["📂 **niyam_state.json**", "━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for key, val in state.items():
        if key == "rage_until" and val:
            dt  = datetime.fromtimestamp(float(val), tz=IST)
            val = dt.strftime("%d-%m-%Y %I:%M:%S %p IST") + f"  (raw: {val})"
        lines.append(f"🔑 `{key}`: `{val}`")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📍 Zone: `{current_zone()}`  |  Rage: `{is_rage_active()}`")
    return "\n".join(lines)


def edit_state_key(key: str, value: str) -> str:
    """
    Directly set a single key in niyam_state.json.
    Tries to auto-cast value to int/float/bool before saving.
    Returns confirmation or error string.

    Allowed keys: rage_mode, rage_until, culprit, culprit_username,
                  culprit_announced
    """
    ALLOWED_KEYS = {
        "rage_mode", "rage_until", "culprit",
        "culprit_username", "culprit_announced",
    }
    if key not in ALLOWED_KEYS:
        allowed = ", ".join(f"`{k}`" for k in sorted(ALLOWED_KEYS))
        return (
            f"❌ **Invalid key:** `{key}`\n\n"
            f"✅ Allowed keys:\n{allowed}"
        )

    # Auto-cast
    cast_val: object = value
    if value.lower() == "true":
        cast_val = True
    elif value.lower() == "false":
        cast_val = False
    elif value.lower() in ("null", "none", ""):
        cast_val = None
    else:
        try:
            cast_val = int(value)
        except ValueError:
            try:
                cast_val = float(value)
            except ValueError:
                cast_val = value  # keep as string

    state = _load()
    old_val = state.get(key, "<not set>")
    if cast_val is None:
        state.pop(key, None)
    else:
        state[key] = cast_val
    _save(state)

    return (
        "✅ **State updated!**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 Key   : `{key}`\n"
        f"📤 Old   : `{old_val}`\n"
        f"📥 New   : `{cast_val}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 Use /niyam_state to verify."
    )
