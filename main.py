import config
from logic import app, LOG, sweep_old_downloads, _retention_label
import command  # registers all handlers as a side effect of import

if __name__ == "__main__":
    print("Starting Video Recorder Bot...")
    if not config.BOT_TOKEN or not config.API_ID or not config.API_HASH:
        raise SystemExit(
            "Missing BOT_TOKEN / API_ID / API_HASH. Set them in Replit Secrets."
        )
    sweep_old_downloads()
    LOG.info(
        "Recordings will be auto-deleted from the server after %s.",
        _retention_label(),
    )
    app.run()
