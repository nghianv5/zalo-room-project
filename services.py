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
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


app = FastAPI()

# 1. Đảm bảo thư mục static tồn tại
if not os.path.exists("static"):
    os.makedirs("static")

# 2. MOUNT thư mục "static" ra URL route "/static"
# Dòng này giúp truy cập được file qua https://domain.com/static/filename.png
app.mount("/static", StaticFiles(directory="static"), name="static")

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
    print(1111)
    payload = {
        "recipient": {"user_id": user_id},
        "message": {"text": ai_reply}
    }
    try:
        print(2222)
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        print(3333)
        return response.json().get("error") == 0
    except Exception as e:
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
def upsert_room_to_db(data: dict, point_id: str = None, media_urls: Optional[List[str]] = None) -> bool:
    try:
        address = str(data.get("address", "")).strip()
        room_name = str(data.get("room_name", "Phòng trọ")).strip()
        phone = str(data.get("landlord_phone", "")).strip()


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
    print(f"query_vector: {bool(query_vector)}")  # In ra True/False thay vì in cả dãy số dài

    # 1. Định nghĩa bộ lọc bắt buộc: Chỉ lấy phòng còn TRỐNG
    status_filter = qdrant_models.Filter(
        must=[
            qdrant_models.FieldCondition(
                key="status",
                match=qdrant_models.MatchValue(value="TRỐNG")
            )
        ]
    )

    # 2. Xử lý trường hợp KHÔNG TẠO ĐƯỢC VECTOR (Lỗi API / Chuỗi rỗng)
    if not query_vector:
        try:
            # Lấy các phòng TRỐNG từ Qdrant
            records, _ = qdrant_client.scroll(
                collection_name=COLLECTION_NAME,
                scroll_filter=status_filter,  # Thêm filter phòng TRỐNG
                limit=top_k * 4,              # Lấy dư ra một chút để sắp xếp
                with_payload=True
            )
            results = []
            for rec in records:
                if rec.payload:
                    p = rec.payload
                    p["id"] = rec.id
                    results.append(p)

            # Sắp xếp theo giá tăng dần trong Python
            sorted_results = sorted(
                results, 
                key=lambda x: float(x.get("price_number", 999_999_999))
            )
            return sorted_results[:top_k]

        except Exception as e:
            print("❌ [SCROLL FALLBACK ERROR]:", e)
            return []

    # 3. Xử lý TÌM KIẾM THEO VECTOR (Có Query Vector)
    try:
        # Lấy Top N phòng khớp nhất với nhu cầu người dùng ĐÃ ĐƯỢC LỌC "TRỐNG"
        search_result = qdrant_client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            query_filter=status_filter,
            limit=top_k * 3,  # Lấy dư Top phòng tương đồng nhất để lọc giá
            with_payload=True
        )

        results = []
        for hit in search_result.points:
            if hit.payload:
                p = hit.payload
                p["id"] = hit.id
                results.append(p)

        # Ưu tiên đưa các phòng RẺ NHẤT trong số các phòng phù hợp nhất lên đầu
        sorted_results = sorted(
            results, 
            key=lambda x: float(x.get("price_number", 999_999_999))
        )
        
        return sorted_results[:top_k]

    except Exception as e:
        print("❌ [VECTOR SEARCH ERROR]:", e)
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

