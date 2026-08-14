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
from sqlalchemy import Column, String, DateTime, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from qdrant_client.http import models as qdrant_models
from google import genai
from google.genai import types
import cloudinary
import cloudinary.uploader
import string


# --- CẤU HÌNH MÔI TRƯỜNG & KHỞI TẠO SERVICES ---
ZALO_ACCESS_TOKEN = os.environ.get("ZALO_ACCESS_TOKEN")
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
        field_name="status",
        field_schema=qdrant_models.PayloadSchemaType.KEYWORD,
    )
    qdrant_client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="price_num",
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


# --- MODEL ORDER_ROOM TRONG SQLALCHEMY ---
class OrderRoom(Base):
    __tablename__ = "order_room"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_zalo_id = Column(String, nullable=True)     # Zalo ID người thuê
    tenant_phone = Column(String, nullable=True)       # SĐT người thuê
    landlord_zalo_id = Column(String, nullable=True)   # Zalo ID chủ nhà
    landlord_phone = Column(String, nullable=True)     # SĐT chủ nhà
    room_code = Column(String, index=True, nullable=False) # Mã phòng 6 ký tự
    created_at = Column(DateTime, default=datetime.utcnow) # Thời gian tạo đơn
    
    
# --- DATABASE DEPENDENCY & CURRENT USER HELPER ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(
    x_user_phone: str = Header(None, alias="X-User-Phone"),
    db: Session = Depends(get_db)
) -> UserWeb:
    if not x_user_phone:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Vui lòng cung cấp số điện thoại hoặc thông tin xác thực!"
        )
    clean_phone = x_user_phone if x_user_phone == "adminpro" else format_national_phone(x_user_phone)
    user = db.query(UserWeb).filter(UserWeb.phone == clean_phone).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy thông tin người dùng!"
        )
    return user

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
    if not price_str or price_str in ["Chưa rõ", "Thỏa thuận", "None", "nan"]:
        return 0.0
    text = str(price_str).lower().replace(",", ".").strip()
    if "triệu" in text or "tr" in text:
        text = re.sub(r'(\d+)\s*(?:triệu|tr)\s*(\d+)', r'\1.\2', text)
        match = re.search(r'(\d+(?:\.\d+)?)', text)
        if match:
            return float(match.group(1)) * 1_000_000
    digits_only = re.sub(r'[^\d]', '', text)
    if digits_only:
        val = float(digits_only)
        if val < 100: 
            return val * 1_000_000
        return val
    return 0.0

def get_get_and_clear_pending_media(user_id: str) -> list:
    if user_id in PENDING_MEDIA_CACHE:
        cached_data = PENDING_MEDIA_CACHE.pop(user_id)
        if time.time() - cached_data["timestamp"] <= CACHE_TTL_SECONDS:
            return cached_data["urls"]
    return []

def add_pending_media(user_id: str, new_urls: list):
    if not new_urls:
        return
    current_urls = []
    if user_id in PENDING_MEDIA_CACHE:
        if time.time() - PENDING_MEDIA_CACHE[user_id]["timestamp"] <= CACHE_TTL_SECONDS:
            current_urls = PENDING_MEDIA_CACHE[user_id]["urls"]
    merged_urls = list(dict.fromkeys(current_urls + new_urls))
    PENDING_MEDIA_CACHE[user_id] = {
        "urls": merged_urls,
        "timestamp": time.time()
    }

