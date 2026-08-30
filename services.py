import os
import json
import requests
import uuid
import time
import re
import random
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
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import redis
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
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
ZALO_ACCESS_TOKEN = os.environ.get("ZALO_ACCESS_TOKEN")
ZALO_SECRET_KEY = os.environ.get("ZALO_SECRET_KEY")
ZALO_REFRESH_TOKEN = os.environ.get("ZALO_REFRESH_TOKEN")
ZALO_TEMPLATE_ID = os.environ.get("YOUR_ZNS_TEMPLATE_ID")
SQLALCHEMY_DATABASE_URL = os.environ.get("SQLALCHEMY_DATABASE_URL")
MEDIA_DIR = "static/media"
os.makedirs(MEDIA_DIR, exist_ok=True)

VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")
COLLECTION_NAME = "rooms_v01"
VECTOR_SIZE = 768

# Database Config
engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()




# External Clients
qdrant_client = QdrantClient(
    url=os.environ.get("QDRANT_URL"),
    api_key=os.environ.get("QDRANT_API_KEY")
)

CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL")
if CLOUDINARY_URL:
    cloudinary.config(cloudinary_url=CLOUDINARY_URL)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Bộ nhớ đệm tạm thời
PENDING_MEDIA_CACHE: Dict[str, dict] = {}
CACHE_TTL_SECONDS = 600

# Initializing Qdrant Collection & Index
try:
    if not qdrant_client.collection_exists(collection_name=COLLECTION_NAME):
        qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
        )
except Exception as e:
    print("❌ Lỗi khởi tạo Qdrant:", e)

try:
    qdrant_client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="address",
        field_schema=qdrant_models.PayloadSchemaType.KEYWORD,
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
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    created_at = Column(DateTime, default=datetime.utcnow)

class ZaloToken(Base):
    __tablename__ = "zalo_tokens"

    id = Column(Integer, primary_key=True, index=True)
    access_token = Column(String, nullable=False)
    refresh_token = Column(String, nullable=False)

Base.metadata.create_all(bind=engine)

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
    allow_pets: Optional[str] = "Chưa rõ"             
    has_balcony: Optional[str] = "Chưa rõ"            
    has_window: Optional[str] = "Chưa rõ"             
    has_fingerprint_lock: Optional[str] = "Chưa rõ"   
    parking_info: Optional[str] = "Chưa rõ"          
    max_occupants: Optional[str] = "Chưa rõ"         
    other_amenities: Optional[str] = "Chưa rõ"       
    service_fees: Optional[str] = "Chưa rõ"           
    media_urls: Optional[List[str]] = []
    move_in_date: Optional[str] = "Vào ở ngay"
    status: Optional[str] = "TRỐNG"
    landlord_phone: Optional[str] = Field(default=None, description="Số điện thoại người đăng / chủ nhà")


    @validator('address', 'price', 'room_code', pre=True)
    def check_not_empty_or_null(cls, value):
        """Validate đảm bảo không nhận giá trị Null/None hoặc chuỗi toàn khoảng trắng."""
        if value is None:
            raise ValueError("Trường này không được để null!")
        
        val_str = str(value).strip()
        if not val_str or val_str.lower() in ["none", "null", "undefined"]:
            raise ValueError("Giá trị không được để trống hoặc invalid!")
            
        return val_str


def cron_refresh_zalo_job():
    print("🔄 [REFRESH TOKEN ZALO] Bắt đầu tự động...", flush=True) # Thêm flush=True
    db = SessionLocal()
    try:
        refresh_zalo_tokens(db)
        print("✅ [REFRESH TOKEN ZALO] Thành công!", flush=True)
    except Exception as e:
        print(f"❌ [REFRESH TOKEN ZALO ERROR]: {e}", flush=True)
    finally:
        db.close()
    
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

def parse_move_in_date(date_str: str) -> float:
    now_vn = datetime.now(VN_TZ)
    if not date_str or str(date_str).strip().lower() in ["vào ở ngay", "ngay", "chưa rõ", "none", "nan", ""]:
        return now_vn.timestamp()
    text = str(date_str).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(text, fmt)
            dt_vn = VN_TZ.localize(dt)
            return dt_vn.timestamp()
        except ValueError:
            pass
    return now_vn.timestamp()

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
def get_get_and_clear_pending_media(user_id: str) -> list:
    cache_key = f"pending_media:{user_id}"
    data = redis_client.get(cache_key)
    if data:
        redis_client.delete(cache_key)  # Xóa sau khi lấy thành công
        return json.loads(data)
    return []

# 1. Lưu Cache với thời hạn tự xóa (ex = CACHE_TTL_SECONDS)
def add_pending_media(user_id: str, new_urls: list):
    cache_key = f"pending_media:{user_id}"
    redis_client.set(cache_key, json.dumps(new_urls), ex=CACHE_TTL_SECONDS)

