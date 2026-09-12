import hashlib
import logging
import os
import re
import sys
import threading
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

from config import Config


_LOGGER_NAME = "zalo_room_app.errors"
_logger = logging.getLogger(_LOGGER_NAME)
_logger.setLevel(logging.ERROR)
_logger.propagate = False
_sender: Optional[Callable[..., bool]] = None
_send_lock = threading.Lock()
_last_sent: dict[str, float] = {}
_thread_state = threading.local()


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


def _configure_file_handler() -> None:
    if _logger.handlers:
        return
    log_path = Path(Config.LOG_FILE_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=Config.LOG_MAX_BYTES,
        backupCount=Config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(_RedactingFormatter("%(asctime)sZ | %(levelname)s | %(message)s"))
    _logger.addHandler(handler)


def _redact(value: object) -> str:
    text = str(value or "")
    patterns = (
        (r"(?i)(password|secret|token|api[_-]?key|authorization)(\s*[:=]\s*)([^\s,;}]+)", r"\1\2***"),
        (r"(?i)(postgres(?:ql)?://[^:/\s]+:)([^@\s]+)(@)", r"\1***\3"),
        (r"(?i)(access_token=)([^&\s]+)", r"\1***"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def configure_error_reporting(zalo_sender: Callable[..., bool]) -> None:
    global _sender
    _sender = zalo_sender


def _send_zalo_alert(message: str, fingerprint: str) -> None:
    if not Config.ZALO_ERROR_ALERTS_ENABLED or not Config.ZALO_ADMIN_ID or not _sender:
        return
    import time

    now = time.monotonic()
    with _send_lock:
        previous = _last_sent.get(fingerprint, 0.0)
        if now - previous < Config.ZALO_ERROR_ALERT_COOLDOWN_SECONDS:
            return
        _last_sent[fingerprint] = now

    def worker() -> None:
        if getattr(_thread_state, "sending", False):
            return
        _thread_state.sending = True
        try:
            sent = _sender(user_id=Config.ZALO_ADMIN_ID, ai_reply=message[:1800])
            if sent is False:
                _logger.error("Zalo từ chối cảnh báo lỗi gửi tới Admin")
        except Exception:
            _logger.error("Không thể gửi cảnh báo lỗi tới Zalo Admin", exc_info=True)
        finally:
            _thread_state.sending = False

    threading.Thread(target=worker, name="zalo-error-alert", daemon=True).start()


def report_error(
    message: str,
    exc: Optional[BaseException] = None,
    context: Optional[str] = None,
    notify: bool = True,
) -> None:
    _configure_file_handler()
    clean_message = _redact(message)
    clean_context = _redact(context or "")
    log_message = clean_message if not clean_context else f"{clean_message} | context={clean_context}"
    if exc:
        _logger.error(log_message, exc_info=(type(exc), exc, exc.__traceback__))
        error_name = type(exc).__name__
        error_detail = _redact(str(exc))
    else:
        _logger.error(log_message)
        error_name = "ApplicationError"
        error_detail = clean_message

    if not notify:
        return
    fingerprint = hashlib.sha256(f"{error_name}|{clean_message}|{clean_context}".encode("utf-8")).hexdigest()
    alert = (
        "🚨 LỖI ỨNG DỤNG NHÀ TRỌ\n"
        f"Thời gian UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"Loại: {error_name}\n"
        f"Vị trí: {clean_context or 'Không xác định'}\n"
        f"Chi tiết: {error_detail[:900]}"
    )
    _send_zalo_alert(alert, fingerprint)


def install_process_exception_hooks() -> None:
    previous_hook = sys.excepthook

    def process_hook(exc_type, exc_value, exc_traceback):
        report_error("Exception chưa được xử lý", exc_value, "process")
        previous_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = process_hook
    if hasattr(threading, "excepthook"):
        previous_thread_hook = threading.excepthook

        def thread_hook(args):
            report_error("Exception chưa được xử lý trong thread", args.exc_value, f"thread:{args.thread.name}")
            previous_thread_hook(args)

        threading.excepthook = thread_hook


_configure_file_handler()