def ai_validate_and_extract_room(row_dict: dict) -> Optional[dict]:
    # Kiểm tra client AI
    if not gemini_client:
        print("⚠️ Gemini Client chưa được khởi tạo!")
        return None

    # Lọc bỏ giá trị NaN / rỗng và chuyển đổi dòng dữ liệu Excel thành văn bản sạch
    clean_row_str = ", ".join(f"{k}: {v}" for k, v in row_dict.items() if pd.notna(v) and str(v).strip() != "")
    if not clean_row_str:
        return None

    # Tìm kiếm phòng tương tự bằng Vector Search
    relevant_rooms = search_rooms_by_vector(clean_row_str, top_k=5)

    # Clean dict để gửi vào prompt (chuyển NaN thành None để JSON hóa hợp lệ)
    clean_dict = {k: (None if pd.isna(v) else v) for k, v in row_dict.items()}

    prompt = f"""
        Bạn là Trợ lý AI Xử lý và Chuẩn hóa Dữ liệu Phòng trọ từ File Excel nhập vào.
        Nhiệm vụ: Tự động phân tích, trích xuất và chuẩn hóa dòng dữ liệu thô từ Excel thành cấu trúc JSON chuẩn.

        DỮ LIỆU DÒNG EXCEL THÔ:
        {json.dumps(clean_dict, ensure_ascii=False)}

        CÁC PHÒNG TƯƠNG TỰ TRONG CƠ SỞ DỮ LIỆU (Dùng để đối chiếu/tránh trùng lặp nếu cần):
        {json.dumps(relevant_rooms, ensure_ascii=False)}

        YÊU CẦU XỬ LÝ:
        1. Tự động nhận diện tất cả tên cột (dù bằng Tiếng Việt, Tiếng Anh hay viết tắt) để trích xuất 19 trường thông tin.
        2. Nếu trường thông tin nào thiếu hoặc rỗng trong Excel, hãy để giá trị là "[Chưa cập nhật]".
        3. Các trường dạng Có/Không (điều hòa, nóng lạnh, vệ sinh riêng, pet, ban công, cửa sổ, khóa vân tay): Đưa về giá trị "Có", "Không", hoặc "[Chưa cập nhật]".
        4. Với `status`: Định dạng chuẩn là "TRỐNG" hoặc "ĐÃ CHO THUÊ".
        5. Xác định `action`: 
           - "ADD_ROOM": Nếu đây là dòng dữ liệu thêm phòng mới hoặc cập nhật thông tin phòng.
           - "UPDATE_STATUS": Nếu dòng này chỉ nhằm mục đích đổi trạng thái phòng.
           - "SEARCH_ROOM": Nếu thông tin mang tính chất truy vấn.

        TRẢ VỀ DUY NHẤT 1 CHUỖI JSON ĐÚNG CẤU TRÚC:
        {{
          "action": "ADD_ROOM | SEARCH_ROOM | UPDATE_STATUS",
          "extracted_data": {{
            "address": "Địa chỉ phòng trọ đầy đủ...",
            "room_name": "Tên/Số phòng...",
            "price": "Giá thuê...",
            "floor": "Tầng...",
            "is_private_bathroom": "Có/Không/[Chưa cập nhật]",
            "has_ac": "Có/Không/[Chưa cập nhật]",
            "has_heater": "Có/Không/[Chưa cập nhật]",
            "has_washer": "Có/Không/[Chưa cập nhật]",
            "allow_pets": "Có/Không/[Chưa cập nhật]",
            "has_balcony": "Có/Không/[Chưa cập nhật]",
            "has_window": "Có/Không/[Chưa cập nhật]",
            "has_fingerprint_lock": "Có/Không/[Chưa cập nhật]",
            "parking_info": "Thông tin để xe...",
            "max_occupants": "Số người ở tối đa...",
            "other_amenities": "Tiện ích khác...",
            "service_fees": "Phí dịch vụ...",
            "status": "TRỐNG hoặc ĐÃ CHO THUÊ",
            "move_in_date": "Ngày có thể chuyển vào...",
            "media_urls": "Các link ảnh/video phân cách bởi dấu phẩy (nếu có)"
          }},
          "ai_reply": "Tóm tắt ngắn gọn thông tin phòng đã chuẩn hóa."
        }}
        """

    try:
        response = gemini_client.models.generate_content(
            model="models/gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        if response and response.text:
            raw_text = response.text.strip()
            print(f"raw_text: {raw_text}")
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```(?:json)?\n?", "", raw_text)
                raw_text = re.sub(r"\n?```$", "", raw_text)
            return json.loads(raw_text)
    except Exception as e:
        print(f"⚠️ Lỗi AI Validation dòng dữ liệu Excel: {e}")
        return None

def process_excel_file(file_url: str, sender_id: str) -> str:
    try:
        res = requests.get(file_url, timeout=30)
        if res.status_code != 200:
            return 0
        temp_file = "temp_rooms.xlsx"
        with open(temp_file, "wb") as f:
            f.write(res.content)

        df = pd.read_excel(temp_file).fillna("[Chưa cập nhật]")
        success_count, fail_count, ai_rejected_count = 0, 0, 0
        for _, row in df.iterrows():
            validated_data = ai_validate_and_extract_room(row.to_dict())
            extracted = validated_data.get("extracted_data", {}) if validated_data else {}
            raw_address = str(extracted.get("address") or "").strip()

            # Kiểm tra nếu địa chỉ rỗng hoặc rơi vào các từ khóa "thiếu dữ liệu"
            if not raw_address or raw_address.lower() in ["[chưa cập nhật]", "none", "null", "chưa rõ", ""]:
                ai_rejected_count += 1
                continue

            
            if upsert_room_to_db(data=extracted):
                success_count += 1
            else:
                fail_count += 1
        
        if os.path.exists(temp_file):
            os.remove(temp_file)
        return f"AI đã xử lý xong! Thành công: {success_count} phòng. (Bỏ qua {ai_rejected_count} dòng lỗi, {fail_count} lỗi DB)."
    except Exception as e:
        print("❌ [EXCEL PROCESS ERROR]:", e)
        return 0

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
    
    phone = get_phone_by_user_id(db, user_id)
    try:
        relevant_rooms = search_rooms_by_vector(message_text, top_k=5)
        system_prompt = f"""
        Bạn là Trợ lý AI Quản lý và Tư vấn Phòng trọ thông minh trên Zalo.
        Phân tích tin nhắn người dùng và trích xuất đúng 13 trường thông tin:

        1. `address`: Địa chỉ 4 cấp đầy đủ (Số nhà/Đường, Phường/Xã, Quận/Huyện, Tỉnh/Thành phố).
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
        12. `status`: Trạng thái phòng ("TRỐNG" hoặc "ĐÃ CHO THUÊ").
        13. `move_in_date`: Ngày có thể chuyển vào phòng để ở
`       
        DỮ LIỆU ĐẦU VÀO:
        - Tin nhắn: "{message_text}"
        - Media kèm theo: {json.dumps(all_current_media)}
        - Phòng khớp từ Vector Search: {json.dumps(relevant_rooms, ensure_ascii=False)}

        YÊU CẦU TRẢ VỀ JSON:
        - Nếu thiếu thông tin trường nào, đặt giá trị là "[Chưa cập nhật]".
        - Trình bày `ai_reply` đẹp mắt, sạch sẽ để gửi lại trên Zalo cho người dùng. ĐỪNG ĐÂM ĐƯỜNG LINK HÌNH ÁNH VÀO CÂU TRẢ LỜI, hình ảnh sẽ được hệ thống hiển thị đính kèm tự động.

        QUY TẮC PHÂN LOẠI ACTION:
        - "ADD_ROOM": Dùng khi người dùng ĐĂNG PHÒNG MỚI hoặc CẬP NHẬT/SỬA BẤT KỲ THÔNG TIN NÀO CỦA PHÒNG (Địa chỉ, Giá, Tiện ích, Ảnh, Tầng...).
        - "UPDATE_STATUS": CHỈ DÙNG khi người dùng báo phòng "ĐÃ CHO THUÊ", "ĐÃ CÓ NGƯỜI BẮT", "ĐÃ CHỐT" hoặc "ĐỔI SANG TRỐNG".
        - "SEARCH_ROOM": Dùng khi khách tìm kiếm phòng.

        TRẢ VỀ DUY NHẤT 1 CHUỖI JSON ĐÚNG CẤU TRÚC:
        {{
          "action": "ADD_ROOM | SEARCH_ROOM | UPDATE_STATUS",
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
        result_data = json.loads(raw_text)
        ai_reply = result_data.get("ai_reply", "Dạ em đã ghi nhận thông tin rồi ạ!")
        action = result_data.get("action")
        extracted = result_data.get("extracted_data", {})
        existing_point_id = relevant_rooms[0].get("id") if (relevant_rooms and len(relevant_rooms) > 0) else None
        print(f"action: {action}")
        if action == "SEARCH_ROOM":
            print(f"message_text: {message_text}")
            # 1. Lấy toàn bộ phòng khả dụng từ Vector Search hoặc DB (Top 50 phòng)
            search_results = search_rooms_by_vector(message_text, top_k=50) or []
            
            print(f"search_results: {search_results}")
            if not search_results:
                ai_reply = "Dạ hiện tại bên em chưa có phòng trống nào phù hợp với yêu cầu của anh/chị ạ!"
            else:
                # 4. Yêu cầu Gemini định dạng hiển thị danh sách: CHỈ hiển thị thông tin CÓ, ẩn hoàn toàn thông tin thiếu
                format_prompt = f"""
                Bạn là Trợ lý AI Tư vấn Tìm kiếm Phòng trọ trên Zalo.
                Nhiệm vụ: Định dạng danh sách {len(search_results)} phòng rẻ nhất dưới đây thành 1 tin nhắn phản hồi hoàn chỉnh cho khách hàng.

                DANH SÁCH {len(search_results)} PHÒNG RẺ NHẤT TRONG HỆ THỐNG:
                {json.dumps(search_results, ensure_ascii=False)}

                QUY TẮC HIỂN THỊ CỰC KỲ QUAN TRỌNG:
                1. **ẨN HOÀN TOÀN THÔNG TIN THIẾU**:
                   - Tuyệt đối KHÔNG hiển thị các dòng chứa "[Chưa cập nhật]", "Không", "Chưa rõ", "null", hoặc khoảng trắng.
                   - Chỉ liệt kê các tiện ích/thông tin thực sự CÓ (Ví dụ: Nếu phòng có điều hòa thì ghi "• Có điều hòa", nếu không có hoặc chưa cập nhật thì BỎ HẲN dòng đó).
                2. **ĐỊNH DẠNG NGẮN GỌN CHO ZALO**:
                   - Mở đầu bằng 1 câu chào thân thiện.
                   - Đánh số thứ tự từng phòng (1, 2, 3...).
                   - Mỗi phòng liệt kê ngắn gọn: Tên/Số phòng, Địa chỉ, Giá thuê, và danh sách tiện ích có sẵn.
                   - Dùng icon/emoji sinh động. KHÔNG chèn bất kỳ đường link ảnh nào.

                TRẢ VỀ DUY NHẤT 1 CHUỖI JSON:
                {{
                  "ai_reply": "Nội dung danh sách phòng đã định dạng ngắn gọn..."
                }}
                """
                formatted_raw = generate_content_with_retry(format_prompt, mime_type="application/json")
                if formatted_raw:
                    formatted_json = json.loads(formatted_raw)
                    ai_reply = formatted_json.get("ai_reply", ai_reply)

            # Vì là tìm kiếm nên không đính kèm media đăng phòng của người dùng
            urls_to_send = []
        
        elif action == "ADD_ROOM":
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

                success = upsert_room_to_db(data=extracted, media_urls=all_current_media, point_id=None)
                if success:
                    get_get_and_clear_pending_media(user_id)
                   
        elif action == "UPDATE_STATUS" and existing_point_id:
            new_status = extracted.get("status") or "ĐÃ CHO THUÊ"
            update_room_status_in_db(point_id=existing_point_id, new_status=new_status, zalo_user_id=user_id)

    except Exception as err:
        print("❌ [AI Logic Exception]:", err)
        ai_reply = "Dạ hệ thống đang bận một chút, anh/chị chờ em vài giây rồi nhắn lại giúp em nhé!"
    print(f"user_id: {user_id}")
    print(f"ai_reply: {ai_reply}")
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
    
def ai_search_rooms(user_message: str, database_rooms: list) -> Optional[dict]:
    """
    Hàm xử lý tìm kiếm phòng trọ trên Zalo.
    - Lọc và sắp xếp lấy 20 phòng rẻ nhất trong DB.
    - AI định dạng văn bản gửi cho khách: chỉ hiển thị thông tin CÓ, ẩn hoàn toàn thông tin thiếu.
    """
    if not gemini_client:
        print("⚠️ Gemini Client chưa được khởi tạo!")
        return None

    if not database_rooms:
        return {
            "action": "SEARCH_ROOM",
            "ai_reply": "Rất tiếc, hiện tại hệ thống chưa có dữ liệu phòng trọ phù hợp."
        }

    # 1. Hàm hỗ trợ trích xuất giá tiền dạng số để sắp xếp chính xác bằng Python
    def parse_price(room):
        try:
            p_str = str(room.get("price", "999999999")).lower().replace(",", ".")
            num = float(re.search(r"[\d.]+", p_str).group())
            if "triệu" in p_str or "tr" in p_str:
                num *= 1_000_000
            elif "k" in p_str:
                num *= 1_000
            return num
        except Exception:
            return 999_999_999  # Nếu không đọc được giá thì đẩy xuống cuối danh sách

    # 2. Lọc chỉ lấy các phòng còn "TRỐNG", sắp xếp từ giá thấp đến cao và lấy đúng 20 phòng rẻ nhất
    available_rooms = [r for r in database_rooms if str(r.get("status", "")).upper() != "ĐÃ CHO THUÊ"]
    top_20_cheapest_rooms = sorted(available_rooms, key=parse_price)[:20]

    # Clean dữ liệu trước khi gửi vào prompt
    clean_rooms = [
        {k: (None if pd.isna(v) else v) for k, v in room.items()} 
        for room in top_20_cheapest_rooms
    ]

    # 3. Prompt yêu cầu Gemini tạo câu trả lời ngắn gọn
    prompt = f"""
        Bạn là Trợ lý AI Tư vấn Tìm kiếm Phòng trọ trên Zalo.
        Nhiệm vụ: Trình bày danh sách 20 phòng rẻ nhất dưới đây để gửi phản hồi cho khách hàng trên Zalo.

        YÊU CẦU CỦA KHÁCH HÀNG:
        "{user_message}"

        DANH SÁCH 20 PHÒNG RẺ NHẤT TRONG DATABASE:
        {json.dumps(clean_rooms, ensure_ascii=False)}

        QUY TẮC BẮT BUỘC KHI TẠO `ai_reply`:
        1. **CHỈ HIỂN THỊ THÔNG TIN CÓ**:
           - Tuyệt đối KHÔNG hiển thị các trường ghi "[Chưa cập nhật]", "Không", "Chưa rõ", "null", hoặc rỗng.
           - Chỉ trình bày các thông tin thực sự CÓ của phòng (Ví dụ: Nếu phòng có Điều hòa thì ghi "• Điều hòa", nếu không có hoặc chưa cập nhật thì BỎ HOÀN TOÀN dòng đó).
        2. **ĐỊNH DẠNG NGẮN GỌN CHO ZALO**:
           - Mở đầu bằng 1 câu chào ngắn gọn nhẹ nhàng.
           - Đánh số thứ tự từng phòng (1, 2, 3...).
           - Mỗi phòng trình bày ngắn gọn: Tên/Số phòng, Địa chỉ, Giá, và danh sách tiện ích có sẵn.
           - ĐƯỢC CHÈN icon/emoji nhẹ nhàng cho dễ đọc.
           - KHÔNG chèn bất kỳ đường link hình ảnh nào vào văn bản `ai_reply`.

        TRẢ VỀ DUY NHẤT 1 CHUỖI JSON ĐÚNG CẤU TRÚC:
        {{
          "action": "SEARCH_ROOM",
          "total_found": {len(top_20_cheapest_rooms)},
          "ai_reply": "Văn bản danh sách tối đa 20 phòng rẻ nhất cực kỳ ngắn gọn, đẹp mắt trên Zalo, chỉ chứa thông tin CÓ, ẩn toàn bộ thông tin thiếu."
        }}
        """

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
        print(f"⚠️ Lỗi AI Search Rooms: {e}")
        return None
        
def parse_room_price(room: dict) -> float:
    """Hàm phụ trợ ép kiểu giá phòng về dạng số float để sắp xếp chính xác."""
    try:
        p_str = str(room.get("price", "999999999")).lower().replace(",", ".")
        match = re.search(r"[\d.]+", p_str)
        if not match:
            return 999_999_999.0
        num = float(match.group())
        if "triệu" in p_str or "tr" in p_str:
            num *= 1_000_000
        elif "k" in p_str:
            num *= 1_000
        return num
    except Exception:
        return 999_999_999.0