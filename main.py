import asyncio
import aiohttp
from pyrogram import idle, enums
from utils import app, LOG
import config
import niyam
import handlers as _h  # noqa: F401 — registers @app.on_message
import niyam_commands as _nc  # noqa: F401

BASE = f"https://api.telegram.org/bot{config.BOT_TOKEN}"

# ─────────────────────────────────────────────────────────────────────────────
# Helper objects
# ─────────────────────────────────────────────────────────────────────────────

class _FakeUser:
    def __init__(self, d: dict):
        self.id            = d.get("id", 0)
        self.username      = d.get("username")
        self.first_name    = d.get("first_name", "")
        self.last_name     = d.get("last_name")
        self.is_bot        = d.get("is_bot", False)
        self.language_code = d.get("language_code")
        self.mention       = f"@{self.username}" if self.username else self.first_name


class _FakeChat:
    def __init__(self, d: dict):
        self.id       = d.get("id", 0)
        self.username = d.get("username")
        self.title    = d.get("title")
        _t = d.get("type", "private")
        self.type = {
            "private":    enums.ChatType.PRIVATE,
            "group":      enums.ChatType.GROUP,
            "supergroup": enums.ChatType.SUPERGROUP,
            "channel":    enums.ChatType.CHANNEL,
        }.get(_t, enums.ChatType.PRIVATE)


class _FakeMedia:
    def __init__(self, d: dict):
        self.file_id        = d.get("file_id", "")
        self.file_unique_id = d.get("file_unique_id", "")
        self.file_size      = d.get("file_size", 0)
        self.file_name      = d.get("file_name")
        self.mime_type      = d.get("mime_type")
        self.width          = d.get("width", 0)
        self.height         = d.get("height", 0)
        self.duration       = d.get("duration", 0)
        self.thumb          = None


# ─────────────────────────────────────────────────────────────────────────────
# FakeMessage
# ─────────────────────────────────────────────────────────────────────────────