def send_zalo_message(user_id: str, ai_reply: str, media_urls: list = None) -> bool:
    db = SessionLocal()
    data_token =  get_current_tokens_from_db(db)
    if not data_token:
        print("❌ [ZALO ERROR]: Thiếu ZALO_ACCESS_TOKEN")
        return False
        
    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    headers = {"Content-Type": "application/json", "access_token": data_token["access_token"]}

    # 1. Chia nhỏ ai_reply thành các đoạn dưới 1800 ký tự
    text_chunks = split_text_by_limit(ai_reply, max_length=1800)

    try:
        for idx, chunk in enumerate(text_chunks):
            # Với tin nhắn cuối cùng: nếu có media_urls thì chèn kèm hình ảnh
            if idx == len(text_chunks) - 1 and media_urls and len(media_urls) > 0:
                payload = {
                    "recipient": {"user_id": user_id},
                    "message": {
                        "text": chunk,
                        "attachment": {
                            "type": "template",
                            "payload": {
                                "template_type": "media",
                                "elements": [
                                    {
                                        "media_type": "image",
                                        "url": media_urls[0]
                                    }
                                ]
                            }
                        }
                    }
                }
            else:
                payload = {
                    "recipient": {"user_id": user_id},
                    "message": {"text": chunk}
                }

            response = requests.post(url, headers=headers, json=payload, timeout=10)
            res_data = response.json()
            print(f"📩 [ZALO RES Part {idx+1}/{len(text_chunks)}]: {res_data}")

            if res_data.get("error") != 0:
                print(f"❌ [ZALO FAIL]: Code {res_data.get('error')} - {res_data.get('message')}")
                return False
            
            # Tạm dừng 0.3s để tránh bị rate limit khi gửi liên tiếp nhiều tin
            if len(text_chunks) > 1:
                time.sleep(0.3)

        return True

    except Exception as e:
        print("❌ [ZALO REQUEST EXCEPTION]:", e)
        return False

def save_media_file(zalo_media_url: str, is_video: bool = False) -> str:
    if not zalo_media_url:
        return ""
    resource_type = "video" if is_video else "image"
    if CLOUDINARY_URL:
        try:
            res = cloudinary.uploader.upload(zalo_media_url, resource_type=resource_type, folder="zalo_room_media")
            url = res.get("secure_url", "")
            if url:
                return url
        except Exception as e:
            print(f"❌ [Cloudinary Error]: {e}, chuyển sang lưu local...")

    try:
        response = requests.get(zalo_media_url, timeout=15, stream=True)
        if response.status_code == 200:
            ext = ".mp4" if is_video else ".jpg"
            filename = f"{uuid.uuid4().hex}{ext}"
            filepath = os.path.join(MEDIA_DIR, filename)
            with open(filepath, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            server_domain = os.environ.get("SERVER_DOMAIN", "http://localhost:8000").rstrip("/")
            return f"{server_domain}/static/media/{filename}"
    except Exception as e:
        print(f"❌ [Local Save Error]: {e}")
    return zalo_media_url

def get_text_embedding(text: str, retries: int = 3) -> List[float]:
    if not text or not text.strip():
        return []
    if gemini_client:
        for _ in range(retries):
            try:
                response = gemini_client.models.embed_content(
                    model="models/gemini-embedding-001",
                    contents=text,
                    config=types.EmbedContentConfig(output_dimensionality=768)
                )
                if response and response.embedding and response.embedding.values:
                    return response.embedding.values
            except Exception:
                time.sleep(1)
    if GEMINI_API_KEY:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={GEMINI_API_KEY}"
        headers = {"Content-Type": "application/json"}
        payload = {"model": "models/gemini-embedding-001", "content": {"parts": [{"text": text}]}, "outputDimensionality": 768}
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=10).json()
            if "embedding" in res and "values" in res["embedding"]:
                return res["embedding"]["values"]
        except Exception as e:
            print("❌ [REST FALLBACK ERROR]:", e)
    return []

def generate_content_with_retry(prompt: str, mime_type: str = "application/json", retries: int = 3) -> str:
    if not gemini_client:
        return ""
    models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    for model_name in models_to_try:
        for attempt in range(retries):
            try:
                config = types.GenerateContentConfig()
                if mime_type:
                    config.response_mime_type = mime_type
                response = gemini_client.models.generate_content(
                    model=model_name, contents=prompt, config=config
                )
                if response and response.text:
                    return response.text.strip()
            except Exception as e:
                if "503" in str(e) or "429" in str(e):
                    time.sleep(2 ** attempt)
                else:
                    break
    return ""

