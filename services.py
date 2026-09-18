import os
import io
import json
import requests
import uuid
import time
import re
import random
import hashlib
import ipaddress
import unicodedata
from urllib.parse import urlparse
from typing import Dict, List, Optional
from datetime import datetime, timedelta, timezone
import pytz
import bcrypt
import phonenumbers
from phonenumbers import NumberParseException
import pandas as pd
from pydantic import BaseModel, Field, validator
from fastapi import HTTPException, Header, Depends, status
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from qdrant_client.http import models as qdrant_models
from google import genai
from google.genai import types
import cloudinary
import cloudinary.uploader
import string
from error_reporting import report_error
from config import Config
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import redis
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, JSON, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql import func
from contextlib import asynccontextmanager

redis_client = redis.Redis.from_url(os.environ.get("REDIS_URL"), decode_responses=True)

app = FastAPI()

# 1. Đảm bảo thư mục static tồn tại
if not os.path.exists("static"):
    os.makedirs("static")

# 2. MOUNT thư mục "static" ra URL route "/static"
# Dòng này giúp truy cập được file qua https://domain.com/static/filename.png
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- CẤU HÌNH MÔI TRƯỜNG & KHỞI TẠO SERVICES ---
ZALO_OA_ID = os.environ.get("ZALO_OA_ID")
ZALO_APP_ID = os.environ.get("ZALO_APP_ID")
ZALO_ACCESS_TOKEN = os.environ.get("ZALO_ACCESS_TOKEN")
ZALO_SECRET_KEY = os.environ.get("ZALO_SECRET_KEY")
ZALO_REFRESH_TOKEN = os.environ.get("ZALO_REFRESH_TOKEN")
ZALO_TEMPLATE_ID = os.environ.get("YOUR_ZNS_TEMPLATE_ID")
SQLALCHEMY_DATABASE_URL = os.environ.get("SQLALCHEMY_DATABASE_URL")
MEDIA_DIR = "static/media"
os.makedirs(MEDIA_DIR, exist_ok=True)

APP_TIMEZONE = "Asia/Ho_Chi_Minh"
VN_TZ = pytz.timezone(APP_TIMEZONE)

# Đồng bộ múi giờ của toàn bộ tiến trình trên Render/Linux.
os.environ["TZ"] = APP_TIMEZONE
if hasattr(time, "tzset"):
    time.tzset()


def vietnam_now() -> datetime:
    """Giờ Việt Nam dạng naive, tương thích các cột PostgreSQL TIMESTAMP hiện có."""
    return datetime.now(VN_TZ).replace(tzinfo=None)


