import os


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


class Config:
    ROOM_INDEX = os.getenv("ROOM_INDEX", "rooms_v01")
    APP_ID = os.getenv("ZALO_APP_ID", "").strip()
    OA_ID = os.getenv("ZALO_OA_ID", "").strip()
    SECRET_KEY = os.getenv("SESSION_SECRET", "")
    ZALO_ADMIN_ID = os.getenv("ZALO_ADMIN_ID", "").strip()
    ZALO_ERROR_ALERTS_ENABLED = os.getenv("ZALO_ERROR_ALERTS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    ZALO_NOTIFY_HTTP_4XX = os.getenv("ZALO_NOTIFY_HTTP_4XX", "false").lower() in {"1", "true", "yes", "on"}
    ZALO_ERROR_ALERT_COOLDOWN_SECONDS = _env_int("ZALO_ERROR_ALERT_COOLDOWN_SECONDS", 300, 0)
    LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", "logs/app.log")
    LOG_MAX_BYTES = _env_int("LOG_MAX_BYTES", 5_000_000, 100_000)
    LOG_BACKUP_COUNT = _env_int("LOG_BACKUP_COUNT", 5, 1)