# --- QDRANT VECTOR & ROOM SERVICES ---
def upsert_room_to_db(data: dict, point_id: str = None, media_urls: Optional[List[str]] = None, current_excel_row: int = 0) -> Optional[str]:
    try:
        address = str(data.get("address", "")).strip()
        room_name = str(data.get("room_name", "Phòng trọ")).strip()
        phone = str(data.get("landlord_phone", "")).strip()
        
        # Nếu tìm thấy thì bản ghi thì k cập nhật mà bỏ qua
        existing_id = find_existing_room_id(address=address, room_name=room_name)

        if existing_id:
            point_id = existing_id
            return f"❌ Đăng ký thành công đến dòng {current_excel_row - 1}. Lỗi từ dòng {current_excel_row}: Phòng đã được bạn hoặc người dùng khác đăng ký."
        
        if not address:
            return f"❌ Đăng ký thành công đến dòng {current_excel_row - 1}. Lỗi từ dòng {current_excel_row}: Địa chỉ thiếu hoặc địa chỉ không đúng."

        media_list = media_urls if media_urls is not None else data.get("media_urls", [])
        if isinstance(media_list, str):
            media_list = [x.strip() for x in media_list.split(",") if x.strip()]

        # 1. Thu thập và chuẩn hóa dữ liệu tiện ích (Booleans)
        amenities_list = [
            bool_to_text(data.get("is_private_bathroom"), "Vệ sinh khép kín / VS riêng", "Vệ sinh chung"),
            bool_to_text(data.get("has_ac"), "Có điều hòa"),
            bool_to_text(data.get("has_heater"), "Có nóng lạnh"),
            bool_to_text(data.get("has_washer"), "Có máy giặt"),
            bool_to_text(data.get("allow_pets"), "Cho phép nuôi thú cưng / pet"),
            bool_to_text(data.get("has_balcony"), "Có ban công thoáng mát"),
            bool_to_text(data.get("has_window"), "Có cửa sổ thoáng"),
            bool_to_text(data.get("has_fingerprint_lock"), "Khóa vân tay ra vào tự do"),
        ]
        # Lọc bỏ các giá trị rỗng
        amenities_str = ", ".join([item for item in amenities_list if item])

        # 2. Xử lý các trường thông tin bổ sung
        floor_str = f"Tầng {data.get('floor')}" if data.get('floor') else ""
        max_occ_str = f"Tối đa {data.get('max_occupants')} người ở" if data.get('max_occupants') else ""
        move_in_str = f"Vào ở từ ngày: {data.get('move_in_date')}" if data.get('move_in_date') else ""

        # 3. Ghép thành văn bản hoàn chỉnh để tạo Vector
        text_to_embed = f"""
        Thông tin phòng trọ:
        - Địa chỉ: {data.get('address', '')}
        - Tên phòng: {data.get('room_name', '')} {floor_str}
        - Giá thuê: {data.get('price', '')}
        - Tiện nghi: {amenities_str}. {data.get('other_amenities', '')}
        - Chỗ để xe: {data.get('parking_info', '')}
        - Quy định: {max_occ_str}. {move_in_str}
        - Phí dịch vụ: {data.get('service_fees', '')}
        """.strip()

        # 4. Lấy vector embedding từ chuỗi văn bản trên
        vector = get_text_embedding(text_to_embed)
        
        if not vector:
            print(f"❌ [SYSTEM ERROR] Không thể tạo Vector Embedding cho phòng: {data.get('address')}")
            return f"❌ Đăng ký thành công đến dòng {current_excel_row - 1}. Lỗi từ dòng {current_excel_row}: [SYSTEM ERROR] Không thể tạo Vector Embedding cho phòng."
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


        if not address or address.lower() in ["none", "null"]:
            raise ValueError("Lỗi: 'address' không được để trống hoặc null!")
            
        price = str(data.get("price", "Chưa rõ"))
        if not price or price.lower() in ["none", "null"]:
            raise ValueError("Lỗi: 'price' không được để trống hoặc null!")

        # 2. Đảm bảo room_code không null
        room_code = data.get("room_code")
        if not room_code or str(room_code).strip().lower() in ["none", "null", ""]:
            room_code = generate_unique_room_code()
        else:
            room_code = str(room_code).strip().upper()

        payload = {
            "address": address,
            "room_name": room_name,
            "room_code": room_code,
            "price": parse_price_to_number(data.get("price", "")),
            "floor": str(data.get("floor", "Chưa rõ")),
            "is_private_bathroom": str(data.get("is_private_bathroom", "Chưa rõ")),
            "has_ac": str(data.get("has_ac", "Chưa rõ")),
            "has_heater": str(data.get("has_heater", "Chưa rõ")),
            "has_washer": str(data.get("has_washer", "Chưa rõ")),
            "allow_pets": str(data.get("allow_pets", "Chưa rõ")),
            "has_balcony": str(data.get("has_balcony", "Chưa rõ")),
            "has_window": str(data.get("has_window", "Chưa rõ")),
            "has_fingerprint_lock": str(data.get("has_fingerprint_lock", "Chưa rõ")),
            "parking_info": str(data.get("parking_info", "Chưa rõ")),
            "max_occupants": str(data.get("max_occupants", "Chưa rõ")),
            "other_amenities": str(data.get("other_amenities", "Chưa rõ")),
            "service_fees": str(data.get("service_fees", "Chưa rõ")),
            "media_urls": media_list,
            "move_in_date": str(data.get("move_in_date", "Vào ở ngay")),
            "move_in_timestamp": parse_move_in_date(data.get("move_in_date")),
            "status": str(data.get("status", "TRỐNG")),
            "landlord_phone": format_national_phone(phone),
            "created_at": created_at,
            "updated_at": now_str
        }

        qdrant_client.upsert(
            collection_name=COLLECTION_NAME,
            points=[PointStruct(id=new_point_id, vector=vector, payload=payload)],
            wait=True
        )
        return True
    except Exception as e:
        print("❌ [QDRANT UPSERT EXCEPTION]:", e)
        return f"❌ [QDRANT UPSERT EXCEPTION]: {e}"

