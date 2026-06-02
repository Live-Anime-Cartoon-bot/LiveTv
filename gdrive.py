import asyncio
import json
import logging
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional

LOG = logging.getLogger(__name__)

_SCOPES          = ["https://www.googleapis.com/auth/drive.file"]
_DEVICE_AUTH_URL = "https://oauth2.googleapis.com/device/code"
_TOKEN_URL       = "https://oauth2.googleapis.com/token"
_GRANT_TYPE_DEV  = "urn:ietf:wg:oauth:2.0:device_code"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _oauth_enabled() -> bool:
    import config
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def _sa_enabled() -> bool:
    import config
    return bool(config.GDRIVE_SA_JSON and config.GDRIVE_FOLDER_ID)


def _is_enabled() -> bool:
    return _sa_enabled() or _oauth_enabled()


# ---------------------------------------------------------------------------
# Per-user token storage
# ---------------------------------------------------------------------------

def _token_dir() -> str:
    import config
    d = os.path.join(config.DATA_DIRECTORY, "gdrive_tokens")
    os.makedirs(d, exist_ok=True)
    return d


def _token_path(user_id: int) -> str:
    return os.path.join(_token_dir(), f"{user_id}.json")


def is_user_connected(user_id: int) -> bool:
    return os.path.exists(_token_path(user_id))


def disconnect_user(user_id: int) -> bool:
    p = _token_path(user_id)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def _save_token(user_id: int, token_data: dict):
    token_data["saved_at"] = time.time()
    with open(_token_path(user_id), "w") as f:
        json.dump(token_data, f)


def _load_token(user_id: int) -> dict:
    with open(_token_path(user_id)) as f:
        return json.load(f)


def get_sa_email() -> str:
    try:
        import config
        return json.loads(config.GDRIVE_SA_JSON).get("client_email", "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# OAuth2 device flow
# ---------------------------------------------------------------------------

def start_device_flow_sync() -> dict:
    """Start OAuth2 device flow. Returns {device_code, user_code, verification_url, interval, expires_in}."""
    import config
    data = urllib.parse.urlencode({
        "client_id": config.GOOGLE_CLIENT_ID,
        "scope":     " ".join(_SCOPES),
    }).encode()
    req = urllib.request.Request(
        _DEVICE_AUTH_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent":   "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _poll_token_sync(device_code: str) -> Optional[dict]:
    """Poll for token. Returns token dict if authorized, None if still pending."""
    import config
    data = urllib.parse.urlencode({
        "client_id":     config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "device_code":   device_code,
        "grant_type":    _GRANT_TYPE_DEV,
    }).encode()
    req = urllib.request.Request(
        _TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent":   "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            return resp if "access_token" in resp else None
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        err  = body.get("error", "")
        if err in ("authorization_pending", "slow_down"):
            return None
        raise Exception(f"OAuth2 error: {err} — {body.get('error_description', '')}")


async def poll_and_save_token(client, user_id: int, device_code: str,
                               interval: int, expires_in: int):
    """Background task: polls until user authorizes or code expires."""
    deadline = time.time() + expires_in
    while time.time() < deadline:
        await asyncio.sleep(max(interval, 5))
        try:
            tok = await asyncio.to_thread(_poll_token_sync, device_code)
        except Exception as e:
            LOG.error(f"GDrive OAuth poll error uid={user_id}: {e}")
            try:
                await client.send_message(user_id, f"❌ Google Drive auth failed: `{e}`")
            except Exception:
                pass
            return
        if tok:
            _save_token(user_id, tok)
            LOG.info(f"GDrive OAuth token saved for uid={user_id}")
            try:
                await client.send_message(
                    user_id,
                    "✅ **Google Drive Connected!**\n\n"
                    "Ab aapki recordings automatically **aapki Google Drive** par upload hongi.\n\n"
                    "Disconnect karne ke liye: /googledrive disconnect\n"
                    "Status dekhne ke liye: /googledrive status",
                )
            except Exception:
                pass
            return
    try:
        await client.send_message(
            user_id,
            "⏰ **Google Drive auth timeout.**\n\n"
            "Code expire ho gaya. Fir se try karein: /googledrive"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Drive service builders
# ---------------------------------------------------------------------------

def _build_user_service(user_id: int):
    import config
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleRequest
    from googleapiclient.discovery import build

    tok   = _load_token(user_id)
    creds = Credentials(
        token         = tok.get("access_token"),
        refresh_token = tok.get("refresh_token"),
        token_uri     = _TOKEN_URL,
        client_id     = config.GOOGLE_CLIENT_ID,
        client_secret = config.GOOGLE_CLIENT_SECRET,
        scopes        = _SCOPES,
    )
    if not creds.valid and creds.refresh_token:
        creds.refresh(GoogleRequest())
        _save_token(user_id, {
            "access_token":  creds.token,
            "refresh_token": creds.refresh_token,
        })
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _build_sa_service():
    import config
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    sa_info = json.loads(config.GDRIVE_SA_JSON)
    creds   = service_account.Credentials.from_service_account_info(sa_info, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def _upload_sync(file_path: str, filename: str, folder_id: Optional[str],
                 user_id: Optional[int]) -> str:
    from googleapiclient.http import MediaFileUpload

    if user_id and is_user_connected(user_id):
        service = _build_user_service(user_id)
        meta    = {"name": filename}
        if folder_id:
            meta["parents"] = [folder_id]
    else:
        import config
        service = _build_sa_service()
        meta    = {"name": filename, "parents": [folder_id or config.GDRIVE_FOLDER_ID]}

    mime_type = "video/x-matroska" if filename.endswith(".mkv") else "video/mp4"
    media     = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
    f = service.files().create(
        body=meta, media_body=media, fields="id,webViewLink"
    ).execute()
    link = f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view"
    LOG.info(f"GDrive upload done: {filename} → {link}")
    return link


async def upload_and_notify(client, chat_id: int, file_path: str, filename: str):
    """Upload to Drive (user tokens preferred, SA as fallback) and send the link."""
    user_connected = is_user_connected(chat_id)
    if not user_connected and not _sa_enabled():
        return
    try:
        import config
        folder_id = None if user_connected else config.GDRIVE_FOLDER_ID
        link = await asyncio.to_thread(
            _upload_sync, file_path, filename, folder_id, chat_id
        )
        await client.send_message(
            chat_id,
            f"🤖 **Google Drive Upload Complete!**\n\n"
            f"📄 File: `{filename}`\n"
            f"🔗 [Open in Drive]({link})",
            disable_web_page_preview=True,
        )
    except Exception as e:
        LOG.error(f"GDrive upload failed for {filename}: {e}")
        try:
            await client.send_message(chat_id, f"⚠️ Google Drive upload failed: `{e}`")
        except Exception:
            pass
