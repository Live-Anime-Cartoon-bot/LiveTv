"""
niyam_commands.py — Owner-only commands to view and edit niyam_state.json
Add these handlers to your handlers.py file.

Commands added:
  /rage_reset                  — Force reset rage mode (turant)
  /rage_on USER_ID [hrs]       — Manually activate rage mode
  /niyam_state                 — View niyam_state.json contents
  /niyam_edit KEY VALUE        — Directly edit a specific key in state
  /handlersfree <OwnerID>      — 4-min temporary bypass (bina rage reset kiye)
  /handlersUser All <N> minus  — Sabka rage time N ghante kam karo
  /handlersUser All <N> plus   — Sabka rage time N ghante aur badha do
"""

from pyrogram import filters
from pyrogram.types import Message
import config
import niyam
from utils import app

# ═══════════════════════════════════════════════════════════════════════════
# /rage_reset  — Force-reset rage mode immediately
# Usage: /rage_reset
# ═══════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("rage_reset") & filters.user(config.OWNER_ID))
async def rage_reset_cmd(client, message: Message):
    result = niyam.manual_reset_rage()
    await message.reply_text(result)


# ═══════════════════════════════════════════════════════════════════════════
# /rage_on  — Manually activate rage mode
# Usage: /rage_on USER_ID [hours]
# Example: /rage_on 987654321 6
#          /rage_on 987654321          ← uses default 12 hours
# ═══════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("rage_on") & filters.user(config.OWNER_ID))
async def rage_on_cmd(client, message: Message):
    args = message.command[1:]

    if not args:
        return await message.reply_text(
            "❌ **Usage:**\n"
            "`/rage_on USER_ID [hours]`\n\n"
            "**Examples:**\n"
            "`/rage_on 987654321`        — 12 ghante ke liye\n"
            "`/rage_on 987654321 6`      — 6 ghante ke liye\n"
            "`/rage_on 987654321 0.5`    — 30 minute ke liye"
        )

    try:
        culprit_id = int(args[0])
    except ValueError:
        return await message.reply_text("❌ **Invalid USER_ID** — sirf number daalein.")

    hours = niyam.RAGE_HOURS
    if len(args) >= 2:
        try:
            hours = float(args[1])
            if hours <= 0:
                return await message.reply_text("❌ **Hours must be > 0.**")
        except ValueError:
            return await message.reply_text("❌ **Invalid hours** — jaise: `6` ya `0.5`")

    # Try to fetch username from Telegram
    culprit_username = str(culprit_id)
    try:
        user_obj = await client.get_users(culprit_id)
        culprit_username = user_obj.username or user_obj.first_name or str(culprit_id)
    except Exception:
        pass

    result = niyam.manual_activate_rage(culprit_id, culprit_username, hours)
    await message.reply_text(result)


# ═══════════════════════════════════════════════════════════════════════════
# /niyam_state  — View raw niyam_state.json
# Usage: /niyam_state
# ═══════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("niyam_state") & filters.user(config.OWNER_ID))
async def niyam_state_cmd(client, message: Message):
    result = niyam.get_full_state()
    await message.reply_text(result)


# ═══════════════════════════════════════════════════════════════════════════
# /niyam_edit  — Directly set any key in niyam_state.json
# Usage: /niyam_edit KEY VALUE
# Examples:
#   /niyam_edit culprit_announced false
#   /niyam_edit rage_mode false
#   /niyam_edit culprit_username newuser123
# ═══════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("niyam_edit") & filters.user(config.OWNER_ID))
async def niyam_edit_cmd(client, message: Message):
    args = message.command[1:]

    if len(args) < 2:
        return await message.reply_text(
            "❌ **Usage:**\n"
            "`/niyam_edit KEY VALUE`\n\n"
            "**Allowed keys:**\n"
            "• `rage_mode` — true / false\n"
            "• `rage_until` — Unix timestamp (seconds)\n"
            "• `culprit` — User ID (number)\n"
            "• `culprit_username` — Username string\n"
            "• `culprit_announced` — true / false\n\n"
            "**Examples:**\n"
            "`/niyam_edit culprit_announced false`\n"
            "`/niyam_edit culprit_username baduser99`\n"
            "`/niyam_edit rage_mode false`\n\n"
            "💡 Poora state reset karne ke liye `/rage_reset` use karein."
        )

    key   = args[0].strip()
    value = " ".join(args[1:]).strip()

    result = niyam.edit_state_key(key, value)
    await message.reply_text(result)


