import os
import json
import requests
import uuid
import time
import io
from typing import Dict, List, Optional
import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from google import genai
from google.genai import types
import cloudinary
import cloudinary.uploader
import pandas as pd
from datetime import datetime, date
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
import re
from qdrant_client.http import models
import pytz

from user_login import router as auth_router # 👈 Import router
app = FastAPI()
app.include_router(auth_router)

app = FastAPI()

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

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "123456")

# --- 0. BỘ NHỚ ĐỆM TẠM THỜI (PENDING MEDIA CACHE) ---
PENDING_MEDIA_CACHE: Dict[str, dict] = {}
CACHE_TTL_SECONDS = 600  # Bộ nhớ đệm tự hủy sau 10 phút

# --- API ĐĂNG NHẬP & ĐỔI MẬT KHẨU ---
class AdminLoginSchema(BaseModel):
    username: str
    password: str

class AdminChangePasswordSchema(BaseModel):
    old_password: str
    new_password: str

@app.post("/api/admin/login")
def admin_login(data: AdminLoginSchema):
    current_user = os.environ.get("ADMIN_USERNAME", ADMIN_USERNAME)
    current_pass = os.environ.get("ADMIN_PASSWORD", ADMIN_PASSWORD)

    if data.username == current_user and data.password == current_pass:
        return {"status": "success", "message": "Đăng nhập thành công!"}
    
    raise HTTPException(status_code=401, detail="Tài khoản hoặc mật khẩu không chính xác!")

@app.post("/api/admin/change-password")
def change_admin_password(data: AdminChangePasswordSchema):
    current_pass = os.environ.get("ADMIN_PASSWORD", ADMIN_PASSWORD)
    if data.old_password != current_pass:
        raise HTTPException(status_code=400, detail="Mật khẩu hiện tại không chính xác!")

    os.environ["ADMIN_PASSWORD"] = data.new_password
    return {
        "status": "success", 
        "message": "Đã cập nhật mật khẩu mới thành công!"
    }


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


    
COLLECTION_NAME = "rooms_v01"
VECTOR_SIZE = 768

qdrant_client = QdrantClient(
    url=os.environ.get("QDRANT_URL"),
    api_key=os.environ.get("QDRANT_API_KEY")
)

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

@app.get("/api/admin/rooms")
async def get_rooms_filter(
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    status: Optional[str] = None
):
    must_conditions = []

    # 1. Lọc theo khoảng giá bằng Range Query của Qdrant
    if min_price is not None or max_price is not None:
        price_range = {}
        if min_price is not None:
            price_range["gte"] = min_price  # Lớn hơn hoặc bằng min_price
        if max_price is not None:
            price_range["lte"] = max_price  # Nhỏ hơn hoặc bằng max_price

        must_conditions.append(
            models.FieldCondition(
                key="price_num",
                range=models.Range(**price_range)
            )
        )

    # 2. Lọc theo trạng thái phòng (nếu có)
    if status:
        must_conditions.append(
            models.FieldCondition(
                key="status",
                match=models.MatchValue(value=status)
            )
        )

    # Truy vấn dữ liệu từ Qdrant
    query_filter = models.Filter(must=must_conditions) if must_conditions else None
    
    records = qdrant_client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=query_filter,
        limit=50
    )[0]

    return [rec.payload for rec in records]
    
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
    
# --- SCHEMA DỮ LIỆU PHÒNG TRỌ CHUẨN ĐẦY ĐỦ THEO 8 MỤC YÊU CẦU ---
class RoomCreateUpdateSchema(BaseModel):
    # 1. Địa chỉ
    address: str
    # 2. Tên phòng
    room_name: Optional[str] = "Phòng trọ"
    # 3. Giá thuê
    price: Optional[str] = "Chưa rõ"
    # 4. Thông tin phòng chi tiết
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
    # 5. Phí dịch vụ
    service_fees: Optional[str] = "Chưa rõ"           
    # 6. Ảnh, video phòng
    media_urls: Optional[List[str]] = []
    # 7. Thời gian khách vào ở được
    move_in_date: Optional[str] = "Vào ở ngay"
    # 8. Trạng thái phòng trống hay đã cho thuê
    status: Optional[str] = "TRỐNG"
    # Thông tin liên hệ
    landlord_phone: Optional[str] = "Chưa rõ"

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
            "landlord_phone": phone,
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

# --- API ROUTES ---
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html")

