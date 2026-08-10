import os
import json
import requests
import uuid
import time
import io
import random
from typing import Dict, List, Optional
import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks, UploadFile, File, HTTPException, Depends, Header, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from google import genai
from google.genai import types
import cloudinary
import cloudinary.uploader
import pandas as pd
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
import re
from qdrant_client.http import models
import pytz
from datetime import datetime, timedelta, timezone, date
import bcrypt
# 1. Thêm declarative_base vào import
from sqlalchemy import Column, Integer, String, DateTime, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from fastapi import FastAPI, Request, Depends
from sqlalchemy.orm import Session
import phonenumbers
from phonenumbers import NumberParseException

app = FastAPI()

# --- CONFIG DATABASE (SQLAlchemy) ---
ZALO_ACCESS_TOKEN = os.environ.get("ZALO_ACCESS_TOKEN")  # Token cấp từ Zalo OA
ZALO_TEMPLATE_ID = os.environ.get("YOUR_ZNS_TEMPLATE_ID")      # ID mẫu tin nhắn OTP đã được Zalo duyệt

DATABASE_URL = os.environ.get("SQLALCHEMY_DATABASE_URL")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Model lưu OTP và Mapping User ID - SĐT
class UserWeb(Base):
    __tablename__ = "user_web"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, unique=True, index=True, nullable=True)
    phone = Column(String, unique=True, index=True, nullable=False)
    otp = Column(String, nullable=True)  # Cho phép NULL sau khi đăng ký thành công
    password = Column(String, nullable=True)  # Lưu mật khẩu đã hash
    expired_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    
# Ví dụ cấu hình SQLite (hoặc thay bằng URL PostgreSQL của bạn trên Render)
SQLALCHEMY_DATABASE_URL = os.environ.get("SQLALCHEMY_DATABASE_URL")
# Nếu dùng PostgreSQL: "postgresql://user:password@postgresserver/db"

engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Tự động tạo bảng otp_logs trong DB nếu chưa tồn tại
Base.metadata.create_all(bind=engine)

# Hàm Dependency để lấy DB session cho các API
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()



VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

COLLECTION_NAME = "rooms_v01"
VECTOR_SIZE = 768

qdrant_client = QdrantClient(
    url=os.environ.get("QDRANT_URL"),
    api_key=os.environ.get("QDRANT_API_KEY")
)

# --- 0. BỘ NHỚ ĐỆM TẠM THỜI (PENDING MEDIA CACHE) ---
PENDING_MEDIA_CACHE: Dict[str, dict] = {}
CACHE_TTL_SECONDS = 600  # Bộ nhớ đệm tự hủy sau 10 phút

    
# --- CẤU HÌNH MEDIA ---
MEDIA_DIR = "static/media"
os.makedirs(MEDIA_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL")
if CLOUDINARY_URL:
    cloudinary.config(cloudinary_url=CLOUDINARY_URL)

# --- CẤU HÌNH GEMINI & QDRANT ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


try:
    if not qdrant_client.collection_exists(collection_name=COLLECTION_NAME):
        qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
        )
        print(f"✅ Tạo mới collection '{COLLECTION_NAME}' thành công!")
except Exception as e:
    print("❌ Lỗi khởi tạo Qdrant:", e)

try:
    # Tạo index cho trường status và price_num để lọc trên Admin
    qdrant_client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="status",
        field_schema=models.PayloadSchemaType.KEYWORD,
    )
    qdrant_client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="price_num",
        field_schema=models.PayloadSchemaType.FLOAT,
    )
except Exception as e:
    pass
    

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

# --- API ĐĂNG NHẬP & ĐỔI MẬT KHẨU ---
class AdminLoginSchema(BaseModel):
    username: str
    password: str

class AdminChangePasswordSchema(BaseModel):
    username: Optional[str] = None  # Thêm field này vào
    old_password: str
    new_password: str
    
@app.get("/")
async def read_root(request: Request):
    # Lấy biến môi trường, mặc định là chuỗi rỗng nếu chưa cài
    zalo_oa_id = os.environ.get("ZALO_OA_ID")
    return templates.TemplateResponse(
        request=request,
        name="form_send zalo_otp.html",
        context={"ZALO_OA_ID": zalo_oa_id}
    )
    