def search_rooms_by_vector(query_text: str, top_k: int = 20) -> List[dict]:
    """
    Tìm phòng trống.

    QUAN TRỌNG:
    - Với truy vấn chung / tìm phòng rẻ:
      Qdrant ORDER BY price ASC trên toàn bộ collection.
    - Không fallback sang scroll 200 record vì cách đó
      KHÔNG đảm bảo tìm được phòng rẻ nhất toàn DB.
    - Với truy vấn có nội dung cụ thể:
      dùng vector search.
    """

    clean_query = query_text.strip().lower() if query_text else ""

    # Giới hạn top_k để tránh request quá lớn
    top_k = max(1, min(int(top_k), 100))

    # Chỉ tìm phòng còn trống
    status_filter = qdrant_models.Filter(
        must=[
            qdrant_models.FieldCondition(
                key="status",
                match=qdrant_models.MatchValue(value="TRỐNG")
            )
        ]
    )

    # Các câu hỏi mang tính tìm phòng chung / tìm phòng rẻ
    is_general_cheap_search = any(
        kw in clean_query
        for kw in [
            "giá rẻ",
            "phòng rẻ",
            "rẻ nhất",
            "danh sách phòng",
            "phòng trống",
            "tìm phòng"
        ]
    ) and len(clean_query.split()) <= 5

    # ============================================================
    # 1. TÌM PHÒNG RẺ NHẤT TOÀN DATABASE
    # ============================================================
    if not clean_query or is_general_cheap_search:

        try:
            search_result = qdrant_client.query_points(
                collection_name=COLLECTION_NAME,

                # Chỉ lấy phòng TRỐNG
                query_filter=status_filter,

                # QUAN TRỌNG:
                # Qdrant tự sắp xếp trên toàn bộ database
                query=qdrant_models.OrderByQuery(
                    order_by=qdrant_models.OrderBy(
                        key="price",
                        direction=qdrant_models.Direction.ASC
                    )
                ),

                # Chỉ lấy đúng số lượng cần
                limit=top_k,

                with_payload=True
            )

            results = [
                hit.payload | {"id": hit.id}
                for hit in search_result.points
                if hit.payload
            ]

            return results

        except Exception as e:
            # KHÔNG fallback về scroll(200)
            # vì sẽ có nguy cơ trả kết quả sai.
            print(
                "❌ [CHEAPEST SEARCH ERROR] "
                "Qdrant không thể ORDER BY price:",
                repr(e)
            )

            return []

    # ============================================================
    # 2. TÌM THEO NGỮ NGHĨA / VECTOR
    # ============================================================

    query_vector = get_text_embedding(query_text)

    if not query_vector:
        print("⚠️ Không tạo được embedding cho query.")
        return []

    try:
        search_result = qdrant_client.query_points(
            collection_name=COLLECTION_NAME,

            query=query_vector,

            query_filter=status_filter,
            
            # Lấy dư một chút để có thêm lựa chọn
            limit=min(top_k * 3, 300),

            with_payload=True
        )

        results = [
            hit.payload | {"id": hit.id}
            for hit in search_result.points
            if hit.payload
        ]

        # Sắp xếp các kết quả tương đồng theo giá tăng dần
        results.sort(key=parse_price_safe)

        return results[:top_k]

    except Exception as e:
        print(
            "❌ [VECTOR SEARCH ERROR]:",
            repr(e)
        )
        return []

def update_room_status_in_db(point_id: str, new_status: str) -> bool:
    if not point_id:
        return False
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        qdrant_client.set_payload(
            collection_name=COLLECTION_NAME,
            payload={
                "status": new_status,
                "raw_data.status": new_status,
                "updated_at": now_str
            },
            points=[point_id],
            wait=True
        )
        return True
    except Exception as e:
        print("❌ [UPDATE STATUS ERROR]:", e)
        return False

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
              "allow_pets": "",
              "has_balcony": "",
              "has_window": "",
              "has_fingerprint_lock": "",
              "parking_info": "",
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

    models_to_try = [
        "models/gemini-2.5-flash", 
        "models/gemini-2.0-flash", 
        "models/gemini-1.5-flash"
    ]

    for model_name in models_to_try:
        for attempt in range(3):
            # 💡 FIX LỖI: Khởi tạo parsed_data = None ngay trước khối try
            parsed_data = None 
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                
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
        df = pd.read_excel(temp_file)
        rows = df.to_dict(orient="records")

        success_count = 0
        
        # Danh sách lưu thông tin lỗi theo dạng: [(số_dòng, lý_do)]
        failed_rows_details = []

        BATCH_SIZE = 15

        # 3. Duyệt theo từng batch
        for i in range(0, len(rows), BATCH_SIZE):
            batch_rows = rows[i:i + BATCH_SIZE]
            batch_results = ai_validate_and_extract_room_batch(batch_rows)

            # 4. Duyệt qua từng kết quả trong batch bằng enumerate
            for offset, validated_data in enumerate(batch_results):
                # Tính số dòng chính xác trong Excel (+2 do Header và Index từ 1)
                current_excel_row = i + offset + 2

                if isinstance(validated_data, list) and len(validated_data) > 0:
                    validated_data = validated_data[0]

                if not validated_data or not isinstance(validated_data, dict):
                    return (f"❌ Đăng ký thành công đến dòng {current_excel_row - 1}. Lỗi từ dòng {current_excel_row}: AI không phân tích được dữ liệu.")

                extracted = validated_data.get("extracted_data", {})
                if not isinstance(extracted, dict):
                    return (f"❌ Đăng ký thành công đến dòng {current_excel_row - 1}. Lỗi từ dòng {current_excel_row}: Dữ liệu AI trích xuất không hợp lệ")

                raw_address = str(extracted.get("address") or "").strip()

                if not raw_address or raw_address.lower() in ["[chưa cập nhật]", "none", "null", "chưa rõ", ""]:
                    return (f"❌ Đăng ký thành công đến dòng {current_excel_row - 1}. Lỗi từ dòng {current_excel_row}: Địa chỉ trống hoặc không hợp lệ")

                # upsert_room_to_db trả về None/"" nếu thành công, trả về string lỗi nếu thất bại
                message = upsert_room_to_db(data=extracted, current_excel_row=current_excel_row)
                
                if message in ("SUCCESS"):
                    success_count += 1
                else:
                    return f"❌ Đăng ký thành công đến dòng {current_excel_row - 1}. Lỗi từ dòng {current_excel_row}: Lỗi DB - {message}"


        # Dọn dẹp file tạm
        if os.path.exists(temp_file):
            os.remove(temp_file)

        # 5. Đóng gói thông báo trả về
        msg = (
            f"AI đã xử lý xong!\n"
            f"- Thành công: {success_count} phòng.\n"
        )

        if failed_rows_details:
            # Lấy tối đa 10 dòng lỗi đầu tiên để tránh tin nhắn quá dài
            error_lines = [f"  + Dòng {row}: {reason}" for row, reason in failed_rows_details[:10]]
            msg += "\n\nChi tiết các dòng bị lỗi:\n" + "\n".join(error_lines)

            if len(failed_rows_details) > 10:
                msg += f"\n  ... và {len(failed_rows_details) - 10} dòng lỗi khác."

        return msg

    except Exception as e:
        print("❌ [EXCEL PROCESS ERROR]:", e)
        if os.path.exists(temp_file):
            os.remove(temp_file)
        return "Lỗi trong quá trình xử lý file Excel."