@app.get("/api/rooms")
def get_all_rooms(
    limit: int = 500,
    address: Optional[str] = None,
    room_name: Optional[str] = None,
    status: Optional[str] = None,
    landlord_phone: Optional[str] = None
):
    try:
        records, _ = qdrant_client.scroll(
            collection_name=COLLECTION_NAME,
            limit=limit,
            with_payload=True,
            with_vectors=False
        )
        results = []
        
        print(" get_all_rooms", records)
        
        for r in records:
            p = r.payload or {}

            if address and address.lower() not in str(p.get("1_address", "")).lower(): continue
            if room_name and room_name.lower() not in str(p.get("2_room_name", "")).lower(): continue
            if status and p.get("8_status") != status: continue
            if landlord_phone and landlord_phone not in str(p.get("landlord_phone", "")): continue

            results.append({
                "id": r.id,
                "address": p.get("1_address"),
                "room_name": p.get("2_room_name"),
                "price": p.get("3_price"),
                "floor": p.get("4_floor"),
                "is_private_bathroom": p.get("4_is_private_bathroom"),
                "has_ac": p.get("4_has_ac"),
                "has_heater": p.get("4_has_heater"),
                "has_washer": p.get("4_has_washer"),
                "allow_pets": p.get("4_allow_pets"),
                "has_balcony": p.get("4_has_balcony"),
                "has_window": p.get("4_has_window"),
                "has_fingerprint_lock": p.get("4_has_fingerprint_lock"),
                "parking_info": p.get("4_parking_info"),
                "max_occupants": p.get("4_max_occupants"),
                "other_amenities": p.get("4_other_amenities"),
                "service_fees": p.get("5_service_fees"),
                "media_urls": p.get("6_media_urls", []),
                "move_in_date": p.get("7_move_in_date"),
                "status": p.get("8_status"),
                "landlord_phone": p.get("landlord_phone"),
                "zalo_user_id": p.get("zalo_user_id"),
                "created_at": p.get("created_at"),
                "updated_at": p.get("updated_at")
            })
        return {"status": "success", "data": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rooms")
def create_or_update_room(data: RoomCreateUpdateSchema, point_id: Optional[str] = None):
    extracted = data.dict()
    success = upsert_room_to_db(data=extracted, point_id=point_id, zalo_user_id="ADMIN_WEB")
    if success:
        return {"status": "success", "message": "Thao tác thành công!"}
    raise HTTPException(status_code=500, detail="Lỗi lưu dữ liệu.")

@app.delete("/api/rooms/{point_id}")
def delete_room_from_web(point_id: str):
    try:
        qdrant_client.delete(collection_name=COLLECTION_NAME, points_selector=[point_id], wait=True)
        return {"status": "success", "message": "Đã xóa!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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

# --- API UPLOAD EXCEL / CSV CÓ AI CHECK ---
@app.post("/api/rooms/upload-excel")
async def upload_excel_rooms(file: UploadFile = File(...)):
    print("upload_excel_rooms")
    if not file.filename.endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(status_code=400, detail="Vui lòng tải lên tệp định dạng .xlsx, .xls hoặc .csv!")

    contents = await file.read()
    try:
        if file.filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(contents))
        else:
            df = pd.read_excel(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không thể đọc file Excel/CSV: {str(e)}")

    df.fillna("Chưa rõ", inplace=True)
    success_count = 0
    fail_count = 0
    ai_rejected_count = 0

    for _, row in df.iterrows():
        raw_row_dict = row.to_dict()
        # 1. Gọi AI Kiểm tra và Chuẩn hóa dữ liệu
        validated_data = ai_validate_and_extract_room(raw_row_dict)

        # 2. Kiểm tra nếu AI phát hiện dữ liệu không hợp lệ (không có địa chỉ)
        if not validated_data or not validated_data.get("address"):
            ai_rejected_count += 1
            continue

        # 3. Tiến hành Lưu vào Database (Qdrant)
        if upsert_room_to_db(data=validated_data, zalo_user_id="EXCEL_AI_IMPORT"):
            success_count += 1
        else:
            fail_count += 1

    return {
        "status": "success",
        "message": f"AI đã xử lý xong! Thành công: {success_count} phòng. (Bỏ qua {ai_rejected_count} dòng dữ liệu lỗi/rác, {fail_count} lỗi lưu DB)."
    }
    
# --- API TẢI FILE EXCEL MẪU ---
@app.get("/api/rooms/download-template")
def download_room_template():
    # Đường dẫn tới file mẫu trong thư mục templates
    template_path = os.path.join(BASE_DIR, "templates", "Mau_Nhap_Danh_Sach_Phong.xlsx")
    
    if not os.path.exists(template_path):
        raise HTTPException(
            status_code=404, 
            detail="Không tìm thấy tệp mẫu 'Mau_Nhap_Danh_Sach_Phong.xlsx' trong thư mục templates!"
        )
    
    return FileResponse(
        path=template_path,
        filename="Mau_Nhap_Danh_Sach_Phong.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
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
) -> str:
    incoming_media_urls = []
    for item in media_items:
        saved_url = save_media_file(item["url"], is_video=item.get("is_video", False))
        if saved_url:
            incoming_media_urls.append(saved_url)

    if not message_text.strip() and incoming_media_urls:
        add_pending_media(user_id, incoming_media_urls)
        total_pending = len(PENDING_MEDIA_CACHE.get(user_id, {}).get("urls", []))
        reply = f"📸 Em đã nhận {len(incoming_media_urls)} ảnh/video! (Tổng đã nhận: {total_pending} file)\n\n👉 Anh/Chị gửi thêm thông tin phòng để em tạo bài nhé!"
        send_zalo_message(user_id, reply, media_urls=incoming_media_urls)
        return

    pending_urls = get_and_clear_pending_media(user_id)
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

        # 1. Lấy ID phòng khớp nhất từ kết quả Vector Search (nếu có)
        existing_point_id = None
        if relevant_rooms and len(relevant_rooms) > 0:
            existing_point_id = relevant_rooms[0].get("id")

        # 2. Xử lý theo Action
        if action == "ADD_ROOM":
            address = str(extracted.get("address", "")).strip()
            
            if address and address.lower() not in ["null", "none", "chưa rõ", ""]:
                # ❌ KHÔNG TRUYỀN existing_point_id KHI THÊM MỚI PHÒNG
                # Điều này đảm bảo phòng mới độc lập 100%, không bao giờ xóa nhầm các phòng cũ có địa chỉ tương tự
                upsert_room_to_db(
                    data=extracted,
                    media_urls=all_current_media,
                    point_id=None, # Ép tạo phòng độc lập
                    zalo_user_id=user_id
                )
                print(f"➕ [ADD NEW ROOM SUCCESS]: Đã thêm phòng mới tại {address}")

        elif action == "SEARCH_ROOM" and relevant_rooms:
            print(f"SEARCH_ROOM SUCCESS]")
            for room in relevant_rooms:
                m_urls = room.get("8_media_urls") or room.get("media_urls")
                if m_urls and len(m_urls) > 0:
                    urls_to_send = m_urls
                    break

        # 🔵 TRƯỜNG HỢP 2: CẬP NHẬT PHÒNG (UPDATE_ROOM)
        elif action == "UPDATE_ROOM":
            address = str(extracted.get("address", "")).strip()

            if address and address.lower() not in ["null", "none", "chưa rõ", ""]:
                # Truyền existing_point_id vào để nếu ĐỔI ĐỊA CHỈ thì sẽ xóa record cũ
                upsert_room_to_db(
                    data=extracted,
                    media_urls=all_current_media,
                    point_id=existing_point_id,
                    zalo_user_id=user_id
                )
                if success:
                    print(f"🔄 [UPDATE ROOM SUCCESS]: Cập nhật thành công phòng ID {existing_point_id}")
            else:
                print("⚠️ [UPDATE ROOM CANCELLED]: Thiếu địa chỉ phòng.")

        # 🟡 TRƯỜNG HỢP 3: CẬP NHẬT TRẠNG THÁI (UPDATE_STATUS)
        elif action == "UPDATE_STATUS":
            new_status = extracted.get("status") or "ĐÃ CHO THUÊ"

            if existing_point_id:
                # Cập nhật riêng trường status qua set_payload
                success = update_room_status_in_db(
                    point_id=existing_point_id, 
                    new_status=new_status,
                    zalo_user_id=user_id
                )
                if success:
                    print(f"✅ [UPDATE STATUS SUCCESS]: ID {existing_point_id} -> {new_status}")
            else:
                print("⚠️ [UPDATE STATUS WARN]: Không tìm thấy phòng khớp trong DB để đổi trạng thái.")

    except Exception as err:
        print("❌ [AI Logic Exception]:", err)
        ai_reply = "Dạ hệ thống đang bận một chút, anh/chị chờ em vài giây rồi nhắn lại giúp em nhé!"

    send_zalo_message(user_id, ai_reply, media_urls=urls_to_send)




@app.get("/api/admin/rooms")
async def get_rooms_filter(
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    status: Optional[str] = None # Cho phép None nếu Admin muốn xem TẤT CẢ các phòng
):
    must_conditions = []

    # 1. Lọc theo trạng thái phòng (nếu truyền lên status)
    if status:
        must_conditions.append(
            models.FieldCondition(
                key="status",
                match=models.MatchValue(value=status)
            )
        )

    # 2. Lọc theo khoảng giá
    if min_price is not None or max_price is not None:
        price_range = {}
        if min_price is not None: price_range["gte"] = min_price
        if max_price is not None: price_range["lte"] = max_price
        
        must_conditions.append(
            models.FieldCondition(key="price_num", range=models.Range(**price_range))
        )

    # 3. Lọc theo khoảng thời gian phòng trống
    # Trong API get_rooms_filter:
    if from_date or to_date:
        time_range = {}
        if from_date:
            dt_from = datetime.strptime(from_date, "%Y-%m-%d")
            dt_from_vn = VN_TZ.localize(dt_from)
            time_range["gte"] = dt_from_vn.timestamp()
            
        if to_date:
            # Lấy đến cuối ngày to_date (23:59:59)
            dt_to = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            dt_to_vn = VN_TZ.localize(dt_to)
            time_range["lte"] = dt_to_vn.timestamp()

        must_conditions.append(
            models.FieldCondition(
                key="move_in_timestamp",
                range=models.Range(**time_range)
            )
        )

    # Truy vấn Qdrant
    query_filter = models.Filter(must=must_conditions) if must_conditions else None
    
    records, _ = qdrant_client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=query_filter,
        limit=100
    )

    # Sửa lỗi cú pháp trả về dữ liệu kèm theo ID phòng
    results = []
    for rec in records:
        if rec.payload:
            payload_data = rec.payload
            payload_data["id"] = rec.id  # Gán ID để frontend thao tác sửa/xóa
            results.append(payload_data)
            
    return results

# --- 9. WEBHOOK RECEIVER ---
@app.post("/webhook/zalo")
async def zalo_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        event_name = str(data.get("event_name", "")).strip()
        sender_id = data.get("user_id_by_app") or data.get("sender", {}).get("id")

        if not sender_id:
            return {"status": "ignored"}

        if "user_follow_oa" in event_name:
            send_zalo_message(sender_id, "👋 Chào mừng bạn! Hãy gửi thông tin hoặc hình ảnh phòng trọ để bắt đầu nhé!")
            return {"status": "success"}

        # Thêm xử lý khi người dùng gửi File (.xlsx, .csv)
        if event_name == "user_send_file":
            attachments = data.get("message", {}).get("attachments", [])
            for att in attachments:
                file_payload = att.get("payload", {})
                file_url = file_payload.get("url")
                file_name = str(file_payload.get("name", "")).lower()

                # Nếu là file Excel (.xlsx hoặc .xls)
                if file_url and (file_name.endswith(".xlsx") or file_name.endswith(".xls")):
                    send_zalo_message(sender_id, "📥 Em đã nhận file Excel! Đang tiến hành cập nhật dữ liệu phòng...")
                    
                    # Chạy xử lý ngầm (Background Task)
                    def handle_excel():
                        count = process_excel_file(file_url, sender_id)
                        send_zalo_message(sender_id, f"✅ Đã nhập/cập nhật thành công {count} phòng từ file Excel vào hệ thống!")

                    background_tasks.add_task(handle_excel)
                    return {"status": "success"}
                    
        
        if event_name in ["user_send_text", "user_send_image", "user_send_file", "user_send_video"]:
            message_obj = data.get("message", {})
            text = message_obj.get("text", "")

            attachments = message_obj.get("attachments", [])
            media_items = []
            for item in attachments:
                payload = item.get("payload", {})
                media_url = payload.get("url") or payload.get("thumbnailUrl")
                if media_url:
                    media_items.append({
                        "url": media_url,
                        "is_video": (item.get("type") == "video") or ("user_send_video" in event_name)
                    })

            background_tasks.add_task(process_zalo_ai_logic, text, media_items, sender_id )
    
    
    except Exception as e:
        print("❌ [Webhook Exception]:", e)

    return {"status": "success"}



if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)