# 2. Hàm hỗ trợ mã hóa mật khẩu đơn giản (hoặc dùng bcrypt/passlib)
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()



def send_otp_via_zalo_oa(user_zalo_id: str, otp: str, access_token: str):
    """
    Gửi tin nhắn OTP trực tiếp từ Zalo OA tới người dùng.
    :param user_zalo_id: User ID (ZID) của người dùng trên Zalo OA
    :param otp: Mã OTP cần gửi
    :param access_token: OA Access Token còn hạn
    """
    url = "https://openapi.zalo.me/v2.0/oa/message"
    
    headers = {
        "Content-Type": "application/json",
        "access_token": access_token
    }
    
    payload = {
        "recipient": {
            "user_id": user_zalo_id  # Lưu ý: Đây là Zalo User ID (ZID) người dùng cấp cho OA, không phải SĐT
        },
        "message": {
            "text": f"Mã xác thực OTP của bạn là: {otp}. Mã có hiệu lực trong 5 phút. Vui lòng không chia sẻ mã này cho ai."
        }
    }
    
    response = requests.post(url, headers=headers, data=json.dumps(payload))
    result = response.json()
    
    print("Zalo OA Send Message Result:", result)
    return result

def format_national_phone(phone_str: str, default_region: str = "VN") -> str:
    """
    Chuẩn hóa số điện thoại về chuẩn quốc gia bắt đầu bằng số 0 (Ví dụ: 0333593681).
    - Nếu nhập '84333593681' hoặc '+84333593681' -> Trả về '0333593681'
    - Nếu nhập '0333593681' -> Trả về '0333593681'
    - Nếu nhập số có khoảng trắng/dấu gạch ngang (ví dụ: '033 359 3681') -> Trả về '0333593681'
    """
    if not phone_str:
        return ""
    
    phone_str = str(phone_str).strip()
    
    # Xử lý trường hợp chuỗi bắt đầu bằng '84' mà không có dấu '+' phía trước
    # (phonenumbers mặc định sẽ coi 84... là số địa phương nếu không có dấu +)
    if phone_str.startswith("84") and not phone_str.startswith("+"):
        phone_str = "+" + phone_str

    try:
        parsed_number = phonenumbers.parse(phone_str, default_region)
        
        if phonenumbers.is_valid_number(parsed_number):
            # Lấy định dạng NATIONAL (Ví dụ với VN sẽ là "033 359 3681")
            national_format = phonenumbers.format_number(
                parsed_number, phonenumbers.PhoneNumberFormat.NATIONAL
            )
            # Loại bỏ toàn bộ ký tự không phải là số (khoảng trắng, dấu gạch ngang...)
            return re.sub(r'\D', '', national_format)
    except NumberParseException:
        pass

    # Fallback bằng Regex nếu thư viện không parse được
    clean_digits = re.sub(r'\D', '', str(phone_str))
    
    # Nếu là số Việt Nam bắt đầu bằng 84 (ví dụ 84333593681) -> Chuyển thành 0333593681
    if clean_digits.startswith("84") and len(clean_digits) == 11:
        return "0" + clean_digits[2:]
        
    return clean_digits



def parse_move_in_date(date_str: str) -> float:
    """
    Chuyển đổi các chuỗi ngày sang Unix Timestamp theo múi giờ Việt Nam (UTC+7).
    """
    now_vn = datetime.now(VN_TZ)
    
    if not date_str or str(date_str).strip().lower() in ["vào ở ngay", "ngay", "chưa rõ", "none", "nan", ""]:
        return now_vn.timestamp()

    text = str(date_str).strip()
    
    # Parse các định dạng ngày chuẩn và gán múi giờ Việt Nam
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(text, fmt)
            # Gán timezone Việt Nam vào đối tượng datetime
            dt_vn = VN_TZ.localize(dt)
            return dt_vn.timestamp()
        except ValueError:
            pass

    return now_vn.timestamp()
    
    