def process_zalo_ai_logic(message_text: str, media_items: list = None, user_id: str = "SYSTEM", db: Session = None):
    incoming_media_urls = []
    for item in (media_items or []):
        saved_url = save_media_file(item["url"], is_video=item.get("is_video", False))
        if saved_url:
            incoming_media_urls.append(saved_url)
    if not message_text.strip() and incoming_media_urls:
        add_pending_media(user_id, incoming_media_urls)
        total_pending = len(PENDING_MEDIA_CACHE.get(user_id, {}).get("urls", []))
        reply = f"📸 Em đã nhận {len(incoming_media_urls)} ảnh/video! (Tổng đã nhận: {total_pending} file)\n\n👉 Anh/Chị gửi thêm thông tin phòng để em tạo bài nhé!"
        send_zalo_message(user_id, reply, media_urls=incoming_media_urls)
        return
    cached_data = PENDING_MEDIA_CACHE.get(user_id, {})
    pending_urls = cached_data.get("urls", []) if (time.time() - cached_data.get("timestamp", 0) <= CACHE_TTL_SECONDS) else []
    all_current_media = list(dict.fromkeys(pending_urls + incoming_media_urls))
    urls_to_send = all_current_media
    
    # 1. Lấy lịch sử chat của User từ Redis
    history_list = get_chat_history(user_id)
    history_context_str = json.dumps(history_list, ensure_ascii=False) if history_list else "Chưa có lịch sử hội thoại trước đó."
    
    print(f"history_list: {history_list}")
    print(f"history_context_str: {history_context_str}")
    # Kiểm tra từ khóa Reset Session từ người dùng
    clean_message = message_text.strip().lower()
    if any(keyword in clean_message for keyword in ["bắt đầu lại", "tìm phòng khác", "xóa lịch sử", "reset", "làm mới"]):
        clear_chat_history(user_id)
        ai_reply = "Dạ em đã làm mới cuộc hội thoại rồi ạ. Anh/chị muốn tìm phòng trọ ở khu vực nào và ngân sách khoảng bao nhiêu ạ?"
        send_zalo_message(user_id, ai_reply)
        return
    
    phone = get_phone_by_user_id(db, user_id)
    try:
        
        system_prompt = f"""
        Bạn là Trợ lý AI Quản lý và Tư vấn Phòng trọ thông minh trên Zalo.
        
        LỊCH SỬ HỘI THOẠI GẦN ĐÂY CỦA NGUỜI DÙNG:
        {history_context_str}
        
        Phân tích tin nhắn người dùng và trích xuất đúng 13 trường thông tin:

        1. `address`: Địa chỉ đầy đủ gộp lại.
        2. `room_name`: Tên hoặc số phòng (VD: Phòng 301, Phòng tầng 2...).
        3. `price`: Giá thuê.
        4. `floor`: Tầng bao nhiêu.
        5. `is_private_bathroom`: Vệ sinh riêng hay khép kín hay không.
        6. `has_ac`:  Điều hoà có hay không?
        7. `has_heater`: có bình nóng lạnh không?
        8. `has_washer`: Có máy giặt không?
        9. `allow_pets`: Có cho nuôi pet không?
        10. `has_balcony`: Có ban công không?
        11. `has_window`: Có cửa sổ không?
        12. `has_fingerprint_lock`: Ra vào bằng khoá vân tay có hay không?
        13. `parking_info`: có chỗ để xe không?
        14. `max_occupants`: số ng ở tối đã
        15. `other_amenities`: Có thêm tiện ích gì khác
        16. `service_fees`: Phí dịch vụ (điện, nước, wifi)...
        17. `status`: Trạng thái phòng ("TRỐNG" hoặc "ĐÃ CHO THUÊ").
        18. `move_in_date`: Ngày có thể chuyển vào phòng để ở
        19. `min_price`: Giá thuê thấp nhất
        20. `max_price`: Giá thuê cao nhất

        DỮ LIỆU ĐẦU VÀO:
        - Tin nhắn: "{message_text}"
        - Media kèm theo: {json.dumps(all_current_media)}

        QUY TẮC PHÂN LOẠI ACTION:
        - "ADD_ROOM": Dùng khi người dùng ĐĂNG PHÒNG MỚI hoặc CẬP NHẬT/SỬA BẤT KỲ THÔNG TIN NÀO CỦA PHÒNG.
        - "UPDATE_STATUS": CHỈ DÙNG khi người dùng báo phòng "ĐÃ CHO THUÊ", "ĐÃ CHỐT" hoặc "ĐỔI SANG TRỐNG".
        - "SEARCH_ROOM": Dùng khi khách có nhu cầu TÌM KIẾM phòng trọ.

        HƯỚNG DẪN TẠO `ai_reply` CHO TỪNG ACTION:
        1. Nếu action là "ADD_ROOM" hoặc "UPDATE_STATUS": 
           - Viết câu xác nhận ngắn gọn, lịch sự cho chủ nhà.
        2. Nếu action là "SEARCH_ROOM":
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
          "action": "ADD_ROOM | SEARCH_ROOM | UPDATE_STATUS",
          "is_valid_search": true/false,
          "extracted_search": {{
            "location_search": "Tên đường hoặc phường/xã trích xuất được (hoặc null)",
            "min_price": Giá thuê thấp nhất,
            "max_price": Giá thuê cao nhất
          }},
          "extracted_data": {{
            "address": "Địa chỉ phòng trọ...",
            "room_name": "Tên phòng trọ...",
            "price": "Giá thuê (ví dụ: 3.5 triệu)...",
            "floor": "Tầng số...",
            "is_private_bathroom": "Có/Không",
            "has_ac": "Có/Không",
            "has_heater": "Có/Không",
            "has_washer": "Có/Không",
            "allow_pets": "Có/Không",
            "has_balcony": "Có/Không",
            "has_window": "Có/Không",
            "has_fingerprint_lock": "Có/Không",
            "parking_info": "Thông tin để xe...",
            "max_occupants": "Số người ở tối đa...",
            "other_amenities": "Tiện ích khác...",
            "service_fees": "Phí dịch vụ (điện, nước, wifi)...",
            "move_in_date": "Ngày có thể chuyển vào...",
            "media_urls": {json.dumps(all_current_media)},
            "landlord_phone": "{phone}"
          }},
          "ai_reply": "Mô tả chi tiết dạng văn bản đẹp mắt..."
        }}
        """
                
        raw_text = generate_content_with_retry(system_prompt, mime_type="application/json")
        if not raw_text:
            raise Exception("Gemini không phản hồi dữ liệu.")
            
        # Clean chuỗi trước khi load JSON
        cleaned_text = clean_json_string(raw_text)
        if not cleaned_text:
            raise ValueError("Phản hồi từ Gemini bị rỗng sau khi làm sạch.")
        result_data = json.loads(cleaned_text)
        ai_reply = result_data.get("ai_reply", "Dạ em đã ghi nhận thông tin rồi ạ!")
        print(f"ai_reply 1: {ai_reply}")
        action = result_data.get("action")
        extracted = result_data.get("extracted_data", {})
        
        print(f"landlord_phone: {extracted.get("landlord_phone")}")
        if action == "SEARCH_ROOM":
            is_valid_search = result_data.get("is_valid_search", False)
        
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
                search_params = result_data.get("extracted_search", {})
                location_query = search_params.get("location_search", "")
                min_p = search_params.get("min_price", 0)
                max_p = search_params.get("max_price", 0)
                print(f"min_p : {min_p}")
                print(f"max_p : {max_p}")
                
                # Tạo chuỗi truy vấn kết hợp thông tin
                full_query = f"{message_text}"
                print(f"full_query : {full_query}")
                # Gọi tìm kiếm phòng có truyền kèm khoảng giá
                search_results = search_rooms_with_filter(
                    query_text=full_query, 
                    min_price=min_p, 
                    max_price=max_p, 
                    top_k=20
                )
                print(f"search_result : {search_results}")
                if not search_results:
                    ai_reply = f"Dạ tiếc quá, hệ thống chưa tìm thấy phòng nào ở khu vực **{location_query}** với tầm giá từ **{min_p:,.0f}đ đến {max_p:,.0f}đ** ạ!"
                else:
                    # ... (Đoạn gọi Gemini định dạng danh sách phòng giữ nguyên như cũ) ...
                    # 1. Chuẩn bị Prompt cho Gemini định dạng danh sách phòng
                    prompt_format_rooms = f"""
                    Bạn là một trợ lý tư vấn tìm phòng trọ thân thiện và chuyên nghiệp.
                    Dưới đây là danh sách các phòng trọ phù hợp với yêu cầu của khách hàng:
                    Phân tích tin nhắn người dùng và trích xuất đúng 13 trường thông tin:

                    1. `address`: Địa chỉ đầy đủ gộp lại.
                    2. `room_name`: Tên hoặc số phòng (VD: Phòng 301, Phòng tầng 2...).
                    3. `price`: Giá thuê.
                    4. `floor`: Tầng bao nhiêu.
                    5. `is_private_bathroom`: Vệ sinh riêng hay khép kín hay không.
                    6. `has_ac`:  Điều hoà có hay không?
                    7. `has_heater`: có bình nóng lạnh không?
                    8. `has_washer`: Có máy giặt không?
                    9. `allow_pets`: Có cho nuôi pet không?
                    10. `has_balcony`: Có ban công không?
                    11. `has_window`: Có cửa sổ không?
                    12. `has_fingerprint_lock`: Ra vào bằng khoá vân tay có hay không?
                    13. `parking_info`: có chỗ để xe không?
                    14. `max_occupants`: số ng ở tối đã
                    15. `other_amenities`: Có thêm tiện ích gì khác
                    16. `service_fees`: Phí dịch vụ (điện, nước, wifi)...
                    17. `status`: Trạng thái phòng ("TRỐNG" hoặc "ĐÃ CHO THUÊ").
                    18. `move_in_date`: Ngày có thể chuyển vào phòng để ở
                    19. `min_price`: Giá thuê thấp nhất
                    20. `max_price`: Giá thuê cao nhất
                    
                    Yêu cầu của khách: "{message_text}"
                    Danh sách phòng tìm được: {search_results}
                    
                    HƯỚNG DẪN TẠO `ai_reply`:
                    - Liệt kê các phòng rõ ràng, dễ đọc (tên/mã phòng, địa chỉ, giá tiền, tiện ích nổi bật).
                    - Giữ văn phong lịch sự, tư vấn nhiệt tình.
                    - Không tự bịa ra thông tin ngoài dữ liệu được cung cấp.
                    - Nếu "Danh sách phòng trống" RỖNG: Trả lời lịch sự báo hiện chưa có phòng phù hợp.
                    - Nếu CÓ PHÒNG: Định dạng ngay danh sách phòng thành 1 tin nhắn phản hồi đẹp mắt trên Zalo:
                        + Đánh số thứ tự (1, 2, 3...).
                        + Tuyệt đối KHÔNG hiển thị các trường ghi "[Chưa cập nhật]", "Không", "Chưa rõ", "null", hoặc rỗng.
                        + Bắt buộc hiển thị: Mã phòng, Tên phòng, Địa chỉ, Giá thuê, Media URLs (nếu có).
                        + Dùng emoji sinh động. KHÔNG tự chèn đường link ảnh vào văn bản.
                        + Mỗi phòng liệt kê ngắn gọn: Tên/Số phòng, Địa chỉ, Giá thuê, và danh sách tiện ích có sẵn.
                        + Dùng icon/emoji sinh động. KHÔNG chèn bất kỳ đường link ảnh nào.
                        + Phải có thông tin mã phòng để người dùng đặt phòng
                        + Nếu đường link media media_urls tồn tại thì phải hiển thị
                        
                    TRẢ VỀ DUY NHẤT 1 CHUỖI JSON ĐÚNG CẤU TRÚC:
                    {{
                      "ai_reply": "Mô tả chi tiết dạng văn bản đẹp mắt..."
                    }}
                    
                    """
                    raw_text_search = generate_content_with_retry(prompt_format_rooms, mime_type="application/json")
                    cleaned_text_search = clean_json_string(raw_text_search)
                    # 1. Parse JSON
                    result_data_search = json.loads(cleaned_text_search)
                    
                    ai_reply = result_data_search.get("ai_reply", "Dạ em đã ghi nhận thông tin rồi ạ!")
                    
                    
                    raw_text_search = generate_content_with_retry(prompt_format_rooms, mime_type="application/json")
                    
                    
                    if not raw_text_search:
                        raise Exception("Gemini không phản hồi dữ liệu.")
                        
                    # Clean chuỗi trước khi load JSON
                    cleaned_text_search = clean_json_string(raw_text_search)
                    if not cleaned_text_search:
                        raise ValueError("Phản hồi từ Gemini bị rỗng sau khi làm sạch.")
                    result_data = json.loads(cleaned_text_search)
                    ai_reply = result_data.get("ai_reply", "Dạ em đã ghi nhận thông tin rồi ạ!")
    
                    
                # Vì là tìm kiếm nên không đính kèm media đăng phòng của người dùng
                urls_to_send = []

        elif action == "ADD_ROOM":
            # ✅ TRƯỜNG HỢP 2: ĐỊA CHỈ ĐÃ ĐỦ RÕ RÀNG -> TIẾN HÀNH LƯU DATABASE
            address = str(extracted.get("address", "")).strip()
            if address and address.lower() not in ["null", "none", "chưa rõ", ""]:
                #nếu người dùng chưa đăng ký phòng trên zalo hay web thì sẽ tạo mới data cho user
          
                if not phone:
                    # 🚨 BẮT BUỘC: Nếu chưa có SĐT -> Chặn lại và yêu cầu chia sẻ SĐT
                    request_phone_message = {
                            "recipient": {"user_id": zalo_user_id},
                            "message": {
                                "text": "⚠️ Để đăng bài cho thuê phòng, bạn vui lòng bấm nút bên dưới để chia sẻ Số điện thoại liên hệ nhé!",
                                "attachment": {
                                    "type": "template",
                                    "payload": {
                                        "template_type": "request_user_info",
                                        "elements": [{
                                            "title": "Xác thực Số điện thoại",
                                            "subtitle": "Yêu cầu cung cấp SĐT chính chủ trên Zalo để tạo tài khoản đăng phòng.",
                                            "image_url": "https://your-domain.com/static/icon.png"
                                        }]
                                    }
                                }
                            }
                        }
                    # Gọi Zalo Open API gửi yêu cầu xin SĐT
                    send_zalo_request(request_phone_message)
                    return {"status": "phone_required"}

                message = upsert_room_to_db(data=extracted, media_urls=all_current_media, point_id=None)
                if message in ("SUCCESS"):
                    get_get_and_clear_pending_media(user_id)
               
        elif action == "UPDATE_STATUS":
            relevant_rooms = search_rooms_by_vector(message_text, top_k=5)
            existing_point_id = relevant_rooms[0].get("id") if (relevant_rooms and len(relevant_rooms) > 0) else None
            if existing_point_id:
                new_status = extracted.get("status") or "ĐÃ CHO THUÊ"
                update_room_status_in_db(point_id=existing_point_id, new_status=new_status, zalo_user_id=user_id)

    except Exception as err:
        print("❌ [AI Logic Exception]:", err)
        ai_reply = "Dạ hệ thống đang bận một chút, anh/chị chờ em vài giây rồi nhắn lại giúp em nhé!"
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
            print(f"⚠️ Lỗi kết nối Qdrant khi kiểm tra room_code, đang thử lại... Chi tiết: {e}")
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
        print("❌ Lỗi truy vấn Qdrant:", e)
        return "Dạ hệ thống đang gặp sự cố, vui lòng thử lại sau ít phút!"

    if not records:
        return f"❌ Không tìm thấy phòng trọ nào có mã phòng **{room_code}**. Vui lòng kiểm tra lại mã!"


    room_data = records[0].payload
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
            created_at=datetime.utcnow()
        )
        db.add(new_order)
        db.commit()
        db.refresh(new_order)
        print(f"✅ Đã lưu đơn đặt phòng thành công: Order ID {new_order.id}")
    except Exception as e:
        db.rollback()
        print(f"❌ Lỗi SQL khi chèn dữ liệu vào order_room: {e}")
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
        print(f"❌ Lỗi SQL khi lấy phone của user {user_id}: {e}")
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
        print(f"❌ Lỗi SQL khi lấy user_id của user {phone}: {e}")
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
            user.updated_at = datetime.utcnow()
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
        print(f"❌ Lỗi SQL khi lưu/cập nhật user_web: {e}")
        raise e
        
        
