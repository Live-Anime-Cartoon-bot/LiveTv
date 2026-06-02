from os import environ
import logging

_log = logging.getLogger(__name__)


def _parse_id_list(name: str, raw: str) -> list:
    ids = []
    bad = []
    for tok in (raw or "").replace(",", " ").split():
        tok = tok.strip()
        if not tok:
            continue
        try:
            ids.append(int(tok))
        except ValueError:
            bad.append(tok)
    if bad:
        _log.warning(
            "%s contains non-numeric values that were skipped: %s. "
            "Telegram user IDs must be integers (use @userinfobot to find yours).",
            name,
            bad,
        )
    return ids


def _parse_int(name: str, raw: str) -> int:
    try:
        return int((raw or "0").strip())
    except ValueError:
        _log.error("%s must be an integer, got: %r", name, raw)
        return 0


API_ID = _parse_int("API_ID", environ.get("API_ID", "0"))
API_HASH = environ.get("API_HASH", "")
BOT_TOKEN = environ.get("BOT_TOKEN", "")

AUTH_USERS = _parse_id_list("AUTH_USERS", environ.get("AUTH_USERS", ""))
OWNER_IDS = _parse_id_list("OWNER_IDS", environ.get("OWNER_IDS", ""))

DOWNLOAD_DIRECTORY = environ.get("DOWNLOAD_DIRECTORY", "./bot/downloads")
DATA_DIRECTORY = environ.get("DATA_DIRECTORY", "./bot/data")
COOKIES_DIRECTORY = environ.get("COOKIES_DIRECTORY", "./bot/data/cookies")

# How long to keep uploaded recordings on the server before auto-deletion.
RETENTION_HOURS = _parse_int("RETENTION_HOURS", environ.get("RETENTION_HOURS", "3"))

DEFAULT_METADATA = environ.get("DEFAULT_METADATA", "")
DEFAULT_FILENAME = environ.get("DEFAULT_FILENAME", "Anime Cartoon")
# Brand header that appears as the first line of every uploaded video caption.
BRAND_TITLE = environ.get("BRAND_TITLE", "Anime Cartoon")

TIMEZONE = environ.get("TIMEZONE", "Asia/Kolkata")

SUPPORT_USERNAME = environ.get("SUPPORT_USERNAME", "LS_Owner_bot")
SUPPORT_CHANNEL = environ.get("SUPPORT_CHANNEL", "LittleSinghamChannel")

# Group membership gate.
# Set GROUP_CHAT_ID to the numeric ID of your group (bot must be a member).
# Users not in the group (and not owner/admin) will be shown a join prompt.
# Set to 0 to disable the gate entirely.
GROUP_CHAT_ID   = _parse_int("GROUP_CHAT_ID", environ.get("GROUP_CHAT_ID", "-1003726271113"))
GROUP_INVITE_LINK = environ.get("GROUP_INVITE_LINK", "https://t.me/+MuzbPV3m55llNmFl")

# Shrinkme.io API key for ad-click verification links.
SHRINKME_API_KEY = environ.get("SHRINKME_API_KEY", "9503d9bf87c90aa9e0aab35d4dec7d1ce24c0a23")

# Bot username (without @) — used to build per-user deep-links for /verify.
BOT_USERNAME = environ.get("BOT_USERNAME", "M3u8LiveRecordingBot")

# Google Drive — Service Account (optional, for shared/admin uploads).
# Set both to enable. Users can also connect their own accounts via /googledrive.
GDRIVE_SA_JSON   = environ.get("GDRIVE_SA_JSON",   "")
GDRIVE_FOLDER_ID = environ.get("GDRIVE_FOLDER_ID", "")

# Google Drive — OAuth2 for per-user login via /googledrive command.
# Create an OAuth2 client of type "TVs and Limited Input devices" in Google Cloud Console.
# Enable the Drive API, then copy the client_id and client_secret here.
GOOGLE_CLIENT_ID     = environ.get("GOOGLE_CLIENT_ID",     "")
GOOGLE_CLIENT_SECRET = environ.get("GOOGLE_CLIENT_SECRET", "")