def parse_price_to_number(price_str: str) -> float:
    """
    Chuyển đổi chuỗi giá (ví dụ: '3.5 triệu', '3,500,000đ', '4tr5') thành số thực
    """
    if not price_str or price_str in ["Chưa rõ", "Thỏa thuận", "None", "nan"]:
        return 0.0

    text = str(price_str).lower().replace(",", ".").strip()
    
    # Trường hợp: "3.5 triệu", "3,5 tr", "4tr5"
    if "triệu" in text or "tr" in text:
        # Xử lý dạng "4tr5" -> "4.5"
        text = re.sub(r'(\d+)\s*(?:triệu|tr)\s*(\d+)', r'\1.\2', text)
        match = re.search(r'(\d+(?:\.\d+)?)', text)
        if match:
            return float(match.group(1)) * 1_000_000

    # Trường hợp: "3.500.000", "3500000"
    digits_only = re.sub(r'[^\d]', '', text)
    if digits_only:
        val = float(digits_only)
        # Nếu nhập số quá nhỏ (ví dụ nhập 3.5 thay vì 3500000)
        if val < 100: 
            return val * 1_000_000
        return val

    return 0.0
    

def get_text_embedding(text: str, retries: int = 3) -> List[float]:
    """Tạo Vector Embedding luôn ép chuẩn 768 chiều"""
    if not text or not text.strip():
        return []

    # 1. Gọi qua SDK Google GenAI với output_dimensionality=768
    if gemini_client:
        for attempt in range(retries):
            try:
                response = gemini_client.models.embed_content(
                    model="models/gemini-embedding-001",
                    contents=text,
                    config=types.EmbedContentConfig(
                        output_dimensionality=768
                    )
                )
                if response and response.embedding and response.embedding.values:
                    return response.embedding.values
            except Exception as e:
                time.sleep(1)

    # 2. Fallback gọi REST API có tham số outputDimensionality: 768
    if GEMINI_API_KEY:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={GEMINI_API_KEY}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "model": "models/gemini-embedding-001",
            "content": {"parts": [{"text": text}]},
            "outputDimensionality": 768
        }
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=10)
            res_json = res.json()
            if "embedding" in res_json and "values" in res_json["embedding"]:
                return res_json["embedding"]["values"]
        except Exception as e:
            print("❌ [REST FALLBACK ERROR]:", e)

    return []
    

def upsert_room_to_db(
    data: dict, 
    point_id: str = None, 
    zalo_user_id: str = "SYSTEM",
    media_urls: Optional[List[str]] = None  # ✅ Thêm tham số này để nhận media_urls truyền vào
) -> bool:
    try:
        address = str(data.get("address", "")).strip()
        room_name = str(data.get("room_name", "Phòng trọ")).strip()
        phone = str(data.get("landlord_phone", "Chưa rõ")).strip()

        if not address:
            return False

        # Nếu có media_urls truyền rời thì ưu tiên dùng, nếu không thì lấy từ trong dict 'data'
        if media_urls is not None:
            media_list = media_urls
        else:
            media_list = data.get("media_urls", [])

        if isinstance(media_list, str):
            media_list = [x.strip() for x in media_list.split(",") if x.strip()]

        # Gom văn bản tổng hợp để làm vector hóa tìm kiếm
        text_to_embed = f"Địa chỉ: {address} Tên phòng: {room_name} Giá: {data.get('price', '')} Đồ đạc: AC:{data.get('has_ac')}, Heater:{data.get('has_heater')}, Washer:{data.get('has_washer')}, {data.get('other_amenities', '')} Phí dịch vụ: {data.get('service_fees', '')}"
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

        # Lấy giá và tính toán timestamp
        raw_price = data.get("price", "")
        price_number = parse_price_to_number(raw_price)
        move_in_ts = parse_move_in_date(data.get("move_in_date"))

        payload = {
            "address": address,
            "room_name": room_name,
            "price": str(data.get("price", "Chưa rõ")),
            "price_num": price_number,
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
            "move_in_timestamp": move_in_ts,
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



# --- API LẤY DANH SÁCH PHÒNG LỌC THEO TÀI KHOẢN ---
# Hàm lấy thông tin user hiện tại từ Header (hoặc Cookie/Session)
def get_current_user(
    x_user_phone: str = Header(None, alias="X-User-Phone"), # Hoặc lấy từ Authorization Bearer Token
    db: Session = Depends(get_db)
) -> UserWeb:
    if not x_user_phone:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Vui lòng cung cấp số điện thoại hoặc thông tin xác thực!"
        )
    
    # Chuẩn hóa SĐT từ Header (trừ tài khoản adminpro)
    clean_phone = x_user_phone if x_user_phone == "adminpro" else format_national_phone(x_user_phone)
    # Tìm user trong CSDL
    user = db.query(UserWeb).filter(UserWeb.phone == clean_phone).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Không tìm thấy thông tin người dùng!"
        )
    return user
    

