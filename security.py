import base64, hashlib, hmac, json, os, time
from dataclasses import dataclass
from typing import Optional
from fastapi import Depends, Header, HTTPException, Request

SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "28800"))
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
WEBHOOK_SECRET = os.getenv("ZALO_WEBHOOK_SECRET", "")

@dataclass(frozen=True)
class Principal:
    username: str
    role: str

def _enc(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

def _dec(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))

def validate_security_config() -> None:
    if len(SESSION_SECRET) < 32:
        raise RuntimeError("SESSION_SECRET phải có tối thiểu 32 ký tự.")

def create_session_token(username: str, role: str) -> str:
    validate_security_config()
    body = _enc(json.dumps({"sub": username, "role": role, "iat": int(time.time()), "exp": int(time.time()) + SESSION_TTL_SECONDS}, separators=(",", ":")).encode())
    signature = _enc(hmac.new(SESSION_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"

def decode_session_token(token: str) -> Principal:
    try:
        body, supplied = token.split(".", 1)
        expected = _enc(hmac.new(SESSION_SECRET.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied, expected): raise ValueError()
        payload = json.loads(_dec(body))
        if int(payload["exp"]) < int(time.time()): raise ValueError()
        return Principal(str(payload["sub"]), str(payload["role"]))
    except Exception as exc:
        raise HTTPException(401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn.") from exc

def get_current_user(authorization: Optional[str] = Header(default=None)) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Vui lòng đăng nhập.")
    return decode_session_token(authorization[7:].strip())

def require_admin(user: Principal = Depends(get_current_user)) -> Principal:
    if user.role != "SUPER_ADMIN": raise HTTPException(403, "Bạn không có quyền quản trị.")
    return user

async def verify_zalo_webhook(request: Request) -> None:
    if not WEBHOOK_SECRET:
        if os.getenv("ENVIRONMENT", "development").lower() == "production":
            raise HTTPException(503, "Webhook secret chưa được cấu hình.")
        return
    supplied = request.headers.get("X-ZEvent-Signature", "").removeprefix("sha256=")
    expected = hmac.new(WEBHOOK_SECRET.encode(), await request.body(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, expected): raise HTTPException(401, "Chữ ký webhook không hợp lệ.")

def enforce_rate_limit(redis_client, key: str, limit: int, window_seconds: int) -> None:
    try:
        redis_key = f"rate_limit:{key}"
        count = redis_client.incr(redis_key)
        if count == 1: redis_client.expire(redis_key, window_seconds)
        if count > limit: raise HTTPException(429, "Bạn thao tác quá nhanh. Vui lòng thử lại sau.")
    except HTTPException: raise
    except Exception: return
