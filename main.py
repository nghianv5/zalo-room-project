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

# --- 0. CẤU HÌNH MEDIA ---
MEDIA_DIR = "static/media"
os.makedirs(MEDIA_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL")
if CLOUDINARY_URL:
    cloudinary.config(cloudinary_url=CLOUDINARY_URL)

# --- 1. CẤU HÌNH GEMINI & QDRANT ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

COLLECTION_NAME = "rooms_v15_fixed_dim768"
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

# --- SCHEMA DỮ LIỆU ---
class RoomStandardSchema(BaseModel):
    address: str = Field(description="Địa chỉ 4 cấp chuẩn hóa")
    room_name: str = Field(default="Phòng trọ", description="Tên hoặc số phòng")
    floor: Optional[str] = Field(default="Chưa rõ", description="Tầng bao nhiêu")
    price: int = Field(description="Giá thuê tính theo VNĐ")
    is_private_bathroom: bool = Field(default=False, description="Khép kín (True/False)")
    appliances: List[str] = Field(default=[], description="Danh sách thiết bị")
    allow_pets: bool = Field(default=False, description="Cho nuôi thú cưng không")
    status: str = Field(default="TRỐNG", description="Trạng thái")
    landlord_phone: Optional[str] = Field(default="Chưa rõ", description="Số điện thoại")

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
            except Exception:
                time.sleep(1)
    return ""

def search_rooms_by_vector(query_text: str, top_k: int = 5) -> List[dict]:
    query_vector = get_text_embedding(query_text)
    if not query_vector:
        try:
            records, _ = qdrant_client.scroll(collection_name=COLLECTION_NAME, limit=top_k, with_payload=True)
            return [dict(rec.payload, id=rec.id) for rec in records if rec.payload]
        except Exception:
            return []
    try:
        search_result = qdrant_client.query_points(
            collection_name=COLLECTION_NAME, query=query_vector, limit=top_k, with_payload=True
        )
        return [dict(hit.payload, id=hit.id) for hit in search_result.points if hit.payload]
    except Exception as e:
        print("❌ [VECTOR SEARCH ERROR]:", e)
        return []

def upsert_room_to_db(extracted_data: dict, media_urls: list, point_id: str = None, zalo_user_id: str = "SYSTEM", landlord_phone: str = "Chưa rõ") -> bool:
    try:
        address = extracted_data.get("address", "")
        room_name = extracted_data.get("room_name", "Phòng trọ")
        phone = landlord_phone or extracted_data.get("landlord_phone", "Chưa rõ")

        text_to_embed = f"Địa chỉ: {address} Tên: {room_name} Giá: {extracted_data.get('price', '')} Thiết bị: {extracted_data.get('appliances', '')}"
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

        payload = {
            "1_address": address,
            "2_room_name": room_name,
            "3_floor": extracted_data.get("floor", "Chưa rõ"),
            "4_price": extracted_data.get("price", "Chưa rõ"),
            "5_is_private_bathroom": extracted_data.get("is_private_bathroom", "Chưa rõ"),
            "6_appliances": extracted_data.get("appliances", "Chưa rõ"),
            "7_allow_pets": extracted_data.get("allow_pets", "Chưa rõ"),
            "8_media_urls": media_urls,
            "9_move_in_date": extracted_data.get("move_in_date", "Vào ở ngay"),
            "10_has_balcony": extracted_data.get("has_balcony", "Chưa rõ"),
            "11_has_window": extracted_data.get("has_window", "Chưa rõ"),
            "12_status": extracted_data.get("status", "TRỐNG"),
            "zalo_user_id": zalo_user_id,
            "landlord_phone": phone,
            "created_at": created_at,
            "updated_at": now_str,
            "raw_data": extracted_data
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

# --- API LẤY PHÒNG CÓ HỖ TRỢ LỌC TẤT CẢ CÁC TRƯỜNG ---
@app.get("/api/rooms")
def get_all_rooms(
    limit: int = 500,
    address: Optional[str] = None,
    room_name: Optional[str] = None,
    floor: Optional[str] = None,
    price: Optional[str] = None,
    is_private_bathroom: Optional[str] = None,
    allow_pets: Optional[str] = None,
    has_balcony: Optional[str] = None,
    has_window: Optional[str] = None,
    status: Optional[str] = None,
    landlord_phone: Optional[str] = None,
    appliances: Optional[str] = None,
    has_ac: Optional[bool] = None,         # Điều hòa
    has_heater: Optional[bool] = None,     # Nóng lạnh
    has_washer: Optional[bool] = None      # Máy giặt
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
            app_str = str(p.get("6_appliances", "")).lower()

            # Lọc Backend
            if address and address.lower() not in str(p.get("1_address", "")).lower(): continue
            if room_name and room_name.lower() not in str(p.get("2_room_name", "")).lower(): continue
            if floor and floor.lower() not in str(p.get("3_floor", "")).lower(): continue
            if price and price.lower() not in str(p.get("4_price", "")).lower(): continue
            if is_private_bathroom and p.get("5_is_private_bathroom") != is_private_bathroom: continue
            if allow_pets and p.get("7_allow_pets") != allow_pets: continue
            if has_balcony and p.get("10_has_balcony") != has_balcony: continue
            if has_window and p.get("11_has_window") != has_window: continue
            if status and p.get("12_status") != status: continue
            if landlord_phone and landlord_phone not in str(p.get("landlord_phone", "")): continue
            if appliances and appliances.lower() not in app_str: continue

            # Lọc riêng các thiết bị quan trọng
            if has_ac is True and ("điều hòa" not in app_str and "máy lạnh" not in app_str): continue
            if has_heater is True and ("nóng lạnh" not in app_str and "bình nóng lạnh" not in app_str): continue
            if has_washer is True and "máy giặt" not in app_str: continue

            results.append({
                "id": r.id,
                "address": p.get("1_address"),
                "room_name": p.get("2_room_name"),
                "floor": p.get("3_floor"),
                "price": p.get("4_price"),
                "is_private_bathroom": p.get("5_is_private_bathroom"),
                "appliances": p.get("6_appliances"),
                "allow_pets": p.get("7_allow_pets"),
                "media_urls": p.get("8_media_urls", []),
                "move_in_date": p.get("9_move_in_date"),
                "has_balcony": p.get("10_has_balcony"),
                "has_window": p.get("11_has_window"),
                "status": p.get("12_status"),
                "landlord_phone": p.get("landlord_phone"),
                "zalo_user_id": p.get("zalo_user_id"),
                "created_at": p.get("created_at"),
                "updated_at": p.get("updated_at")
            })
        return {"status": "success", "data": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class RoomCreateUpdateSchema(BaseModel):
    address: str
    room_name: Optional[str] = "Phòng trọ"
    floor: Optional[str] = "Chưa rõ"
    price: Optional[str] = "Chưa rõ"
    is_private_bathroom: Optional[str] = "Chưa rõ"
    appliances: Optional[str] = "Chưa rõ"
    allow_pets: Optional[str] = "Chưa rõ"
    media_urls: Optional[List[str]] = []
    move_in_date: Optional[str] = "Vào ở ngay"
    has_balcony: Optional[str] = "Chưa rõ"
    has_window: Optional[str] = "Chưa rõ"
    status: Optional[str] = "TRỐNG"
    landlord_phone: Optional[str] = "Chưa rõ"

@app.post("/api/rooms")
def create_or_update_room(data: RoomCreateUpdateSchema, point_id: Optional[str] = None):
    extracted = data.dict()
    success = upsert_room_to_db(extracted_data=extracted, media_urls=data.media_urls, point_id=point_id, zalo_user_id="ADMIN_WEB", landlord_phone=data.landlord_phone)
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