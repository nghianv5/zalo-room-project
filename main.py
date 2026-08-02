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
    allow_methods=["*"],  # Cho phép tất cả phương thức GET, POST, PUT, DELETE...
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# --- 0. CẤU HÌNH MEDIA & CLOUDINARY ---
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
            vectors_config=VectorParams(
                size=VECTOR_SIZE,
                distance=Distance.COSINE
            )
        )
        print(f"✅ Tạo mới collection '{COLLECTION_NAME}' (Vector Dim: {VECTOR_SIZE}) thành công!")
except Exception as e:
    print("❌ Lỗi khởi tạo Qdrant:", e)


# --- SCHEMA DỮ LIỆU CHUẨN HÓA DÙNG CHO AI ---
class RoomStandardSchema(BaseModel):
    address: str = Field(description="Địa chỉ 4 cấp chuẩn hóa (Số nhà, Phường/Xã, Quận/Huyện, Tỉnh/TP)")
    room_name: str = Field(default="Phòng trọ", description="Tên hoặc số phòng")
    floor: Optional[str] = Field(default="Chưa rõ", description="Tầng bao nhiêu (dạng chuỗi hoặc số)")
    price: int = Field(description="Giá thuê tính theo VNĐ (ví dụ: 3500000 thay vì 3.5tr)")
    is_private_bathroom: bool = Field(default=False, description="Khép kín (True/False)")
    appliances: List[str] = Field(default=[], description="Danh sách thiết bị: ['Điều hòa', 'Nóng lạnh', ...]")
    allow_pets: bool = Field(default=False, description="Cho nuôi thú cưng không")
    status: str = Field(default="TRỐNG", description="Trạng thái: 'TRỐNG' hoặc 'ĐÃ CHO THUÊ'")
    landlord_phone: Optional[str] = Field(default="Chưa rõ", description="Số điện thoại chuẩn 10 chữ số")


# --- 2. HÀM TẠO VECTOR EMBEDDING ---
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


# --- 3. TƯƠNG TÁC GEMINI GENERATIVE ---
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
                    break

    return ""


# --- 4. TƯƠNG TÁC DATABASE QDRANT ---
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
                p["id"] = hit.id
                results.append(p)
        return results
    except Exception as e:
        print("❌ [VECTOR SEARCH ERROR]:", e)
        return []


def upsert_room_to_db(
    extracted_data: dict, 
    media_urls: list, 
    point_id: str = None,
    zalo_user_id: str = "SYSTEM",
    landlord_phone: str = "Chưa rõ"
) -> bool:
    """Hàm Upsert cơ bản dùng chung cho Web Admin và Import Excel"""
    try:
        address = extracted_data.get("address", "")
        room_name = extracted_data.get("room_name", "Phòng trọ")
        phone = landlord_phone or extracted_data.get("landlord_phone", "Chưa rõ")

        text_to_embed = f"""
        Địa chỉ: {address}
        Tên/Số phòng: {room_name}
        Tầng: {extracted_data.get('floor', '')}
        Giá thuê: {extracted_data.get('price', '')}
        Khép kín: {extracted_data.get('is_private_bathroom', '')}
        Thiết bị: {extracted_data.get('appliances', '')}
        Cho nuôi chó mèo: {extracted_data.get('allow_pets', '')}
        Thời gian vào ở: {extracted_data.get('move_in_date', '')}
        Ban công: {extracted_data.get('has_balcony', '')}
        Cửa sổ: {extracted_data.get('has_window', '')}
        Trạng thái: {extracted_data.get('status', 'TRỐNG')}
        SĐT Chủ nhà: {phone}
        """.strip()

        vector = get_text_embedding(text_to_embed)
        if not vector:
            return False

        new_point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{address.strip().lower()}_{str(room_name).strip().lower()}"))
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        created_at = now_str

        if point_id and point_id != new_point_id:
            try:
                old_records = qdrant_client.retrieve(collection_name=COLLECTION_NAME, ids=[point_id])
                if old_records and old_records[0].payload:
                    created_at = old_records[0].payload.get("created_at", now_str)
                qdrant_client.delete(collection_name=COLLECTION_NAME, points_selector=[point_id])
            except Exception:
                pass
        elif point_id and point_id == new_point_id:
            try:
                records = qdrant_client.retrieve(collection_name=COLLECTION_NAME, ids=[point_id])
                if records and records[0].payload:
                    created_at = records[0].payload.get("created_at", now_str)
            except Exception:
                pass

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