# --- HÀM AI DÙNG GEMINI KIỂM TRA & CHUẨN HÓA DỮ LIỆU ---
def ai_validate_and_extract_room(row_dict: dict) -> Optional[dict]:
    def manual_fallback(data: dict) -> Optional[dict]:
        raw_address = (
            data.get("address") or 
            data.get("Địa chỉ") or 
            data.get("1. Địa chỉ") or 
            data.get("Địa chỉ & Tên phòng") or
            data.get("1. Địa chỉ & 2. Tên phòng")
        )
        print("lỗi kết nối gemini-2.5-flash, dùng manual_fallback")
        if not raw_address or pd.isna(raw_address) or str(raw_address).strip().lower() in ["nan", "none", "null", "", "chưa rõ"]:
            return None

        return {
            "address": str(raw_address).strip(),
            "room_name": str(data.get("room_name") or data.get("Tên phòng") or data.get("2. Tên phòng") or "Phòng trọ").strip(),
            "price": str(data.get("price") or data.get("Giá thuê") or data.get("3. Giá thuê") or "Chưa rõ").strip(),
            "floor": str(data.get("floor") or data.get("Tầng") or "Chưa rõ").strip(),
            "is_private_bathroom": str(data.get("is_private_bathroom") or data.get("WC Khép kín") or "Chưa rõ").strip(),
            "has_ac": str(data.get("has_ac") or data.get("Điều hòa") or "Chưa rõ").strip(),
            "has_heater": str(data.get("has_heater") or data.get("Nóng lạnh") or "Chưa rõ").strip(),
            "has_washer": str(data.get("has_washer") or data.get("Máy giặt") or "Chưa rõ").strip(),
            "allow_pets": str(data.get("allow_pets") or data.get("Nuôi chó mèo") or "Chưa rõ").strip(),
            "has_balcony": str(data.get("has_balcony") or data.get("Ban công") or "Chưa rõ").strip(),
            "has_window": str(data.get("has_window") or data.get("Cửa sổ") or "Chưa rõ").strip(),
            "has_fingerprint_lock": str(data.get("has_fingerprint_lock") or data.get("Khóa vân tay") or "Chưa rõ").strip(),
            "parking_info": str(data.get("parking_info") or data.get("Chỗ để xe") or "Chưa rõ").strip(),
            "max_occupants": str(data.get("max_occupants") or data.get("Ở tối đa") or "Chưa rõ").strip(),
            "other_amenities": str(data.get("other_amenities") or data.get("Nội thất") or "Chưa rõ").strip(),
            "service_fees": str(data.get("service_fees") or data.get("5. Phí dịch vụ") or "Chưa rõ").strip(),
            "move_in_date": str(data.get("move_in_date") or data.get("7. Ngày ở") or "Vào ở ngay").strip(),
            "status": str(data.get("status") or data.get("8. Trạng thái") or "TRỐNG").strip(),
            "landlord_phone": str(data.get("landlord_phone") or data.get("SĐT Chủ nhà") or "Chưa rõ").strip(),
            "media_urls": []
        }

    if not gemini_client:
        return manual_fallback(row_dict)

    prompt = f"""
    Bạn là một trợ lý AI kiểm định dữ liệu phòng trọ. 
    Hãy phân tích dữ liệu đầu vào từ 1 dòng file Excel sau đây và chuyển thành JSON chuẩn.

    Dữ liệu đầu vào:
    {json.dumps(row_dict, ensure_ascii=False)}

    Yêu cầu chuẩn hóa:
    - "address": Địa chỉ chi tiết (nếu không có địa chỉ cụ thể hoặc dữ liệu trống/rác, trả về null).
    - "room_name": Tên/số phòng (mặc định "Phòng trọ" nếu thiếu).
    - "price": Giá thuê.
    - "is_private_bathroom", "has_ac", "has_heater", "has_washer", "allow_pets", "has_balcony", "has_window", "has_fingerprint_lock": Trả về đúng 1 trong 3 giá trị: "Có", "Không", hoặc "Chưa rõ".
    - "status": Trả về "TRỐNG" hoặc "ĐÃ CHO THUÊ".

    Format JSON duy nhất:
    {{
        "address": string hoặc null,
        "room_name": string,
        "price": string,
        "floor": string,
        "is_private_bathroom": string,
        "has_ac": string,
        "has_heater": string,
        "has_washer": string,
        "allow_pets": string,
        "has_balcony": string,
        "has_window": string,
        "has_fingerprint_lock": string,
        "parking_info": string,
        "max_occupants": string,
        "other_amenities": string,
        "service_fees": string,
        "move_in_date": string,
        "status": string,
        "landlord_phone": string,
        "media_urls": list
    }}
    """
    
    try:
        response = gemini_client.models.generate_content(
            model="models/gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        print("Kết nối thành công gemini-2.5-flash")
        if response and response.text:
            raw_text = response.text.strip()
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```(?:json)?\n?", "", raw_text)
                raw_text = re.sub(r"\n?```$", "", raw_text)
                
            return json.loads(raw_text)
            
    except Exception as e:
        print("⚠️ Lỗi AI Validation, chuyển sang Fallback thủ công:", e)
    
    return manual_fallback(row_dict)
    
    
# --- 7. HÀM QUẢN LÝ PENDING MEDIA CACHE ---
def get_and_clear_pending_media(user_id: str) -> list:
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




# --- 5. TƯƠNG TÁC DATABASE QDRANT (CẬP NHẬT QUERY_POINTS) ---
# --- SỬA HÀM search_rooms_by_vector ---
def search_rooms_by_vector(query_text: str, top_k: int = 5) -> List[dict]:
    query_vector = get_text_embedding(query_text)
    
    if not query_vector:
        try:
            records, _ = qdrant_client.scroll(collection_name=COLLECTION_NAME, limit=top_k, with_payload=True)
            results = []
            for rec in records:
                if rec.payload:
                    p = rec.payload
                    p["id"] = rec.id  # <--- GÁN THÊM ID VÀO DỰ PHÒNG
                    results.append(p)
            return results
        except Exception as e:
            print("❌ Lỗi scroll dự phòng:", e)
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
                p["id"] = hit.id  # <--- THÊM DÒNG NÀY: Lưu lại Point ID thực tế của Qdrant
                results.append(p)
        return results
    except Exception as e:
        print("❌ [VECTOR SEARCH ERROR]:", e)
        return []

# --- 4. TƯƠNG TÁC GEMINI GENERATIVE ---
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
                    model=model_name,
                    contents=prompt,
                    config=config
                )
                if response and response.text:
                    return response.text.strip()

            except Exception as e:
                err_msg = str(e)
                if "503" in err_msg or "429" in err_msg or "UNAVAILABLE" in err_msg:
                    time.sleep(2 ** attempt)
                else:
                    print(f"❌ [GEMINI ERROR]: Model '{model_name}': {e}")
                    break

    return ""
    
    
    