class FakeMessage:
    def __init__(self, msg: dict, chat_override: dict | None = None):
        from_d  = msg.get("from") or {}
        chat_d  = chat_override or msg.get("chat") or {}
        reply_d = msg.get("reply_to_message")

        self.id           = msg.get("message_id", 0)
        self.date         = msg.get("date", 0)
        self.from_user    = _FakeUser(from_d)
        self.chat         = _FakeChat(chat_d)
        self.text         = msg.get("text") or msg.get("caption") or ""
        self.caption      = msg.get("caption")
        self.video        = _FakeMedia(msg["video"])    if "video"    in msg else None
        self.document     = _FakeMedia(msg["document"]) if "document" in msg else None
        self.photo        = msg.get("photo")
        self.audio        = _FakeMedia(msg["audio"])    if "audio"    in msg else None
        self.reply_to_message = FakeMessage(reply_d) if reply_d else None
        self._client      = app

        # .command list: ["cmdname", "arg1", "arg2", ...]
        self.command: list = []
        if self.text.startswith("/"):
            parts = self.text.split()
            cmd   = parts[0].lstrip("/").split("@")[0]
            self.command = [cmd] + parts[1:]

    async def reply_text(self, text, quote=False, reply_markup=None,
                         parse_mode=None, disable_web_page_preview=False, **_):
        pm = enums.ParseMode.MARKDOWN if parse_mode is None else parse_mode
        try:
            return await app.send_message(
                chat_id=self.chat.id, text=text, parse_mode=pm,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        except Exception as e:
            LOG.error(f"reply_text: {e}")

    async def edit_text(self, text, reply_markup=None, parse_mode=None,
                        disable_web_page_preview=False, **_):
        pm = enums.ParseMode.MARKDOWN if parse_mode is None else parse_mode
        try:
            return await app.edit_message_text(
                chat_id=self.chat.id, message_id=self.id,
                text=text, parse_mode=pm,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        except Exception as e:
            LOG.warning(f"edit_text fell back to send: {e}")
            return await self.reply_text(text, reply_markup=reply_markup,
                                         parse_mode=parse_mode)

    async def edit_reply_markup(self, reply_markup=None, **_):
        try:
            return await app.edit_message_reply_markup(
                chat_id=self.chat.id, message_id=self.id,
                reply_markup=reply_markup,
            )
        except Exception as e:
            LOG.warning(f"edit_reply_markup: {e}")

    async def delete(self):
        try:
            await app.delete_messages(self.chat.id, self.id)
        except Exception:
            pass

    async def reply_document(self, doc, caption=None, parse_mode=None, **kw):
        try:
            return await app.send_document(self.chat.id, doc, caption=caption,
                                           parse_mode=parse_mode, **kw)
        except Exception as e:
            LOG.error(f"reply_document: {e}")

    async def reply_video(self, video, caption=None, parse_mode=None,
                          duration=None, width=None, height=None, thumb=None, **kw):
        try:
            return await app.send_video(self.chat.id, video, caption=caption,
                                        parse_mode=parse_mode, duration=duration,
                                        width=width, height=height, thumb=thumb, **kw)
        except Exception as e:
            LOG.error(f"reply_video: {e}")

    async def reply_photo(self, photo, caption=None, **kw):
        try:
            return await app.send_photo(self.chat.id, photo, caption=caption, **kw)
        except Exception as e:
            LOG.error(f"reply_photo: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# FakeCallbackQuery  (mimics pyrogram.types.CallbackQuery)
# ─────────────────────────────────────────────────────────────────────────────

class FakeCallbackQuery:
    """
    Wraps an HTTP Bot API callback_query dict and exposes the Pyrogram
    CallbackQuery interface used by callback_router.
    """
    def __init__(self, cq: dict):
        self.id        = cq.get("id", "")
        self.data      = cq.get("data", "")
        self.from_user = _FakeUser(cq.get("from") or {})
        # The message that carried the inline keyboard
        raw_msg = cq.get("message") or {}
        self.message   = FakeMessage(raw_msg)
        self._client   = app

    async def answer(self, text: str = None, show_alert: bool = False, **_):
        """Dismiss the 'loading' spinner on the button."""
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(f"{BASE}/answerCallbackQuery", json={
                    "callback_query_id": self.id,
                    **({"text": text} if text else {}),
                    "show_alert": show_alert,
                })
        except Exception as e:
            LOG.warning(f"answerCallbackQuery: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Command router
# ─────────────────────────────────────────────────────────────────────────────

def _build_router() -> dict:
    return {
        "start":               _h.start,
        "alive":               _h.alive_cmd,
        "verify":              _h.verify_cmd,
        "help":                _h.help_cmd,
        "status":              _h.status_cmd,
        "history":             _h.history_cmd,
        "limit":               _h.limit_cmd,
        "recording_old":       _h.recording_old_cmd,
        "hindi_or_english":    _h.hindi_or_english_cmd,
        "ott_download":        _h.ott_download_cmd,
        "schedule":            _h.schedule_cmd,
        "schedules":           _h.schedules_cmd,
        "cancel_schedule":     _h.cancel_schedule_cmd,
        "cancel":              _h.cancel_command,
        "rec":                 _h.rec_command,
        "download":            _h.download_command,
        "compress":            _h.compress_cmd,
        "screenshot":          _h.screenshot_cmd,
        "trim":                _h.trim_cmd,
        "get_media_information": _h.get_media_info_cmd,
        "cookies_add":         _h.cookies_add_cmd,
        "cookies_status":      _h.cookies_status_cmd,
        "del_cookies":         _h.del_cookies_cmd,
        "playlist_add":        _h.playlistadd_cmd,
        "playlistadd":         _h.playlistadd_cmd,
        "playlist_delete":     _h.playlistdelete_cmd,
        "playlistdelete":      _h.playlistdelete_cmd,
        "channel_activate":    _h.channel_activate_cmd,
        "channel":             _h.channel_cmd,
        "setlimit":            _h.setlimit_cmd,
        "grant_access":        _h.grant_access_cmd,
        "rage_reset":          _nc.rage_reset_cmd,
        "rage_on":             _nc.rage_on_cmd,
        "niyam_state":         _nc.niyam_state_cmd,
        "niyam_edit":          _nc.niyam_edit_cmd,
        "handlersfree":        _nc.handlersfree_cmd,
        "handlersuser":        _nc.handlersuser_cmd,
        "unlock":              _nc.unlock_cmd,
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTTP long-polling bridge
# ─────────────────────────────────────────────────────────────────────────────

async def _http_bridge(router: dict):
    offset = 0
    LOG.info(f"[Bridge] started — {len(router)} commands registered")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    f"{BASE}/getUpdates",
                    params={
                        "offset": offset, "limit": 100, "timeout": 20,
                        "allowed_updates": '["message","callback_query","edited_message"]',
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json()

                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1

                    if "callback_query" in upd:
                        cq_d = upd["callback_query"]
                        uid  = (cq_d.get("from") or {}).get("id", 0)
                        LOG.info(f"[Bridge] callback uid={uid} data={cq_d.get('data','')!r}")
                        asyncio.create_task(_handle_callback(cq_d))
                        continue

                    msg_d = upd.get("message") or upd.get("edited_message")
                    if not msg_d:
                        continue
                    uid  = (msg_d.get("from") or {}).get("id", 0)
                    txt  = msg_d.get("text") or msg_d.get("caption") or ""
                    LOG.info(f"[Bridge] uid={uid} text={txt[:200]!r}")
                    asyncio.create_task(_handle_msg(msg_d, router))

            except asyncio.CancelledError:
                LOG.info("[Bridge] stopped")
                return
            except Exception as e:
                LOG.warning(f"[Bridge] error: {e}")
                await asyncio.sleep(3)


async def _handle_msg(msg_d: dict, router: dict):
    fake = FakeMessage(msg_d)
    cmd  = fake.command[0].lower() if fake.command else ""

    LOG.info(f"[Handle] cmd={cmd!r} uid={fake.from_user.id}")

    if fake.document and not cmd:
        try:
            await _h.document_handler(app, fake)
        except Exception as e:
            LOG.error(f"[Handle] document_handler: {e}", exc_info=True)
        return

    handler_fn = router.get(cmd)
    if handler_fn:
        try:
            await handler_fn(app, fake)
        except Exception as e:
            LOG.error(f"[Handle] {cmd!r}: {e}", exc_info=True)
    elif fake.text and not cmd:
        try:
            await _h.text_router(app, fake)
        except Exception as e:
            LOG.error(f"[Handle] text_router: {e}", exc_info=True)
    elif cmd:
        LOG.warning(f"[Handle] unknown cmd={cmd!r}")


async def _handle_callback(cq_d: dict):
    fake_cq = FakeCallbackQuery(cq_d)
    LOG.info(f"[CB] data={fake_cq.data!r} uid={fake_cq.from_user.id}")
    try:
        await _h.callback_router(app, fake_cq)
    except Exception as e:
        LOG.error(f"[CB] callback_router: {e}", exc_info=True)
        try:
            await fake_cq.answer("⚠️ Error processing request", show_alert=True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    import datetime, zoneinfo
    await app.start()
    me = await app.get_me()
    LOG.info(f"Bot ready: @{me.username}  id={me.id}")

    router = _build_router()
    bridge = asyncio.create_task(_http_bridge(router))

    # ── Startup notification ──────────────────────────────────────────────
    tz  = zoneinfo.ZoneInfo("Asia/Kolkata")
    now = datetime.datetime.now(tz).strftime("%d %b %Y, %I:%M:%S %p")
    startup_msg = (
        "**Bot is Now Online!**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 **Bot:** {me.first_name}\n"
        f"🕒 **Started At:** `{now}`\n"
        "🌍 **Timezone:** Asia/Kolkata"
    )
    for oid in config.OWNER_ID:
        try:
            await app.send_message(oid, startup_msg)
        except Exception as e:
            LOG.warning(f"Startup msg to {oid} failed: {e}")

    announcement = niyam.pop_culprit_announcement()
    if announcement:
        for oid in config.OWNER_ID:
            try:
                await app.send_message(oid, announcement)
            except Exception:
                pass

    await idle()
    bridge.cancel()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