# --- 5. TƯƠNG TÁC ZALO OA API ---
def upload_image_to_zalo(image_url_or_path: str) -> str:
    access_token = os.environ.get("ZALO_ACCESS_TOKEN")
    if not access_token or not image_url_or_path:
        return ""

    url = "https://openapi.zalo.me/v2.0/oa/upload/image"
    headers = {"access_token": access_token}

    try:
        img_bytes = None
        if image_url_or_path.startswith("http") and "localhost" not in image_url_or_path and "127.0.0.1" not in image_url_or_path:
            res = requests.get(image_url_or_path, timeout=10)
            if res.status_code == 200:
                img_bytes = res.content
        else:
            filename = os.path.basename(image_url_or_path)
            local_filepath = os.path.join(MEDIA_DIR, filename)
            if os.path.exists(local_filepath):
                with open(local_filepath, "rb") as f:
                    img_bytes = f.read()

        if not img_bytes:
            return ""

        files = {'file': ('image.jpg', img_bytes, 'image/jpeg')}
        response = requests.post(url, headers=headers, files=files, timeout=15)
        res_data = response.json()
        if res_data.get("error") == 0:
            return res_data.get("data", {}).get("attachment_id", "")
    except Exception as e:
        print("❌ [ZALO UPLOAD EXCEPTION]:", e)
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
        attachment_id = upload_image_to_zalo(media_urls[0])

    if attachment_id:
        payload = {
            "recipient": {"user_id": user_id},
            "message": {
                "text": ai_reply,
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "media",
                        "elements": [{"media_type": "image", "attachment_id": attachment_id}]
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


# --- 6. LUỒNG XỬ LÝ AI ZALO (CHỈ CHO PHÉP TÌM PHÒNG) ---
def process_zalo_ai_logic(
    message_text: str, 
    user_id: str = "SYSTEM"
):
    try:
        relevant_rooms = search_rooms_by_vector(message_text, top_k=5)

        system_prompt = f"""
Bạn là Trợ lý AI Chuyên Tư vấn và Tìm kiếm Phòng trọ trên Zalo.

MỤC TIÊU DUY NHẤT:
Hỗ trợ khách hàng TÌM KIẾM phòng trọ theo yêu cầu (`SEARCH_ROOM`).

LƯU Ý QUAN TRỌNG:
- Kênh Zalo CHỈ DÀNH CHO KHÁCH HÀNG TÌM PHÒNG.
- Không cho phép Thêm, Sửa hay Đổi trạng thái phòng trên Zalo.
- Nếu người dùng gửi nội dung đăng/thêm/sửa phòng hoặc đổi trạng thái (Đã cho thuê, Đã chốt...), trả về `action`: "MANAGEMENT_NOT_ALLOWED".

DỮ LIỆU ĐẦU VÀO:
- Tin nhắn người dùng: "{message_text}"
- Danh sách phòng tìm được: {json.dumps(relevant_rooms, ensure_ascii=False)}

TRẢ VỀ DUY NHẤT CHUỖI JSON DẠNG:
{{
    "action": "SEARCH_ROOM" | "MANAGEMENT_NOT_ALLOWED",
    "ai_reply": "Nội dung trả lời chi tiết, thân thiện..."
}}
"""

        raw_text = generate_content_with_retry(system_prompt, mime_type="application/json")
        if not raw_text:
            raise Exception("Gemini không phản hồi dữ liệu.")

        result_data = json.loads(raw_text)
        action = result_data.get("action")
        ai_reply = result_data.get("ai_reply", "Dạ em có thể giúp gì cho anh/chị ạ?")

        urls_to_send = []

        if action == "SEARCH_ROOM" and relevant_rooms:
            for room in relevant_rooms:
                m_urls = room.get("8_media_urls") or room.get("media_urls")
                if m_urls and len(m_urls) > 0:
                    urls_to_send = m_urls
                    break

        elif action == "MANAGEMENT_NOT_ALLOWED":
            ai_reply = "Dạ kênh Zalo hiện chỉ hỗ trợ Khách hàng Tìm phòng trọ!\n\nĐể **Đăng mới, Sửa thông tin hoặc Cập nhật trạng thái phòng**, anh/chị vui lòng thực hiện trên **Trang Quản trị Web Admin** nhé!"

    except Exception as err:
        print("❌ [AI Logic Exception]:", err)
        ai_reply = "Dạ hệ thống đang bận một chút, anh/chị thử lại sau giây lát nhé!"

    send_zalo_message(user_id, ai_reply, media_urls=urls_to_send)


# --- 7. ZALO WEBHOOK RECEIVER ---
@app.post("/webhook/zalo")
async def zalo_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        event_name = str(data.get("event_name", "")).strip()
        sender_id = data.get("user_id_by_app") or data.get("sender", {}).get("id")

        if not sender_id:
            return {"status": "ignored"}

        if "user_follow_oa" in event_name:
            send_zalo_message(sender_id, "👋 Chào mừng bạn! Bạn cần tìm phòng trọ ở khu vực nào, giá khoảng bao nhiêu ạ?")
            return {"status": "success"}

        if event_name in ["user_send_text", "user_send_image", "user_send_file", "user_send_video"]:
            message_obj = data.get("message", {})
            text = message_obj.get("text", "")

            background_tasks.add_task(process_zalo_ai_logic, text, sender_id)
    
    except Exception as e:
        print("❌ [Webhook Exception]:", e)

    return {"status": "success"}


# --- 8. WEB ADMIN APIS & ROUTING ---
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


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    """Giao diện Web Admin Dashboard"""
    return templates.TemplateResponse(
        request=request, 
        name="admin.html"
    )


@app.get("/api/rooms")
def get_all_rooms(limit: int = 100):
    """Lấy danh sách phòng cho Web Admin"""
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


@app.post("/api/rooms")
def create_or_update_room(
    data: RoomCreateUpdateSchema, 
    point_id: Optional[str] = None  # Nhận point_id từ Query Param (?point_id=...)
):
    """
    Tạo mới hoặc Cập nhật phòng trọ từ Web Admin.
    - Nếu có point_id: Cập nhật phòng tương ứng.
    - Nếu không có point_id: Thêm phòng mới.
    """
    extracted = {
        "address": data.address,
        "room_name": data.room_name,
        "floor": data.floor,
        "price": data.price,
        "is_private_bathroom": data.is_private_bathroom,
        "appliances": data.appliances,
        "allow_pets": data.allow_pets,
        "move_in_date": data.move_in_date,
        "has_balcony": data.has_balcony,
        "has_window": data.has_window,
        "status": data.status,
        "landlord_phone": data.landlord_phone
    }
    
    success = upsert_room_to_db(
        extracted_data=extracted,
        media_urls=data.media_urls,
        point_id=point_id,
        zalo_user_id="ADMIN_WEB",
        landlord_phone=data.landlord_phone
    )
    
    if success:
        action_text = "Cập nhật" if point_id else "Thêm mới"
        return {"status": "success", "message": f"{action_text} phòng trọ thành công!"}
        
    raise HTTPException(status_code=500, detail="Lỗi khi lưu dữ liệu vào CSDL.")


@app.delete("/api/rooms/{point_id}")
def delete_room_from_web(point_id: str):
    """Xóa phòng từ Web Admin"""
    try:
        qdrant_client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=[point_id],
            wait=True
        )
        return {"status": "success", "message": f"Đã xóa thành công phòng ID {point_id}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi khi xóa: {str(e)}")


TEMPLATE_FILE_PATH = os.path.join(os.path.dirname(__file__), "templates", "Mau_Nhap_Danh_Sach_Phong.xlsx")

@app.get("/api/rooms/download-template")
def download_excel_template():
    """Tải file mẫu Excel"""
    if not os.path.exists(TEMPLATE_FILE_PATH):
        raise HTTPException(status_code=404, detail="File mẫu không tồn tại trên hệ thống server!")
    
    return FileResponse(
        path=TEMPLATE_FILE_PATH,
        filename="Mau_Nhap_Danh_Sach_Phong.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# --- 8.1. HÀM AI CHUẨN HÓA DỮ LIỆU EXCEL ---
def normalize_excel_batch_with_ai(df_chunk: pd.DataFrame) -> List[dict]:
    raw_records = df_chunk.to_dict(orient="records")
    
    prompt = f"""
Bạn là Trợ lý AI Chuyên viên Xử lý và Chuẩn hóa Dữ liệu Phòng trọ.
Hãy đọc danh sách dữ liệu thô từ file Excel dưới đây và chuẩn hóa về đúng định dạng JSON yêu cầu.

YÊU CẦU CHUẨN HÓA DỮ LIỆU:
1. `address`: Chuẩn hóa lại tên đường, phường/xã, quận/huyện đầy đủ.
2. `room_name`: Tên/Số phòng (mặc định "Phòng trọ" nếu không rõ).
3. `floor`: Số tầng (dạng chuỗi, VD: "Tầng 2").
4. `price`: Đổi toàn bộ các dạng viết tắt như "3.5 tr", "3tr5", "3,500,000đ" thành SỐ NGUYÊN VNĐ (ví dụ: 3500000).
5. `is_private_bathroom` & `allow_pets`: Chuyển thành kiểu Boolean `true` hoặc `false` dựa trên ngữ cảnh ("có", "khép kín" -> true; "không", "chung" -> false).
6. `appliances`: Chuyển chuỗi liệt kê đồ đạc thành mảng các từ khóa chuẩn (VD: ["Điều hòa", "Nóng lạnh", "Máy giặt"]).
7. `landlord_phone`: Chuẩn hóa số điện thoại về dạng 10 chữ số (xóa khoảng trắng, dấu chấm, +84).
8. `status`: Viết hoa, chỉ nhận 2 giá trị "TRỐNG" hoặc "ĐÃ CHO THUÊ".

DỮ LIỆU THÔ ĐẦU VÀO:
{json.dumps(raw_records, ensure_ascii=False)}

TRẢ VỀ DUY NHẤT 1 MẢNG JSON CÁC OBJECT ĐÃ ĐƯỢC CHUẨN HÓA:
[
  {{
    "address": "...",
    "room_name": "...",
    "floor": "Tầng 2",
    "price": 3500000,
    "is_private_bathroom": true,
    "appliances": ["Điều hòa", "Nóng lạnh"],
    "allow_pets": false,
    "status": "TRỐNG",
    "landlord_phone": "0987654321"
  }}
]
"""
    try:
        raw_text = generate_content_with_retry(prompt, mime_type="application/json")
        if raw_text:
            cleaned_records = json.loads(raw_text)
            return cleaned_records
    except Exception as e:
        print("❌ Lỗi khi AI chuẩn hóa batch:", e)
    
    return []


# --- 8.2. API UPLOAD EXCEL TÍCH HỢP AI TỰ ĐỘNG CHUẨN HÓA & LƯU DB ---
# Sửa lại URL chuẩn khớp với Frontend: /api/rooms/upload-excel
@app.post("/api/rooms/upload-excel")
async def upload_and_process_excel(file: UploadFile = File(...)):
    """Upload Excel -> AI Đọc & Chuẩn hóa dữ liệu -> Lưu trực tiếp vào Qdrant DB"""
    if not (file.filename.endswith(".xlsx") or file.filename.endswith(".xls") or file.filename.endswith(".csv")):
        raise HTTPException(status_code=400, detail="Chỉ chấp nhận file Excel (.xlsx, .xls) hoặc .csv")

    try:
        contents = await file.read()
        if file.filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(contents))
        else:
            df = pd.read_excel(io.BytesIO(contents))

        df = df.fillna("")

        batch_size = 10
        total_rows = len(df)
        success_count = 0
        failed_count = 0

        for i in range(0, total_rows, batch_size):
            df_chunk = df.iloc[i:i + batch_size]
            
            # 1. Đưa qua Gemini AI chuẩn hóa
            normalized_batch = normalize_excel_batch_with_ai(df_chunk)

            # 2. Duyệt từng dòng đã chuẩn hóa và lưu Qdrant
            for index, item in enumerate(normalized_batch):
                try:
                    # Kiểm tra dữ liệu chuẩn bằng Pydantic Model
                    valid_data = RoomStandardSchema(**item)
                    
                    # Chuyển đổi định dạng phù hợp cho hàm upsert_room_to_db
                    extracted = {
                        "address": valid_data.address,
                        "room_name": valid_data.room_name,
                        "floor": valid_data.floor,
                        "price": f"{valid_data.price:,} VNĐ" if valid_data.price else "Chưa rõ",
                        "is_private_bathroom": "Có" if valid_data.is_private_bathroom else "Không",
                        "appliances": ", ".join(valid_data.appliances) if valid_data.appliances else "Chưa rõ",
                        "allow_pets": "Có" if valid_data.allow_pets else "Không",
                        "move_in_date": "Vào ở ngay",
                        "has_balcony": "Chưa rõ",
                        "has_window": "Chưa rõ",
                        "status": valid_data.status,
                        "landlord_phone": valid_data.landlord_phone
                    }

                    # Trích xuất media_urls nếu file Excel thô có kèm cột ảnh
                    raw_row = df_chunk.iloc[index] if index < len(df_chunk) else {}
                    raw_media = str(raw_row.get("Media URLs", raw_row.get("media_urls", "")))
                    media_urls = [u.strip() for u in raw_media.split(",") if u.strip()] if raw_media else []

                    # 3. Lưu trực tiếp vào Database Qdrant
                    ok = upsert_room_to_db(
                        extracted_data=extracted,
                        media_urls=media_urls,
                        point_id=None,
                        zalo_user_id="EXCEL_AI_IMPORT",
                        landlord_phone=valid_data.landlord_phone
                    )

                    if ok:
                        success_count += 1
                    else:
                        failed_count += 1

                except Exception as val_err:
                    print("⚠️ Bản ghi không qua được Pydantic/Lưu DB:", val_err)
                    failed_count += 1

        return {
            "status": "success",
            "message": f"Đã chuẩn hóa và nhập thành công {success_count}/{total_rows} phòng vào Database!",
            "details": {
                "total": total_rows,
                "success": success_count,
                "failed": failed_count
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi xử lý file Excel: {str(e)}")


# --- 9. MAIN ENTRY POINT ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"🚀 Server đang chạy tại cổng {port}...")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)