def send_zalo_message(user_id: str, ai_reply: str, media_urls: list = None):
    access_token = ZALO_ACCESS_TOKEN
    if not access_token:
        return False
    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    headers = {"Content-Type": "application/json", "access_token": access_token}

    payload = {
        "recipient": {"user_id": user_id},
        "message": {"text": ai_reply}
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        return response.json().get("error") == 0
    except Exception as e:
        print("❌ [ZALO CRITICAL ERROR]:", e)
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
def upsert_room_to_db(data: dict, point_id: str = None, zalo_user_id: str = "SYSTEM", media_urls: Optional[List[str]] = None) -> bool:
    try:
        address = str(data.get("address", "")).strip()
        room_name = str(data.get("room_name", "Phòng trọ")).strip()
        
        if zalo_user_id in ["EXCEL_AI_IMPORT", "ADMIN_WEB", "SYSTEM"]:
            phone = str(data.get("landlord_phone", "Chưa rõ")).strip()  #Lấy phone web
        else:
            phone = get_phone_by_user_id(user_id=zalo_user_id)      #Lấy phone zalo từ bảng user_web
        if not address:
            return False

        media_list = media_urls if media_urls is not None else data.get("media_urls", [])
        if isinstance(media_list, str):
            media_list = [x.strip() for x in media_list.split(",") if x.strip()]

        text_to_embed = f"Địa chỉ: {address} Tên phòng: {room_name} Giá: {data.get('price', '')} Đồ đạc: {data.get('other_amenities', '')} Phí dịch vụ: {data.get('service_fees', '')}"
        vector = get_text_embedding(text_to_embed)
        if not vector:
            return False

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
            "price": price,
            "price_num": parse_price_to_number(data.get("price", "")),
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
            "zalo_user_id": zalo_user_id,
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
        return False

def search_rooms_by_vector(query_text: str, top_k: int = 5) -> List[dict]:
    query_vector = get_text_embedding(query_text)
    if not query_vector:
        try:
            records, _ = qdrant_client.scroll(collection_name=COLLECTION_NAME, limit=top_k, with_payload=True)
            results = []
            for rec in records:
                if rec.payload:
                    p = rec.payload
                    p["id"] = rec.id
                    results.append(p)
            return results
        except Exception:
            return []
    try:
        search_result = qdrant_client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            limit=top_k,
            with_payload=True
        )
        results = []
        for hit in search_result.points:
            if hit.payload:
                p = hit.payload
                p["id"] = hit.id
                results.append(p)
        return results
    except Exception as e:
        print("❌ [VECTOR SEARCH ERROR]:", e)
        return []

def update_room_status_in_db(point_id: str, new_status: str, zalo_user_id: str = "SYSTEM") -> bool:
    if not point_id:
        return False
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        qdrant_client.set_payload(
            collection_name=COLLECTION_NAME,
            payload={
                "status": new_status,
                "raw_data.status": new_status,
                "updated_at": now_str,
                "zalo_user_id": zalo_user_id
            },
            points=[point_id],
            wait=True
        )
        return True
    except Exception as e:
        print("❌ [UPDATE STATUS ERROR]:", e)
        return False

