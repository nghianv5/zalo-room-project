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
from datetime import datetime
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

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
        "message": "Đã cập nhật mật khẩu tạm thời thành công!"
    }

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

COLLECTION_NAME = "rooms_v16_structured"
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

# --- SCHEMA DỮ LIỆU PHÒNG TRỌ CHUẨN ĐẦY ĐỦ THEO 8 MỤC YÊU CẦU ---
class RoomCreateUpdateSchema(BaseModel):
    # 1. Địa chỉ
    address: str
    # 2. Tên phòng
    room_name: Optional[str] = "Phòng trọ"
    # 3. Giá thuê
    price: Optional[str] = "Chưa rõ"
    # 4. Thông tin phòng chi tiết
    floor: Optional[str] = "Chưa rõ"                  # Tầng bao nhiêu
    is_private_bathroom: Optional[str] = "Chưa rõ"   # Phòng khép kín hay không (có WC riêng không)
    has_ac: Optional[str] = "Chưa rõ"                 # Có điều hoà không
    has_heater: Optional[str] = "Chưa rõ"             # Có nóng lạnh không
    has_washer: Optional[str] = "Chưa rõ"             # Có máy giặt không
    allow_pets: Optional[str] = "Chưa rõ"             # Có được nuôi chó mèo không
    has_balcony: Optional[str] = "Chưa rõ"            # Có ban công không
    has_window: Optional[str] = "Chưa rõ"             # Có cửa sổ không
    has_fingerprint_lock: Optional[str] = "Chưa rõ"   # Có khoá vân tay không
    parking_info: Optional[str] = "Chưa rõ"          # Có chỗ để xe máy, oto không
    max_occupants: Optional[str] = "Chưa rõ"         # Ở tối đa bao nhiêu người
    other_amenities: Optional[str] = "Chưa rõ"       # Các thông tin/tiện ích khác
    # 5. Phí dịch vụ
    service_fees: Optional[str] = "Chưa rõ"           # Điện, nước, internet, vệ sinh...
    # 6. Ảnh, video phòng
    media_urls: Optional[List[str]] = []
    # 7. Thời gian khách vào ở được
    move_in_date: Optional[str] = "Vào ở ngay"
    # 8. Trạng thái phòng trống hay đã cho thuê
    status: Optional[str] = "TRỐNG"
    # Thông tin liên hệ
    landlord_phone: Optional[str] = "Chưa rõ"

def get_text_embedding(text: str, retries: int = 3) -> List[float]:
    if not text or not text.strip():
        return []
    if gemini_client:
        for attempt in range(retries):
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
    return []

def upsert_room_to_db(data: dict, point_id: str = None, zalo_user_id: str = "SYSTEM") -> bool:
    try:
        address = data.get("address", "")
        room_name = data.get("room_name", "Phòng trọ")
        phone = data.get("landlord_phone", "Chưa rõ")

        # Gom văn bản tổng hợp để làm vector hóa tìm kiếm
        text_to_embed = f"Địa chỉ: {address} Tên phòng: {room_name} Giá: {data.get('price', '')} Đồ đạc: AC:{data.get('has_ac')}, Heater:{data.get('has_heater')}, Washer:{data.get('has_washer')}, {data.get('other_amenities', '')} Phí dịch vụ: {data.get('service_fees', '')}"
        vector = get_text_embedding(text_to_embed)
        if not vector:
            return False

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

        # Lưu đầy đủ chuẩn xác 8 mục dữ liệu vào Database Qdrant Payload
        payload = {
            "1_address": address,
            "2_room_name": room_name,
            "3_price": data.get("price", "Chưa rõ"),
            "4_floor": data.get("floor", "Chưa rõ"),
            "4_is_private_bathroom": data.get("is_private_bathroom", "Chưa rõ"),
            "4_has_ac": data.get("has_ac", "Chưa rõ"),
            "4_has_heater": data.get("has_heater", "Chưa rõ"),
            "4_has_washer": data.get("has_washer", "Chưa rõ"),
            "4_allow_pets": data.get("allow_pets", "Chưa rõ"),
            "4_has_balcony": data.get("has_balcony", "Chưa rõ"),
            "4_has_window": data.get("has_window", "Chưa rõ"),
            "4_has_fingerprint_lock": data.get("has_fingerprint_lock", "Chưa rõ"),
            "4_parking_info": data.get("parking_info", "Chưa rõ"),
            "4_max_occupants": data.get("max_occupants", "Chưa rõ"),
            "4_other_amenities": data.get("other_amenities", "Chưa rõ"),
            "5_service_fees": data.get("service_fees", "Chưa rõ"),
            "6_media_urls": data.get("media_urls", []),
            "7_move_in_date": data.get("move_in_date", "Vào ở ngay"),
            "8_status": data.get("status", "TRỐNG"),
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
        for r in records:
            p = r.payload or {}

            # Lọc Backend
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

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)