def send_zalo_message(user_id: str, ai_reply: str, media_urls: list = None):
    access_token = os.environ.get("ZALO_ACCESS_TOKEN")
    if not access_token:
        return False

    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    headers = {
        "Content-Type": "application/json",
        "access_token": access_token
    }

    attachment_id = None
    if media_urls and len(media_urls) > 0:
        # Lấy URL ảnh đầu tiên upload lên Zalo
        attachment_id = upload_image_to_zalo(media_urls[0])

    # Gửi tin nhắn dạng Media Template để Zalo tự render ảnh ra màn hình
    if attachment_id:
        payload = {
            "recipient": {"user_id": user_id},
            "message": {
                "text": ai_reply,
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "media",
                        "elements": [
                            {
                                "media_type": "image",
                                "attachment_id": attachment_id
                            }
                        ]
                    }
                }
            }
        }
    else:
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
        
        
        
        

# --- 8. LUỒNG XỬ LÝ AI VÀ LOGIC DỮ LIỆU PHÒNG ---
def process_zalo_ai_logic(
    message_text: str, 
    media_items: list = None, 
    user_id: str = "SYSTEM",
):
    incoming_media_urls = []
    for item in (media_items or []):
        saved_url = save_media_file(item["url"], is_video=item.get("is_video", False))
        if saved_url:
            incoming_media_urls.append(saved_url)

    # 1. Nếu chỉ gửi Media mà không gửi chữ -> Thêm vào Cache và phản hồi
    if not message_text.strip() and incoming_media_urls:
        add_pending_media(user_id, incoming_media_urls)
        total_pending = len(PENDING_MEDIA_CACHE.get(user_id, {}).get("urls", []))
        reply = f"📸 Em đã nhận {len(incoming_media_urls)} ảnh/video! (Tổng đã nhận: {total_pending} file)\n\n👉 Anh/Chị gửi thêm thông tin phòng để em tạo bài nhé!"
        send_zalo_message(user_id, reply, media_urls=incoming_media_urls)
        return

    # 2. Lấy danh sách Media (Xem trước, CHƯA XÓA Cache)
    cached_data = PENDING_MEDIA_CACHE.get(user_id, {})
    pending_urls = cached_data.get("urls", []) if (time.time() - cached_data.get("timestamp", 0) <= CACHE_TTL_SECONDS) else []
    all_current_media = list(dict.fromkeys(pending_urls + incoming_media_urls))
    urls_to_send = all_current_media

    try:
        relevant_rooms = search_rooms_by_vector(message_text, top_k=5)

        system_prompt = f"""
Bạn là Trợ lý AI Quản lý và Tư vấn Phòng trọ thông minh trên Zalo.
Phân tích tin nhắn người dùng và trích xuất đúng 12 trường thông tin:

1. `address`: Địa chỉ 4 cấp đầy đủ (Số nhà/Đường, Phường/Xã, Quận/Huyện, Tỉnh/Thành phố).
2. `room_name`: Tên hoặc số phòng (VD: Phòng 301, Phòng tầng 2...).
3. `floor`: Tầng bao nhiêu.
4. `price`: Giá thuê.
5. `is_private_bathroom`: Khép kín hay không.
6. `appliances`: Có điều hòa, nóng lạnh, máy giặt không.
7. `allow_pets`: Cho nuôi chó mèo không.
8. `media_urls`: Danh sách ảnh/video.
9. `move_in_date`: Thời gian vào ở được.
10. `has_balcony`: Có ban công không.
11. `has_window`: Có cửa sổ không.
12. `status`: Trạng thái phòng ("TRỐNG" hoặc "ĐÃ CHO THUÊ").

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
    "action": "ADD_ROOM" | "SEARCH_ROOM" | "UPDATE_STATUS",
    "extracted_data": {{
        "address": "...",
        "room_name": "...",
        "floor": "...",
        "price": "...",
        "is_private_bathroom": "...",
        "appliances": "...",
        "allow_pets": "...",
        "move_in_date": "...",
        "has_balcony": "...",
        "has_window": "...",
        "status": "TRỐNG",
        "landlord_phone":"..."
    }},
    "ai_reply": "Mô tả chi tiết 12 thông tin dạng văn bản đẹp mắt..."
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
                success = upsert_room_to_db(
                    data=extracted,
                    media_urls=all_current_media,
                    point_id=None, 
                    zalo_user_id=user_id
                )
                if success:
                    # ✅ CHỈ XÓA CACHE KHI ĐÃ LƯU DB THÀNH CÔNG
                    get_and_clear_pending_media(user_id)
                    print(f"➕ [ADD NEW ROOM SUCCESS]: Đã thêm phòng mới tại {address}")

        elif action == "SEARCH_ROOM" and relevant_rooms:
            for room in relevant_rooms:
                m_urls = room.get("media_urls")
                if m_urls and len(m_urls) > 0:
                    urls_to_send = m_urls
                    break

        elif action == "UPDATE_ROOM":
            address = str(extracted.get("address", "")).strip()
            if address and address.lower() not in ["null", "none", "chưa rõ", ""]:
                success = upsert_room_to_db(
                    data=extracted,
                    media_urls=all_current_media,
                    point_id=existing_point_id,
                    zalo_user_id=user_id
                )
                if success:
                    get_and_clear_pending_media(user_id)
                    print(f"🔄 [UPDATE ROOM SUCCESS]: Cập nhật thành công phòng ID {existing_point_id}")

        elif action == "UPDATE_STATUS":
            new_status = extracted.get("status") or "ĐÃ CHO THUÊ"
            if existing_point_id:
                success = update_room_status_in_db(
                    point_id=existing_point_id, 
                    new_status=new_status,
                    zalo_user_id=user_id
                )

    except Exception as err:
        print("❌ [AI Logic Exception]:", err)
        ai_reply = "Dạ hệ thống đang bận một chút, anh/chị chờ em vài giây rồi nhắn lại giúp em nhé!"

    send_zalo_message(user_id, ai_reply, media_urls=urls_to_send)





def process_excel_file(file_url: str, sender_id: str) -> int:
    """Tải file Excel, đọc dữ liệu từng dòng và lưu vào Qdrant"""
    try:
        # 1. Tải file về tạm thời
        res = requests.get(file_url, timeout=30)
        if res.status_code != 200:
            return 0
            
        temp_file = "temp_rooms.xlsx"
        with open(temp_file, "wb") as f:
            f.write(res.content)

        # 2. Đọc file bằng Pandas (chuyển các ô trống thành chuỗi "")
        df = pd.read_excel(temp_file).fillna("[Chưa cập nhật]")
        
        success_count = 0
        
        # 3. Duyệt từng dòng trong Excel
        for _, row in df.iterrows():
            # Áp vào cấu hình 12 trường thông tin
            extracted_data = {
                "address": str(row.get("Địa chỉ", "")).strip(),
                "room_name": str(row.get("Tên phòng", "Phòng trọ")).strip(),
                "floor": str(row.get("Tầng", "[Chưa cập nhật]")),
                "price": str(row.get("Giá", "[Chưa cập nhật]")),
                "is_private_bathroom": str(row.get("Vệ sinh", "[Chưa cập nhật]")),
                "appliances": str(row.get("Thiết bị", "[Chưa cập nhật]")),
                "allow_pets": str(row.get("Thú cưng", "[Chưa cập nhật]")),
                "move_in_date": str(row.get("Ngày vào ở", "[Chưa cập nhật]")),
                "has_balcony": str(row.get("Ban công", "[Chưa cập nhật]")),
                "has_window": str(row.get("Cửa sổ", "[Chưa cập nhật]")),
                "status": str(row.get("Trạng thái", "TRỐNG"))
            }

            # Lấy link ảnh/video nếu cột đó có dữ liệu
            media_raw = str(row.get("Media", ""))
            media_urls = [url.strip() for url in media_raw.split(",") if url.strip() and url != "[Chưa cập nhật]"]

            if extracted_data["address"] and extracted_data["address"] != "[Chưa cập nhật]":
                # Gọi hàm upsert để cập nhật hoặc thêm mới vào Qdrant
                if upsert_room_to_db(extracted_data, media_urls, point_id=None , zalo_user_id=sender_id):
                    success_count += 1

        # Xóa file tạm
        if os.path.exists(temp_file):
            os.remove(temp_file)

        return success_count

    except Exception as e:
        print("❌ [EXCEL PROCESS ERROR]:", e)
        return 0
        
        
        


# --- SỬA HÀM update_room_status_in_db ---
def update_room_status_in_db(point_id: str, new_status: str, zalo_user_id: str = "SYSTEM") -> bool:
    """Cập nhật trạng thái phòng dựa trên Point ID thực tế tìm được"""
    if not point_id:
        print("❌ [UPDATE STATUS ERROR]: Không có point_id hợp lệ")
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
        print(f"🎉 [UPDATE STATUS SUCCESS]: Point ID {point_id} -> '{new_status}'")
        return True
    except Exception as e:
        print("❌ [UPDATE STATUS ERROR]:", e)
        return False

# --- 2. HÀM LƯU FILE MEDIA VĨNH VIỄN ---
def save_media_file(zalo_media_url: str, is_video: bool = False) -> str:
    if not zalo_media_url:
        return ""

    resource_type = "video" if is_video else "image"

    if CLOUDINARY_URL:
        try:
            res = cloudinary.uploader.upload(
                zalo_media_url,
                resource_type=resource_type,
                folder="zalo_room_media"
            )
            url = res.get("secure_url", "")
            if url:
                print(f"✅ [Cloudinary Upload Success]: {url}")
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
            local_url = f"{server_domain}/static/media/{filename}"
            print(f"✅ [Local Media Saved]: {local_url}")
            return local_url
    except Exception as e:
        print(f"❌ [Local Save Error]: {e}")

    return zalo_media_url