# ═══════════════════════════════════════════════════════════════════════════
# /handlersfree  — Owner ke liye 4-minute temporary bypass
#
# Rage mode ya schedule bilkul change nahi hota.
# Sirf owner ko ek short window milti hai bina kisi restriction ke.
#
# Usage:
#   /handlersfree <OwnerID>          ← default 4 minutes
#   /handlersfree <OwnerID> 10       ← custom 10 minutes
#
# Example:
#   /handlersfree 5856009289
#   /handlersfree 5856009289 6
# ═══════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("handlersfree") & filters.user(config.OWNER_ID))
async def handlersfree_cmd(client, message: Message):
    args = message.command[1:]

    if not args:
        return await message.reply_text(
            "❌ **Usage:**\n"
            "`/handlersfree <OwnerID> [minutes]`\n\n"
            "**Examples:**\n"
            "`/handlersfree 5856009289`     — default 4 minute bypass\n"
            "`/handlersfree 5856009289 10`  — 10 minute bypass\n\n"
            "💡 Ye rage mode reset **nahi** karta — sirf aapko temporary access deta hai."
        )

    try:
        owner_id = int(args[0])
    except ValueError:
        return await message.reply_text("❌ **Invalid OwnerID** — sirf number daalein.")

    # Verify requested ID is actually an owner
    if owner_id not in config.OWNER_ID:
        return await message.reply_text(
            f"❌ `{owner_id}` config mein OWNER_ID nahi hai.\n"
            "Sirf registered owners ka bypass set ho sakta hai."
        )

    minutes = niyam._BYPASS_MINUTES
    if len(args) >= 2:
        try:
            minutes = float(args[1])
            if minutes <= 0:
                return await message.reply_text("❌ Minutes must be > 0.")
        except ValueError:
            return await message.reply_text("❌ **Invalid minutes** — jaise: `4` ya `10`")

    result = niyam.set_owner_bypass(owner_id, minutes)

    # Live countdown notification — bot 4-min baad automatically expire karega
    await message.reply_text(result)

    # Ek background task: bypass expire hone par owner ko notify karo
    import asyncio

    async def _notify_bypass_expired():
        await asyncio.sleep(minutes * 60)
        if not niyam._is_owner_bypassed(owner_id):
            try:
                await client.send_message(
                    owner_id,
                    "⏰ **Bypass Expired!**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Aapka {minutes:.0f}-minute bypass khatam ho gaya.\n"
                    f"📍 Current zone: `{niyam.current_zone()}`  |  "
                    f"Rage: `{niyam.is_rage_active()}`"
                )
            except Exception:
                pass

    asyncio.create_task(_notify_bypass_expired())


