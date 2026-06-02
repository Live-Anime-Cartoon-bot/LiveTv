"""
Quota & daily verification limit system.

Data file: <DATA_DIRECTORY>/user_limits.json
Schema per user:
  {
    "rec_limit":    int,   -- current recording credits
    "verify_left":  int,   -- verifications remaining this cycle (max 10)
    "verify_done":  int,   -- verifications completed this cycle
    "is_lucky":     bool,  -- lucky user flag (set once at creation, ~20% chance)
    "last_refresh": float, -- unix timestamp of last quota auto-reset
    "first_time":   bool,  -- True until user first interacts
  }
"""

import json
import os
import random
import time

import config

# ── Tunable constants ────────────────────────────────────────────────────────

DEFAULT_REC_LIMIT   = 1        # credits a brand-new user starts with
DEFAULT_VERIFY_LEFT = 10       # verifications allowed per 12-hour cycle
LUCKY_RATIO         = 5        # 1 in 5 users is "lucky" (~20%)
REFRESH_SECONDS     = 12 * 3600

# Reward table — indexed by verify_done count (clamped to last entry)
# result_rec : absolute value to set rec_limit to after this verify
VERIFY_STEPS = [
    {"result_rec": 4, "msg": "🎉 Pehli baar verify! Aapko **Rec 4** mil gaye!"},
    {"result_rec": 3, "msg": "✅ Verify bonus! Aapki limit: **Rec 3**"},
    {"result_rec": 3, "msg": "✅ Verify bonus! Aapki limit: **Rec 3**"},
    {"result_rec": 4, "msg": "🌟 Lucky Step! Aapki limit: **Rec 4**"},
    {"result_rec": 3, "msg": "✅ Verify bonus! Aapki limit: **Rec 3**"},
    {"result_rec": 3, "msg": "✅ Verify bonus! Aapki limit: **Rec 3**"},
    {"result_rec": 4, "msg": "🌟 Lucky Step! Aapki limit: **Rec 4**"},
    {"result_rec": 3, "msg": "✅ Verify bonus! Aapki limit: **Rec 3**"},
    {"result_rec": 3, "msg": "✅ Verify bonus! Aapki limit: **Rec 3**"},
    {"result_rec": 3, "msg": "✅ Last verify! Aapki limit: **Rec 3**"},
]


# ── Internal helpers ─────────────────────────────────────────────────────────

def _limit_file() -> str:
    return os.path.join(config.DATA_DIRECTORY, "user_limits.json")