def vietnam_datetime_iso(value: Optional[datetime]) -> Optional[str]:
    """Xuất datetime theo ISO 8601 và luôn kèm offset giờ Việt Nam (+07:00)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = VN_TZ.localize(value)
    else:
        value = value.astimezone(VN_TZ)
    return value.isoformat()
COLLECTION_NAME = "rooms_v01"
VECTOR_SIZE = 768

# Database Config
engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()




# External Clients
QDRANT_TIMEOUT_SECONDS = float(os.getenv("QDRANT_TIMEOUT_SECONDS", "30"))
QDRANT_SEARCH_RETRIES = max(1, int(os.getenv("QDRANT_SEARCH_RETRIES", "3")))
SEARCH_CANDIDATE_LIMIT = max(100, int(os.getenv("SEARCH_CANDIDATE_LIMIT", "2000")))
qdrant_client = QdrantClient(
    url=os.environ.get("QDRANT_URL"),
    api_key=os.environ.get("QDRANT_API_KEY"),
    timeout=QDRANT_TIMEOUT_SECONDS
)

CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL")
if CLOUDINARY_URL:
    cloudinary.config(cloudinary_url=CLOUDINARY_URL)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash").strip()
GEMINI_TEXT_RETRIES = max(1, min(int(os.getenv("GEMINI_TEXT_RETRIES", "2")), 3))
GEMINI_EMBED_RETRIES = max(1, min(int(os.getenv("GEMINI_EMBED_RETRIES", "2")), 3))
GEMINI_EMBED_CACHE_TTL = max(3600, int(os.getenv("GEMINI_EMBED_CACHE_TTL", "604800")))
GEMINI_MAX_OUTPUT_TOKENS = max(256, int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "2048")))
GEMINI_EXCEL_MAX_OUTPUT_TOKENS = max(2048, int(os.getenv("GEMINI_EXCEL_MAX_OUTPUT_TOKENS", "8192")))

# Bộ nhớ đệm tạm thời
CACHE_TTL_SECONDS = 600
MAX_MEDIA_PER_ROOM = max(0, int(os.getenv("MAX_MEDIA_PER_ROOM", "0")))
MAX_SEARCH_MEDIA = max(0, int(os.getenv("MAX_SEARCH_MEDIA", "0")))
MAX_SEARCH_ROOMS = min(20, max(1, int(os.getenv("MAX_SEARCH_ROOMS", "20"))))
MAX_SEARCH_MEDIA_PER_ROOM = max(0, int(os.getenv("MAX_SEARCH_MEDIA_PER_ROOM", "0")))
MAX_IMAGE_UPLOAD_BYTES = max(1, int(os.getenv("MAX_IMAGE_UPLOAD_MB", "15"))) * 1024 * 1024
MAX_VIDEO_UPLOAD_BYTES = max(1, int(os.getenv("MAX_VIDEO_UPLOAD_MB", "80"))) * 1024 * 1024


def get_public_server_domain(fallback_url: str = "") -> str:
    """Lấy origin HTTPS công khai từ cấu hình Render hoặc request hiện tại."""
    candidates = (
        os.getenv("SERVER_DOMAIN", ""),
        os.getenv("RENDER_EXTERNAL_URL", ""),
        fallback_url,
    )
    for candidate in candidates:
        try:
            parsed = urlparse(str(candidate or "").strip())
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not host or host == "localhost" or not parsed.netloc:
                continue
            try:
                if ipaddress.ip_address(host).is_private:
                    continue
            except ValueError:
                pass
            # Chỉ giữ scheme + host/port; loại path/query có thể bị nhập nhầm.
            return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        except Exception:
            continue
    return ""


def get_zalo_request_image_url(fallback_url: str = "") -> str:
    domain = get_public_server_domain(fallback_url)
    return f"{domain}/static/icon_zalo_room.png" if domain else ""


def is_valid_zalo_media_url(value: str) -> bool:
    """Loại URL rỗng, local hoặc không HTTPS trước khi gọi Zalo OA API."""
    try:
        parsed = urlparse(str(value or "").strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or host == "localhost":
            return False
        try:
            return not ipaddress.ip_address(host).is_private
        except ValueError:
            return True
    except Exception:
        return False


def apply_media_limit(items: List[str], limit: int = MAX_MEDIA_PER_ROOM) -> List[str]:
    """limit=0 nghĩa là giữ toàn bộ media, không cắt danh sách."""
    values = list(items or [])
    return values if limit <= 0 else values[:limit]


def normalize_room_media_urls(value) -> List[str]:
    """Chuẩn hóa danh sách URL media từ form, AI hoặc dữ liệu cũ trong DB."""
    if value is None:
        return []
    raw_items = re.split(r"[,;\n\r]+", value) if isinstance(value, str) else list(value)
    normalized = []
    for item in raw_items:
        url = str(item or "").strip()
        if url and url not in normalized:
            normalized.append(url)
    return apply_media_limit(normalized)

# Initializing Qdrant Collection & Index
try:
    if not qdrant_client.collection_exists(collection_name=COLLECTION_NAME):
        qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
        )
except Exception as e:
    report_error("Lỗi khởi tạo Qdrant", e, "services.startup")

try:
    qdrant_client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="address",
        field_schema=qdrant_models.TextIndexParams(
            type="text",
            tokenizer=qdrant_models.TokenizerType.WORD, # Tách từ để tìm kiếm linh hoạt (MatchText)
            min_token_len=2,
            max_token_len=20,
            lowercase=True, # Không phân biệt hoa/thường
    )
)
    qdrant_client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="status",
        field_schema=qdrant_models.PayloadSchemaType.KEYWORD,
    )
    qdrant_client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="price",
        field_schema=qdrant_models.PayloadSchemaType.FLOAT,
    )
    qdrant_client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="landlord_phone",
        field_schema=qdrant_models.PayloadSchemaType.KEYWORD,
    )
    qdrant_client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="room_code",
        field_schema=qdrant_models.PayloadSchemaType.KEYWORD,
    )
    
    qdrant_client.create_payload_index(
    collection_name=COLLECTION_NAME,
    field_name="room_name",
    field_schema=qdrant_models.PayloadSchemaType.KEYWORD,
)
except Exception:
    pass

# --- SQLALCHEMY MODELS ---
class UserWeb(Base):
    __tablename__ = "user_web"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, unique=True, index=True, nullable=True)
    phone = Column(String, unique=True, index=True, nullable=False)
    otp = Column(String, nullable=True)
    password = Column(String, nullable=True)
    expired_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=vietnam_now, onupdate=vietnam_now)

# --- MODEL ORDER_ROOM TRONG SQLALCHEMY ---
class OrderRoom(Base):
    __tablename__ = "order_room"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # ✅ SỬA THÀNH: Bỏ unique=True, chỉ giữ index=True để tra cứu nhanh
    tenant_zalo_id = Column(String, index=True, unique=False)
    
    tenant_phone = Column(String, nullable=True)
    landlord_zalo_id = Column(String, nullable=True)
    landlord_phone = Column(String, nullable=True)
    room_code = Column(String, nullable=False)
    # 🆕 Thêm trường thời gian đến xem phòng
    viewing_time = Column(DateTime, nullable=True)
    status = Column(String, nullable=False, default="CHỜ XEM")
    created_at = Column(DateTime, default=vietnam_now)
    updated_at = Column(DateTime, default=vietnam_now, onupdate=vietnam_now)

class ZaloToken(Base):
    __tablename__ = "zalo_tokens"

    id = Column(Integer, primary_key=True, index=True)
    access_token = Column(String, nullable=False)
    refresh_token = Column(String, nullable=False)

class RoomRecord(Base):
    """Bản dữ liệu nghiệp vụ có thể sao lưu; Qdrant tiếp tục làm chỉ mục tìm kiếm."""
    __tablename__ = "room_records"
    id = Column(String, primary_key=True)
    landlord_phone = Column(String, index=True, nullable=False)
    room_code = Column(String, unique=True, index=True, nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=vietnam_now)
    updated_at = Column(DateTime, default=vietnam_now, onupdate=vietnam_now)


class RoomReport(Base):
    __tablename__ = "room_reports"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    room_id = Column(String, index=True, nullable=False)
    room_code = Column(String, index=True, nullable=False)
    room_name = Column(String, nullable=True)
    address = Column(String, nullable=False)
    reporter_username = Column(String, index=True, nullable=False)
    content = Column(String, nullable=False)
    status = Column(String, nullable=False, default="MỚI")
    created_at = Column(DateTime, default=vietnam_now, index=True)
    updated_at = Column(DateTime, default=vietnam_now, onupdate=vietnam_now)


class RoomPostingBlock(Base):
    """Quy tắc chặn người dùng tạo/cập nhật phòng theo SĐT hoặc một phần địa chỉ."""
    __tablename__ = "room_posting_blocks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    block_type = Column(String, index=True, nullable=False)  # PHONE hoặc ADDRESS
    block_value = Column(String, nullable=False)
    normalized_value = Column(String, index=True, nullable=False)
    reason = Column(String, nullable=True)
    is_active = Column(Integer, nullable=False, default=1)
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime, default=vietnam_now, index=True)
    updated_at = Column(DateTime, default=vietnam_now, onupdate=vietnam_now)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    actor = Column(String, index=True, nullable=False)
    action = Column(String, index=True, nullable=False)
    target_id = Column(String, index=True, nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=vietnam_now, index=True)

Base.metadata.create_all(bind=engine)


def ensure_order_room_columns() -> None:
    """Bổ sung cột cho DB hiện hữu vì create_all không thay đổi bảng đã tồn tại."""
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE order_room ADD COLUMN IF NOT EXISTS status VARCHAR"))
        connection.execute(text("ALTER TABLE order_room ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"))
        connection.execute(text("UPDATE order_room SET status = 'CHỜ XEM' WHERE status IS NULL OR BTRIM(status) = ''"))
        connection.execute(text("UPDATE order_room SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Ho_Chi_Minh') WHERE updated_at IS NULL"))
        connection.execute(text("ALTER TABLE order_room ALTER COLUMN status SET DEFAULT 'CHỜ XEM'"))
        connection.execute(text("ALTER TABLE order_room ALTER COLUMN status SET NOT NULL"))


ensure_order_room_columns()

# --- PYDANTIC SCHEMAS ---
class UnifiedLoginSchema(BaseModel):
    phone: Optional[str] = None       
    password: str

class RegisterModel(BaseModel):
    phone: str
    otp: str
    password: str
    
class LoginUserSchema(BaseModel):
    phone: str
    password: str

class AdminLoginSchema(BaseModel):
    username: str
    password: str

class AdminChangePasswordSchema(BaseModel):
    username: Optional[str] = None
    old_password: str
    new_password: str

class RequestOTPModel(BaseModel):
    phone: str


class DeleteSelectedRoomsSchema(BaseModel):
    room_ids: List[str] = Field(..., min_items=1, max_items=500)

    @validator("room_ids", pre=True)
    def validate_room_ids(cls, value):
        if not isinstance(value, list):
            raise ValueError("Danh sách phòng không hợp lệ.")
        room_ids = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if not room_ids:
            raise ValueError("Vui lòng chọn ít nhất một phòng.")
        if len(room_ids) > 500:
            raise ValueError("Chỉ được xóa tối đa 500 phòng trong một lần.")
        return room_ids


class RoomReportStatusSchema(BaseModel):
    status: str

    @validator("status", pre=True)
    def validate_report_status(cls, value):
        clean_value = str(value or "").strip().upper()
        if clean_value not in {"MỚI", "ĐANG XỬ LÝ", "ĐÃ XỬ LÝ", "BỎ QUA"}:
            raise ValueError("Trạng thái report không hợp lệ.")
        return clean_value


class RoomPostingBlockSchema(BaseModel):
    block_type: str
    block_value: str = Field(..., min_length=2, max_length=255)
    reason: Optional[str] = Field(default=None, max_length=500)
    is_active: bool = True

    @validator("block_type", pre=True)
    def validate_block_type(cls, value):
        clean_value = str(value or "").strip().upper()
        if clean_value not in {"PHONE", "ADDRESS"}:
            raise ValueError("Loại chặn phải là PHONE hoặc ADDRESS.")
        return clean_value

    @validator("block_value", pre=True)
    def validate_block_value(cls, value):
        clean_value = str(value or "").strip()
        if len(clean_value) < 2:
            raise ValueError("Giá trị chặn không được để trống.")
        return clean_value

class RoomCreateUpdateSchema(BaseModel):
    room_code: Optional[str] = Field(default=None, description="Mã phòng 6 ký tự")
    address: str = Field(..., min_length=1, description="Địa chỉ phòng không được để trống")
    price: str = Field(..., min_length=1, description="Giá phòng không được để trống")
    room_name: Optional[str] = "Phòng trọ"
    floor: Optional[str] = "Chưa rõ"                  
    is_private_bathroom: Optional[str] = "Chưa rõ"   
    has_ac: Optional[str] = "Chưa rõ"                 
    has_heater: Optional[str] = "Chưa rõ"             
    has_washer: Optional[str] = "Chưa rõ"             
    has_fridge: Optional[str] = "Chưa rõ"
    allow_pets: Optional[str] = "Chưa rõ"             
    has_balcony: Optional[str] = "Chưa rõ"            
    has_window: Optional[str] = "Chưa rõ"             
    has_fingerprint_lock: Optional[str] = "Chưa rõ"   
    parking_info: Optional[str] = "Chưa rõ"     
    bed: Optional[str] = "Chưa rõ" 
    wardrobe: Optional[str] = "Chưa rõ"  
    room_size: Optional[str] = "Chưa rõ"
    max_occupants: Optional[str] = "Chưa rõ"         
    other_amenities: Optional[str] = "Chưa rõ"       
    service_fees: Optional[str] = "Chưa rõ"           
    media_urls: Optional[List[str]] = Field(default_factory=list)
    move_in_date: Optional[str] = None
    status: Optional[str] = "TRỐNG"
    landlord_phone: str = Field(..., min_length=10, description="Số điện thoại người đăng / chủ nhà (bắt buộc)")


    @validator('address', 'price', 'room_code', 'landlord_phone', pre=True)
    def check_not_empty_or_null(cls, value):
        """Validate đảm bảo không nhận giá trị Null/None hoặc chuỗi toàn khoảng trắng."""
        if value is None:
            raise ValueError("Trường này không được để null!")
        
        val_str = str(value).strip()
        if not val_str or val_str.lower() in ["none", "null", "undefined"]:
            raise ValueError("Giá trị không được để trống hoặc invalid!")
            
        return val_str

    @validator("media_urls", pre=True)
    def validate_media_urls(cls, value):
        values = normalize_room_media_urls(value)
        invalid = [url for url in values if not is_valid_zalo_media_url(url)]
        if invalid:
            raise ValueError(
                "Link ảnh/video phải là URL HTTPS công khai hợp lệ: " + ", ".join(invalid[:3])
            )
        return values


class OrderRoomCreateUpdateSchema(BaseModel):
    tenant_zalo_id: Optional[str] = None
    tenant_phone: str = Field(..., min_length=10, description="Số điện thoại người thuê")
    landlord_zalo_id: Optional[str] = None
    landlord_phone: Optional[str] = None
    room_code: str = Field(..., min_length=6, max_length=6, description="Mã phòng 6 ký tự")
    viewing_time: Optional[datetime] = None
    status: Optional[str] = "CHỜ XEM"

    @validator("tenant_phone", pre=True)
    def validate_tenant_phone(cls, value):
        phone = format_national_phone(str(value or "").strip())
        if not re.fullmatch(r"0[35789][0-9]{8}", phone):
            raise ValueError("Số điện thoại người thuê không hợp lệ.")
        return phone

    @validator("landlord_phone", pre=True)
    def validate_landlord_phone(cls, value):
        if value is None or not str(value).strip():
            return None
        phone = format_national_phone(str(value).strip())
        if not re.fullmatch(r"0[35789][0-9]{8}", phone):
            raise ValueError("Số điện thoại chủ nhà không hợp lệ.")
        return phone

    @validator("status", pre=True)
    def validate_status(cls, value):
        status_value = str(value or "CHỜ XEM").strip().upper()
        if status_value not in {"CHỜ XEM", "ĐÃ XEM", "ĐÃ THUÊ"}:
            raise ValueError("Trạng thái đặt phòng không hợp lệ.")
        return status_value

    @validator("room_code", pre=True)
    def normalize_room_code(cls, value):
        code = str(value or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{6}", code):
            raise ValueError("Mã phòng phải gồm đúng 6 chữ cái hoặc chữ số.")
        return code

    @validator("tenant_zalo_id", "landlord_zalo_id", pre=True)
    def normalize_optional_id(cls, value):
        clean_value = str(value or "").strip()
        return clean_value or None


class OrderRoomStatusUpdateSchema(BaseModel):
    status: str

    @validator("status", pre=True)
    def validate_status(cls, value):
        status_value = str(value or "").strip().upper()
        if status_value not in {"CHỜ XEM", "ĐÃ XEM", "ĐÃ THUÊ"}:
            raise ValueError("Trạng thái đặt phòng không hợp lệ.")
        return status_value


def cron_refresh_zalo_job():
    print("🔄 [REFRESH TOKEN ZALO] Bắt đầu tự động...", flush=True) # Thêm flush=True
    if not str(ZALO_APP_ID or "").strip() or not str(ZALO_SECRET_KEY or "").strip():
        report_error(
            "Bỏ qua refresh Zalo vì thiếu ZALO_APP_ID hoặc ZALO_SECRET_KEY",
            context="cron_refresh_zalo_job",
        )
        return
    lock_key = "lock:zalo_token_refresh"
    lock_acquired = False
    try:
        lock_acquired = bool(redis_client.set(lock_key, str(os.getpid()), nx=True, ex=300))
        if not lock_acquired:
            return
    except Exception:
        lock_acquired = False
    db = SessionLocal()
    try:
        refresh_zalo_tokens(db)
        print("✅ [REFRESH TOKEN ZALO] Thành công!", flush=True)
    except Exception as e:
        report_error("Refresh token Zalo thất bại", e, "cron_refresh_zalo_job")
    finally:
        db.close()
        if lock_acquired:
            try:
                redis_client.delete(lock_key)
            except Exception:
                pass
    
# --- DATABASE DEPENDENCY & CURRENT USER HELPER ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

#def get_current_user(
#    x_user_phone: str = Header(None, alias="X-User-Phone"),
#    db: Session = Depends(get_db)
#) -> UserWeb:
#    if not x_user_phone:
#        raise HTTPException(
#            status_code=status.HTTP_401_UNAUTHORIZED,
#            detail="Vui lòng cung cấp số điện thoại hoặc thông tin xác thực!"
#        )
#    clean_phone = x_user_phone if x_user_phone == "adminpro" else format_national_phone(x_user_phone)
#    user = db.query(UserWeb).filter(UserWeb.phone == clean_phone).first()
#    if not user:
#        raise HTTPException(
#            status_code=status.HTTP_404_NOT_FOUND,
#            detail="Không tìm thấy thông tin người dùng!"
#        )
#    return user

# --- CORE UTILITY FUNCTIONS ---
def format_national_phone(phone_str: str, default_region: str = "VN") -> str:
    if not phone_str:
        return ""
    phone_str = str(phone_str).strip()
    if phone_str.startswith("84") and not phone_str.startswith("+"):
        phone_str = "+" + phone_str
    try:
        parsed_number = phonenumbers.parse(phone_str, default_region)
        if phonenumbers.is_valid_number(parsed_number):
            national_format = phonenumbers.format_number(
                parsed_number, phonenumbers.PhoneNumberFormat.NATIONAL
            )
            return re.sub(r'\D', '', national_format)
    except NumberParseException:
        pass
    clean_digits = re.sub(r'\D', '', str(phone_str))
    if clean_digits.startswith("84") and len(clean_digits) == 11:
        return "0" + clean_digits[2:]
    return clean_digits

MOVE_IN_EMPTY_VALUES = {"", "none", "null", "nan", "chưa rõ", "undefined", "[chưa cập nhật]"}


def normalize_move_in_date(date_value=None, now_value: Optional[datetime] = None) -> tuple:
    """Chuẩn hóa ngày vào ở thành ``dd/mm/YYYY`` và timestamp 00:00 giờ Việt Nam."""
    raw_text = str(date_value or "").strip()
    lowered = raw_text.lower()
    if lowered in MOVE_IN_EMPTY_VALUES:
        return "", None

    now_vn = now_value or datetime.now(VN_TZ)
    if now_vn.tzinfo is None:
        now_vn = VN_TZ.localize(now_vn)
    else:
        now_vn = now_vn.astimezone(VN_TZ)

    relative_days = None
    if re.search(r"\b(ngày\s*(mốt|kia)|mốt|kia)\b", lowered):
        relative_days = 2
    elif re.search(r"\b(ngày\s*mai|mai)\b", lowered):
        relative_days = 1
    elif (
        re.search(r"\b(hôm\s*nay|ngày\s*nay)\b", lowered)
        or re.search(r"\b(vào|ở|chuyển|dọn)(?:\s+ở)?\s+ngay\b", lowered)
        or lowered in {"ngay", "vào ngay", "ở ngay", "vào ở ngay", "có thể vào ngay", "dọn vào ngay"}
    ):
        relative_days = 0

    if relative_days is not None:
        target = now_vn + timedelta(days=relative_days)
        target_midnight = VN_TZ.localize(datetime(target.year, target.month, target.day))
        return target_midnight.strftime("%d/%m/%Y"), target_midnight.timestamp()

    # Chấp nhận cả dữ liệu từ giao diện HTML (YYYY-MM-DD) và cách nhập phổ biến tại Việt Nam.
    date_match = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", lowered)
    candidates = []
    if date_match:
        day, month = int(date_match.group(1)), int(date_match.group(2))
        year = int(date_match.group(3)) if date_match.group(3) else now_vn.year
        candidates.append((year + 2000 if year < 100 else year, month, day))
    iso_match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lowered)
    if iso_match:
        candidates.insert(0, tuple(map(int, iso_match.groups())))

    for year, month, day in candidates:
        try:
            target_midnight = VN_TZ.localize(datetime(year, month, day))
            return target_midnight.strftime("%d/%m/%Y"), target_midnight.timestamp()
        except ValueError:
            continue
    return "", None


def parse_move_in_date(date_str=None) -> Optional[float]:
    """Tương thích code cũ; dữ liệu trống hoặc không hợp lệ trả về ``None``."""
    return normalize_move_in_date(date_str)[1]


def extract_move_in_date_from_text(message_text: str, now_value: Optional[datetime] = None) -> str:
    """Đọc ngày vào ở trực tiếp từ câu người dùng, không phụ thuộc kết quả Gemini."""
    text_value = str(message_text or "").strip().lower()
    if not text_value:
        return ""

    relative_pattern = (
        r"\b(?:hôm\s*nay|ngày\s*nay|ngày\s*mai|ngày\s*mốt|ngày\s*kia)\b"
        r"|\b(?:vào|ở|chuyển|dọn)(?:\s+ở)?\s+"
        r"(?:ngay|hôm\s*nay|ngày\s*nay|ngày\s*mai|mai|ngày\s*mốt|ngày\s*kia|mốt|kia)\b"
    )
    relative_match = re.search(relative_pattern, text_value)
    if relative_match:
        return normalize_move_in_date(relative_match.group(0), now_value)[0]

    relative_offset_match = re.search(r"\b(\d{1,3})\s*(?:ngày|hôm)\s*nữa\b", text_value)
    if relative_offset_match:
        now_vn = now_value or datetime.now(VN_TZ)
        if now_vn.tzinfo is None:
            now_vn = VN_TZ.localize(now_vn)
        target = now_vn.astimezone(VN_TZ) + timedelta(days=int(relative_offset_match.group(1)))
        return target.strftime("%d/%m/%Y")

    date_token = r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|\d{4}-\d{1,2}-\d{1,2}"
    explicit_patterns = (
        rf"(?:vào|ở|chuyển|dọn)(?:\s+ở)?[^\n,.]{{0,30}}?\b({date_token})\b",
        rf"\b({date_token})\b[^\n,.]{{0,30}}?(?:vào|ở|chuyển|dọn)(?:\s+ở)?\b",
    )
    for pattern in explicit_patterns:
        match = re.search(pattern, text_value)
        if match:
            return normalize_move_in_date(match.group(1), now_value)[0]
    return ""
  

def parse_price_to_number(price_str: str) -> float:
    if not price_str or str(price_str).strip().lower() in ["chưa rõ", "thỏa thuận", "none", "nan", ""]:
        return 0.0
    
    # 1. Nếu input truyền vào đã là số (int hoặc float)
    if isinstance(price_str, (int, float)):
        val = float(price_str)
        if val < 100:  # Ví dụ nhập 2.5 nghĩa là 2.5 triệu
            return val * 1_000_000
        return val

    text = str(price_str).lower().strip()

    # 2. Nếu chuỗi chỉ chứa toàn chữ số thuần túy (VD: "2000000")
    if text.isdigit():
        val = float(text)
        if val < 100:  # Nếu gõ "2" hoặc "3" hiểu là 2 triệu, 3 triệu
            return val * 1_000_000
        return val

    # 3. Xử lý các chuỗi dạng "2.5 triệu", "2,5tr", "2tr5"
    if "triệu" in text or "tr" in text:
        # Chuẩn hóa "2tr5" hoặc "2 triệu 5" thành "2.5"
        text = re.sub(r'(\d+)\s*(?:triệu|tr)\s*(\d+)', r'\1.\2', text)
        match = re.search(r'(\d+(?:\.\d+)?)', text.replace(',', '.'))
        if match:
            return float(match.group(1)) * 1_000_000

    if "k" in text:
        match = re.search(r'(\d+(?:\.\d+)?)', text.replace(',', '.'))
        if match:
            return float(match.group(1)) * 1_000

    # 4. Trường hợp chuỗi có dấu phân cách hàng nghìn (VD: "2,000,000" hoặc "2.000.000")
    digits_only = re.sub(r'[^\d]', '', text)
    if digits_only:
        val = float(digits_only)
        if val < 100:
            return val * 1_000_000
        return val

    return 0.0

# 2. Lấy dữ liệu và xóa Cache
def get_pending_media(user_id: str) -> list:
    cache_key = f"pending_media:{user_id}"
    data = redis_client.get(cache_key)
    return json.loads(data) if data else []

def get_get_and_clear_pending_media(user_id: str) -> list:
    urls = get_pending_media(user_id)
    if urls:
        redis_client.delete(f"pending_media:{user_id}")
    return urls

# 1. Lưu Cache với thời hạn tự xóa (ex = CACHE_TTL_SECONDS)
def add_pending_media(user_id: str, new_urls: list):
    cache_key = f"pending_media:{user_id}"
    existing = get_pending_media(user_id)
    merged = apply_media_limit(list(dict.fromkeys(existing + list(new_urls or []))))
    redis_client.set(cache_key, json.dumps(merged), ex=CACHE_TTL_SECONDS)
    return merged


def clear_pending_media(user_id: str) -> None:
    """Xóa media đang chờ chọn phòng của đúng người dùng Zalo."""
    redis_client.delete(f"pending_media:{user_id}")


def get_rooms_by_landlord_phone(landlord_phone: str, limit: int = 8) -> List[dict]:
    """Lấy các phòng thuộc đúng SĐT chủ nhà để họ chọn mã phòng gắn media."""
    safe_phone = format_national_phone(landlord_phone)
    if not safe_phone:
        return []
    records, _ = qdrant_client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=qdrant_models.Filter(
            must=[qdrant_models.FieldCondition(
                key="landlord_phone",
                match=qdrant_models.MatchValue(value=safe_phone),
            )]
        ),
        limit=min(max(int(limit), 1), 50),
        with_payload=True,
        with_vectors=False,
    )
    rooms = []
    for record in records:
        if record.payload:
            room = dict(record.payload)
            room["id"] = str(record.id)
            rooms.append(room)
    rooms.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return rooms


def format_room_media_choices(rooms: List[dict], pending_count: int) -> str:
    """Tạo danh sách xác nhận rõ mã, tên và địa chỉ; không lộ phòng của chủ khác."""
    if not rooms:
        return (
            f"📸 Đã lưu tạm {pending_count} ảnh/video nhưng chưa tìm thấy phòng nào thuộc SĐT của bạn.\n\n"
            "Bạn hãy gửi thông tin phòng (tên phòng, địa chỉ, giá...) để tạo phòng mới."
        )
    lines = [
        f"📸 Đã lưu tạm {pending_count} ảnh/video.",
        "Bạn hãy gửi thông tin phòng (tên phòng, địa chỉ, giá...) để tạo phòng mới",
        "Hoặc xác nhận phòng cần cập nhật bằng cách nhắn: CẬP NHẬT ẢNH <MÃ PHÒNG>",
        "",
        "Các phòng thuộc SĐT của bạn:",
    ]
    for index, room in enumerate(rooms, start=1):
        code = str(room.get("room_code") or "[chưa có mã]").strip()
        name = str(room.get("room_name") or "[chưa có tên]").strip()
        address = str(room.get("address") or "[chưa có địa chỉ]").strip()
        lines.append(f"{index}. 🏠 {code} — {name}\n   📍 {address}")
    lines.append("\nVí dụ: CẬP NHẬT ẢNH SP840D")
    return "\n".join(lines)


def build_room_media_choices(landlord_phone: str, pending_count: int) -> str:
    """Lấy và định dạng danh sách phòng, đồng thời phản hồi rõ nếu Qdrant tạm lỗi."""
    try:
        return format_room_media_choices(
            get_rooms_by_landlord_phone(landlord_phone),
            pending_count,
        )
    except Exception as exc:
        report_error("Không thể tải danh sách phòng để chọn media", exc, "build_room_media_choices")
        return (
            f"📸 Hệ thống vẫn đang giữ tạm {pending_count} ảnh/video.\n\n"
            "⚠️ Chưa tải được danh sách phòng. Bạn vui lòng thử lại bằng cách nhắn: "
            "CẬP NHẬT ẢNH <MÃ PHÒNG>."
        )


def build_owned_room_reference(landlord_phone: str) -> str:
    """Liệt kê mã, tên và địa chỉ phòng để người dùng xác định đúng phòng cần sửa."""
    try:
        rooms = get_rooms_by_landlord_phone(landlord_phone)
    except Exception as exc:
        report_error("Không thể tải danh sách phòng của chủ nhà", exc, "build_owned_room_reference")
        return "⚠️ Hiện chưa tải được danh sách mã phòng, vui lòng thử lại sau."
    if not rooms:
        return "Hiện SĐT của bạn chưa có phòng nào trên hệ thống."
    lines = ["Các phòng gần đây thuộc SĐT của bạn:"]
    for index, room in enumerate(rooms, start=1):
        code = str(room.get("room_code") or "[chưa có mã]").strip()
        name = str(room.get("room_name") or "[chưa có tên]").strip()
        address = str(room.get("address") or "[chưa có địa chỉ]").strip()
        lines.append(f"{index}. 🏠 {code} — {name}\n   📍 {address}")
    return "\n".join(lines)


def is_my_rooms_request(message_text: str) -> bool:
    """Nhận dạng nhu cầu xem phòng do chính người gửi đăng bằng ngôn ngữ tự nhiên."""
    clean_text = re.sub(r"\s+", " ", str(message_text or "").strip().lower())
    owner_phrases = (
        "phòng của tôi",
        "phòng cho thuê của tôi",
        "phòng trọ của tôi",
        "phòng tôi đăng",
        "phòng tôi đã đăng",
        "phòng do tôi đăng",
        "phòng tôi cho thuê",
        "danh sách phòng của mình",
        "phòng của mình",
        "phòng đã đăng của tôi",
    )
    owner_signal = any(owner_phrase in clean_text for owner_phrase in owner_phrases) or bool(
        re.search(r"phòng.*(?:của tôi|của mình|tôi.*(?:đã\s+)?đăng|tôi.*cho thuê)", clean_text)
    )
    list_phrases = (
        "danh sách", "liệt kê", "cho xem", "xem", "quản lý", "kiểm tra",
        "những phòng nào", "các phòng nào", "tôi có phòng nào",
    )
    return owner_signal and any(list_phrase in clean_text for list_phrase in list_phrases)


def is_room_listing_request(message_text: str) -> bool:
    """Nhận dạng chủ nhà muốn đăng/cho thuê phòng, ưu tiên trước ý định tìm phòng."""
    clean_text = normalize_location_search(message_text)
    listing_patterns = (
        r"\b(?:toi|minh|em|anh|chi)\s+(?:(?:can|muon|co nhu cau)\s+)?(?:dang|cho thue)\s+(?:mot\s+)?(?:phong|phong tro)\b",
        r"^(?:can|muon|co nhu cau)\s+(?:dang|cho thue)\s+(?:mot\s+)?(?:phong|phong tro)\b",
        r"^(?:(?:toi|minh|em|anh|chi)\s+)?cho thue\s+(?:mot\s+)?(?:phong|phong tro)\b",
        r"\b(?:dang|rao)\s+(?:(?:tin|bai)\s+)?(?:cho thue\s+)?(?:phong|phong tro)\b",
        r"\bdang ky\s+(?:cho thue\s+)?(?:phong|phong tro)\b",
        r"\b(?:toi|minh|em|anh|chi)\s+co\s+(?:mot\s+)?(?:phong|phong tro).*\b(?:can|muon)\s+cho thue\b",
        r"\bco\s+(?:mot\s+)?(?:phong|phong tro).*\b(?:cho thue|can nguoi thue|tim nguoi thue)\b",
        r"\b(?:phong|phong tro).*\b(?:can|muon)\s+(?:cho thue|tim nguoi thue)\b",
    )
    return any(re.search(pattern, clean_text) for pattern in listing_patterns)


def format_my_rooms_overview(rooms: List[dict]) -> str:
    """Định dạng danh sách quản lý phòng của chủ nhà, không lẫn kết quả tìm thuê."""
    if not rooms:
        return "🏠 SĐT của bạn hiện chưa có phòng nào được đăng trên hệ thống."
    lines = [f"🏠 DANH SÁCH PHÒNG CỦA BẠN ({len(rooms)} phòng)", ""]
    for index, room in enumerate(rooms, start=1):
        code = str(room.get("room_code") or "[chưa có mã]").strip()
        name = str(room.get("room_name") or "Phòng trọ").strip()
        address = str(room.get("address") or "[chưa có địa chỉ]").strip()
        status_text = str(room.get("status") or "[chưa cập nhật]").strip()
        price_value = parse_price_safe(room)
        price_text = f"{price_value:,.0f}đ/tháng" if price_value > 0 else "[chưa cập nhật giá]"
        lines.append(
            f"{index}. 🏷️ {code} — {name}\n"
            f"   📍 {address}\n"
            f"   💰 {price_text} | 📌 {status_text}"
        )
    lines.append("\nMuốn sửa, bạn có thể nhắn: Cập nhật phòng <MÃ PHÒNG> <thông tin cần sửa>.")
    return "\n".join(lines)


def send_my_rooms_overview(user_id: str, landlord_phone: str) -> None:
    """Chỉ lấy phòng theo SĐT đã liên kết của đúng người gửi Zalo."""
    try:
        rooms = get_rooms_by_landlord_phone(landlord_phone, limit=50)
        send_zalo_message(user_id, format_my_rooms_overview(rooms))
    except Exception as exc:
        report_error("Không thể tải danh sách phòng của người dùng", exc, f"zalo_user:{user_id}")
        send_zalo_message(user_id, "⚠️ Chưa tải được danh sách phòng của bạn. Vui lòng thử lại sau.")


def extract_room_code_for_media(message_text: str) -> Optional[str]:
    """Chỉ nhận mã 6 ký tự có cả chữ và số khi đang có media chờ xử lý."""
    candidates = re.findall(r"\b(?=[A-Za-z0-9]{6}\b)(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6}\b", message_text or "")
    return candidates[0].upper() if len(candidates) == 1 else None


def attach_media_to_owned_room(landlord_phone: str, room_code: str, media_urls: List[str]) -> dict:
    """Gắn media vào phòng sau khi xác thực đồng thời room_code và landlord_phone."""
    safe_phone = format_national_phone(landlord_phone)
    safe_code = str(room_code or "").strip().upper()
    unique_new_media = [
        str(url).strip() for url in dict.fromkeys(media_urls or [])
        if str(url or "").strip().startswith(("https://", "http://"))
    ]
    if not safe_phone or not safe_code or not unique_new_media:
        raise ValueError("Thiếu SĐT, mã phòng hoặc ảnh/video cần cập nhật.")

    records, _ = qdrant_client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=qdrant_models.Filter(must=[
            qdrant_models.FieldCondition(
                key="landlord_phone",
                match=qdrant_models.MatchValue(value=safe_phone),
            ),
            qdrant_models.FieldCondition(
                key="room_code",
                match=qdrant_models.MatchValue(value=safe_code),
            ),
        ]),
        limit=2,
        with_payload=True,
        with_vectors=False,
    )
    if len(records) != 1 or not records[0].payload:
        raise ValueError(f"Không tìm thấy mã phòng {safe_code} thuộc đúng SĐT của bạn.")

    record = records[0]
    room_payload = dict(record.payload)
    old_media = room_payload.get("media_urls") or []
    if isinstance(old_media, str):
        old_media = [item.strip() for item in old_media.split(",") if item.strip()]
    merged_media = apply_media_limit(list(dict.fromkeys(list(old_media) + unique_new_media)))
    updated_at = vietnam_now().strftime("%Y-%m-%d %H:%M:%S")
    qdrant_client.set_payload(
        collection_name=COLLECTION_NAME,
        points=[record.id],
        payload={"media_urls": merged_media, "updated_at": updated_at},
        wait=True,
    )

    room_payload["media_urls"] = merged_media
    room_payload["updated_at"] = updated_at
    mirror_db = SessionLocal()
    try:
        mirror = mirror_db.query(RoomRecord).filter(RoomRecord.id == str(record.id)).first()
        if mirror:
            mirror.payload = room_payload
            mirror.updated_at = vietnam_now()
        else:
            mirror_db.add(RoomRecord(
                id=str(record.id),
                landlord_phone=safe_phone,
                room_code=safe_code,
                payload=room_payload,
                created_at=vietnam_now(),
                updated_at=vietnam_now(),
            ))
        mirror_db.commit()
    except Exception as exc:
        mirror_db.rollback()
        report_error("Đồng bộ media phòng vào room_records thất bại", exc, f"room:{safe_code}")
    finally:
        mirror_db.close()

    write_audit_log(safe_phone, "ROOM_MEDIA_UPDATE", str(record.id), {
        "room_code": safe_code,
        "new_media_count": len(unique_new_media),
        "total_media_count": len(merged_media),
    })
    room_payload["new_media_count"] = len(unique_new_media)
    return room_payload


def collect_search_media_urls(search_results: List[dict], limit: int = MAX_SEARCH_MEDIA) -> List[str]:
    """Lấy media từ kết quả tìm phòng, giữ thứ tự và loại bỏ URL trùng."""
    collected = []
    for room in search_results or []:
        room_media = room.get("media_urls") or []
        if isinstance(room_media, str):
            room_media = [item.strip() for item in room_media.split(",")]
        for media_url in room_media:
            clean_url = str(media_url or "").strip()
            if clean_url.startswith(("https://", "http://")) and clean_url not in collected:
                collected.append(clean_url)
                if limit > 0 and len(collected) >= limit:
                    return collected
    return collected


def _normalise_room_media(room: dict, limit: int = MAX_SEARCH_MEDIA_PER_ROOM) -> List[str]:
    """Chuẩn hóa media của đúng một phòng và loại URL không hợp lệ/trùng lặp."""
    room_media = room.get("media_urls") or []
    if isinstance(room_media, str):
        room_media = [item.strip() for item in room_media.split(",")]

    valid_media = []
    for item in room_media:
        clean_url = str(item or "").strip()
        if clean_url.startswith(("https://", "http://")) and clean_url not in valid_media:
            valid_media.append(clean_url)
            if limit > 0 and len(valid_media) >= limit:
                break
    return valid_media


def _has_meaningful_room_value(value) -> bool:
    return str(value or "").strip().lower() not in {
        "", "none", "null", "nan", "chưa rõ", "chưa cập nhật", "không có"
    }


def _is_enabled_amenity(value) -> bool:
    """Nhận cả boolean và chuỗi tiếng Việt thường được lưu trong dữ liệu cũ."""
    if value is True:
        return True
    if value is False or value is None:
        return False
    normalized = str(value).strip().lower()
    return normalized in {"có", "co", "yes", "true", "1", "x"}


def format_room_search_message(room: dict, position: int, include_action_instructions: bool = True) -> str:
    """Tạo nội dung ổn định từ DB để ghép đúng với ảnh của từng phòng."""
    lines = [f"🏠 PHÒNG {position}"]
    room_name = room.get("room_name")
    room_code = room.get("room_code")
    address = room.get("address")
    price = room.get("price")

    if _has_meaningful_room_value(room_name):
        lines.append(f"🛏️ {room_name}")
    if _has_meaningful_room_value(room_code):
        lines.append(f"🔖 Mã phòng: {room_code}")
    if _has_meaningful_room_value(address):
        lines.append(f"📍 Địa chỉ: {address}")
    if _has_meaningful_room_value(price):
        numeric_price = parse_price_safe({"price": price})
        price_text = f"{numeric_price:,.0f}đ/tháng" if numeric_price > 0 else str(price)
        lines.append(f"💰 Giá: {price_text}")

    optional_fields = [
        ("room_size", "📐 Diện tích", " m²"),
        ("floor", "🏢 Tầng", ""),
        ("max_occupants", "👥 Tối đa", " người"),
        ("move_in_date", "📅 Có thể vào ở", ""),
        ("parking_info", "🛵 Chỗ để xe", ""),
        ("service_fees", "🧾 Phí dịch vụ", ""),
    ]
    for key, label, suffix in optional_fields:
        value = room.get(key)
        if _has_meaningful_room_value(value):
            value_text = str(value).strip()
            display_suffix = suffix
            if key == "room_size" and re.search(r"m\s*(?:2|²)\b", value_text, re.I):
                display_suffix = ""
            elif key == "max_occupants" and "người" in value_text.lower():
                display_suffix = ""
            lines.append(f"{label}: {value_text}{display_suffix}")

    amenity_labels = [
        ("is_private_bathroom", "VS khép kín"),
        ("has_ac", "Điều hòa"),
        ("has_heater", "Nóng lạnh"),
        ("has_washer", "Máy giặt"),
        ("has_fridge", "Tủ lạnh"),
        ("bed", "Giường"),
        ("wardrobe", "Tủ quần áo"),
        ("has_balcony", "Ban công"),
        ("has_window", "Cửa sổ"),
        ("has_fingerprint_lock", "Khóa vân tay"),
        ("allow_pets", "Cho nuôi thú cưng"),
    ]
    amenities = [label for key, label in amenity_labels if _is_enabled_amenity(room.get(key))]
    other_amenities = room.get("other_amenities")
    if _has_meaningful_room_value(other_amenities):
        for item in re.split(r"[,;\n]+", str(other_amenities)):
            clean_item = item.strip()
            if clean_item and clean_item.lower() not in {value.lower() for value in amenities}:
                amenities.append(clean_item)
    if amenities:
        lines.append(f"✅ Tiện nghi: {', '.join(amenities)}")

    if room_code and include_action_instructions:
        normalized_code = str(room_code).strip().upper()
        lines.append(f"📅 Đặt lịch xem phòng: nhắn “XEM PHÒNG {normalized_code}”")
        lines.append(f"🚩 Report phòng: nhắn “REPORT PHÒNG {normalized_code}”")
        lines.append(f"🏠 Tôi thấy phòng đã cho thuê: nhắn “TÔI THẤY PHÒNG ĐÃ CHO THUÊ {normalized_code}”")
    else:
        lines.append("👉 Nhắn OA để được tư vấn phòng này.")
    message = "\n".join(lines)
    # Loại bỏ các đường gạch dài bị chèn trước emoji khi format/paste source.
    return re.sub(r"(?m)^\s*[-–—_]{5,}\s*", "", message).strip()


def send_zalo_room_action_buttons(user_id: str, room_code: str) -> bool:
    """Gửi button và luôn gửi câu lệnh chữ để Zalo PC cũng thao tác được."""
    normalized_code = str(room_code or "").strip().upper()
    if str(user_id) != Config.ZALO_ADMIN_ID:
        action_room = get_room_for_zalo_action(normalized_code)
        action_block = get_room_access_block(action_room)
        if action_block:
            return send_zalo_message(user_id, format_room_access_block_message(action_block))
    fallback_text = (
        f"📅 Đặt lịch xem phòng: nhắn “XEM PHÒNG {normalized_code}”\n"
        f"🚩 Report phòng: nhắn “REPORT PHÒNG {normalized_code}”\n"
        f"🏠 Tôi thấy phòng đã cho thuê: nhắn “TÔI THẤY PHÒNG ĐÃ CHO THUÊ {normalized_code}”"
    )
    if not re.fullmatch(r"[A-Z0-9]{6}", normalized_code):
        return send_zalo_message(user_id, fallback_text)

    db = SessionLocal()
    try:
        token_data = get_current_tokens_from_db(db)
    finally:
        db.close()
    if not token_data or not token_data.get("access_token"):
        return send_zalo_message(user_id, fallback_text)

    payload = {
        "recipient": {"user_id": str(user_id)},
        "message": {
            "text": f"Bạn muốn thao tác gì với phòng {normalized_code}?",
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "buttons": [
                        {
                            "title": "📅 Đặt lịch xem phòng",
                            "type": "oa.query.show",
                            "payload": f"XEM PHÒNG {normalized_code}",
                        },
                        {
                            "title": "🚩 Report phòng",
                            "type": "oa.query.show",
                            "payload": f"REPORT PHÒNG {normalized_code}",
                        },
                        {
                            "title": "🏠 Tôi thấy phòng đã cho thuê",
                            "type": "oa.query.show",
                            "payload": f"TÔI THẤY PHÒNG ĐÃ CHO THUÊ {normalized_code}",
                        },
                    ],
                },
            },
        },
    }
    button_sent = False
    try:
        response_data, _ = _post_zalo_with_token_retry(
            "https://openapi.zalo.me/v3.0/oa/message/cs",
            payload,
            token_data["access_token"],
        )
        if response_data.get("error") == 0:
            button_sent = True
        else:
            report_error(
                f"Zalo từ chối button template: {response_data}",
                context="send_zalo_room_action_buttons",
                notify=False,
            )
    except Exception as exc:
        report_error("Gửi nút thao tác phòng Zalo thất bại", exc, "send_zalo_room_action_buttons", notify=False)
    # API có thể báo gửi button thành công nhưng Zalo PC không render template.
    # Vì vậy câu lệnh chữ luôn được gửi, không chỉ gửi khi button bị lỗi.
    text_sent = send_zalo_message(user_id, fallback_text)
    return button_sent or text_sent


def get_room_for_zalo_action(room_code: str) -> Optional[dict]:
    """Lấy một phòng theo mã để xem/report trực tiếp trên Zalo."""
    normalized_code = str(room_code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6}", normalized_code):
        return None
    records, _ = qdrant_client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=qdrant_models.Filter(
            must=[
                qdrant_models.FieldCondition(
                    key="room_code",
                    match=qdrant_models.MatchValue(value=normalized_code),
                )
            ]
        ),
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    if not records or not records[0].payload:
        return None
    room = dict(records[0].payload)
    room["id"] = str(records[0].id)
    room["room_code"] = normalized_code
    access_block = get_room_access_block(room)
    room["posting_blocked"] = bool(access_block)
    room["posting_block_reason"] = str((access_block or {}).get("reason") or "")
    return room


def get_pending_zalo_room_report(user_id: str) -> Optional[dict]:
    try:
        raw_value = redis_client.get(f"pending:room_report:{user_id}")
        return json.loads(raw_value) if raw_value else None
    except Exception:
        return None


def clear_pending_zalo_room_report(user_id: str) -> None:
    try:
        redis_client.delete(f"pending:room_report:{user_id}")
    except Exception:
        pass


def start_zalo_room_report(user_id: str, room_code: str) -> str:
    """Khóa mã/tên/địa chỉ phòng và yêu cầu người dùng nhập nội dung report."""
    try:
        room = get_room_for_zalo_action(room_code)
    except Exception as exc:
        report_error("Tra cứu phòng để report thất bại", exc, "start_zalo_room_report", notify=False)
        return "⚠️ Chưa thể mở report lúc này. Bạn vui lòng thử lại sau."
    if not room:
        return f"❌ Không tìm thấy phòng có mã {str(room_code).strip().upper()}."
    if str(user_id) != Config.ZALO_ADMIN_ID and room.get("posting_blocked"):
        return format_room_access_block_message({"reason": room.get("posting_block_reason")})

    pending_data = {
        "room_code": room["room_code"],
        "room_id": room["id"],
    }
    try:
        redis_client.set(
            f"pending:room_report:{user_id}",
            json.dumps(pending_data, ensure_ascii=False),
            ex=900,
        )
    except Exception as exc:
        report_error("Lưu report Zalo chờ nhập thất bại", exc, "start_zalo_room_report", notify=False)
        return "⚠️ Chưa thể mở report lúc này. Bạn vui lòng thử lại sau."

    room_name = str(room.get("room_name") or "[chưa có tên]").strip()
    address = str(room.get("address") or "[chưa có địa chỉ]").strip()
    return (
        "🚩 REPORT PHÒNG\n"
        f"🔖 Mã phòng: {room['room_code']}\n"
        f"🛏️ Tên phòng: {room_name}\n"
        f"📍 Địa chỉ: {address}\n\n"
        "Bạn hãy nhập nội dung cần report trong tin nhắn tiếp theo (5–2000 ký tự)."
    )


def submit_zalo_room_report(db: Session, user_id: str, room_code: str, content: str) -> str:
    """Lưu report do khách gửi trên Zalo vào cùng bảng quản lý của Admin."""
    clean_content = re.sub(r"\s+", " ", str(content or "")).strip()
    if len(clean_content) < 5:
        return "⚠️ Nội dung report phải có ít nhất 5 ký tự. Bạn vui lòng nhập rõ hơn."
    if len(clean_content) > 2000:
        return "⚠️ Nội dung report không được vượt quá 2000 ký tự."

    try:
        rate_key = f"rate:room-report:{user_id}"
        report_count = redis_client.incr(rate_key)
        if report_count == 1:
            redis_client.expire(rate_key, 3600)
        if report_count > 10:
            return "⚠️ Bạn đã gửi quá nhiều report trong một giờ. Vui lòng thử lại sau."
    except Exception:
        pass

    try:
        room = get_room_for_zalo_action(room_code)
        if not room:
            clear_pending_zalo_room_report(user_id)
            return f"❌ Phòng {str(room_code).strip().upper()} không còn tồn tại."
        if str(user_id) != Config.ZALO_ADMIN_ID and room.get("posting_blocked"):
            clear_pending_zalo_room_report(user_id)
            return format_room_access_block_message({"reason": room.get("posting_block_reason")})
        reporter = get_phone_by_user_id(db, str(user_id)) or f"ZALO:{user_id}"
        room_report = RoomReport(
            room_id=room["id"],
            room_code=room["room_code"],
            room_name=str(room.get("room_name") or "").strip() or None,
            address=str(room.get("address") or "[chưa có địa chỉ]").strip(),
            reporter_username=reporter,
            content=clean_content,
            status="MỚI",
        )
        db.add(room_report)
        db.commit()
        db.refresh(room_report)
        clear_pending_zalo_room_report(user_id)
        write_audit_log(
            reporter,
            "ROOM_REPORT_CREATE_ZALO",
            room["id"],
            {"report_id": room_report.id, "room_code": room["room_code"], "zalo_user_id": str(user_id)},
        )
        return f"✅ Đã gửi report phòng {room['room_code']}. Quản trị viên sẽ kiểm tra và xử lý."
    except Exception as exc:
        db.rollback()
        report_error("Lưu report phòng từ Zalo thất bại", exc, "submit_zalo_room_report")
        return "⚠️ Chưa thể lưu report lúc này. Bạn vui lòng thử lại sau."


def _find_zalo_user_id_by_phone(db: Session, phone: str) -> Optional[str]:
    """Tìm Zalo ID của chủ nhà, hỗ trợ cả định dạng 0xxx và +84xxx."""
    national_phone = format_national_phone(phone)
    if not national_phone:
        return None
    phone_variants = {national_phone}
    if national_phone.startswith("0"):
        phone_variants.update({f"+84{national_phone[1:]}", f"84{national_phone[1:]}"})
    account = db.query(UserWeb).filter(UserWeb.phone.in_(phone_variants)).first()
    return str(account.user_id).strip() if account and account.user_id else None


def format_rented_confirmation_room_details(room: dict) -> str:
    """Hiển thị toàn bộ trường nghiệp vụ cần thiết để chủ nhà xác nhận đúng phòng."""
    def display(field_name: str) -> str:
        value = room.get(field_name)
        return str(value).strip() if _has_meaningful_room_value(value) else "[chưa cập nhật]"

    numeric_price = parse_price_safe({"price": room.get("price")})
    price_text = f"{numeric_price:,.0f}đ/tháng" if numeric_price > 0 else display("price")
    media_count = len(normalize_room_media_urls(room.get("media_urls")))
    return "\n".join([
        "🏠 THÔNG TIN PHÒNG CẦN XÁC NHẬN",
        f"🔖 Mã phòng: {display('room_code')}",
        f"🛏️ Tên phòng: {display('room_name')}",
        f"📍 Địa chỉ: {display('address')}",
        f"💰 Giá: {price_text}",
        f"🏢 Tầng: {display('floor')}",
        f"📐 Diện tích: {display('room_size')}",
        f"🚿 WC riêng: {display('is_private_bathroom')}",
        f"❄️ Điều hòa: {display('has_ac')}",
        f"♨️ Nóng lạnh: {display('has_heater')}",
        f"🧺 Máy giặt: {display('has_washer')}",
        f"🧊 Tủ lạnh: {display('has_fridge')}",
        f"🛏️ Giường: {display('bed')}",
        f"🚪 Tủ quần áo: {display('wardrobe')}",
        f"🐾 Thú cưng: {display('allow_pets')}",
        f"🌤️ Ban công: {display('has_balcony')}",
        f"🪟 Cửa sổ: {display('has_window')}",
        f"🔐 Khóa vân tay: {display('has_fingerprint_lock')}",
        f"🛵 Để xe: {display('parking_info')}",
        f"👥 Số người tối đa: {display('max_occupants')}",
        f"➕ Tiện ích khác: {display('other_amenities')}",
        f"🧾 Phí dịch vụ: {display('service_fees')}",
        f"📅 Ngày vào: {display('move_in_date')}",
        f"📌 Trạng thái hiện tại: {display('status')}",
        f"📸 Ảnh/video: {media_count} tệp",
    ])


def send_zalo_rented_confirmation(owner_user_id: str, room: dict) -> bool:
    """Gửi đầy đủ thông tin phòng và hai lựa chọn xác nhận cho đúng chủ nhà."""
    room_code = str(room.get("room_code") or "").strip().upper()
    room_details = format_rented_confirmation_room_details(room)
    fallback_text = (
        f"{room_details}\n\n"
        "Có người dùng báo phòng này có thể đã được cho thuê. Chủ nhà vui lòng xác nhận:\n"
        f"✅ PHÒNG ĐÃ CHO THUÊ {room_code}\n"
        f"↩️ PHÒNG CHƯA CHO THUÊ {room_code}"
    )

    db = SessionLocal()
    try:
        token_data = get_current_tokens_from_db(db)
    finally:
        db.close()
    if not token_data or not token_data.get("access_token"):
        return send_zalo_message(owner_user_id, fallback_text)

    payload = {
        "recipient": {"user_id": str(owner_user_id)},
        "message": {
            "text": fallback_text,
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "buttons": [
                        {
                            "title": "✅ Phòng đã cho thuê",
                            "type": "oa.query.show",
                            "payload": f"PHÒNG ĐÃ CHO THUÊ {room_code}",
                        },
                        {
                            "title": "↩️ Phòng chưa cho thuê",
                            "type": "oa.query.show",
                            "payload": f"PHÒNG CHƯA CHO THUÊ {room_code}",
                        },
                    ],
                },
            },
        },
    }
    try:
        response_data, _ = _post_zalo_with_token_retry(
            "https://openapi.zalo.me/v3.0/oa/message/cs",
            payload,
            token_data["access_token"],
        )
        if response_data.get("error") == 0:
            return True
        report_error(
            f"Zalo từ chối nút xác nhận phòng đã thuê: {response_data}",
            context="send_zalo_rented_confirmation",
            notify=False,
        )
    except Exception as exc:
        report_error(
            "Gửi xác nhận phòng đã thuê cho chủ nhà thất bại",
            exc,
            "send_zalo_rented_confirmation",
            notify=False,
        )
    # Zalo PC có thể không hiển thị button template, nên gửi câu lệnh chữ dự phòng.
    return send_zalo_message(owner_user_id, fallback_text)


def request_room_rented_confirmation(db: Session, reporter_user_id: str, room_code: str) -> str:
    """Nhận báo cáo của khách và chuyển yêu cầu xác nhận tới đúng chủ nhà."""
    normalized_code = str(room_code or "").strip().upper()
    try:
        # Tối đa 5 lần trong 1 giờ. Lần thứ 6 khóa riêng chức năng này trong 48 giờ.
        if str(reporter_user_id) != str(Config.ZALO_ADMIN_ID):
            block_key = f"block:rented-report:{reporter_user_id}"
            hourly_key = f"rate:rented-report-hour:{reporter_user_id}"
            try:
                blocked_ttl = redis_client.ttl(block_key)
                if blocked_ttl and blocked_ttl > 0:
                    remaining_hours = max(1, (blocked_ttl + 3599) // 3600)
                    return (
                        "⛔ Bạn đang bị tạm khóa chức năng ‘Tôi thấy phòng đã cho thuê’ "
                        f"do gửi quá nhiều lần. Thời gian còn lại khoảng {remaining_hours} giờ."
                    )

                report_count = redis_client.incr(hourly_key)
                if report_count == 1:
                    redis_client.expire(hourly_key, 3600)
                if report_count > 5:
                    redis_client.set(block_key, "1", ex=172800)
                    redis_client.delete(hourly_key)
                    return (
                        "⛔ Bạn đã sử dụng chức năng ‘Tôi thấy phòng đã cho thuê’ quá 5 lần "
                        "trong 1 giờ. Chức năng này đã bị khóa trong 2 ngày."
                    )
            except Exception as rate_error:
                report_error(
                    "Không kiểm tra được giới hạn báo phòng đã thuê",
                    rate_error,
                    "request_room_rented_confirmation.rate_limit",
                    notify=False,
                )

        room = get_room_for_zalo_action(normalized_code)
        if not room:
            return f"❌ Không tìm thấy phòng có mã {normalized_code}."
        if str(reporter_user_id) != Config.ZALO_ADMIN_ID and room.get("posting_blocked"):
            return format_room_access_block_message({"reason": room.get("posting_block_reason")})
        if str(room.get("status") or "").strip().upper() == "ĐÃ CHO THUÊ":
            return f"ℹ️ Phòng {normalized_code} đã được chủ nhà xác nhận là đã cho thuê."

        rate_key = f"rate:rented-report:{reporter_user_id}:{normalized_code}"
        try:
            if not redis_client.set(rate_key, "1", nx=True, ex=300):
                return "ℹ️ Yêu cầu xác nhận phòng này đã được gửi. Vui lòng chờ chủ nhà phản hồi."
        except Exception:
            pass

        owner_phone = format_national_phone(room.get("landlord_phone"))
        owner_user_id = _find_zalo_user_id_by_phone(db, owner_phone)
        if not owner_user_id:
            return (
                f"⚠️ Đã ghi nhận báo cáo phòng {normalized_code}, nhưng chủ nhà chưa liên kết "
                "SĐT với Zalo OA nên hệ thống chưa thể gửi yêu cầu xác nhận."
            )

        pending_data = {
            "reporter_user_id": str(reporter_user_id),
            "owner_user_id": str(owner_user_id),
            "room_code": normalized_code,
        }
        try:
            redis_client.set(
                f"pending:rented-confirmation:{normalized_code}",
                json.dumps(pending_data, ensure_ascii=False),
                ex=86400,
            )
        except Exception:
            pass

        if not send_zalo_rented_confirmation(str(owner_user_id), room):
            return "⚠️ Chưa thể gửi yêu cầu xác nhận tới chủ nhà. Bạn vui lòng thử lại sau."
        return (
            f"✅ Đã gửi yêu cầu xác nhận phòng {normalized_code} tới chủ nhà. "
            "Phòng chỉ chuyển sang trạng thái ĐÃ CHO THUÊ sau khi chủ nhà xác nhận."
        )
    except Exception as exc:
        report_error("Xử lý báo phòng đã cho thuê thất bại", exc, "request_room_rented_confirmation")
        return "⚠️ Chưa thể gửi yêu cầu xác nhận lúc này. Bạn vui lòng thử lại sau."


def confirm_room_rented_status(
    db: Session,
    owner_user_id: str,
    room_code: str,
    is_rented: bool,
) -> str:
    """Chỉ chủ nhà đúng SĐT (hoặc super admin) được xác nhận trạng thái phòng."""
    normalized_code = str(room_code or "").strip().upper()
    try:
        room = get_room_for_zalo_action(normalized_code)
        if not room:
            return f"❌ Không tìm thấy phòng có mã {normalized_code}."

        sender_phone = format_national_phone(get_phone_by_user_id(db, str(owner_user_id)))
        landlord_phone = format_national_phone(room.get("landlord_phone"))
        is_admin = str(owner_user_id) == str(Config.ZALO_ADMIN_ID)
        if not is_admin and (not sender_phone or sender_phone != landlord_phone):
            return "⛔ Chỉ chủ nhà có SĐT trùng với phòng này mới được xác nhận trạng thái."

        new_status = "ĐÃ CHO THUÊ" if is_rented else "TRỐNG"
        if not update_room_status_in_db(room["id"], new_status):
            return "⚠️ Chưa thể cập nhật trạng thái phòng. Bạn vui lòng thử lại sau."

        pending_key = f"pending:rented-confirmation:{normalized_code}"
        try:
            pending_raw = redis_client.get(pending_key)
            pending_data = json.loads(pending_raw) if pending_raw else {}
            redis_client.delete(pending_key)
        except Exception:
            pending_data = {}

        write_audit_log(
            sender_phone or str(owner_user_id),
            "ROOM_RENTED_STATUS_CONFIRM_ZALO",
            room["id"],
            {"room_code": normalized_code, "status": new_status},
        )
        reporter_user_id = str(pending_data.get("reporter_user_id") or "").strip()
        if reporter_user_id and reporter_user_id != str(owner_user_id):
            send_zalo_message(
                reporter_user_id,
                f"ℹ️ Chủ nhà đã xác nhận phòng {normalized_code}: {new_status}.",
            )

        if is_rented:
            return (
                f"✅ Đã cập nhật phòng {normalized_code} thành ĐÃ CHO THUÊ. "
                "Phòng sẽ không còn xuất hiện trong kết quả tìm kiếm."
            )
        return f"✅ Đã xác nhận phòng {normalized_code} vẫn còn TRỐNG."
    except Exception as exc:
        report_error("Xác nhận trạng thái phòng từ Zalo thất bại", exc, "confirm_room_rented_status")
        return "⚠️ Chưa thể xác nhận trạng thái phòng lúc này. Bạn vui lòng thử lại sau."


def send_zalo_search_results(user_id: str, search_results: List[dict]) -> bool:
    """Gửi mỗi phòng thành một cụm riêng và hiển thị toàn bộ media của phòng."""
    candidate_rooms = list(search_results or [])
    if str(user_id) != Config.ZALO_ADMIN_ID:
        rules = get_active_room_posting_blocks()
        candidate_rooms = [room for room in candidate_rooms if not get_room_access_block(room, rules)]
    rooms = candidate_rooms[:MAX_SEARCH_ROOMS]
    if not rooms:
        return send_zalo_message(user_id, "Dạ chưa tìm thấy phòng đang khả dụng phù hợp với yêu cầu của bạn.")

    total_found = len(candidate_rooms)
    intro = (
        f"🔎 Tìm thấy {total_found} phòng phù hợp. "
        f"Hiển thị {len(rooms)}/{total_found} phòng:"
    )
    if not send_zalo_message(user_id, intro):
        return False

    for position, room in enumerate(rooms, start=1):
        room_code = str(room.get("room_code") or f"PHÒNG {position}").strip().upper()
        room_media = _normalise_room_media(room, 0)
        if room_media:
            room_message = format_room_search_message(
                room,
                position,
                include_action_instructions=False,
            )
            if not send_zalo_message(
                user_id,
                room_message,
                media_urls=room_media,
                media_first=True,
            ):
                return False
        else:
            room_message = f"{format_room_search_message(room, position, include_action_instructions=False)}\n📷 Phòng {room_code} chưa có ảnh/video."
            if not send_zalo_message(user_id, room_message):
                return False
        if not send_zalo_room_action_buttons(user_id, room_code):
            return False
        time.sleep(0.5)
    return True


def _refresh_zalo_token_after_invalid() -> str:
    """Refresh đồng bộ một lần khi Zalo trả -216 và trả về access token mới."""
    lock_key = "lock:zalo_token_refresh"
    lock_acquired = False
    try:
        try:
            lock_acquired = bool(redis_client.set(lock_key, str(os.getpid()), nx=True, ex=60))
        except Exception:
            lock_acquired = True

        if not lock_acquired:
            time.sleep(1)
            db = SessionLocal()
            try:
                return str(get_current_tokens_from_db(db).get("access_token") or "")
            finally:
                db.close()

        db = SessionLocal()
        try:
            refresh_zalo_tokens(db)
            db.expire_all()
            return str(get_current_tokens_from_db(db).get("access_token") or "")
        finally:
            db.close()
    except Exception as exc:
        report_error("Khôi phục Zalo access token thất bại", exc, "_refresh_zalo_token_after_invalid")
        return ""
    finally:
        if lock_acquired:
            try:
                redis_client.delete(lock_key)
            except Exception:
                pass


def _post_zalo_with_token_retry(url: str, payload: dict, access_token: str) -> tuple:
    """Gửi request và chỉ retry một lần nếu access token không hợp lệ."""
    headers = {"Content-Type": "application/json", "access_token": access_token}
    response = requests.post(url, headers=headers, json=payload, timeout=10)
    result = response.json()
    if result.get("error") != -216:
        return result, access_token

    refreshed_token = _refresh_zalo_token_after_invalid()
    if not refreshed_token:
        return result, access_token
    retry_headers = {"Content-Type": "application/json", "access_token": refreshed_token}
    retry_response = requests.post(url, headers=retry_headers, json=payload, timeout=10)
    return retry_response.json(), refreshed_token

def send_zalo_message(
    user_id: str,
    ai_reply: str,
    media_urls: list = None,
    combine_first_media: bool = False,
    media_first: bool = False,
) -> bool:
    db = SessionLocal()
    try:
        data_token = get_current_tokens_from_db(db)
    finally:
        db.close()
    if not data_token or not data_token.get("access_token"):
        report_error("Thiếu Zalo access token để gửi tin nhắn", context="send_zalo_message", notify=False)
        return False

    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    access_token = data_token["access_token"]
    clean_reply = re.sub(r"(?m)^\s*[-–—_]{5,}\s*", "", str(ai_reply or "")).strip()
    text_chunks = split_text_by_limit(clean_reply, max_length=1800)
    unique_media = apply_media_limit([
        value for value in dict.fromkeys(str(item or "").strip() for item in (media_urls or []))
        if is_valid_zalo_media_url(value)
    ])

    def send_media_items(items: list) -> bool:
        """Gửi media tuần tự để giữ đúng thứ tự hiển thị trên Zalo."""
        nonlocal access_token
        for media_url in items:
            media_type = "video" if re.search(r"\.(mp4|mov|webm)(\?|$)", media_url, re.I) else "image"
            payload = {
                "recipient": {"user_id": user_id},
                "message": {
                    "attachment": {
                        "type": "template",
                        "payload": {
                            "template_type": "media",
                            "elements": [{"media_type": media_type, "url": media_url}],
                        },
                    }
                },
            }
            res_data, access_token = _post_zalo_with_token_retry(url, payload, access_token)
            if res_data.get("error") == -201:
                report_error(
                    f"Zalo từ chối media không hợp lệ: {media_url}",
                    context="send_zalo_message",
                    notify=False,
                )
                continue
            if res_data.get("error") != 0:
                return False
            time.sleep(0.3)
        return True

    try:
        # Riêng kết quả tìm phòng: gửi hết media trước, rồi mới gửi nội dung phòng.
        if media_first and unique_media:
            if not send_media_items(unique_media):
                return False
            unique_media = []
            combine_first_media = False

        for idx, chunk in enumerate(text_chunks):
            message_payload = {"text": chunk}
            # Kết quả tìm phòng: ghép nội dung và ảnh đầu tiên trong cùng request Zalo.
            if combine_first_media and idx == 0 and unique_media:
                first_media = unique_media[0]
                first_media_type = "video" if re.search(
                    r"\.(mp4|mov|webm)(\?|$)", first_media, re.I
                ) else "image"
                message_payload["attachment"] = {
                    "type": "template",
                    "payload": {
                        "template_type": "media",
                        "elements": [{"media_type": first_media_type, "url": first_media}],
                    },
                }
            payload = {"recipient": {"user_id": user_id}, "message": message_payload}
            res_data, access_token = _post_zalo_with_token_retry(url, payload, access_token)
            # Một số phiên bản OA không nhận text + media chung: tự fallback an toàn.
            if res_data.get("error") != 0 and "attachment" in message_payload:
                if res_data.get("error") == -201 and unique_media:
                    unique_media.pop(0)
                payload = {"recipient": {"user_id": user_id}, "message": {"text": chunk}}
                res_data, access_token = _post_zalo_with_token_retry(url, payload, access_token)
                combine_first_media = False
            if res_data.get("error") != 0:
                return False
            if len(text_chunks) > 1:
                time.sleep(0.3)

        media_to_send = unique_media[1:] if combine_first_media and unique_media else unique_media
        if media_to_send and not send_media_items(media_to_send):
            return False
        return True
    except Exception as e:
        report_error("Gửi tin nhắn Zalo thất bại", e, "send_zalo_message", notify=False)
        return False


def write_audit_log(actor: str, action: str, target_id: str = None, details: dict = None) -> None:
    audit_db = SessionLocal()
    try:
        audit_db.add(AuditLog(actor=actor, action=action, target_id=target_id, details=details or {}))
        audit_db.commit()
    except Exception as exc:
        audit_db.rollback()
        report_error("Ghi audit log thất bại", exc, "write_audit_log")
    finally:
        audit_db.close()

def save_media_file(zalo_media_url: str, is_video: bool = False) -> str:
    if not zalo_media_url:
        return ""

    # Tải một lần và xác nhận có dữ liệu thật trước khi chuyển sang Cloudinary.
    # URL media của webhook Zalo có thể hết hạn hoặc trả HTTP 200 nhưng body rỗng.
    max_bytes = MAX_VIDEO_UPLOAD_BYTES if is_video else MAX_IMAGE_UPLOAD_BYTES
    media_bytes = bytearray()
    content_type = ""
    try:
        response = requests.get(
            zalo_media_url,
            timeout=(5, 30),
            stream=True,
            headers={"User-Agent": "ZaloRoomApp/1.0"},
        )
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            media_bytes.extend(chunk)
            if len(media_bytes) > max_bytes:
                print(f"⚠️ [MEDIA USER INPUT] Tệp vượt giới hạn {max_bytes} bytes", flush=True)
                return ""
    except requests.RequestException as exc:
        print(f"⚠️ [MEDIA USER INPUT] Không tải được media Zalo: {exc}", flush=True)
        return ""

    if not media_bytes:
        print("⚠️ [MEDIA USER INPUT] Zalo trả về tệp rỗng; yêu cầu người dùng gửi lại", flush=True)
        return ""

    expected_prefix = "video/" if is_video else "image/"
    if content_type and not (
        content_type.startswith(expected_prefix)
        or content_type == "application/octet-stream"
    ):
        print(f"⚠️ [MEDIA USER INPUT] Sai định dạng media: {content_type}", flush=True)
        return ""

    resource_type = "video" if is_video else "image"
    if CLOUDINARY_URL:
        try:
            res = cloudinary.uploader.upload(
                io.BytesIO(bytes(media_bytes)),
                resource_type=resource_type,
                folder="zalo_room_media",
            )
            url = res.get("secure_url", "")
            if url:
                return url
        except Exception as e:
            # Có dữ liệu hợp lệ nên đây là lỗi lưu trữ; fallback local và chỉ ghi log,
            # tránh gửi hai cảnh báo cho cùng một media.
            report_error("Cloudinary upload thất bại; chuyển lưu local", e, "save_media_file", notify=False)

    try:
        ext = ".mp4" if is_video else ".jpg"
        filename = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(MEDIA_DIR, filename)
        with open(filepath, "wb") as media_file:
            media_file.write(media_bytes)
        server_domain = get_public_server_domain()
        if server_domain:
            return f"{server_domain}/static/media/{filename}"
        report_error("Đã lưu media local nhưng SERVER_DOMAIN chưa phải URL HTTPS công khai", context="save_media_file", notify=False)
    except Exception as e:
        report_error("Lưu media local thất bại", e, "save_media_file", notify=False)
    return ""

def _log_gemini_usage(response, operation: str, model_name: str) -> None:
    """Ghi token sử dụng để theo dõi chức năng nào đang phát sinh chi phí."""
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return
    print(
        "📊 [GEMINI USAGE] "
        f"operation={operation} model={model_name} "
        f"input={getattr(usage, 'prompt_token_count', 0) or 0} "
        f"output={getattr(usage, 'candidates_token_count', 0) or 0} "
        f"total={getattr(usage, 'total_token_count', 0) or 0}",
        flush=True,
    )


def get_text_embedding(text: str, retries: int = GEMINI_EMBED_RETRIES) -> List[float]:
    if not text or not text.strip():
        return []
    normalized_text = re.sub(r"\s+", " ", text.strip())
    cache_key = f"gemini:embedding:{hashlib.sha256(normalized_text.encode('utf-8')).hexdigest()}"
    try:
        cached_vector = redis_client.get(cache_key)
        if cached_vector:
            parsed_vector = json.loads(cached_vector)
            if isinstance(parsed_vector, list) and len(parsed_vector) == VECTOR_SIZE:
                return parsed_vector
    except Exception:
        pass

    if gemini_client:
        for _ in range(retries):
            try:
                print("🤖 [GEMINI] Đang gọi Gemini AI để tạo embedding...", flush=True)
                response = gemini_client.models.embed_content(
                    model="models/gemini-embedding-001",
                    contents=normalized_text,
                    config=types.EmbedContentConfig(output_dimensionality=768)
                )
                if response and response.embedding and response.embedding.values:
                    vector = list(response.embedding.values)
                    _log_gemini_usage(response, "embedding", "gemini-embedding-001")
                    try:
                        redis_client.set(cache_key, json.dumps(vector), ex=GEMINI_EMBED_CACHE_TTL)
                    except Exception:
                        pass
                    return vector
            except Exception:
                time.sleep(1)
    if GEMINI_API_KEY:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={GEMINI_API_KEY}"
        headers = {"Content-Type": "application/json"}
        payload = {"model": "models/gemini-embedding-001", "content": {"parts": [{"text": normalized_text}]}, "outputDimensionality": 768}
        try:
            print("🤖 [GEMINI] Đang gọi Gemini AI để tạo embedding (REST fallback)...", flush=True)
            res = requests.post(url, headers=headers, json=payload, timeout=10).json()
            if "embedding" in res and "values" in res["embedding"]:
                vector = res["embedding"]["values"]
                try:
                    redis_client.set(cache_key, json.dumps(vector), ex=GEMINI_EMBED_CACHE_TTL)
                except Exception:
                    pass
                return vector
        except Exception as e:
            report_error("Gemini embedding REST fallback thất bại", e, "get_text_embedding")
    return []

def normalize_gemini_json_text(raw_text: str) -> str:
    """Bỏ code fence/phần thừa nhưng không tự đoán hay sửa sai nội dung JSON."""
    cleaned = str(raw_text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    first_object = cleaned.find("{")
    first_array = cleaned.find("[")
    starts = [position for position in (first_object, first_array) if position >= 0]
    if starts:
        start = min(starts)
        closing = "}" if cleaned[start] == "{" else "]"
        end = cleaned.rfind(closing)
        if end >= start:
            cleaned = cleaned[start:end + 1]
    return cleaned.strip()


def repair_common_gemini_json_errors(raw_text: str) -> str:
    """Sửa có giới hạn lỗi thiếu/dư dấu phẩy mà Gemini đôi khi tạo ra."""
    cleaned = normalize_gemini_json_text(raw_text)
    cleaned = cleaned.replace("“", '"').replace("”", '"')
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    json_value = r'("(?:\\.|[^"\\])*"|true|false|null|-?\d+(?:\.\d+)?|[}\]])'
    next_key = r'(\s*)(?="[^"\\]+"\s*:)'
    return re.sub(json_value + next_key, r"\1,\2", cleaned)


def build_gemini_generation_config(mime_type: str, max_output_tokens: int):
    """Tắt AFC/thinking khi SDK hỗ trợ vì luồng này chỉ cần JSON, không gọi tool."""
    base_options = {
        "temperature": 0.0,
        "max_output_tokens": max_output_tokens,
    }
    if mime_type:
        base_options["response_mime_type"] = mime_type

    optimized_options = dict(base_options)
    automatic_config = getattr(types, "AutomaticFunctionCallingConfig", None)
    thinking_config = getattr(types, "ThinkingConfig", None)
    if automatic_config:
        optimized_options["automatic_function_calling"] = automatic_config(disable=True)
    if thinking_config:
        optimized_options["thinking_config"] = thinking_config(thinking_budget=0)
    try:
        return types.GenerateContentConfig(**optimized_options)
    except (TypeError, ValueError):
        # Tương thích các bản google-genai cũ chưa có hai tùy chọn trên.
        return types.GenerateContentConfig(**base_options)


class GeminiOutputError(RuntimeError):
    """Gemini có phản hồi nhưng không tạo được JSON dùng được sau khi phục hồi."""


def generate_content_with_retry(prompt: str, mime_type: str = "application/json", retries: int = GEMINI_TEXT_RETRIES) -> str:
    if not gemini_client:
        return ""
    last_error = None
    current_prompt = prompt
    for attempt in range(retries):
        try:
            config = build_gemini_generation_config(mime_type, GEMINI_MAX_OUTPUT_TOKENS)
            print("🤖 [GEMINI] Đang gọi Gemini AI...", flush=True)
            response = gemini_client.models.generate_content(
                model=GEMINI_TEXT_MODEL, contents=current_prompt, config=config
            )
            _log_gemini_usage(response, "generate_content", GEMINI_TEXT_MODEL)
            if response and response.text:
                response_text = response.text.strip()
                if mime_type == "application/json":
                    response_text = normalize_gemini_json_text(response_text)
                    try:
                        json.loads(response_text)
                    except json.JSONDecodeError as exc:
                        repaired_text = repair_common_gemini_json_errors(response_text)
                        try:
                            json.loads(repaired_text)
                            return repaired_text
                        except json.JSONDecodeError:
                            last_error = exc
                        print(
                            f"⚠️ [GEMINI JSON INVALID] thử {attempt + 1}/{retries}: {exc}",
                            flush=True,
                        )
                        current_prompt = (
                            "Sửa JSON dưới đây thành JSON hợp lệ. Giữ nguyên dữ liệu và khóa, "
                            "chỉ sửa cú pháp. Chỉ trả về JSON, không giải thích:\n"
                            f"{response_text}"
                        )
                        continue
                return response_text
        except Exception as e:
            last_error = e
            if attempt + 1 < retries and any(code in str(e) for code in ("503", "429", "UNAVAILABLE")):
                time.sleep(2 ** attempt)
                continue
            break
    if last_error:
        report_error(
            "Gemini không trả JSON hợp lệ sau khi retry",
            last_error,
            "generate_content_with_retry",
            notify=False,
        )
    return ""

# --- QDRANT VECTOR & ROOM SERVICES ---
ROOM_NULL_VALUES = {"", "none", "null", "nan", "chưa rõ", "undefined", "[chưa cập nhật]"}


def has_room_update_value(value) -> bool:
    """Phân biệt dữ liệu người dùng thực sự cung cấp với giá trị rỗng do AI sinh ra."""
    if value is None:
        return False
    if isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return str(value).strip().lower() not in ROOM_NULL_VALUES


def merge_room_partial_update(
    old_payload: dict,
    new_data: dict,
    media_urls: Optional[List[str]] = None,
    replace_media_urls: bool = False,
) -> dict:
    """Giữ dữ liệu cũ và chỉ ghi đè những trường có giá trị trong yêu cầu cập nhật."""
    merged = dict(old_payload or {})
    # Trường cũ không còn được lưu; ngày vào ở chỉ dùng move_in_date.
    merged.pop("move_in_timestamp", None)
    for key, value in dict(new_data or {}).items():
        if key in {"id", "created_at", "updated_at", "move_in_timestamp"}:
            continue
        if has_room_update_value(value):
            merged[key] = value

    if has_room_update_value(new_data.get("move_in_date")):
        merged.pop("move_in_timestamp", None)

    incoming_media = normalize_room_media_urls(
        media_urls if media_urls is not None else new_data.get("media_urls")
    )
    if replace_media_urls:
        merged["media_urls"] = incoming_media
    elif incoming_media:
        old_media = normalize_room_media_urls(old_payload.get("media_urls"))
        merged["media_urls"] = apply_media_limit(list(dict.fromkeys(old_media + incoming_media)))
    return merged


def is_room_update_command(message_text: str) -> bool:
    clean_text = str(message_text or "").strip().lower()
    return bool(re.search(r"\b(cập nhật|sửa|bổ sung|thay đổi|đổi)\b", clean_text))


def infer_explicit_boolean_room_updates(message_text: str) -> dict:
    """Đọc trực tiếp tiện ích Có/Không từ câu cập nhật, kể cả khi Gemini bỏ sót khóa."""
    clean_text = re.sub(r"\s+", " ", str(message_text or "").strip().lower())
    field_keywords = {
        "is_private_bathroom": ("vệ sinh riêng", "wc riêng", "khép kín"),
        "has_ac": ("điều hòa", "điều hoà", "máy lạnh"),
        "has_heater": ("nóng lạnh", "máy nước nóng"),
        "has_washer": ("máy giặt",),
        "has_fridge": ("tủ lạnh", "tủ mát"),
        "allow_pets": ("cho nuôi pet", "cho nuôi chó", "cho nuôi mèo", "cho nuôi thú cưng"),
        "has_balcony": ("ban công",),
        "has_window": ("cửa sổ",),
        "has_fingerprint_lock": (
            "cổng vân tay",
            "cửa vân tay",
            "khóa vân tay",
            "khoá vân tay",
            "khóa cửa vân tay",
            "khoá cửa vân tay",
            "vân tay",
        ),
        "parking_info": ("chỗ để xe", "nơi để xe", "bãi xe", "để xe"),
        "bed": ("giường",),
        "wardrobe": ("tủ quần áo", "tủ áo", "giường tủ"),
    }
    inferred = {}
    for field, keywords in field_keywords.items():
        matched_keyword = next((keyword for keyword in keywords if keyword in clean_text), None)
        if not matched_keyword:
            continue
        negative_phrases = (
            f"không có {matched_keyword}",
            f"không còn {matched_keyword}",
            f"không {matched_keyword}",
            f"bỏ {matched_keyword}",
            f"xóa {matched_keyword}",
            f"xoá {matched_keyword}",
        )
        inferred[field] = "Không" if any(phrase in clean_text for phrase in negative_phrases) else "Có"
    return inferred


def extract_listing_price_from_text(message_text: str) -> Optional[float]:
    """Đọc giá thuê phổ biến: 4tr5, 4tr500, 4,5 triệu, 4.500.000 đồng."""
    text_value = str(message_text or "").strip().lower()
    if not text_value:
        return None

    # Dạng triệu viết gọn. Phần sau "tr" là phần lẻ: 4tr5/4tr50/4tr500 = 4,5 triệu.
    compact_match = re.search(
        r"(?<!\d)(\d{1,3})\s*(?:triệu|trieu|tr)\s*(\d{1,3})(?!\d)",
        text_value,
        re.IGNORECASE,
    )
    if compact_match:
        whole = int(compact_match.group(1))
        fractional_text = compact_match.group(2)
        fractional = int(fractional_text) / (10 ** len(fractional_text))
        return (whole + fractional) * 1_000_000

    unit_match = re.search(
        r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(triệu|trieu|tr|k|nghìn|nghin)(?![a-zA-Z])",
        text_value,
        re.IGNORECASE,
    )
    if unit_match:
        number = float(unit_match.group(1).replace(",", "."))
        unit = normalize_location_search(unit_match.group(2))
        return number * (1_000 if unit in {"k", "nghin"} else 1_000_000)

    # Chỉ nhận số tiền đầy đủ khi gần từ khóa giá để không nhầm số nhà/SĐT.
    full_amount_match = re.search(
        r"\b(?:giá|gia)(?:\s+(?:thuê|thue|phòng|phong|là|la))*\s*([\d.,]{6,})",
        text_value,
        re.IGNORECASE,
    )
    if full_amount_match:
        digits = re.sub(r"\D", "", full_amount_match.group(1))
        return float(digits) if digits else None
    return None


def apply_direct_room_listing_fallbacks(data: dict, message_text: str) -> dict:
    """Bổ sung các trường rõ ràng trong tin đăng nếu AI phân loại/trích xuất thiếu."""
    extracted = dict(data or {})
    raw_text = re.sub(r"\s+", " ", str(message_text or "").strip())

    address_match = re.search(
        r"\b(?:ở|tại)\s+(.+?)(?=\s+(?:phòng\s+)?\d+(?:[.,]\d+)?\s*m(?:2|²)\b|\s+giá\b|$)",
        raw_text,
        flags=re.IGNORECASE,
    )
    if address_match and not has_room_update_value(extracted.get("address")):
        extracted["address"] = address_match.group(1).strip(" ,.;-")

    size_match = re.search(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*m(?:2|²)\b", raw_text, re.IGNORECASE)
    if size_match and not has_room_update_value(extracted.get("room_size")):
        extracted["room_size"] = f"{size_match.group(1).replace(',', '.')}m2"

    direct_price = extract_listing_price_from_text(raw_text)
    if direct_price and is_missing_required_room_price(extracted.get("price")):
        extracted["price"] = direct_price

    extracted.update(infer_explicit_boolean_room_updates(raw_text))
    return extracted


def keep_only_explicit_room_updates(data: dict, message_text: str) -> dict:
    """Không cho giá trị AI suy đoán ghi đè các trường người dùng không nhắc tới."""
    clean_text = str(message_text or "").strip().lower()
    keyword_map = {
        "room_name": ("tên phòng", "số phòng"),
        "price": ("giá", "triệu", "tr/tháng", "đ/tháng"),
        "floor": ("tầng",),
        "is_private_bathroom": ("vệ sinh", "wc", "khép kín"),
        "has_ac": ("điều hòa", "điều hoà", "máy lạnh"),
        "has_heater": ("nóng lạnh", "máy nước nóng"),
        "has_washer": ("máy giặt",),
        "has_fridge": ("tủ lạnh", "tủ mát"),
        "allow_pets": ("thú cưng", "nuôi pet", "nuôi chó", "nuôi mèo"),
        "has_balcony": ("ban công",),
        "has_window": ("cửa sổ",),
        "has_fingerprint_lock": ("vân tay",),
        "parking_info": ("để xe", "giữ xe", "đậu xe"),
        "bed": ("giường",),
        "wardrobe": ("tủ quần áo", "tủ áo"),
        "room_size": ("diện tích", "m2", "m²"),
        "max_occupants": ("người ở", "ở tối đa", "số người"),
        "other_amenities": ("tiện ích khác", "tivi", "bếp"),
        "service_fees": ("phí", "tiền điện", "tiền nước", "wifi"),
        "move_in_date": (
            "vào ở", "vào ngay", "ở ngay", "chuyển vào", "dọn vào",
            "hôm nay", "ngày mai", "ngày mốt", "ngày kia", "ngày nữa", "hôm nữa",
        ),
        "status": ("trạng thái", "đã thuê", "đã cho thuê", "còn trống", "phòng trống"),
    }
    explicit = {
        key: value for key, value in dict(data or {}).items()
        if key in {"address", "room_code", "landlord_phone", "media_urls"}
    }
    for field, keywords in keyword_map.items():
        if any(keyword in clean_text for keyword in keywords) and field in data:
            explicit[field] = data[field]
    # Giá trị đọc trực tiếp từ câu người dùng được ưu tiên hơn kết quả AI.
    explicit.update(infer_explicit_boolean_room_updates(message_text))
    return explicit


def can_update_amenities_without_ai(message_text: str) -> bool:
    """Chỉ bỏ qua Gemini khi câu lệnh rõ ràng và không chứa trường dạng văn bản/số."""
    if not is_room_update_command(message_text) or not extract_room_code_for_media(message_text):
        return False
    if not infer_explicit_boolean_room_updates(message_text):
        return False
    clean_text = str(message_text or "").strip().lower()
    complex_field_keywords = (
        "địa chỉ", "tên phòng", "giá", "triệu", "tầng", "diện tích", "m2", "m²",
        "người ở", "số người", "phí", "tiền điện", "tiền nước", "wifi",
        "vào ở", "chuyển vào", "trạng thái", "tiện ích khác", "tivi", "bếp",
    )
    return not any(keyword in clean_text for keyword in complex_field_keywords)


ROOM_PRICE_REQUIRED = "ROOM_PRICE_REQUIRED"


def normalize_room_posting_block_value(block_type: str, value: str) -> str:
    clean_type = str(block_type or "").strip().upper()
    if clean_type == "PHONE":
        phone = format_national_phone(value)
        if not re.fullmatch(r"0[35789][0-9]{8}", phone or ""):
            raise ValueError("Số điện thoại chặn phải là SĐT Việt Nam hợp lệ gồm 10 số.")
        return phone
    if clean_type == "ADDRESS":
        normalized = normalize_block_address(value)
        if len(normalized) < 3:
            raise ValueError("Địa chỉ chặn phải có ít nhất 3 ký tự có nghĩa.")
        return normalized
    raise ValueError("Loại chặn phải là PHONE hoặc ADDRESS.")


def normalize_block_address(value: str) -> str:
    """Chuẩn hóa địa chỉ không phân biệt hoa/thường, dấu câu và dấu tiếng Việt."""
    normalized = normalize_room_address(value)
    normalized = unicodedata.normalize("NFD", normalized)
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    return normalized.replace("đ", "d")


def is_super_admin_phone(phone: str, db: Optional[Session] = None) -> bool:
    own_session = db is None
    session = db or SessionLocal()
    try:
        account = session.query(UserWeb).filter(UserWeb.user_id == "ADMIN_SUPER").first()
        return bool(
            account
            and format_national_phone(account.phone) == format_national_phone(phone)
        )
    finally:
        if own_session:
            session.close()


def find_room_posting_block(phone: str, address: str, db: Optional[Session] = None) -> Optional[dict]:
    """SĐT khớp chính xác; địa chỉ khớp khi chứa cụm địa chỉ bị chặn."""
    own_session = db is None
    session = db or SessionLocal()
    try:
        normalized_phone = format_national_phone(phone)
        normalized_address = normalize_block_address(address)
        rules = (
            session.query(RoomPostingBlock)
            .filter(RoomPostingBlock.is_active == 1)
            .order_by(RoomPostingBlock.created_at.desc())
            .all()
        )
        return match_room_posting_block(phone, address, rules)
    finally:
        if own_session:
            session.close()


def match_room_posting_block(phone: str, address: str, rules) -> Optional[dict]:
    """So khớp payload phòng với danh sách rule đã tải sẵn, tránh truy vấn DB lặp."""
    normalized_phone = format_national_phone(phone)
    normalized_address = normalize_block_address(address)
    for rule in rules or []:
        if not bool(rule.is_active):
            continue
        matched = (
            rule.block_type == "PHONE"
            and rule.normalized_value == normalized_phone
        ) or (
            rule.block_type == "ADDRESS"
            and bool(rule.normalized_value)
            and rule.normalized_value in normalized_address
        )
        if matched:
            return {
                "id": rule.id,
                "block_type": rule.block_type,
                "block_value": rule.block_value,
                "reason": rule.reason or "",
            }
    return None


def get_active_room_posting_blocks(db: Optional[Session] = None):
    own_session = db is None
    session = db or SessionLocal()
    try:
        return (
            session.query(RoomPostingBlock)
            .filter(RoomPostingBlock.is_active == 1)
            .order_by(RoomPostingBlock.created_at.desc())
            .all()
        )
    finally:
        if own_session:
            session.close()


def get_room_access_block(room: dict, rules=None) -> Optional[dict]:
    """Trả quy tắc đang khóa một phòng dựa trên chủ nhà hoặc địa chỉ."""
    if not room:
        return None
    if rules is None:
        return find_room_posting_block(room.get("landlord_phone"), room.get("address"))
    return match_room_posting_block(
        room.get("landlord_phone"),
        room.get("address"),
        rules,
    )


def format_room_posting_block_message(rule: dict) -> str:
    reason = str((rule or {}).get("reason") or "").strip()
    message = "Bạn đang bị chặn tạo mới hoặc cập nhật thông tin phòng. Vui lòng liên hệ quản trị viên."
    return f"{message} Lý do: {reason}" if reason else message


def format_room_access_block_message(rule: dict) -> str:
    reason = str((rule or {}).get("reason") or "").strip()
    message = "⛔ Phòng này đang bị khóa nên không thể xem, đặt lịch hoặc report."
    return f"{message} Lý do: {reason}" if reason else message


def is_missing_required_room_price(value) -> bool:
    """Nhận diện giá bị thiếu do người dùng hoặc AI trả về giá trị rỗng."""
    if value is None:
        return True
    clean_value = str(value).strip().lower()
    return clean_value in {"", "none", "null", "nan", "undefined", "chưa rõ", "chưa cập nhật"}


def upsert_room_to_db(
    data: dict,
    point_id: str = None,
    media_urls: Optional[List[str]] = None,
    current_excel_row: int = 0,
    type_process: str = None,
    landlord_phone: str = None,
    skip_ai_embedding: bool = False,
    replace_media_urls: bool = False,
    bypass_posting_block: bool = False,
) -> Optional[str]:
    try:
        data = dict(data or {})
        legacy_amenities = str(data.get("other_amenities") or "").lower()
        if not has_room_update_value(data.get("has_fridge")) and "tủ lạnh" in legacy_amenities:
            data["has_fridge"] = "Có"
        address = str(data.get("address", "")).strip()
        address_clean = address.lower()
        if address_clean:
            address_clean = re.sub(r'\b(hn)\b', 'Hà Nội', address_clean)
            address_clean = re.sub(r'\b(hcm|sg|sai gon)\b', 'Hồ Chí Minh', address_clean)
        raw_room_name = data.get("room_name")
        room_name = str(raw_room_name).strip() if has_room_update_value(raw_room_name) else ""
        phone = str(data.get("landlord_phone", "")).strip().lower()
        if phone in ["", "none", "null"]:
            phone = landlord_phone
        phone = format_national_phone(phone)
        if not re.fullmatch(r"0[35789][0-9]{8}", phone or ""):
            if type_process == "EXCEL":
                return f"❌ Dòng {current_excel_row}: landlord_phone là trường bắt buộc và phải là số điện thoại Việt Nam hợp lệ."
            return "landlord_phone là trường bắt buộc và phải là số điện thoại Việt Nam hợp lệ."
        # Import Excel chỉ coi là trùng khi đồng thời trùng địa chỉ + tên phòng.
        # Các luồng cập nhật thường vẫn xác định phòng theo mã/SĐT như trước.
        if point_id:
            existing_id = point_id
        elif type_process == "EXCEL":
            existing_id = find_duplicate_room_id(address_clean, room_name)
        else:
            existing_id = find_existing_room_id(
                address=address_clean,
                room_name=room_name,
                landlord_phone=phone,
                room_code=data.get("room_code"),
            )
        if existing_id:
            point_id = existing_id
            if type_process == "EXCEL":
                return (
                    f"❌ Dòng {current_excel_row}: Phòng bị trùng địa chỉ và tên phòng "
                    f"({address} — {room_name})."
                )

            old_records = qdrant_client.retrieve(
                collection_name=COLLECTION_NAME,
                ids=[existing_id],
                with_payload=True,
                with_vectors=False,
            )
            if not old_records or not old_records[0].payload:
                return "Không thể tải dữ liệu phòng cũ để cập nhật. Vui lòng thử lại."
            data = merge_room_partial_update(
                dict(old_records[0].payload),
                data,
                media_urls,
                replace_media_urls=replace_media_urls,
            )
            media_urls = data.get("media_urls") or []
            address = str(data.get("address") or "").strip()
            address_clean = address.lower()
            room_name = str(data.get("room_name") or "Phòng trọ").strip()

        if not bypass_posting_block and not is_super_admin_phone(phone):
            posting_block = find_room_posting_block(phone, address_clean)
            if posting_block:
                return format_room_posting_block_message(posting_block)
        
        if not address_clean:
            if type_process == "NOT_EXCEL":
                return f"Địa chỉ thiếu hoặc địa chỉ không đúng."
            else:
                return f"❌ Đăng ký thành công đến dòng {current_excel_row - 1}. Lỗi từ dòng {current_excel_row}: Địa chỉ thiếu hoặc địa chỉ không đúng."

        # Đây là lỗi dữ liệu người dùng, không phải lỗi Qdrant/hệ thống.
        # Kiểm tra trước khi gọi embedding để tránh phát sinh chi phí Gemini vô ích.
        if is_missing_required_room_price(data.get("price")):
            if type_process == "EXCEL":
                return f"❌ Dòng {current_excel_row}: Giá phòng là trường bắt buộc."
            return ROOM_PRICE_REQUIRED

        media_list = normalize_room_media_urls(
            media_urls if media_urls is not None else data.get("media_urls", [])
        )

        # 1. Thu thập và chuẩn hóa dữ liệu tiện ích (Booleans)
        amenities_list = [
            bool_to_text(data.get("is_private_bathroom"), "Vệ sinh khép kín / VS riêng", "Vệ sinh chung"),
            bool_to_text(data.get("has_ac"), "Có điều hòa", "Không có điều hòa"),
            bool_to_text(data.get("has_heater"), "Có nóng lạnh", "Không có nóng lạnh"),
            bool_to_text(data.get("has_washer"), "Có máy giặt", "Không có máy giặt"),
            bool_to_text(data.get("has_fridge"), "Có tủ lạnh", "Không có tủ lạnh"),
            bool_to_text(data.get("bed"), "Có giường", "Không có giường"),
            bool_to_text(data.get("wardrobe"), "Có tủ quần áo", "Không có tủ quần áo"),
            bool_to_text(data.get("allow_pets"), "Cho phép nuôi thú cưng / pet", "Không cho nuôi pet"),
            bool_to_text(data.get("has_balcony"), "Có ban công thoáng mát", "Không có ban công"),
            bool_to_text(data.get("has_window"), "Có cửa sổ thoáng", "Không có cửa sổ"),
            bool_to_text(data.get("has_fingerprint_lock"), "Khóa vân tay ra vào tự do", "Không có khóa vân tay"),
        ]
        # Lọc bỏ các giá trị rỗng
        amenities_str = ", ".join([item for item in amenities_list if item])

        # 2. Xử lý các trường thông tin bổ sung
        room_name_str = data.get("room_name", "Chưa rõ")
        price_str = data.get("price", "Chưa rõ")
        floor_str = f"Tầng {data.get('floor')}" if data.get("floor") else "Chưa rõ tầng"
        room_size_str = f"{data.get('room_size')} m²" if data.get("room_size") else "Chưa rõ diện tích"
        max_occ_str = f"Tối đa {data.get('max_occupants')} người ở" if data.get("max_occupants") else "Không giới hạn / Chưa rõ"
        move_in_str = normalize_move_in_date(data.get("move_in_date"))[0]
        status_str = data.get("status", "TRỐNG")
        parking_str = data.get("parking_info", "Chưa rõ thông tin xe")
        other_amenities_str = data.get("other_amenities", "Không có")
        service_fees_str = data.get("service_fees", "Chưa rõ")

        # 3. Ghép thành văn bản hoàn chỉnh để tạo Vector
        text_to_embed = f"""
        Thông tin chi tiết phòng trọ:
        - Địa chỉ: {address_clean}
        - Tên phòng: {room_name_str}
        - Giá thuê: {price_str}
        - Vị trí tầng: {floor_str}
        - Diện tích phòng: {room_size_str}
        - Trạng thái phòng: {status_str}
        - Ngày vào ở: {move_in_str}
        - Quy định số người: {max_occ_str}
        - Danh sách tiện nghi: {amenities_str}
        - Tiện ích bổ sung khác: {other_amenities_str}
        - Chỗ để xe: {parking_str}
        - Phí dịch vụ (điện, nước, wifi,...): {service_fees_str}
        """.strip()

        # File Excel đúng mẫu đã có dữ liệu cấu trúc và tìm kiếm hiện dùng payload
        # (địa chỉ/giá/trạng thái), nên không cần phát sinh phí Gemini Embedding.
        vector = (
            [1.0] + [0.0] * (VECTOR_SIZE - 1)
            if skip_ai_embedding
            else get_text_embedding(text_to_embed)
        )
        
        if not vector:
            report_error("Không thể tạo vector embedding cho phòng", context="upsert_room_to_db")
            if type_process == "NOT_EXCEL":
                return f"Hệ thống AI Vector Embedding đang bận vui lòng thử lại sau"
            else:
                return f"❌ Đăng ký thành công đến dòng {current_excel_row - 1}. Lỗi từ dòng {current_excel_row}: Hệ thống AI Vector Embedding đang bận vui lòng thử lại sau"
            # Trả về thông báo ngắn gọn cho NGUỜI DÙNG

        now_vn = datetime.now(VN_TZ)
        now_str = now_vn.strftime("%Y-%m-%d %H:%M:%S")
        created_at = now_str

        if point_id:
            new_point_id = point_id
            try:
                old_records = qdrant_client.retrieve(collection_name=COLLECTION_NAME, ids=[point_id])
                if old_records and old_records[0].payload:
                    created_at = old_records[0].payload.get("created_at", now_str)
            except Exception:
                pass
        else:
            new_point_id = str(uuid.uuid4())


        if not address_clean or address_clean.lower() in ["none", "null"]:
            raise ValueError("Lỗi: 'address' không được để trống hoặc null!")
            
        # 2. Đảm bảo room_code không null
        room_code = data.get("room_code")
        if not room_code or str(room_code).strip().lower() in ["none", "null", ""]:
            room_code = generate_unique_room_code()
        else:
            room_code = str(room_code).strip().upper()
        
        # Ngày vào ở chỉ lưu dạng dd/mm/YYYY, không lưu timestamp dư thừa.
        move_in_date_str = move_in_str

        raw_status = data.get("status")
        if raw_status is None or str(raw_status).strip().lower() in ["","none","null","chưa rõ","undefined"]:
            status_str = "TRỐNG"
        else:
            status_str = str(raw_status).strip().upper()

        payload = {
            "address": address_clean,
            "room_name": room_name,
            "room_code": room_code,
            "price": parse_price_to_number(data.get("price", "")),
            "floor": str(data.get("floor", "Chưa rõ")),
            "is_private_bathroom": str(data.get("is_private_bathroom", "Chưa rõ")),
            "has_ac": str(data.get("has_ac", "Chưa rõ")),
            "has_heater": str(data.get("has_heater", "Chưa rõ")),
            "has_washer": str(data.get("has_washer", "Chưa rõ")),
            "has_fridge": str(data.get("has_fridge", "Chưa rõ")),
            "allow_pets": str(data.get("allow_pets", "Chưa rõ")),
            "has_balcony": str(data.get("has_balcony", "Chưa rõ")),
            "has_window": str(data.get("has_window", "Chưa rõ")),
            "has_fingerprint_lock": str(data.get("has_fingerprint_lock", "Chưa rõ")),
            "parking_info": str(data.get("parking_info", "Chưa rõ")),
            "bed": str(data.get("bed", "Chưa rõ")),
            "wardrobe": str(data.get("wardrobe", "Chưa rõ")),
            "room_size": str(data.get("room_size", "Chưa rõ")),
            "max_occupants": str(data.get("max_occupants", "Chưa rõ")),
            "other_amenities": str(data.get("other_amenities", "Chưa rõ")),
            "service_fees": str(data.get("service_fees", "Chưa rõ")),
            "media_urls": media_list,
            "move_in_date": move_in_date_str,
            "status": status_str,
            "landlord_phone": phone,
            "created_at": created_at,
            "updated_at": now_str
        }

        qdrant_client.upsert(
            collection_name=COLLECTION_NAME,
            points=[PointStruct(id=new_point_id, vector=vector, payload=payload)],
            wait=True
        )
        mirror_db = SessionLocal()
        try:
            mirror = mirror_db.query(RoomRecord).filter(RoomRecord.id == str(new_point_id)).first()
            if mirror:
                mirror.landlord_phone = payload["landlord_phone"]
                mirror.room_code = payload["room_code"]
                mirror.payload = payload
                mirror.updated_at = vietnam_now()
            else:
                mirror_db.add(RoomRecord(id=str(new_point_id), landlord_phone=payload["landlord_phone"], room_code=payload["room_code"], payload=payload))
            mirror_db.commit()
        except Exception as mirror_error:
            mirror_db.rollback()
            report_error("Đồng bộ room_records thất bại", mirror_error, "upsert_room_to_db")
        finally:
            mirror_db.close()
        return "SUCCESS"
    except Exception as e:
        report_error("Qdrant upsert phòng thất bại", e, "upsert_room_to_db")
        raise Exception(f"❌ [QDRANT UPSERT EXCEPTION]: {e}")

def update_room_status_in_db(point_id: str, new_status: str) -> bool:
    if not point_id:
        return False
    normalized_status = str(new_status or "").strip().upper()
    if normalized_status not in {"TRỐNG", "ĐÃ CHO THUÊ"}:
        return False
    try:
        now_str = vietnam_now().strftime("%Y-%m-%d %H:%M:%S")
        qdrant_client.set_payload(
            collection_name=COLLECTION_NAME,
            payload={
                "status": normalized_status,
                "updated_at": now_str
            },
            points=[point_id],
            wait=True
        )
        mirror_db = SessionLocal()
        try:
            mirror = mirror_db.query(RoomRecord).filter(RoomRecord.id == str(point_id)).first()
            if mirror:
                mirror_payload = dict(mirror.payload or {})
                mirror_payload["status"] = normalized_status
                mirror_payload["updated_at"] = now_str
                mirror.payload = mirror_payload
                mirror.updated_at = vietnam_now()
                mirror_db.commit()
        except Exception as mirror_error:
            mirror_db.rollback()
            report_error(
                "Đồng bộ trạng thái phòng sang room_records thất bại",
                mirror_error,
                "update_room_status_in_db",
            )
            return False
        finally:
            mirror_db.close()
        return True
    except Exception as e:
        report_error("Cập nhật trạng thái phòng thất bại", e, "update_room_status_in_db")
        return False


STANDARD_EXCEL_HEADER_MAP = {
    "ma phong": "room_code",
    "room code": "room_code",
    "dia chi": "address",
    "address": "address",
    "gia thue": "price",
    "gia": "price",
    "price": "price",
    "ten phong": "room_name",
    "room name": "room_name",
    "tang": "floor",
    "floor": "floor",
    "wc rieng": "is_private_bathroom",
    "is private bathroom": "is_private_bathroom",
    "dieu hoa": "has_ac",
    "has ac": "has_ac",
    "nong lanh": "has_heater",
    "has heater": "has_heater",
    "may giat": "has_washer",
    "has washer": "has_washer",
    "tu lanh": "has_fridge",
    "has fridge": "has_fridge",
    "cho nuoi thu cung": "allow_pets",
    "allow pets": "allow_pets",
    "ban cong": "has_balcony",
    "has balcony": "has_balcony",
    "cua so": "has_window",
    "has window": "has_window",
    "khoa van tay": "has_fingerprint_lock",
    "has fingerprint lock": "has_fingerprint_lock",
    "cho de xe": "parking_info",
    "parking info": "parking_info",
    "giuong": "bed",
    "bed": "bed",
    "tu quan ao": "wardrobe",
    "wardrobe": "wardrobe",
    "dien tich": "room_size",
    "room size": "room_size",
    "so nguoi toi da": "max_occupants",
    "max occupants": "max_occupants",
    "tien ich khac": "other_amenities",
    "other amenities": "other_amenities",
    "phi dich vu": "service_fees",
    "service fees": "service_fees",
    "link anh video": "media_urls",
    "media urls": "media_urls",
    "ngay co the vao o": "move_in_date",
    "move in date": "move_in_date",
    "trang thai": "status",
    "status": "status",
    "sdt chu nha": "landlord_phone",
    "so dien thoai chu nha": "landlord_phone",
    "landlord phone": "landlord_phone",
}

EXCEL_BOOLEAN_FIELDS = {
    "is_private_bathroom", "has_ac", "has_heater", "has_washer", "has_fridge",
    "allow_pets", "has_balcony", "has_window", "has_fingerprint_lock",
    "parking_info", "bed", "wardrobe",
}


def normalize_excel_header(value: str) -> str:
    """Chuẩn hóa tiêu đề Excel Việt/Anh để ánh xạ không cần Gemini."""
    text_value = unicodedata.normalize("NFD", str(value or "").strip().lower())
    text_value = "".join(char for char in text_value if unicodedata.category(char) != "Mn")
    text_value = text_value.replace("đ", "d")
    text_value = re.sub(r"[^a-z0-9]+", " ", text_value)
    return re.sub(r"\s+", " ", text_value).strip()


def extract_standard_excel_rows(dataframe) -> Optional[List[dict]]:
    """Đọc file đúng mẫu trực tiếp; trả ``None`` để dùng AI cho file cũ/sai mẫu."""
    column_mapping = {}
    for original_column in dataframe.columns:
        field_name = STANDARD_EXCEL_HEADER_MAP.get(normalize_excel_header(original_column))
        if not field_name or field_name in column_mapping.values():
            return None
        column_mapping[original_column] = field_name

    required_fields = {"address", "price", "room_name"}
    if not required_fields.issubset(set(column_mapping.values())):
        return None

    parsed_rows = []
    for raw_row in dataframe.to_dict(orient="records"):
        extracted = {}
        for original_column, field_name in column_mapping.items():
            raw_value = raw_row.get(original_column)
            value = "" if raw_value is None or pd.isna(raw_value) else str(raw_value).strip()
            if value.lower() in {"nan", "none", "null", "undefined"}:
                value = ""
            if field_name in EXCEL_BOOLEAN_FIELDS and value:
                normalized_value = normalize_excel_header(value)
                if normalized_value in {"co", "yes", "true", "1", "x"}:
                    value = "Có"
                elif normalized_value in {"khong", "no", "false", "0"}:
                    value = "Không"
            elif field_name == "status" and value:
                value = value.upper()
            elif field_name == "move_in_date":
                normalized_date = normalize_move_in_date(value)[0]
                # Giữ nguyên giá trị sai để bước validate trả đúng lỗi cho người dùng.
                value = normalized_date if normalized_date or not value else value
            elif field_name == "media_urls" and value:
                value = ",".join(item.strip() for item in re.split(r"[,;\n]+", value) if item.strip())
            extracted[field_name] = value
        parsed_rows.append(extracted)
    return parsed_rows


def validate_excel_room_data(data: dict) -> List[str]:
    """Trả lỗi theo từng cột, kèm giá trị sai và cách sửa; không ghi log hệ thống."""
    errors = []

    def shown(value) -> str:
        text_value = str(value or "").strip()
        if not text_value:
            return "[đang để trống]"
        return f"'{text_value[:80]}{'…' if len(text_value) > 80 else ''}'"

    address = str(data.get("address") or "").strip()
    room_name = str(data.get("room_name") or "").strip()
    if not address or address.lower() in ROOM_NULL_VALUES:
        errors.append(
            f"Cột “Địa chỉ”: giá trị {shown(address)} không hợp lệ. "
            "Cách sửa: nhập địa chỉ đầy đủ, ví dụ '20 Phan Thị Hành, Phú Thọ Hòa, Tân Phú'."
        )
    if not room_name or room_name.lower() in ROOM_NULL_VALUES:
        errors.append(
            f"Cột “Tên phòng”: giá trị {shown(room_name)} không hợp lệ. "
            "Cách sửa: nhập tên/số phòng, ví dụ 'Phòng 201'."
        )

    raw_price = data.get("price")
    if is_missing_required_room_price(raw_price):
        errors.append(
            "Cột “Giá thuê”: đang để trống. Cách sửa: nhập số tiền VNĐ, ví dụ 5000000."
        )
    elif parse_price_to_number(raw_price) <= 0:
        errors.append(
            f"Cột “Giá thuê”: giá trị {shown(raw_price)} không đọc được hoặc không lớn hơn 0. "
            "Cách sửa: nhập 5000000 hoặc 5 triệu."
        )

    raw_phone = data.get("landlord_phone")
    phone = format_national_phone(raw_phone)
    if not re.fullmatch(r"0[35789][0-9]{8}", phone or ""):
        errors.append(
            f"Cột “SĐT chủ nhà”: giá trị {shown(raw_phone)} không phải SĐT Việt Nam hợp lệ. "
            "Cách sửa: nhập đủ 10 số, bắt đầu bằng 03, 05, 07, 08 hoặc 09; ví dụ 0901234567."
        )

    status_value = str(data.get("status") or "TRỐNG").strip().upper()
    if status_value not in {"TRỐNG", "ĐÃ CHO THUÊ"}:
        errors.append(
            f"Cột “Trạng thái”: giá trị {shown(data.get('status'))} không hợp lệ. "
            "Cách sửa: chỉ nhập TRỐNG hoặc ĐÃ CHO THUÊ."
        )

    raw_move_in = str(data.get("move_in_date") or "").strip()
    if raw_move_in and normalize_move_in_date(raw_move_in)[0] == "":
        errors.append(
            f"Cột “Ngày có thể vào ở”: giá trị {shown(raw_move_in)} không phải ngày hợp lệ. "
            "Cách sửa: nhập ngày/tháng/năm, ví dụ 25/09/2026; nếu chưa xác định thì để trống."
        )

    boolean_field_labels = {
        "is_private_bathroom": "WC riêng",
        "has_ac": "Điều hòa",
        "has_heater": "Nóng lạnh",
        "has_washer": "Máy giặt",
        "has_fridge": "Tủ lạnh",
        "allow_pets": "Cho nuôi thú cưng",
        "has_balcony": "Ban công",
        "has_window": "Cửa sổ",
        "has_fingerprint_lock": "Khóa vân tay",
        "bed": "Giường",
        "wardrobe": "Tủ quần áo",
    }
    allowed_boolean_values = {"", "co", "khong", "chua ro"}
    for field_name, column_label in boolean_field_labels.items():
        raw_value = data.get(field_name)
        if normalize_excel_header(raw_value) not in allowed_boolean_values:
            errors.append(
                f"Cột “{column_label}”: giá trị {shown(raw_value)} không hợp lệ. "
                "Cách sửa: chọn Có, Không hoặc Chưa rõ."
            )

    raw_max_occupants = str(data.get("max_occupants") or "").strip()
    if raw_max_occupants and not re.fullmatch(r"[1-9][0-9]*", raw_max_occupants):
        errors.append(
            f"Cột “Số người tối đa”: giá trị {shown(raw_max_occupants)} không hợp lệ. "
            "Cách sửa: nhập số nguyên lớn hơn 0, ví dụ 2."
        )

    raw_room_size = str(data.get("room_size") or "").strip()
    if raw_room_size and not re.fullmatch(r"\d+(?:[.,]\d+)?\s*(?:m2|m²)?", raw_room_size, re.IGNORECASE):
        errors.append(
            f"Cột “Diện tích”: giá trị {shown(raw_room_size)} không hợp lệ. "
            "Cách sửa: nhập 20m2 hoặc 20."
        )

    media_value = data.get("media_urls") or ""
    media_items = media_value if isinstance(media_value, list) else re.split(r"[,;\n]+", str(media_value))
    invalid_media = [
        str(item).strip() for item in media_items
        if str(item).strip() and not re.match(r"^https://", str(item).strip(), re.IGNORECASE)
    ]
    if invalid_media:
        errors.append(
            f"Cột “Link ảnh/video”: URL {shown(invalid_media[0])} không hợp lệ. "
            "Cách sửa: dùng đường dẫn công khai bắt đầu bằng https://; nhiều link ngăn cách bằng dấu phẩy."
        )
    return errors

def ai_validate_and_extract_room_batch(rows_list: List[dict]) -> List[Optional[dict]]:
    """Xử lý nhóm dữ liệu Excel - Fix triệt để UnboundLocalError & Lỗi parse giá tiền."""
    if not gemini_client or not rows_list:
        return [None] * len(rows_list)

    # 1. Clean dicts (Chuyển NaN thành None và ép chuỗi an toàn cho từng trường)
    clean_rows = []
    for row in rows_list:
        clean_row = {}
        for k, v in row.items():
            if pd.isna(v):
                clean_row[k] = None
            else:
                # Chuyển mọi giá trị sang chuỗi để tránh lỗi kiểu dữ liệu khi gửi cho Prompt
                clean_row[k] = str(v).strip()
        clean_rows.append(clean_row)

    prompt = f"""
        Bạn là Trợ lý AI Xử lý và Chuẩn hóa Dữ liệu Phòng trọ từ File Excel.
        Nhiệm vụ: Phân tích và chuẩn hóa DANH SÁCH {len(clean_rows)} dòng dữ liệu thô dưới đây.

        DỮ LIỆU CÁC DÒNG EXCEL THÔ (JSON):
        {json.dumps(clean_rows, ensure_ascii=False)}

        YÊU CẦU XỬ LÝ QUAN TRỌNG VỀ GIÁ PHÒNG (`price`):
        - Hãy trích xuất giá phòng dưới dạng CHUỖI NGUYÊN BẢN hoặc CHUYỂN VỀ DẠNG SỐ TRÒN CHUẨN (Ví dụ: "3500000", "3.5 triệu", "3,500,000"). 
        - Nếu không có giá hoặc bị trống, để mặc định là "".

        YÊU CẦU XỬ LÝ CÁC TRƯỜNG KHÁC:
        1. Đưa các tiện ích (điều hòa, nóng lạnh, vệ sinh...) về "Có", "Không".
        2. Định dạng `status`: "TRỐNG" hoặc "ĐÃ CHO THUÊ".

        TRẢ VỀ DUY NHẤT 1 MẢNG JSON CÓ ĐÚNG {len(clean_rows)} PHẦN TỬ THEO THỨ TỰ:
        [
          {{
            "index": 0,
            "action": "ADD_ROOM",
            "extracted_data": {{
              "address": "",
              "room_name": "",
              "price": "",
              "floor": "",
              "is_private_bathroom": "",
              "has_ac": "",
              "has_heater": "",
              "has_washer": "",
              "has_fridge": "",
              "allow_pets": "",
              "has_balcony": "",
              "has_window": "",
              "has_fingerprint_lock": "",
              "parking_info": "",
              "bed": "",
              "wardrobe": "",
              "room_size": "",
              "max_occupants": "",
              "other_amenities": "",
              "service_fees": "",
              "status": "TRỐNG",
              "move_in_date": "",
              "media_urls": ""
            }}
          }}
        ]
    """

    models_to_try = [GEMINI_TEXT_MODEL]

    for model_name in models_to_try:
        for attempt in range(GEMINI_TEXT_RETRIES):
            # 💡 FIX LỖI: Khởi tạo parsed_data = None ngay trước khối try
            parsed_data = None 
            try:
                print("🤖 [GEMINI] Đang gọi Gemini AI để xử lý Excel...", flush=True)
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=build_gemini_generation_config(
                        "application/json", GEMINI_EXCEL_MAX_OUTPUT_TOKENS
                    )
                )
                _log_gemini_usage(response, "excel_normalization", model_name)
                
                if response and response.text:
                    raw_text = response.text.strip()
                    
                    # Cắt bỏ Markdown Codeblock nếu AI lỡ đính kèm
                    if raw_text.startswith("```"):
                        raw_text = re.sub(r"^```(?:json)?\n?", "", raw_text)
                        raw_text = re.sub(r"\n?```$", "", raw_text)
                    
                    # Parse JSON từ câu trả lời của AI
                    parsed_data = json.loads(raw_text)
                    
                    # Xử lý trường hợp AI trả về mảng lồng mảng [[{...}]]
                    if isinstance(parsed_data, list) and len(parsed_data) > 0 and isinstance(parsed_data[0], list):
                        parsed_data = parsed_data[0]
                        
                    if isinstance(parsed_data, list):
                        return parsed_data

            except Exception as e:
                err_str = str(e)
                print(f"⚠️ Lỗi Batch Retry ({model_name} - Thử {attempt+1}): {err_str}")
                
                if "503" in err_str or "429" in err_str or "UNAVAILABLE" in err_str:
                    wait_time = (2 ** attempt) + random.uniform(0.5, 1.5)
                    time.sleep(wait_time)
                else:
                    break # Chuyển sang model tiếp theo nếu dính lỗi parse JSON/Logic

    # Trả về danh sách Mặc định nếu tất cả Model/Retry đều thất bại (Tránh văng crash ứng dụng)
    return [None] * len(rows_list)

import os
import requests
import pandas as pd

def process_excel_file(file_url: str, sender_id: str) -> str:
    temp_file = "temp_rooms.xlsx"
    try:
        # 1. Tải file về local
        res = requests.get(file_url, timeout=30)
        if res.status_code != 200:
            return "Không thể tải file Excel từ đường dẫn được cung cấp."

        with open(temp_file, "wb") as f:
            f.write(res.content)

        # 2. Đọc dữ liệu file Excel
        # Giữ nguyên số 0 đầu của SĐT và các mã dạng chuỗi trong Excel.
        df = pd.read_excel(temp_file, dtype=str)
        standard_rows = extract_standard_excel_rows(df)
        used_ai = standard_rows is None
        rows = df.to_dict(orient="records") if used_ai else standard_rows

        success_count = 0
        owner_db = SessionLocal()
        try:
            landlord_phone = get_phone_by_user_id(owner_db, str(sender_id)) or ""
        finally:
            owner_db.close()
        if not landlord_phone:
            return "Vui lòng chia sẻ số điện thoại với OA trước khi nhập danh sách phòng."
        
        # Danh sách lưu thông tin lỗi theo dạng: [(số_dòng, lý_do)]
        failed_rows_details = []

        BATCH_SIZE = 15

        # 3. Duyệt theo từng batch
        for i in range(0, len(rows), BATCH_SIZE):
            batch_rows = rows[i:i + BATCH_SIZE]
            batch_results = (
                ai_validate_and_extract_room_batch(batch_rows)
                if used_ai
                else [
                    {"index": i + offset, "action": "ADD_ROOM", "extracted_data": row}
                    for offset, row in enumerate(batch_rows)
                ]
            )

            # 4. Duyệt qua từng kết quả trong batch bằng enumerate
            for offset, validated_data in enumerate(batch_results):
                # Tính số dòng chính xác trong Excel (+2 do Header và Index từ 1)
                current_excel_row = i + offset + 2

                if isinstance(validated_data, list) and len(validated_data) > 0:
                    validated_data = validated_data[0]

                if not validated_data or not isinstance(validated_data, dict):
                    failed_rows_details.append((current_excel_row, "AI không phân tích được dữ liệu"))
                    continue

                extracted = validated_data.get("extracted_data", {})
                if not isinstance(extracted, dict):
                    failed_rows_details.append((current_excel_row, "Dữ liệu AI trích xuất không hợp lệ"))
                    continue

                raw_address = str(extracted.get("address") or "").strip()

                if not raw_address or raw_address.lower() in ["[chưa cập nhật]", "none", "null", "chưa rõ", ""]:
                    failed_rows_details.append((current_excel_row, "Địa chỉ trống hoặc không hợp lệ"))
                    continue

                raw_room_name = str(extracted.get("room_name") or "").strip()
                if not raw_room_name or raw_room_name.lower() in {"none", "null", "chưa rõ"}:
                    failed_rows_details.append((
                        current_excel_row,
                        "Thiếu tên phòng; cần tên phòng để kiểm tra trùng cùng địa chỉ",
                    ))
                    continue

                # upsert_room_to_db trả về None/"" nếu thành công, trả về string lỗi nếu thất bại
                extracted["landlord_phone"] = landlord_phone
                message = upsert_room_to_db(
                    data=extracted,
                    current_excel_row=current_excel_row,
                    type_process="EXCEL",
                    landlord_phone=landlord_phone,
                    skip_ai_embedding=not used_ai,
                    bypass_posting_block=str(sender_id) == Config.ZALO_ADMIN_ID,
                )
                
                if message == "SUCCESS":
                    success_count += 1
                else:
                    failed_rows_details.append((current_excel_row, f"Lỗi DB - {message}"))


        # Dọn dẹp file tạm
        if os.path.exists(temp_file):
            os.remove(temp_file)

        # 5. Đóng gói thông báo trả về
        mode_text = "AI dự phòng vì file không đúng mẫu" if used_ai else "trực tiếp, không dùng AI"
        msg = f"Đã xử lý {mode_text}!\n- Thành công: {success_count} phòng.\n"

        if failed_rows_details:
            # Lấy tối đa 10 dòng lỗi đầu tiên để tránh tin nhắn quá dài
            error_lines = [f"  + Dòng {row}: {reason}" for row, reason in failed_rows_details[:10]]
            msg += "\n\nChi tiết các dòng bị lỗi:\n" + "\n".join(error_lines)

            if len(failed_rows_details) > 10:
                msg += f"\n  ... và {len(failed_rows_details) - 10} dòng lỗi khác."

        return msg

    except Exception as e:
        report_error("Xử lý file Excel thất bại", e, "process_excel_file")
        if os.path.exists(temp_file):
            os.remove(temp_file)
        return "Lỗi trong quá trình xử lý file Excel."

def process_zalo_ai_logic(
    message_text: str,
    media_items: list = None,
    user_id: str = "SYSTEM",
    db: Session = None,
    public_base_url: str = "",
):
    incoming_media_urls = []
    for item in (media_items or []):
        saved_url = save_media_file(item["url"], is_video=item.get("is_video", False))
        if saved_url:
            incoming_media_urls.append(saved_url)
    if media_items and not incoming_media_urls and not str(message_text or "").strip():
        send_zalo_message(
            user_id,
            "⚠️ Ảnh/video bạn gửi đang rỗng, hết hạn hoặc không đúng định dạng nên hệ thống chưa lưu được. Bạn vui lòng gửi lại tệp khác.",
        )
        return
    if incoming_media_urls:
        add_pending_media(user_id, incoming_media_urls)

    phone = get_phone_by_user_id(db, user_id) if db is not None else None
    pending_urls = get_pending_media(user_id)
    all_current_media = list(dict.fromkeys(pending_urls + incoming_media_urls))
    urls_to_send = all_current_media
    search_results_to_send = None

    # Luồng xác nhận media chạy độc lập với AI để không bao giờ đoán nhầm phòng.
    selected_room_code = extract_room_code_for_media(message_text) if pending_urls else None
    if selected_room_code:
        if not phone:
            send_zalo_message(user_id, "⚠️ Vui lòng chia sẻ SĐT Zalo trước khi chọn phòng cập nhật ảnh/video.")
            return
        try:
            updated_room = attach_media_to_owned_room(phone, selected_room_code, pending_urls)
        except ValueError as exc:
            reply = f"❌ {exc}\n\n{build_room_media_choices(phone, len(pending_urls))}"
            send_zalo_message(user_id, reply)
            return
        except Exception as exc:
            report_error("Cập nhật media phòng thất bại", exc, f"room:{selected_room_code}")
            send_zalo_message(
                user_id,
                f"⚠️ Chưa thể cập nhật phòng {selected_room_code}. Ảnh/video vẫn được giữ tạm, bạn vui lòng thử lại.",
            )
            return
        clear_pending_media(user_id)
        room_name = str(updated_room.get("room_name") or "[chưa có tên]").strip()
        address = str(updated_room.get("address") or "[chưa có địa chỉ]").strip()
        reply = (
            f"✅ Đã cập nhật {updated_room['new_media_count']} ảnh/video vào đúng phòng:\n"
            f"🏠 Mã phòng: {selected_room_code}\n"
            f"📝 Tên phòng: {room_name}\n"
            f"📍 Địa chỉ: {address}\n"
            f"📚 Tổng media hiện có: {len(updated_room.get('media_urls') or [])} file"
        )
        send_zalo_message(user_id, reply)
        return

    if not message_text.strip() and incoming_media_urls:
        if not phone:
            reply = (
                f"📸 Đã lưu tạm {len(pending_urls)} ảnh/video.\n\n"
                "⚠️ Bạn cần chia sẻ SĐT Zalo để hệ thống chỉ hiển thị các phòng thuộc quyền quản lý của bạn."
            )
        else:
            reply = build_room_media_choices(phone, len(pending_urls))
        send_zalo_message(user_id, reply)
        return
    
    # 1. Lấy lịch sử chat của User từ Redis
    history_list = get_chat_history(user_id)
    history_context_str = json.dumps(history_list, ensure_ascii=False) if history_list else "Chưa có lịch sử hội thoại trước đó."
    
    print(f"history_list: {history_list}")
    print(f"history_context_str: {history_context_str}")
    
    # Lấy thông tin phòng đang chờ xác nhận (nếu có)
    pending_room = get_pending_room(user_id)
    pending_room_str = json.dumps(pending_room, ensure_ascii=False) if pending_room else "Không có phòng nào đang chờ xác nhận."
    
    # Kiểm tra từ khóa Reset Session từ người dùng
    clean_message = message_text.strip().lower()
    if any(keyword in clean_message for keyword in ["bắt đầu lại", "tìm phòng khác", "xóa lịch sử", "reset", "làm mới"]):
        clear_chat_history(user_id)
        clear_pending_media(user_id)
        ai_reply = "Dạ em đã làm mới cuộc hội thoại rồi ạ. Anh/chị muốn tìm phòng trọ ở khu vực nào và ngân sách khoảng bao nhiêu ạ?"
        send_zalo_message(user_id, ai_reply)
        return

    # Ý định quản lý phòng của chủ nhà được xử lý trước AI để không nhầm thành tìm thuê.
    if is_my_rooms_request(message_text):
        if not phone:
            send_zalo_message(
                user_id,
                "⚠️ Bạn vui lòng chia sẻ SĐT Zalo trước để hệ thống xác định đúng các phòng do bạn đăng.",
            )
        else:
            send_my_rooms_overview(user_id, phone)
        return

    # Cập nhật tiện ích rõ ràng bằng mã phòng không cần gọi Gemini tạo nội dung.
    if can_update_amenities_without_ai(message_text):
        direct_room_code = extract_room_code_for_media(message_text)
        if not phone:
            send_zalo_message(user_id, "⚠️ Vui lòng chia sẻ SĐT Zalo trước khi cập nhật phòng.")
            return
        existing_point_id = find_existing_room_id(
            address="",
            landlord_phone=phone,
            room_code=direct_room_code,
        )
        if not existing_point_id:
            send_zalo_message(
                user_id,
                f"❌ Không tìm thấy phòng {direct_room_code} thuộc SĐT của bạn.\n\n{build_owned_room_reference(phone)}",
            )
            return
        direct_updates = infer_explicit_boolean_room_updates(message_text)
        direct_updates["room_code"] = direct_room_code
        result = upsert_room_to_db(
            data=direct_updates,
            point_id=existing_point_id,
            media_urls=None,
            type_process="NOT_EXCEL",
            landlord_phone=phone,
            bypass_posting_block=str(user_id) == Config.ZALO_ADMIN_ID,
        )
        if result == "SUCCESS":
            changed_fields = ", ".join(sorted(set(direct_updates) - {"room_code"}))
            send_zalo_message(user_id, f"✅ Đã cập nhật phòng {direct_room_code}: {changed_fields}.")
        else:
            send_zalo_message(user_id, f"❌ Không thể cập nhật phòng {direct_room_code}: {result}")
        return

    # Câu tìm phòng đã có cả địa chỉ và giá được xử lý trực tiếp trước Gemini.
    # Điều này tránh lỗi phân loại/JSON/timeout làm mất một truy vấn hợp lệ.
    listing_intent = is_room_listing_request(message_text)
    direct_search = extract_natural_room_search(message_text)
    direct_search_intent = normalize_location_search(message_text)
    if (
        not listing_intent
        and direct_search.get("location_search")
        and direct_search.get("max_price", 0) > 0
        and re.search(r"\b(?:xem|tim|kiem|thue|can)\s+(?:phong|phong tro)\b", direct_search_intent)
    ):
        add_chat_history(user_id=user_id, user_message=message_text, ai_reply=None)
        location_search = direct_search["location_search"]
        min_p = float(direct_search.get("min_price") or 0)
        max_p = float(direct_search.get("max_price") or 0)
        search_results = search_rooms_with_filter(
            query_text=message_text,
            location_search=location_search,
            min_price=min_p,
            max_price=max_p,
            top_k=MAX_SEARCH_ROOMS,
        )
        if search_results:
            send_zalo_search_results(user_id, search_results)
        else:
            send_zalo_message(
                user_id,
                f"Dạ chưa tìm thấy phòng ở {location_search} với giá từ {min_p:,.0f}đ đến {max_p:,.0f}đ.",
            )
        return

    try:
        
        system_prompt = f"""
        Bạn là Trợ lý AI Quản lý và Tư vấn Phòng trọ thông minh trên Zalo.
        
        THÔNG TIN PHÒNG ĐANG CHỜ XÁC NHẬN CỦA NGUỜI DÙNG (NẾU CÓ):
        {pending_room_str}

        LỊCH SỬ HỘI THOẠI GẦN ĐÂY:
        {history_context_str}
        
        Phân tích tin nhắn người dùng và trích xuất đúng 13 trường thông tin:

        1. `address`: Phân tích địa chỉ đầy đủ
        2. `room_name`: Tên hoặc số phòng (VD: Phòng 301, Phòng tầng 2...).
        3. `price`: Giá thuê.
        4. `floor`: Tầng bao nhiêu.
        5. `is_private_bathroom`: Vệ sinh riêng hay khép kín hay không.
        6. `has_ac`:  Điều hoà có hay không?
        7. `has_heater`: có bình nóng lạnh không?
        8. `has_washer`: Có máy giặt không?
        9. `has_fridge`: Có tủ lạnh không?
        10. `allow_pets`: Có cho nuôi pet không?
        10. `has_balcony`: Có ban công không?
        11. `has_window`: Có cửa sổ không?
        12. `has_fingerprint_lock`: Ra vào bằng khoá vân tay có hay không?
        13. `parking_info`: có chỗ để xe không?
		14. `bed`: Có giường không?
		15. `wardrobe`: Có tủ quần áo không?
		16. `room_size`: Diện tích phòng bao nhiêu?
        17. `max_occupants`: số ng ở tối đã
        18. `other_amenities`: Có thêm tiện ích gì khác
        19. `service_fees`: Phí dịch vụ (điện, nước, wifi)...
        20. `status`: Trạng thái phòng ("TRỐNG" hoặc "ĐÃ CHO THUÊ").
        21. `move_in_date`: Ngày có thể chuyển vào phòng để ở
        22. `min_price`: Giá thuê thấp nhất
        23. `max_price`: Giá thuê cao nhất

        DỮ LIỆU ĐẦU VÀO:
        - Tin nhắn: "{message_text}"
        - Media kèm theo: {json.dumps(all_current_media)}

        QUY TẮC PHÂN LOẠI ACTION:
        - "ADD_ROOM": Dùng khi người dùng ĐĂNG PHÒNG MỚI hoặc CẬP NHẬT/SỬA BẤT KỲ THÔNG TIN NÀO CỦA PHÒNG.
        - "UPDATE_STATUS": CHỈ DÙNG khi người dùng báo phòng "ĐÃ CHO THUÊ", "ĐÃ CHỐT" hoặc "ĐỔI SANG TRỐNG".
        - "SEARCH_ROOM": Dùng khi khách có nhu cầu TÌM KIẾM phòng trọ.
        - "LIST_MY_ROOMS": Dùng khi người dùng muốn xem/liệt kê/quản lý các phòng do chính họ đăng hoặc các phòng cho thuê của họ. Không yêu cầu địa chỉ và giá cho action này.
        
        CÁC QUY TẮC BẮT BUỘC KHI TRÍCH XUẤT ĐỊA CHỈ (address):
        1. GIỮ NGUYÊN 100% TÊN ĐƯỜNG/TÊN PHƯỜNG do người dùng nhập. KHÔNG TỰ Ý SỬA LỖI CHÍNH TẢ TÊN RIÊNG (Ví dụ: "Phan Thị Hành" KHÔNG ĐƯỢC sửa thành "Phan Thị Hạnh").
        2. Chỉ chuẩn hóa từ viết tắt viết tắt tỉnh/thành phố: HN -> Hà Nội, HCM/hcm/sg -> Hồ Chí Minh.
        3. Khi người dùng yêu cầu CẬP NHẬT/SỬA phòng, trường nào người dùng không nhắc tới phải trả về chuỗi rỗng ""; tuyệt đối không tự điền "Không", "Chưa rõ" hoặc giá trị mặc định.
        
        CÁC QUY TẮC BẮT BUỘC KHI TRÍCH XUẤT NGÀY CÓ THỂ CHUYỂN VÀO PHÒNG ĐỂ Ở (move_in_date):
        1. Nếu người dùng CÓ NÓI RÕ ngày có thể vào ở:
           - Hiểu cả ngày cụ thể và cách nói thông dụng như: vào ở ngay, hôm nay, ngày mai, mai, ngày mốt, ngày kia.
           - Quy đổi thành đúng ngày theo múi giờ Việt Nam.
           - Định dạng: %d/%m/%Y
        2. Nếu người dùng KHÔNG NÓI ngày có thể vào ở:
           - BẮT BUỘC trả về chuỗi rỗng: ""
           - KHÔNG tự suy đoán ngày.
           - KHÔNG tự tạo ngày hiện tại.
           - KHÔNG sử dụng ngày từ kiến thức của AI.
        Ví dụ:
        Người dùng:
        "21 Phan Thị Hành giá 5tr, máy lạnh, máy giặt"
        => "move_in_date": ""
        Người dùng:
        "21 Phan Thị Hành giá 5tr, ngày 10/09/2026 vào ở"
        => "move_in_date": "10/09/2026"
        
        THÔNG TIN CHO ACTION "SEARCH_ROOM":
            THÔNG TIN BẮT BUỘC ĐỐI VỚI YÊU CẦU TÌM PHÒNG (SEARCH_ROOM):
            1. `location_search`: Yêu cầu người dùng nhập địa chỉ muốn thuê
            2. `min_price`: Giá thuê tối thiểu khách có thể trả (Dạng số float tính theo VNĐ, ví dụ: 2000000). Nếu khách không nói giá tối thiểu thì mặc định là 0.
            3. `max_price`: Giá thuê tối đa khách có thể trả (Dạng số float tính theo VNĐ, ví dụ: 4000000).

            QUY TẮC KIỂM TRA ĐIỀU KIỆN (BẮT BUỘC CHO SEARCH_ROOM):
            - Nếu người dùng tìm phòng nhưng KHÔNG CÓ thông tin địa chỉ -> Set `is_valid_search` = false.
            - Nếu người dùng tìm phòng nhưng KHÔNG CÓ Giá tối đa -> Set `is_valid_search` = false.
            
            - Nếu cung cấp đủ địa chỉ và Giá -> Set `is_valid_search` = true.
          
        
        TRẢ VỀ DUY NHẤT 1 CHUỖI JSON ĐÚNG CẤU TRÚC:
        {{
          "action": "ADD_ROOM | SEARCH_ROOM | UPDATE_STATUS | LIST_MY_ROOMS",
          "is_valid_search": false,
          "extracted_search": {{
            "location_search": "",
            "min_price": 0,
            "max_price": 0
          }},
          "extracted_data": {{
            "address": "Địa chỉ phòng trọ...",
            "room_name": "Tên phòng trọ...",
            "room_code": "Mã phòng 6 ký tự nếu người dùng cung cấp, nếu không thì để rỗng",
            "price": "Giá thuê (ví dụ: 3.5 triệu)...",
            "floor": "Tầng số...",
            "is_private_bathroom": "Có/Không",
            "has_ac": "Có/Không",
            "has_heater": "Có/Không",
            "has_washer": "Có/Không",
            "has_fridge": "Có/Không",
            "allow_pets": "Có/Không",
            "has_balcony": "Có/Không",
            "has_window": "Có/Không",
            "has_fingerprint_lock": "Có/Không",
            "parking_info": "Có/Không",
            "bed": "Có/Không",
            "wardrobe": "Có/Không",
            "room_size": "Diện tích phòng bảo nhiêu m2",
            "max_occupants": "Số người ở tối đa...",
            "other_amenities": "Tiện ích khác...",
            "service_fees": "Phí dịch vụ (điện, nước, wifi)...",
            "move_in_date": "Ngày có thể chuyển vào...",
            "media_urls": {json.dumps(all_current_media)},
            "landlord_phone": "{phone}"
          }}
        }}
        """
                
        if listing_intent:
            # Tin đăng rõ ràng được xử lý nội bộ, không tốn token và không phụ thuộc JSON Gemini.
            result_data = {
                "action": "ADD_ROOM",
                "is_valid_search": False,
                "extracted_search": {"location_search": "", "min_price": 0, "max_price": 0},
                "extracted_data": apply_direct_room_listing_fallbacks({}, message_text),
                "ai_reply": "Dạ em đã ghi nhận thông tin đăng phòng.",
            }
        else:
            raw_text = generate_content_with_retry(system_prompt, mime_type="application/json")
            if not raw_text:
                raise GeminiOutputError("Gemini không tạo được JSON hợp lệ sau khi phục hồi.")

            cleaned_text = clean_json_string(raw_text)
            if not cleaned_text:
                raise GeminiOutputError("Phản hồi Gemini bị rỗng sau khi làm sạch.")
            result_data = json.loads(cleaned_text)
        ai_reply = result_data.get("ai_reply", "Dạ em đã ghi nhận thông tin rồi ạ!")
        print(f"ai_reply 1: {ai_reply}")
        action = result_data.get("action")
        natural_search = extract_natural_room_search(message_text)
        normalized_intent = normalize_location_search(message_text)
        listing_intent = is_room_listing_request(message_text)
        if (
            not listing_intent
            and
            natural_search.get("location_search")
            and natural_search.get("max_price", 0) > 0
            and re.search(r"\b(?:xem|tim|kiem|thue|can)\s+(?:phong|phong tro)\b", normalized_intent)
        ):
            action = "SEARCH_ROOM"
        extracted = result_data.get("extracted_data", {})
        direct_price = extract_listing_price_from_text(message_text)
        if direct_price and is_missing_required_room_price(extracted.get("price")):
            extracted["price"] = direct_price
        # Không có thông tin ngày trong chính câu người dùng thì bắt buộc để trống.
        # Cách này cũng ngăn Gemini tự suy đoán ngày vào ở.
        extracted["move_in_date"] = extract_move_in_date_from_text(message_text)
        if listing_intent:
            action = "ADD_ROOM"
            extracted = apply_direct_room_listing_fallbacks(extracted, message_text)
        update_command = is_room_update_command(message_text)
        direct_room_code = extract_room_code_for_media(message_text) if update_command else None
        if direct_room_code:
            # Mã phòng trong câu người dùng là nguồn xác định chính, không phụ thuộc Gemini.
            extracted["room_code"] = direct_room_code
            action = "ADD_ROOM"
        address = str(extracted.get("address") or "").strip()
        room_name = str(extracted.get("room_name") or "").strip()
        
        print(f"landlord_phone: {extracted.get("landlord_phone")}")
        print(f"phone: {phone}")
        if action == "LIST_MY_ROOMS":
            if not phone:
                send_zalo_message(
                    user_id,
                    "⚠️ Bạn vui lòng chia sẻ SĐT Zalo trước để xem các phòng do mình đăng.",
                )
            else:
                send_my_rooms_overview(user_id, phone)
            return
        elif action == "SEARCH_ROOM":
            add_chat_history(user_id=user_id, user_message=message_text, ai_reply=None)
            search_params = dict(result_data.get("extracted_search") or {})
            if natural_search.get("location_search") and natural_search.get("max_price", 0) > 0:
                search_params.update(natural_search)
            is_valid_search = bool(
                search_params.get("location_search")
                and float(search_params.get("max_price") or 0) > 0
            )
        
            # 🚨 TRƯỜNG HỢP 1: THIẾU THÔNG TIN BẮT BUỘC
            if not is_valid_search:
                ai_reply = result_data.get(
                    "missing_info_message", 
                    "Dạ để tìm phòng chính xác nhất, bạn vui lòng cung cấp rõ:\n"
                    "1. Tên Đường hoặc Phường/Xã muốn thuê\n"
                    "2. Khoảng giá bạn muốn thuê (Ví dụ: từ 2 triệu đến 4 triệu)"
                )
                urls_to_send = []

            # ✅ TRƯỜNG HỢP 2: ĐÃ ĐỦ THÔNG TIN -> TIẾN HÀNH TÌM KIẾM
            else:
                location_search = search_params.get("location_search", "")
                min_p = float(search_params.get("min_price") or 0)
                max_p = float(search_params.get("max_price") or 0)
                print(f"min_p : {min_p}")
                print(f"max_p : {max_p}")
                
                # Tạo chuỗi truy vấn kết hợp thông tin
                full_query = f"{message_text}"
                print(f"full_query : {full_query}")
                # Gọi tìm kiếm phòng có truyền kèm khoảng giá
                search_results = search_rooms_with_filter(
                    query_text=full_query, 
                    location_search=location_search,
                    min_price=min_p, 
                    max_price=max_p, 
                    top_k=MAX_SEARCH_ROOMS
                )
                print(f"search_result : {search_results}")
                if not search_results:
                    ai_reply = f"Dạ tiếc quá, hệ thống chưa tìm thấy phòng nào ở khu vực **{location_search}** với tầm giá từ **{min_p:,.0f}đ đến {max_p:,.0f}đ** ạ!"
                    urls_to_send = []
                else:
                    # Cuối luồng sẽ gửi từng phòng kèm đúng media của phòng đó.
                    search_results_to_send = search_results
                    urls_to_send = []
                    
                # urls_to_send đã chứa media của kết quả tìm kiếm để Zalo hiển thị trực tiếp.

#        elif action == "CONFIRM_REGISTER":
#            # 💡 Người dùng chốt ĐĂNG KÝ -> Lấy thông tin từ Cache ra ghi vào DB
#            data_to_save = pending_room if pending_room else extracted
#            address = str(data_to_save.get("address", "")).strip()
#
#            if not address or address.lower() in ["null", "none", ""]:
#                ai_reply = "Dạ em chưa nhận đủ thông tin phòng. Anh/chị gửi lại thông tin phòng trọ giúp em nhé!"
#            elif not phone:
#                # Yêu cầu gửi SĐT nếu chưa xác thực
#                send_zalo_request(request_phone_message)
#                return
#            else:
#                # Thực hiện ghi vào database Qdrant
#                db_message = upsert_room_to_db(data=data_to_save, media_urls=data_to_save.get("media_urls", []), point_id=None, type_process = "NOT_EXCEL")
#                if db_message == "SUCCESS" or db_message is True:
#                    ai_reply = f"🎉 **ĐĂNG KÝ PHÒNG THÀNH CÔNG!**\n\nPhòng trọ tại địa chỉ **{address}** đã được lưu lên hệ thống."
#                    clear_pending_room(user_id)  # Xóa cache tạm
#                    get_get_and_clear_pending_media(user_id)  # Xóa media tạm
#                else:
#                    ai_reply = f"❌ Không thể lưu thông tin phòng: {db_message}"
#
        elif action == "ADD_ROOM":

            valid_address = address and address.lower() not in ["null", "none", "chưa rõ", ""]
            # Phòng mới cần địa chỉ; cập nhật phòng cũ có thể chỉ cần mã phòng.
            if valid_address or (update_command and direct_room_code):
                #nếu người dùng chưa đăng ký phòng trên zalo hay web thì sẽ tạo mới data cho user
          
                if not phone:
                    save_pending_room(user_id, extracted)
                    request_image_url = get_zalo_request_image_url(public_base_url)
                    # 🚨 BẮT BUỘC: Nếu chưa có SĐT -> Chặn lại và yêu cầu chia sẻ SĐT
                    request_phone_message = {
                            "recipient": {"user_id": user_id},
                            "message": {
                                "text": "⚠️ Để đăng bài cho thuê phòng, bạn vui lòng bấm nút bên dưới để chia sẻ Số điện thoại liên hệ nhé!",
                                "attachment": {
                                    "type": "template",
                                    "payload": {
                                        "template_type": "request_user_info",
                                        "elements": [{
                                            "title": "Xác thực Số điện thoại",
                                            "subtitle": "Yêu cầu cung cấp SĐT chính chủ trên Zalo để tạo tài khoản đăng phòng.",
                                            **({"image_url": request_image_url} if request_image_url else {})
                                        }]
                                    }
                                }
                            }
                        }
                    # Gọi Zalo Open API gửi yêu cầu xin SĐT
                    if request_image_url:
                        send_zalo_request(request_phone_message)
                    else:
                        send_zalo_message(user_id, "⚠️ Chưa thể mở nút chia sẻ SĐT vì SERVER_DOMAIN chưa được cấu hình bằng URL HTTPS công khai. Vui lòng liên hệ quản trị viên.")
                    return {"status": "phone_required"}

                existing_point_id = find_existing_room_id(
                    address=address,
                    room_name=extracted.get("room_name"),
                    landlord_phone=phone,
                    room_code=direct_room_code or extracted.get("room_code"),
                )
                if update_command and not existing_point_id:
                    ai_reply = (
                        "❌ Không tìm thấy đúng phòng thuộc SĐT của bạn để cập nhật.\n\n"
                        "Bạn hãy kiểm tra lại địa chỉ hoặc gửi theo cú pháp: "
                        "Cập nhật phòng <MÃ PHÒNG> <thông tin cần sửa>.\n\n"
                        f"{build_owned_room_reference(phone)}"
                    )
                    urls_to_send = []
                else:
                    data_to_save = (
                        keep_only_explicit_room_updates(extracted, message_text)
                        if update_command else extracted
                    )
                    # Khi người dùng đang bổ sung trường còn thiếu, ghép với dữ
                    # liệu phòng đã lưu tạm để họ không phải nhập lại từ đầu.
                    if not update_command and pending_room:
                        data_to_save = {
                            **pending_room,
                            **{
                                key: value for key, value in data_to_save.items()
                                if has_room_update_value(value)
                            },
                        }
                    message = upsert_room_to_db(
                        data=data_to_save,
                        media_urls=all_current_media,
                        point_id=existing_point_id,
                        type_process="NOT_EXCEL",
                        landlord_phone=phone,
                        bypass_posting_block=str(user_id) == Config.ZALO_ADMIN_ID,
                    )
                    if message == "SUCCESS":
                        get_get_and_clear_pending_media(user_id)
                        clear_pending_room(user_id)
                        if existing_point_id:
                            room_code = str(direct_room_code or extracted.get("room_code") or "").strip().upper()
                            code_text = f" (mã {room_code})" if room_code else ""
                            ai_reply = f"✅ Đã cập nhật thông tin phòng{code_text} thành công."
                        else:
                            ai_reply = "✅ Bạn đã đăng ký phòng thành công."
                    elif message == ROOM_PRICE_REQUIRED:
                        save_pending_room(user_id, data_to_save)
                        urls_to_send = []
                        ai_reply = (
                            "⚠️ Phòng chưa được đăng vì bạn chưa cung cấp giá thuê.\n\n"
                            "Bạn vui lòng nhập giá, ví dụ:\n"
                            "• Giá 4,5 triệu/tháng\n"
                            "• Giá 3.800.000 đồng/tháng\n\n"
                            "Thông tin phòng đã được lưu tạm, bạn không cần nhập lại từ đầu."
                        )
                    else:
                        ai_reply = message
            else:
                ai_reply = "❌ Vui lòng cung cấp địa chỉ phòng mới hoặc mã phòng cần cập nhật."
                urls_to_send = []
                
        elif action == "UPDATE_STATUS":
            existing_point_id = find_existing_room_id(address=address, room_name=room_name, landlord_phone=phone)
            if existing_point_id:
                new_status = extracted.get("status") or "ĐÃ CHO THUÊ"
                update_room_status_in_db(point_id=existing_point_id, new_status=new_status)

    except GeminiOutputError:
        ai_reply = (
            "⚠️ Tôi chưa đọc được đầy đủ yêu cầu. Bạn vui lòng gửi lại ngắn gọn, ví dụ:\n"
            "• Tìm phòng Phan Thị Hành giá 5 triệu\n"
            "• Cho thuê phòng ở 15 Phan Thị Hành, 20m2, giá 5 triệu"
        )
        urls_to_send = []
    except Exception as err:
        report_error("Xử lý AI Zalo thất bại", err, "process_zalo_ai_logic")
        ai_reply = "Dạ hệ thống đang bận một chút, anh/chị chờ em vài giây rồi nhắn lại giúp em nhé!"
    if search_results_to_send:
        send_zalo_search_results(user_id, search_results_to_send)
    else:
        send_zalo_message(user_id, ai_reply, media_urls=urls_to_send)
    
    
# --- HÀM TẠO MÃ PHÓNG RANDOM 6 KÝ TỰ ---
def generate_unique_room_code() -> str:
    """Sinh ngẫu nhiên mã phòng 6 ký tự gồm CẢ CHỮ VÀ SỐ, đảm bảo duy nhất tuyệt đối trong DB."""
    letters = string.ascii_uppercase
    digits = string.digits
    all_chars = letters + digits

    while True:
        # 1. Bắt buộc có ít nhất 1 chữ và 1 số để không bao giờ ra thuần chữ/thuần số
        code_chars = [random.choice(letters), random.choice(digits)]
        
        # 2. Lấy 4 ký tự ngẫu nhiên còn lại
        code_chars.extend(random.choices(all_chars, k=4))
        
        # 3. Trộn ngẫu nhiên thứ tự các ký tự
        random.shuffle(code_chars)
        code = ''.join(code_chars)

        # 4. Kiểm tra xem mã đã tồn tại trên Qdrant chưa
        try:
            records, _ = qdrant_client.scroll(
                collection_name=COLLECTION_NAME,
                scroll_filter=qdrant_models.Filter(
                    must=[qdrant_models.FieldCondition(key="room_code", match=qdrant_models.MatchValue(value=code))]
                ),
                limit=1
            )
            # Nếu không tìm thấy bản ghi nào trùng trùng -> Mã an toàn!
            if not records:
                return code

        except Exception as e:
            # 💡 Xử lý lỗi kết nối DB: Không return ngay mà log lỗi và thử lại sau 0.5s
            report_error("Kiểm tra room_code trên Qdrant thất bại", e, "generate_unique_room_code")
            return ""

def process_room_booking(tenant_zalo_id: str, room_code: str, raw_message: str, db: Session) -> str:
    """Xử lý xác nhận đặt lịch xem phòng"""
    room_code = room_code.upper().strip()
    
    # 1. Tìm thông tin phòng theo room_code trên Qdrant
    try:
        records, _ = qdrant_client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=qdrant_models.Filter(
                must=[qdrant_models.FieldCondition(key="room_code", match=qdrant_models.MatchValue(value=room_code))]
            ),
            limit=1
        )
    except Exception as e:
        report_error("Truy vấn Qdrant khi đặt phòng thất bại", e, "process_room_booking")
        return "Dạ hệ thống đang gặp sự cố, vui lòng thử lại sau ít phút!"

    if not records:
        return f"❌ Không tìm thấy phòng trọ nào có mã phòng **{room_code}**. Vui lòng kiểm tra lại mã!"


    room_data = records[0].payload
    access_block = get_room_access_block(room_data)
    if str(tenant_zalo_id) != Config.ZALO_ADMIN_ID and access_block:
        return format_room_access_block_message(access_block)
    landlord_phone = room_data.get("landlord_phone", "")
    landlord_zalo_id = get_user_id_by_phone(db, room_data.get("landlord_phone", "")) # lấy id chủ nhà
    room_address = room_data.get("address", "Chưa rõ")
    room_name = room_data.get("room_name", "Phòng trọ")

    # 2. Tìm SĐT của người thuê trong DB
    tenant_phone = get_phone_by_user_id(db, str(tenant_zalo_id)) or "Chưa xác thực SĐT"  # lấy phone ng thuê
    
    already_booked = is_phone_already_ordered(db, tenant_phone=tenant_phone, room_code=room_code)
    # Option B (nếu muốn chặn đặt BẤT KỲ phòng nào):
    # already_booked = is_phone_already_ordered(db, tenant_phone=tenant_phone)

    if already_booked:
        print(f"⚠️ SĐT {tenant_phone} đã đăng ký đặt lịch phòng {room_code} trước đó!")
        
        return f"⚠️ Số điện thoại {tenant_phone} của bạn đã đăng ký đặt lịch xem phòng ({room_code}) trước đó rồi ạ.\n Bên mình sẽ liên hệ lại sớm nhất!"

    # 3. Lưu thông tin đơn vào bảng order_room với Try-Catch kiểm tra
    try:

        # Bóc tách thời gian
        viewing_time_parsed = extract_viewing_time(raw_message)
        
        new_order = OrderRoom(
            id=str(uuid.uuid4()),
            tenant_zalo_id=str(tenant_zalo_id),
            tenant_phone=tenant_phone,
            landlord_zalo_id=landlord_zalo_id,
            landlord_phone=landlord_phone,
            room_code=room_code,
            viewing_time=viewing_time_parsed,  # 👈 Thêm vào đây
            created_at=vietnam_now()
        )
        db.add(new_order)
        db.commit()
        db.refresh(new_order)
        print(f"✅ Đã lưu đơn đặt phòng thành công: Order ID {new_order.id}")
    except Exception as e:
        db.rollback()
        report_error("Ghi order_room thất bại", e, "process_room_booking")
        return "Dạ hệ thống không thể khởi tạo đơn đặt phòng, vui lòng thử lại sau!"
    
    print(f"landlord_zalo_id: {landlord_zalo_id}")
    # 4. Gửi thông báo tới Zalo của Chủ nhà (nếu có landlord_zalo_id)
    if  landlord_zalo_id and landlord_zalo_id not in ["SYSTEM", "ADMIN_WEB", "None"]:
        msg_to_landlord = (
            f"🔔 **CÓ LỊCH HẸN XEM PHÒNG MỚI!**\n\n"
            f"📍 Mã phòng: {room_code} ({room_name} - {room_address})\n"
            f"👤 Zalo ID người thuê: {tenant_zalo_id}\n"
            f"📞 SĐT người thuê: {tenant_phone}\n"
            f"⏰ Thời gian đặt: {datetime.now(VN_TZ).strftime('%H:%M %d/%m/%Y')}\n\n"
            f"👉 Vui lòng liên hệ lại với khách hàng để chốt lịch hẹn chi tiết!"
        )
        send_zalo_message(user_id=str(landlord_zalo_id), ai_reply=msg_to_landlord)

    return (
        f"✅ **ĐẶT LỊCH XEM PHÒNG THÀNH CÔNG!**\n\n"
        f"🏠 Phòng: {room_name} (Mã: {room_code})\n"
        f"📍 Địa chỉ: {room_address}\n"
        f"📞 SĐT chủ nhà: {landlord_phone}\n\n"
        f"Yêu cầu của bạn đã được gửi trực tiếp tới Chủ nhà. Chủ nhà sẽ chủ động liên hệ lại sớm nhất ạ!"
    )
    

def get_phone_by_user_id(db: Session, user_id: str) -> Optional[str]:
    if not user_id:
        return None
    try:
        # Tìm theo user_id (Zalo ID / System ID) thay vì PK `id`
        user = db.query(UserWeb).filter(UserWeb.user_id == str(user_id)).first()
        if user and user.phone:
            return user.phone.strip()
        return None
    except Exception as e:
        report_error("Lấy số điện thoại người dùng thất bại", e, f"get_phone_by_user_id:{user_id}")
        return None

def get_user_id_by_phone(db: Session, phone: str) -> Optional[str]:
    if not phone:
        return None
    try:
        # Tìm theo phone (Zalo ID / System ID) thay vì PK `id`
        user = db.query(UserWeb).filter(UserWeb.phone == str(phone)).first()
        if user and user.user_id:
            return user.user_id.strip()
        return None
    except Exception as e:
        report_error("Lấy Zalo user ID theo số điện thoại thất bại", e, "get_user_id_by_phone")
        return None

def save_or_update_user_web(
    db: Session, 
    zalo_user_id: str, 
    phone: str
) -> UserWeb:
    """
    Tạo mới hoặc Cập nhật người dùng Zalo vào bảng user_web bằng SQLAlchemy.
    
    Args:
        db (Session): SQLAlchemy Database Session
        zalo_user_id (str): ID người dùng trên Zalo
        phone (str): Số điện thoại thu thập từ Zalo
        
    Returns:
        UserWeb: Đối tượng người dùng đã lưu
    """
    if not zalo_user_id or not phone:
        raise ValueError("Lỗi: zalo_user_id và phone là bắt buộc!")

    clean_phone = str(phone).strip()
    clean_zalo_id = str(zalo_user_id).strip()

    try:
        # 1. Tim kiem user theo zalo_user_id
        user = db.query(UserWeb).filter(UserWeb.user_id == clean_zalo_id).first()

        if user:
            # 2A. Nếu tìm thấy -> Cập nhật thông tin
            user.phone = clean_phone
            user.updated_at = vietnam_now()
            print(f"🔄 Đã cập nhật User Web (Zalo ID: {clean_zalo_id}) -> SĐT: {clean_phone}")
        else:
            # 2B. Nếu chưa có -> Tạo mới bản ghi user_web
            user = UserWeb(
                id=clean_zalo_id,  # Dùng luôn Zalo ID làm PK hoặc dùng str(uuid.uuid4())
                user_id=clean_zalo_id,
                phone=clean_phone
            )
            db.add(user)
            print(f"✨ Đã tạo mới User Web (Zalo ID: {clean_zalo_id}) -> SĐT: {clean_phone}")

        # 3. Commit thay đổi vào SQL Database
        db.commit()
        db.refresh(user)
        return user

    except Exception as e:
        db.rollback()  # Hoàn tác nếu có lỗi SQL
        report_error("Lưu hoặc cập nhật user_web thất bại", e, "save_or_update_user_web")
        raise e
        
        
def send_zalo_request(payload: dict) -> bool:
    """Gửi payload tùy chỉnh tới Zalo OA bằng token trong DB và tự retry lỗi -216."""
    recipient_id = str((payload or {}).get("recipient", {}).get("user_id") or "").strip()
    if recipient_id in ["", "ADMIN_WEB", "SYSTEM", "None"]:
        print("⚠️ Zalo User ID không hợp lệ, không thể gửi request.")
        return False
    if not isinstance(payload.get("message"), dict):
        print("⚠️ Payload Zalo thiếu message hợp lệ.")
        return False

    db = SessionLocal()
    try:
        token_data = get_current_tokens_from_db(db)
    finally:
        db.close()
    access_token = str((token_data or {}).get("access_token") or "")
    if not access_token:
        report_error("Thiếu Zalo access token", context="send_zalo_request", notify=False)
        return False

    try:
        url = "https://openapi.zalo.me/v3.0/oa/message/cs"
        result, _ = _post_zalo_with_token_retry(url, payload, access_token)
        if result.get("error") == 0:
            print(f"✅ Đã gửi Zalo request thành công tới: {recipient_id}")
            return True
        report_error(
            f"Zalo request trả lỗi {result.get('error')}: {result.get('message')}",
            context="send_zalo_request",
            notify=False,
        )
        return False
    except Exception as exc:
        report_error("Gửi Zalo request thất bại", exc, "send_zalo_request", notify=False)
        return False
        
def is_phone_already_ordered(db: Session, tenant_phone: str, room_code: str = None) -> bool:
    """
    Kiểm tra xem Số điện thoại đã từng đăng ký đặt lịch xem phòng chưa.
    - Nếu truyền room_code: Check xem đã đặt ĐÚNG phòng đó chưa.
    - Nếu không truyền room_code: Check xem đã đặt BẤT KỲ phòng nào chưa.
    """
    query = db.query(OrderRoom).filter(OrderRoom.tenant_phone == tenant_phone)
    
    if room_code:
        query = query.filter(OrderRoom.room_code == room_code)
        
    existing_order = query.first()
    return existing_order is not None
    

def extract_viewing_time(text: str):
    """
    Trích xuất Ngày và Giờ xem phòng từ tin nhắn Zalo của khách hàng.
    Trả về đối tượng datetime hoặc None nếu không tìm thấy.
    """
    now = vietnam_now()
    text_lower = text.lower()
    
    extracted_date = None
    extracted_hour = 9  # Mặc định 9h sáng nếu khách không nói giờ
    extracted_minute = 0

    # 1. Tim GIỜ (Ví dụ: 15h30, 15h, 9:30, 14:00)
    time_match = re.search(r'(\d{1,2})[h:](\d{1,2})?|\blúc\s*(\d{1,2})\b', text_lower)
    if time_match:
        if time_match.group(1):
            extracted_hour = int(time_match.group(1))
            extracted_minute = int(time_match.group(2)) if time_match.group(2) else 0
        elif time_match.group(3):
            extracted_hour = int(time_match.group(3))

    # 2. Tìm NGÀY
    # Trường hợp A: Gõ ngày cụ thể (Ví dụ: 18/08, 18-08-2026, 18/8)
    date_match = re.search(r'(\d{1,2})[\/-](\d{1,2})(?:[\/-](\d{2,4}))?', text_lower)
    
    if date_match:
        day = int(date_match.group(1))
        month = int(date_match.group(2))
        year = int(date_match.group(3)) if date_match.group(3) else now.year
        if year < 100: 
            year += 2000
        try:
            extracted_date = datetime(year, month, day, extracted_hour, extracted_minute)
        except ValueError:
            extracted_date = None

    # Trường hợp B: Dùng từ tương đối ("hôm nay", "ngày mai", "ngày kia")
    elif "mai" in text_lower:
        tomorrow = now + timedelta(days=1)
        extracted_date = datetime(tomorrow.year, tomorrow.month, tomorrow.day, extracted_hour, extracted_minute)
    elif "kia" in text_lower:
        day_after = now + timedelta(days=2)
        extracted_date = datetime(day_after.year, day_after.month, day_after.day, extracted_hour, extracted_minute)
    elif "hôm nay" in text_lower or "nay" in text_lower:
        extracted_date = datetime(now.year, now.month, now.day, extracted_hour, extracted_minute)

    return extracted_date
 
        
def split_text_by_limit(text: str, max_length: int = 1800) -> List[str]:
    # Kiểm tra nếu text là None hoặc rỗng
    if not text:
        return []
    """Cắt nhỏ văn bản dưới max_length ký tự, ưu tiên cắt tại vị trí xuống dòng"""
    if len(text) <= max_length:
        return [text]
        
    chunks = []
    while len(text) > max_length:
        # Tìm vị trí xuống dòng gần nhất trong phạm vi max_length
        split_idx = text.rfind("\n", 0, max_length)
        
        # Nếu không có dấu xuống dòng, tìm khoảng trắng gần nhất
        if split_idx == -1:
            split_idx = text.rfind(" ", 0, max_length)
            
        # Nếu vẫn không có, cắt cứng tại max_length
        if split_idx == -1:
            split_idx = max_length

        chunks.append(text[:split_idx].strip())
        text = text[split_idx:].strip()

    if text:
        chunks.append(text)
    return chunks
    
def parse_price_safe(room: dict) -> float:
    """Hàm trích xuất và ép kiểu giá về dạng float an toàn tuyệt đối"""
    val = room.get("price")
    
    # 1. Nếu đã là số int/float
    if isinstance(val, (int, float)):
        return float(val)
    
    # 2. Nếu là chuỗi số "3500000"
    if isinstance(val, str) and val.isdigit():
        return float(val)
        
    # 3. Fallback: Parse từ chuỗi "price" mô tả (VD: "3.5 triệu", "3,500,000")
    p_str = str(room.get("price", "999999999")).lower().replace(",", ".")
    try:
        match = re.search(r"[\d.]+", p_str)
        if match:
            num = float(match.group())
            if "triệu" in p_str or "tr" in p_str:
                num *= 1_000_000
            elif "k" in p_str:
                num *= 1_000
            return num
    except Exception:
        pass
        
    return 999_999_999.0


def normalize_location_search(value: str) -> str:
    """Chuẩn hóa dấu tiếng Việt, dấu câu và khoảng trắng để so khớp địa chỉ."""
    text_value = str(value or "").strip().lower().replace("đ", "d")
    text_value = "".join(
        char for char in unicodedata.normalize("NFD", text_value)
        if unicodedata.category(char) != "Mn"
    )
    text_value = re.sub(r"\b(?:tp\.?\s*hcm|tphcm|sai\s*gon)\b", "ho chi minh", text_value)
    text_value = re.sub(r"[^a-z0-9]+", " ", text_value)
    return re.sub(r"\s+", " ", text_value).strip()


def get_location_match_level(address: str, location_search: str) -> int:
    """3: khớp đầy đủ; 2: đủ từ khóa; 1: khớp tên đường; 0: không khớp."""
    normalized_address = normalize_location_search(address)
    normalized_query = normalize_location_search(location_search)
    if not normalized_address or not normalized_query:
        return 0
    if normalized_query in normalized_address:
        return 3

    ignored_tokens = {"duong", "phuong", "quan", "huyen", "thi", "xa", "thanh", "pho", "tp"}
    query_tokens = [token for token in normalized_query.split() if token not in ignored_tokens]
    address_tokens = set(normalized_address.split())
    if query_tokens and all(token in address_tokens for token in query_tokens):
        return 2

    first_component = normalize_location_search(str(location_search or "").split(",", 1)[0])
    first_component = re.sub(r"^\s*(?:duong)\s+", "", first_component).strip()
    if first_component and first_component in normalized_address:
        return 1
    return 0


def extract_natural_room_search(message_text: str) -> dict:
    """Tách địa chỉ và khoảng giá trực tiếp, không phụ thuộc kết quả Gemini."""
    raw_text = str(message_text or "").strip()
    price_pattern = re.compile(
        r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(triệu|trieu|tr|k|nghìn|nghin)\s*(\d{1,3})?(?!\d|[a-zA-Z])",
        re.IGNORECASE,
    )
    prices = []

    def parse_price_match(match) -> str:
        number = float(match.group(1).replace(",", "."))
        unit = normalize_location_search(match.group(2))
        multiplier = 1_000 if unit in {"k", "nghin"} else 1_000_000
        compact_fraction = match.group(3)
        if compact_fraction and multiplier == 1_000_000:
            number += int(compact_fraction) / (10 ** len(compact_fraction))
        prices.append(number * multiplier)
        return " "

    location_text = price_pattern.sub(parse_price_match, raw_text)
    for match in re.finditer(r"(?<!\d)(\d{6,})(?!\d)", location_text):
        prices.append(float(match.group(1)))
    location_text = re.sub(r"(?<!\d)\d{6,}(?!\d)", " ", location_text)
    location_text = re.sub(
        r"^\s*(?:cho\s+(?:tôi|toi|mình|minh)\s+)?"
        r"(?:(?:xem|tìm|tim|kiếm|kiem|cần|can|thuê|thue)\s+)*"
        r"(?:phòng\s*trọ|phong\s*tro|phòng|phong)\s*",
        "",
        location_text,
        flags=re.IGNORECASE,
    )
    location_text = re.sub(
        r"\b(?:giá|gia)\s*(?:từ|tu|đến|den|dưới|duoi|tối đa|toi da|khoảng|khoang)?\s*$",
        "",
        location_text,
        flags=re.IGNORECASE,
    )
    location_text = re.sub(r"^[\s,:;.-]+|[\s,:;.-]+$", "", location_text).strip()
    return {
        "location_search": location_text,
        "min_price": min(prices) if len(prices) > 1 else 0,
        "max_price": max(prices) if prices else 0,
    }
    
    
    
def search_rooms_with_filter(
    query_text: str, 
    location_search: str = None,
    min_price: int = 0, 
    max_price: int = 0,
    top_k: int = 20
) -> List[dict]:
    
    must_conditions = [
        qdrant_models.FieldCondition(
            key="status",
            match=qdrant_models.MatchValue(value="TRỐNG")
        )
    ]


    # 🆕 Bổ sung lọc theo khoảng Giá tối thiểu - Giá tối đa
    price_range = {}
    if min_price > 0:
        price_range["gte"] = min_price
    if max_price > 0:
        price_range["lte"] = max_price

    if price_range:
        must_conditions.append(
            qdrant_models.FieldCondition(
                key="price",
                range=qdrant_models.Range(**price_range)
            )
        )

    status_filter = qdrant_models.Filter(must=must_conditions)
    print(f"query_text : {query_text}")
    safe_top_k = min(max(int(top_k or MAX_SEARCH_ROOMS), 1), MAX_SEARCH_ROOMS)
    candidates = []
    try:
        scroll_offset = None
        while len(candidates) < SEARCH_CANDIDATE_LIMIT:
            records, next_offset = qdrant_client.scroll(
                collection_name=COLLECTION_NAME,
                scroll_filter=status_filter,
                limit=min(256, SEARCH_CANDIDATE_LIMIT - len(candidates)),
                offset=scroll_offset,
                with_payload=True,
                with_vectors=False
            )
            candidates.extend(
                record.payload | {"id": str(record.id)}
                for record in records
                if record.payload
            )
            if next_offset is None or next_offset == scroll_offset:
                break
            scroll_offset = next_offset
    except Exception as exc:
        report_error("Qdrant search filter thất bại", exc, "search_rooms_by_filter")
        return []

    if location_search:
        match_levels = [
            (get_location_match_level(room.get("address"), location_search), room)
            for room in candidates
        ]
        # Ưu tiên địa chỉ đầy đủ. Chỉ fallback tên đường nếu không có bản ghi nào
        # khớp toàn bộ, tránh câu tìm chi tiết bị trả rỗng vì khác dấu/cách viết.
        best_level = max((level for level, _ in match_levels), default=0)
        accepted_level = best_level if best_level >= 2 else 1
        rooms = [room for level, room in match_levels if level >= accepted_level]
    else:
        rooms = candidates

    # Với truy vấn "6tr", giá tối đa đã lọc ở Qdrant; ưu tiên phòng gần 6tr nhất.
    rooms.sort(key=parse_price_safe, reverse=True)
    return rooms[:safe_top_k]
        



def refresh_zalo_tokens(db):
    current_token_entry = get_current_tokens_from_db(db)
    current_refresh_token = str((current_token_entry or {}).get("refresh_token") or "").strip()
    
    app_id_clean = str(ZALO_APP_ID).strip() if ZALO_APP_ID else ""
    secret_key_clean = str(ZALO_SECRET_KEY).strip() if ZALO_SECRET_KEY else ""

    # Kiểm tra an toàn trước khi gọi API
    if not app_id_clean:
        raise ValueError("Thiếu ZALO_APP_ID trong biến môi trường.")
    if not app_id_clean.isdigit():
        raise ValueError("ZALO_APP_ID phải là App ID dạng số lấy từ ứng dụng trên Zalo Developers.")
    if not secret_key_clean:
        raise ValueError("Thiếu ZALO_SECRET_KEY trong biến môi trường.")
    if not current_refresh_token:
        raise ValueError("Không có Zalo refresh token trong DB hoặc biến môi trường.")

    oauth_url = "https://oauth.zaloapp.com/v4/oa/access_token"
    headers = {
        "secret_key": secret_key_clean,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "refresh_token": current_refresh_token,
        "app_id": app_id_clean, # Bắt buộc phải là App ID hợp lệ
        "grant_type": "refresh_token"
    }
    
    response = requests.post(oauth_url, headers=headers, data=data, timeout=15)
    try:
        res_json = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Zalo OAuth trả dữ liệu không hợp lệ (HTTP {response.status_code}).") from exc
    
    if res_json.get("access_token") and res_json.get("refresh_token"):
        new_access_token = res_json["access_token"]
        new_refresh_token = res_json["refresh_token"]
        update_tokens_in_db(db, new_access_token, new_refresh_token)
        print("🎉 [ZALO OAUTH] Tự động Refresh Token và lưu DB thành công!", flush=True)
    else:
        oauth_error = RuntimeError(
            f"Zalo OAuth Error {res_json.get('error')}: "
            f"{res_json.get('error_description') or res_json.get('error_name') or 'Không xác định'}"
        )
        report_error("Zalo OAuth không trả access token", oauth_error, "refresh_zalo_tokens")
        raise oauth_error

def get_current_tokens_from_db(db: Session) -> dict:
    """
    Lấy thông tin Zalo Token hiện tại từ Database.
    Nếu DB chưa có, fallback về biến môi trường .env.
    """
    token_record = db.query(ZaloToken).first()
    
    # 🟢 Sửa lỗi cú pháp: dùng `is None` thay vì `is none`
    if not token_record or token_record.access_token is None:
        return {
            "access_token": ZALO_ACCESS_TOKEN,
            "refresh_token": ZALO_REFRESH_TOKEN,
        }
    
    return {
        "access_token": token_record.access_token,
        "refresh_token": token_record.refresh_token,
    }



   
def update_tokens_in_db(db: Session, new_access_token: str, new_refresh_token: str):
    """
    Cập nhật Access Token và Refresh Token mới vào Database.
    """
    try:
        # 1. Lấy bản ghi token đầu tiên trong DB
        token_entry = db.query(ZaloToken).first()
        
        if token_entry:
            # Nếu đã có dữ liệu -> Cập nhật bản ghi hiện tại
            token_entry.access_token = new_access_token
            token_entry.refresh_token = new_refresh_token
        else:
            # Nếu chưa có bản ghi nào -> Tạo mới bản ghi đầu tiên
            token_entry = ZaloToken(
                access_token=new_access_token,
                refresh_token=new_refresh_token
            )
            db.add(token_entry)
            
        # 2. Lưu thay đổi vào Database
        db.commit()
        db.refresh(token_entry)
        print("💾 [DATABASE] Đã lưu thành công cặp Token mới vào DB!", flush=True)
        return token_entry

    except Exception as e:
        # Nếu có lỗi DB -> Rollback để tránh nghẽn/khóa connection pool
        db.rollback()
        report_error("Không thể lưu Zalo token vào DB", e, "update_tokens_in_db")
        raise e
        
        
def normalize_room_address(value: str) -> str:
    """Chuẩn hóa vừa đủ để so khớp địa chỉ nhưng không làm thay đổi tên riêng."""
    normalized = str(value or "").strip().lower()
    normalized = re.sub(r"[,.\-/]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_room_name(value: str) -> str:
    """Chuẩn hóa tên phòng để so khớp chính xác, không phân biệt hoa/thường."""
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def find_duplicate_room_id(address: str, room_name: str) -> Optional[str]:
    """Phòng chỉ được xem là trùng khi đồng thời trùng địa chỉ và tên phòng."""
    safe_address = str(address or "").strip()
    safe_room_name = str(room_name or "").strip()
    if not safe_address or not safe_room_name:
        return None
    try:
        records, _ = qdrant_client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="address",
                        match=qdrant_models.MatchText(text=safe_address),
                    )
                ]
            ),
            limit=100,
            with_payload=True,
            with_vectors=False,
        )
        normalized_address = normalize_room_address(safe_address)
        normalized_name = normalize_room_name(safe_room_name)
        for record in records:
            payload = record.payload or {}
            if (
                normalize_room_address(payload.get("address")) == normalized_address
                and normalize_room_name(payload.get("room_name")) == normalized_name
            ):
                return str(record.id)
        return None
    except Exception as exc:
        raise Exception(f"❌ Lỗi truy vấn Qdrant khi kiểm tra phòng trùng: {exc}")


def find_existing_room_id(
    address: str,
    room_name: str = "",
    landlord_phone: str = "",
    room_code: str = "",
) -> Optional[str]:
    """Tìm duy nhất một phòng thuộc chủ nhà bằng mã hoặc địa chỉ đã chuẩn hóa."""
    try:
        safe_address = str(address or "").strip()
        safe_room_name = str(room_name or "").strip() if has_room_update_value(room_name) else ""
        safe_phone = format_national_phone(landlord_phone)
        safe_code = str(room_code or "").strip().upper() if has_room_update_value(room_code) else ""

        if not safe_phone or (not safe_address and not safe_code):
            return None

        must_conditions = [
            qdrant_models.FieldCondition(
                key="landlord_phone",
                match=qdrant_models.MatchValue(value=safe_phone),
            )
        ]
        if safe_code:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="room_code",
                    match=qdrant_models.MatchValue(value=safe_code),
                )
            )
        elif safe_address:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="address",
                    match=qdrant_models.MatchText(text=safe_address),
                )
            )

        records, _ = qdrant_client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=qdrant_models.Filter(must=must_conditions),
            limit=50,
            with_payload=True,
            with_vectors=False,
        )

        if safe_code:
            exact_code_records = [
                record for record in records
                if str((record.payload or {}).get("room_code") or "").strip().upper() == safe_code
            ]
            return str(exact_code_records[0].id) if len(exact_code_records) == 1 else None

        normalized_address = normalize_room_address(safe_address)
        exact_address_records = [
            record for record in records
            if normalize_room_address((record.payload or {}).get("address")) == normalized_address
        ]
        if safe_room_name:
            normalized_name = normalize_room_name(safe_room_name)
            named_records = [
                record for record in exact_address_records
                if normalize_room_name((record.payload or {}).get("room_name")) == normalized_name
            ]
            if len(named_records) == 1:
                return str(named_records[0].id)
            return None
        if len(exact_address_records) == 1:
            return str(exact_address_records[0].id)

        return None
    except Exception as e:
        raise Exception(f"❌ Lỗi truy vấn Qdrant khi tìm phòng trùng: {e}")
    
# Hàm trợ lý nhỏ để đổi True/False thành văn bản dễ hiểu cho Model Embedding
def bool_to_text(val, true_str, false_str=""):
    if isinstance(val, bool):
        return true_str if val else false_str
    if str(val).lower() in ["true", "1", "có", "yes"]:
        return true_str
    return false_str
    
    
MAX_HISTORY_MESSAGES = 8  # Giữ tối đa 4 cặp gần nhất để giảm token đầu vào Gemini
CHAT_HISTORY_TTL = 7200    # Hết hạn sau 1 giờ không tương tác

def get_chat_history(user_id: str) -> list:
    """Lấy lịch sử hội thoại của user từ Redis"""
    cache_key = f"chat_history:{user_id}"
    data = redis_client.get(cache_key)
    if data:
        try:
            return json.loads(data)
        except Exception:
            return []
    return []

def add_chat_history(user_id: str, user_message: str, ai_reply: str):
    """Lưu tin nhắn người dùng và phản hồi AI vào Redis"""
    history = get_chat_history(user_id)
    
    if user_message:
        history.append({"role": "user", "text": user_message})
    if ai_reply:
        history.append({"role": "model", "text": ai_reply})
        
    # Giữ lại số lượng tin nhắn gần nhất
    history = history[-MAX_HISTORY_MESSAGES:]
    
    cache_key = f"chat_history:{user_id}"
    redis_client.set(cache_key, json.dumps(history, ensure_ascii=False), ex=CHAT_HISTORY_TTL)
    
    
def clear_chat_history(user_id: str):
    """Xóa toàn bộ lịch sử hội thoại của user trong Redis"""
    cache_key = f"chat_history:{user_id}"
    redis_client.delete(cache_key)
    
def clean_json_string(text: str) -> str:
    """Loại bỏ các ký tự markdown block và khoảng trắng thừa từ phản hồi của AI."""
    if not text:
        return ""
    # Xóa ```json và ``` ở đầu/cuối chuỗi
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()
    
    
# --- QUẢN LÝ TRẠNG THÁI CHỜ XÁC NHẬN ĐĂNG PHÒNG ---
CONFIRM_CACHE_TTL = 1800  # Lưu phòng chờ xác nhận trong 30 phút

def save_pending_room(user_id: str, room_data: dict):
    """Lưu dữ liệu phòng đang chờ xác nhận vào Redis"""
    cache_key = f"pending_room:{user_id}"
    redis_client.set(cache_key, json.dumps(room_data, ensure_ascii=False), ex=CONFIRM_CACHE_TTL)

def get_pending_room(user_id: str) -> Optional[dict]:
    """Lấy dữ liệu phòng đang chờ xác nhận"""
    cache_key = f"pending_room:{user_id}"
    data = redis_client.get(cache_key)
    return json.loads(data) if data else None

def clear_pending_room(user_id: str):
    """Xóa dữ liệu phòng tạm sau khi đã đăng ký xong"""
    cache_key = f"pending_room:{user_id}"
    redis_client.delete(cache_key)