def send_zalo_request_phone(tenant_zalo_id: str) -> bool:
    if not tenant_zalo_id or str(tenant_zalo_id).strip() in ["ADMIN_WEB", "SYSTEM", "None", ""]:
        print("⚠️ Zalo User ID không hợp lệ, không thể gửi yêu cầu SĐT.")
        return False

    # ✅ SỬA TẠI ĐÂY: URL không chứa token
    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    
    # ✅ SỬA TẠI ĐÂY: Đặt access_token vào Header
    headers = {
        "Content-Type": "application/json",
        "access_token": ZALO_ACCESS_TOKEN
    }

    payload = {
        "recipient": {
            "user_id": str(tenant_zalo_id)
        },
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "request_user_info",
                    "elements": [
                        {
                            "title": "Xác thực số điện thoại",
                            "subtitle": "Vui lòng chia sẻ Số điện thoại Zalo của bạn để hoàn tất đăng ký/đặt phòng.",
                            "image_url": "https://zalo-room-project-jjsx.onrender.com/static/icon_zalo_room.png"
                        }
                    ]
                }
            }
        }
    }

    try:
        # Dùng json=payload thay vì data=json.dumps(payload) cho gọn nhẹ
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        res_data = response.json()

        if res_data.get("error") == 0:
            print(f"✅ Đã gửi yêu cầu SĐT thành công tới Zalo ID: {tenant_zalo_id}")
            
            return True
        else:
            print(f"❌ Lỗi từ Zalo API ({res_data.get('error')}): {res_data.get('message')}")
            return False

    except Exception as e:
        print(f"❌ Lỗi kết nối khi gửi yêu cầu SĐT tới Zalo: {e}")
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
    now = datetime.now()
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


    if location_search:
        must_conditions.append(
            qdrant_models.FieldCondition(
                key="address",
                match=qdrant_models.MatchText(text=location_search) # Tìm tương đối tên đường/quận trong address
            )
        )

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
    query_vector = get_text_embedding(query_text)
    print(f"query_text : {query_text}")
    try:
        search_result = qdrant_client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector if query_vector else None,
            query_filter=status_filter,
            limit=top_k,
            with_payload=True
        )
        
        return [hit.payload | {"id": hit.id} for hit in search_result.points if hit.payload]
    except Exception as e:
        print("❌ [SEARCH FILTER ERROR]:", e)
        return []
        