def _load() -> dict:
    try:
        with open(_limit_file(), "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict) -> None:
    os.makedirs(config.DATA_DIRECTORY, exist_ok=True)
    with open(_limit_file(), "w") as f:
        json.dump(data, f, indent=2)


def _new_record() -> dict:
    return {
        "rec_limit":    DEFAULT_REC_LIMIT,
        "verify_left":  DEFAULT_VERIFY_LEFT,
        "verify_done":  0,
        "is_lucky":     random.random() < (1.0 / LUCKY_RATIO),
        "last_refresh": time.time(),
        "joined_at":    time.time(),
        "first_time":   True,
    }


def _maybe_refresh(user: dict) -> dict:
    """Auto-reset if 12 hours have passed since last refresh."""
    if time.time() - user.get("last_refresh", 0) >= REFRESH_SECONDS:
        user["rec_limit"]    = 3 if user.get("is_lucky") else 0
        user["verify_left"]  = DEFAULT_VERIFY_LEFT
        user["verify_done"]  = 0
        user["last_refresh"] = time.time()
    return user


# ── Public API ───────────────────────────────────────────────────────────────

def get_user(user_id: int) -> dict:
    """Return the user's quota record, creating and auto-refreshing as needed."""
    data = _load()
    uid  = str(user_id)
    if uid not in data:
        data[uid] = _new_record()
        _save(data)
        return dict(data[uid])
    data[uid] = _maybe_refresh(data[uid])
    _save(data)
    return dict(data[uid])


def use_rec(user_id: int) -> tuple:
    """
    Consume 1 recording credit.
    Returns (True, info_msg) on success or (False, error_msg) when out of credits.
    """
    data = _load()
    uid  = str(user_id)
    if uid not in data:
        data[uid] = _new_record()
    user = _maybe_refresh(data[uid])
    if user["rec_limit"] <= 0:
        data[uid] = user
        _save(data)
        return False, (
            "❌ **Rec limit khatam ho gayi!**\n\n"
            "Use /verify to get more recording credits.\n"
            "Use /limit to check your current status."
        )
    user["rec_limit"] -= 1
    user["first_time"]  = False
    data[uid] = user
    _save(data)
    return True, f"✅ 1 Rec used. Remaining: **Rec {user['rec_limit']}**"


def apply_verify_bonus(user_id: int) -> tuple:
    """
    Grant recording credits for a completed ad-click verification.
    Returns (True, reward_msg) or (False, error_msg).
    """
    data = _load()
    uid  = str(user_id)
    if uid not in data:
        data[uid] = _new_record()
    user = _maybe_refresh(data[uid])

    if user["verify_left"] <= 0:
        data[uid] = user
        _save(data)
        elapsed     = time.time() - user.get("last_refresh", time.time())
        remaining_s = max(REFRESH_SECONDS - elapsed, 0)
        rh = int(remaining_s // 3600)
        rm = int((remaining_s % 3600) // 60)
        return False, (
            f"🚫 **Aaj ke liye sab verifications lock ho gaye!**\n"
            f"⏱️ Refresh in: **{rh}h {rm}m**"
        )

    step_idx          = min(user["verify_done"], len(VERIFY_STEPS) - 1)
    step              = VERIFY_STEPS[step_idx]
    bonus             = 1 if user.get("is_lucky") else 0
    user["rec_limit"] = step["result_rec"] + bonus
    user["verify_left"] = max(0, user["verify_left"] - 1)
    user["verify_done"] += 1
    user["first_time"]  = False
    data[uid] = user
    _save(data)

    msg = step["msg"]
    if bonus:
        msg += "\n⭐ **Lucky Bonus:** +1 extra Rec!"
    msg += (
        f"\n\n🎯 **Total: Rec {user['rec_limit']}** "
        f"| Verify left: **{user['verify_left']}**"
    )
    return True, msg


def format_limit_message(user_id: int) -> str:
    """Return the full /limit status block for this user."""
    user      = get_user(user_id)
    rec       = user["rec_limit"]
    v_left    = user["verify_left"]
    v_done    = user["verify_done"]
    is_lucky  = user.get("is_lucky", False)
    is_first  = user.get("first_time", False)
    is_locked = v_left <= 0

    elapsed     = time.time() - user.get("last_refresh", time.time())
    remaining_s = max(REFRESH_SECONDS - elapsed, 0)
    rh = int(remaining_s // 3600)
    rm = int((remaining_s % 3600) // 60)
    refresh_str = f"{rh}h {rm}m" if remaining_s > 0 else "Abhi refresh hoga! 🔄"

    if is_locked:
        verify_line = "⚠️ **VERIFY NO USE** — Aaj ki limit lock hai!"
    elif is_first:
        verify_line = "👉 Pehli baar verify karne par aapka quota unlock ho jayega!"
    else:
        verify_line = "👉 Verify karein aur aur Rec paaein!"

    lucky_line = "⭐ **Lucky User:** Refresh ke baad Rec 3 milega!\n" if is_lucky else ""

    step_labels = [
        ("1️⃣", "First Use  ➔ Verify 2", "(Aapko milenge +Rec 4)"),
        ("2️⃣", "Second Use ➔ Verify 1", "(Aapki limit ghatkar hogi: Rec 3)"),
        ("3️⃣", "Dobara Use ➔ Verify 1", "(Aapki limit aur ghatkar hogi: Rec 3)"),
        ("4️⃣", "Third Use  ➔ Verify 10", "(Lock 🚫 Today Limit Expired)"),
    ]

    flow_lines = []
    for i, (num, action, reward) in enumerate(step_labels):
        if i < v_done:
            prefix = "✅"
        elif i == v_done and not is_locked:
            prefix = "▶️"
        else:
            prefix = num
        flow_lines.append(f"  {prefix} {action} {reward}")

    return (
        "📊 **BOT VERIFICATION STATUS** 📊\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **Your Current Limit:** Rec {rec}\n"
        "Aap iska use kar sakte hain:\n"
        "👉 `/rec LINK 00:00:30 Filename`\n"
        f"🆓 **Remaining Verify Limit:** {v_left} Verification\n"
        f"{verify_line}\n"
        f"{lucky_line}"
        "🔢 **Countdown Flow & Rewards:**\n"
        + "\n".join(flow_lines) + "\n\n"
        "🌅 **SURPRISE GIFT (Lucky User):**\n"
        "Every 20% users mein se 1 lucky user ko extra badal-badal kar rewards milenge!\n\n"
        f"⏱️ **Daily Refresh Timer:** {refresh_str}\n"
        "🔄 Har 12 ghante me system fresh ho jayega. "
        "Normal users ka Rec 0 hoga, par Lucky User ka balance Rec 3 rahega!"
    )