# ═══════════════════════════════════════════════════════════════════════════
# /handlersUser  — Global rage time adjust karo
#
# Usage:
#   /handlersUser All <N> minus   ← sabka rage time N ghante kam karo
#   /handlersUser All <N> plus    ← sabka rage time N ghante badha do
#
# Examples:
#   /handlersUser All 4 minus     ← 4 ghante kam karo (users ka anger shant)
#   /handlersUser All 6 plus      ← 6 ghante aur punishment badha do
#   /handlersUser All 0.5 minus   ← 30 minute kam karo
# ═══════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("handlersUser") & filters.user(config.OWNER_ID))
async def handlersuser_cmd(client, message: Message):
    args = message.command[1:]

    # Expect: All <N> minus/plus
    if len(args) < 3 or args[0].lower() != "all":
        return await message.reply_text(
            "❌ **Usage:**\n"
            "`/handlersUser All <hours> minus`\n"
            "`/handlersUser All <hours> plus`\n\n"
            "**Examples:**\n"
            "`/handlersUser All 4 minus`   — 4 ghante kam (users angry hain)\n"
            "`/handlersUser All 3 minus`   — 3 ghante kam\n"
            "`/handlersUser All 6 plus`    — 6 ghante aur badha do\n"
            "`/handlersUser All 0.5 minus` — 30 minute kam\n\n"
            "⚠️ Sirf rage mode active hone par kaam karta hai."
        )

    try:
        hours = float(args[1])
        if hours <= 0:
            return await message.reply_text("❌ Hours must be > 0.")
    except ValueError:
        return await message.reply_text("❌ **Invalid hours** — jaise: `4` ya `0.5`")

    direction = args[2].lower()
    if direction not in ("minus", "plus"):
        return await message.reply_text(
            "❌ Direction galat hai.\n"
            "Sirf `minus` ya `plus` use karein.\n\n"
            "Example: `/handlersUser All 4 minus`"
        )

    result = niyam.adjust_rage_time(hours, direction)
    await message.reply_text(result)


# ═══════════════════════════════════════════════════════════════════════════
# /unlock restart only  — Owner ke liye aaj ka shutdown 10 PM tak extend karo
#
# Kya hota hai:
#   ✅ Owner = aaj 7 PM ke baad bhi bot use kar sakta hai (until 10 PM)
#   ❌ Normal/Verified users = hamesha ki tarah 7 PM par offline
#   🔁 Kal se auto-expire — koi manual cancel nahi chahiye
#
# Usage:
#   /unlock restart only <OwnerID>
#   /unlock cancel <OwnerID>        ← extension wapas 7 PM par lana ho toh
#
# Examples:
#   /unlock restart only 5856009289
#   /unlock cancel 5856009289
# ═══════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("unlock") & filters.user(config.OWNER_ID))
async def unlock_cmd(client, message: Message):
    args = message.command[1:]

    # Expect: restart only <OwnerID>  OR  cancel <OwnerID>
    if len(args) < 2:
        return await message.reply_text(
            "❌ **Usage:**\n"
            "`/unlock restart only <OwnerID>`  — 10 PM tak extend karo\n"
            "`/unlock cancel <OwnerID>`         — wapas 7 PM par lao\n\n"
            "**Example:**\n"
            f"`/unlock restart only {message.from_user.id}`\n\n"
            "💡 Extension sirf aaj ke liye valid hai — kal auto-expire."
        )

    # Detect subcommand: "restart only" or "cancel"
    subcommand = args[0].lower()

    # Handle: /unlock restart only <ID>
    if subcommand == "restart" and len(args) >= 3 and args[1].lower() == "only":
        try:
            owner_id = int(args[2])
        except ValueError:
            return await message.reply_text("❌ **Invalid OwnerID** — sirf number daalein.")

        if owner_id not in config.OWNER_ID:
            return await message.reply_text(
                f"❌ `{owner_id}` config mein OWNER_ID nahi hai.\n"
                "Sirf registered owners ka unlock set ho sakta hai."
            )

        result = niyam.set_owner_extended_close(owner_id)
        await message.reply_text(result)

    # Handle: /unlock cancel <ID>
    elif subcommand == "cancel" and len(args) >= 2:
        try:
            owner_id = int(args[1])
        except ValueError:
            return await message.reply_text("❌ **Invalid OwnerID** — sirf number daalein.")

        result = niyam.cancel_owner_extended_close(owner_id)
        await message.reply_text(result)

    else:
        await message.reply_text(
            "❌ **Galat format!**\n\n"
            "✅ Sahi tarike:\n"
            "`/unlock restart only <OwnerID>`\n"
            "`/unlock cancel <OwnerID>`"
        )