def refresh_zalo_tokens(db):
    current_token_entry = get_current_tokens_from_db(db)
    current_refresh_token = current_token_entry["refresh_token"]
    
    app_id_clean = str(ZALO_OA_ID).strip() if ZALO_OA_ID else ""
    secret_key_clean = str(ZALO_SECRET_KEY).strip() if ZALO_SECRET_KEY else ""

    # Kiểm tra an toàn trước khi gọi API
    if not app_id_clean or app_id_clean.lower() == "none":
        raise Exception("❌ Thiếu ZALO_OA_ID trong biến môi trường!")

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
    
    response = requests.post(oauth_url, headers=headers, data=data)
    res_json = response.json()
    
    if "access_token" in res_json:
        new_access_token = res_json["access_token"]
        new_refresh_token = res_json["refresh_token"]
        update_tokens_in_db(db, new_access_token, new_refresh_token)
        print("🎉 [ZALO OAUTH] Tự động Refresh Token và lưu DB thành công!", flush=True)
    else:
        print(f"❌ [ZALO Refresh access token False: ]: {res_json}", flush=True)
        send_zalo_message(os.environ.get("ZALO_ADMIN_ID"), "⚠️ Refresh Token Zalo đã hết hạn hẳn! Vui lòng đăng nhập cấp lại quyền.")
        raise Exception(f"Zalo OAuth Error: {res_json.get('error_description', res_json)}")

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
        print(f"❌ [DATABASE ERROR]: Không thể lưu token vào DB: {e}", flush=True)
        raise e
        
        