def ai_validate_and_extract_room(row_dict: dict) -> Optional[dict]:
    def manual_fallback(data: dict) -> Optional[dict]:
        raw_address = data.get("address") or data.get("Địa chỉ") or data.get("1. Địa chỉ")
        if not raw_address or pd.isna(raw_address) or str(raw_address).strip().lower() in ["nan", "none", "null", "", "chưa rõ"]:
            return None
        return {
            "address": str(raw_address).strip(),
            "room_code": str(data.get("room_code")),
            "room_name": str(data.get("room_name") or data.get("Tên phòng") or "Phòng trọ").strip(),
            "price": str(data.get("price") or data.get("Giá thuê") or "Chưa rõ").strip(),
            "floor": str(data.get("floor") or "Chưa rõ").strip(),
            "is_private_bathroom": str(data.get("is_private_bathroom") or "Chưa rõ").strip(),
            "has_ac": str(data.get("has_ac") or "Chưa rõ").strip(),
            "has_heater": str(data.get("has_heater") or "Chưa rõ").strip(),
            "has_washer": str(data.get("has_washer") or "Chưa rõ").strip(),
            "allow_pets": str(data.get("allow_pets") or "Chưa rõ").strip(),
            "has_balcony": str(data.get("has_balcony") or "Chưa rõ").strip(),
            "has_window": str(data.get("has_window") or "Chưa rõ").strip(),
            "has_fingerprint_lock": str(data.get("has_fingerprint_lock") or "Chưa rõ").strip(),
            "parking_info": str(data.get("parking_info") or "Chưa rõ").strip(),
            "max_occupants": str(data.get("max_occupants") or "Chưa rõ").strip(),
            "other_amenities": str(data.get("other_amenities") or "Chưa rõ").strip(),
            "service_fees": str(data.get("service_fees") or "Chưa rõ").strip(),
            "move_in_date": str(data.get("move_in_date") or "Vào ở ngay").strip(),
            "status": str(data.get("status") or "TRỐNG").strip(),
            "landlord_phone": str(data.get("landlord_phone") or "Chưa rõ").strip(),
            "media_urls": []
        }

    if not gemini_client:
        return manual_fallback(row_dict)

    prompt = f"Bạn là trợ lý AI kiểm định dữ liệu phòng trọ. Chuẩn hóa dữ liệu JSON: {json.dumps(row_dict, ensure_ascii=False)}"
    try:
        response = gemini_client.models.generate_content(
            model="models/gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        if response and response.text:
            raw_text = response.text.strip()
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```(?:json)?\n?", "", raw_text)
                raw_text = re.sub(r"\n?```$", "", raw_text)
            return json.loads(raw_text)
    except Exception as e:
        print("⚠️ Lỗi AI Validation:", e)
    return manual_fallback(row_dict)

def process_excel_file(file_url: str, sender_id: str) -> int:
    try:
        res = requests.get(file_url, timeout=30)
        if res.status_code != 200:
            return 0
        temp_file = "temp_rooms.xlsx"
        with open(temp_file, "wb") as f:
            f.write(res.content)

        df = pd.read_excel(temp_file).fillna("[Chưa cập nhật]")
        success_count = 0
        for _, row in df.iterrows():
            extracted_data = {
                "address": str(row.get("Địa chỉ", "")).strip(),
                "room_name": str(row.get("Tên phòng", "Phòng trọ")).strip(),
                "floor": str(row.get("Tầng", "[Chưa cập nhật]")),
                "price": str(row.get("Giá", "[Chưa cập nhật]")),
                "status": str(row.get("Trạng thái", "TRỐNG"))
            }
            media_raw = str(row.get("Media", ""))
            media_urls = [url.strip() for url in media_raw.split(",") if url.strip() and url != "[Chưa cập nhật]"]
            if extracted_data["address"] and extracted_data["address"] != "[Chưa cập nhật]":
                if upsert_room_to_db(extracted_data, media_urls=media_urls, point_id=None, zalo_user_id=sender_id):
                    success_count += 1

        if os.path.exists(temp_file):
            os.remove(temp_file)
        return success_count
    except Exception as e:
        print("❌ [EXCEL PROCESS ERROR]:", e)
        return 0

def process_zalo_ai_logic(message_text: str, media_items: list = None, user_id: str = "SYSTEM"):
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

    try:
        relevant_rooms = search_rooms_by_vector(message_text, top_k=5)
        system_prompt = f"""
Bạn là Trợ lý AI Quản lý và Tư vấn Phòng trọ thông minh trên Zalo.
Phân tích tin nhắn: "{message_text}"
Media: {json.dumps(all_current_media)}
Phòng từ Vector Search: {json.dumps(relevant_rooms, ensure_ascii=False)}

TRẢ VỀ DUY NHẤT 1 CHUỖI JSON ĐÚNG CẤU TRÚC:
{{
    "action": "ADD_ROOM" | "SEARCH_ROOM" | "UPDATE_STATUS",
    "extracted_data": {{
        "address": "...", "room_name": "...", "price": "...", "status": "TRỐNG", "landlord_phone":"..."
    }},
    "ai_reply": "Mô tả chi tiết dạng văn bản đẹp mắt..."
}}
"""
        raw_text = generate_content_with_retry(system_prompt, mime_type="application/json")
        if not raw_text:
            raise Exception("Gemini không phản hồi dữ liệu.")

        result_data = json.loads(raw_text)
        ai_reply = result_data.get("ai_reply", "Dạ em đã ghi nhận thông tin rồi ạ!")
        action = result_data.get("action")
        extracted = result_data.get("extracted_data", {})
        existing_point_id = relevant_rooms[0].get("id") if (relevant_rooms and len(relevant_rooms) > 0) else None

        if action == "ADD_ROOM":
            address = str(extracted.get("address", "")).strip()
            if address and address.lower() not in ["null", "none", "chưa rõ", ""]:
                #nếu người dùng chưa đăng ký phòng trên zalo hay web thì sẽ tạo mới data cho user
                user_id = get_phone_by_user_id(user_id)
                if not user_id:
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

                
                success = upsert_room_to_db(data=extracted, media_urls=all_current_media, point_id=None, zalo_user_id=user_id)
                if success:
                    get_get_and_clear_pending_media(user_id)

        elif action == "UPDATE_STATUS" and existing_point_id:
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

def process_room_booking(tenant_zalo_id: str, room_code: str, db: Session) -> str:
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
    landlord_zalo_id = room_data.get("zalo_user_id", "")
    room_address = room_data.get("address", "Chưa rõ")
    room_name = room_data.get("room_name", "Phòng trọ")

    # 2. Tìm SĐT của người thuê (nếu người thuê đã từng đăng ký/xác thực OTP trên Zalo)
    tenant_user = db.query(UserWeb).filter(UserWeb.user_id == tenant_zalo_id).first()
    tenant_phone = tenant_user.phone if tenant_user else "Chưa xác thực SĐT"

    # 3. Lưu thông tin đơn vào bảng order_room
    new_order = OrderRoom(
        tenant_zalo_id=tenant_zalo_id,
        tenant_phone=tenant_phone,
        landlord_zalo_id=landlord_zalo_id,
        landlord_phone=landlord_phone,
        room_code=room_code,
        created_at=datetime.utcnow()
    )
    db.add(new_order)
    db.commit()

    # 4. Gửi thông báo chủ động tới Zalo của Chủ nhà (nếu có landlord_zalo_id)
    if landlord_zalo_id and landlord_zalo_id != "SYSTEM":
        msg_to_landlord = (
            f"🔔 **CÓ LỊCH HẸN XEM PHÓNG MỚI!**\n\n"
            f"📍 Mã phòng: {room_code} ({room_name} - {room_address})\n"
            f"👤 Zalo ID người thuê: {tenant_zalo_id}\n"
            f"📞 SĐT người thuê: {tenant_phone}\n"
            f"⏰ Thời gian đặt: {datetime.now(VN_TZ).strftime('%H:%M %d/%m/%Y')}\n\n"
            f"👉 Vui lòng liên hệ lại với khách hàng để chốt lịch hẹn chi tiết!"
        )
        send_zalo_message(user_id=landlord_zalo_id, ai_reply=msg_to_landlord)

    return (
        f"✅ **ĐẶT LỊCH XEM PHÓNG THÀNH CÔNG!**\n\n"
        f"🏠 Phòng: {room_name} (Mã: {room_code})\n"
        f"📍 Địa chỉ: {room_address}\n"
        f"📞 SĐT chủ nhà: {landlord_phone}\n\n"
        f"Yêu cầu của bạn đã được gửi trực tiếp tới Chủ nhà. Chủ nhà sẽ chủ động liên hệ lại sớm nhất ạ!"
    )
    

    def get_phone_by_user_id(db: Session, user_id: str) -> Optional[str]:
        """
        Lấy số điện thoại người dùng từ bảng user_web trong SQL Database.
        
        Args:
            db (Session): Database session
            user_id (str): ID của người dùng
            
        Returns:
            Optional[str]: Số điện thoại hoặc None
        """
        if not user_id:
            return None

        try:
            # Truy vấn tìm user theo ID
            user = db.query(UserWeb).filter(UserWeb.id == user_id).first()
            
            if user and user.phone:
                return user.phone.strip()
                
            return None

        except Exception as e:
            print(f"❌ Lỗi SQL khi lấy phone của user {user_id}: {e}")
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
            user = db.query(UserWeb).filter(UserWeb.zalo_user_id == clean_zalo_id).first()

            if user:
                # 2A. Nếu tìm thấy -> Cập nhật thông tin
                user.phone = clean_phone
                user.updated_at = datetime.datetime.utcnow()
                print(f"🔄 Đã cập nhật User Web (Zalo ID: {clean_zalo_id}) -> SĐT: {clean_phone}")
            else:
                # 2B. Nếu chưa có -> Tạo mới bản ghi user_web
                user = UserWeb(
                    id=clean_zalo_id,  # Dùng luôn Zalo ID làm PK hoặc dùng str(uuid.uuid4())
                    zalo_user_id=clean_zalo_id,
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
            user = db.query(UserWeb).filter(UserWeb.zalo_user_id == clean_zalo_id).first()

            if user:
                # 2A. Nếu tìm thấy -> Cập nhật thông tin
                user.phone = clean_phone
                user.updated_at = datetime.datetime.utcnow()
                print(f"🔄 Đã cập nhật User Web (Zalo ID: {clean_zalo_id}) -> SĐT: {clean_phone}")
            else:
                # 2B. Nếu chưa có -> Tạo mới bản ghi user_web
                user = UserWeb(
                    id=clean_zalo_id,  # Dùng luôn Zalo ID làm PK hoặc dùng str(uuid.uuid4())
                    zalo_user_id=clean_zalo_id,
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