def find_existing_room_id(address: str, room_name: str = "") -> Optional[str]:
    """
    Tìm ID phòng đã tồn tại bằng cách lọc trực tiếp trên Qdrant theo Address và Room name.
    """
    clean_address = str(address).strip()
    
    if not clean_address:
        return None

    try:
        must_conditions = [
            qdrant_models.FieldCondition(
                key="address", 
                match=qdrant_models.MatchValue(value=clean_address)
            )
        ]
        

        # Nếu có tên/số phòng -> Thêm điều kiện lọc theo room_name
        if room_name and room_name.strip():
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="room_name", 
                    match=qdrant_models.MatchValue(value=room_name.strip())
                )
            )

        records, _ = qdrant_client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=qdrant_models.Filter(must=must_conditions),
            limit=1
        )
        
        if records:
            return records[0].id

    except Exception as e:
        print(f"❌ Lỗi truy vấn Qdrant khi tìm phòng trùng: {e}")
        
    return None
    
# Hàm trợ lý nhỏ để đổi True/False thành văn bản dễ hiểu cho Model Embedding
def bool_to_text(val, true_str, false_str=""):
    if isinstance(val, bool):
        return true_str if val else false_str
    if str(val).lower() in ["true", "1", "có", "yes"]:
        return true_str
    return false_str
    
    
MAX_HISTORY_MESSAGES = 30  # Lưu 5 cặp câu hỏi - trả lời gần nhất
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