import os
import json
import requests
import uuid
import time
from typing import Dict, List, Optional
import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from google import genai
from google.genai import types
# Tích hợp Cloudinary
import cloudinary
import cloudinary.uploader
import pandas as pd
from pydantic import BaseModel
from datetime import datetime
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import io

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# --- 0. BỘ NHỚ ĐỆM TẠM THỜI (PENDING MEDIA CACHE) ---
PENDING_MEDIA_CACHE: Dict[str, dict] = {}
CACHE_TTL_SECONDS = 600  # Bộ nhớ đệm tự hủy sau 10 phút

MEDIA_DIR = "static/media"
os.makedirs(MEDIA_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL")
if CLOUDINARY_URL:
    cloudinary.config(cloudinary_url=CLOUDINARY_URL)


# --- 1. CẤU HÌNH GEMINI & QDRANT (ĐÃ ÉP CỐ ĐỊNH 768 DIMENSIONS) ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Đổi sang Collection mới để tránh đụng độ cấu hình cũ trên Qdrant Cloud
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


# --- 3. TẠO VECTOR EMBEDDING ÉP CHUẨN 768 CHIỀU ---
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


from datetime import datetime

def upsert_room_to_db(
    extracted_data: dict, 
    media_urls: list, 
    point_id: str = None,
    zalo_user_id: str = "SYSTEM",
) -> bool:
    """Đăng mới / Cập nhật phòng kèm thông tin quản lý (User ID, Phone, Created/Updated At)"""
    try:
        address = extracted_data.get("address", "")
        room_name = extracted_data.get("room_name", "Phòng trọ")
        
        # Lấy số điện thoại từ AI trích xuất (nếu có) hoặc dùng tham số truyền vào
        phone = extracted_data.get("landlord_phone")or "Chưa rõ"

        # 1. Tạo chuỗi văn bản tổng hợp phục vụ Vector Search
        text_to_embed = f"""
        Địa chỉ: {address}
        Tên/Số phòng: {room_name}
        Tầng: {extracted_data.get('floor', '')}
        Giá thuê: {extracted_data.get('price', '')}
        Khép kín: {extracted_data.get('is_private_bathroom', '')}
        Thiết bị (Điều hòa, nóng lạnh, máy giặt): {extracted_data.get('appliances', '')}
        Cho nuôi chó mèo: {extracted_data.get('allow_pets', '')}
        Thời gian vào ở: {extracted_data.get('move_in_date', '')}
        Ban công: {extracted_data.get('has_balcony', '')}
        Cửa sổ: {extracted_data.get('has_window', '')}
        Trạng thái: {extracted_data.get('status', 'TRỐNG')}
        SĐT Chủ nhà: {phone}
        """.strip()

        vector = get_text_embedding(text_to_embed)
        if not vector:
            print("❌ Không thể tạo vector cho phòng, hủy upsert.")
            return False

        # 2. Tính toán Deterministic ID mới dựa trên địa chỉ + tên phòng
        new_point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{address.strip().lower()}_{str(room_name).strip().lower()}"))

        # 3. Quản lý Thời gian (Created_at / Updated_at)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        created_at = now_str # Mặc định thời gian tạo mới

        # Nếu truyền point_id cũ vào và đổi địa chỉ -> Xóa ID cũ
        if point_id and point_id != new_point_id:
            try:
                # Thử lấy thông tin bản ghi cũ trước khi xóa để giữ lại created_at ban đầu
                old_records = qdrant_client.retrieve(collection_name=COLLECTION_NAME, ids=[point_id])
                if old_records and old_records[0].payload:
                    created_at = old_records[0].payload.get("created_at", now_str)

                qdrant_client.delete(
                    collection_name=COLLECTION_NAME,
                    points_selector=[point_id]
                )
                print(f"🗑️ [QDRANT DELETE OLD]: Đã xóa bản ghi cũ (ID: {point_id})")
            except Exception as del_err:
                print(f"⚠️ Lỗi khi xóa bản ghi cũ: {del_err}")
        elif point_id and point_id == new_point_id:
            # Nếu giữ nguyên ID (Cập nhật cùng địa chỉ), lấy lại created_at cũ
            try:
                records = qdrant_client.retrieve(collection_name=COLLECTION_NAME, ids=[point_id])
                if records and records[0].payload:
                    created_at = records[0].payload.get("created_at", now_str)
            except Exception:
                pass

        final_point_id = new_point_id

        # 4. Payload mở rộng đầy đủ các trường
        payload = {
            "1_address": address,
            "2_room_name": room_name,
            "3_floor": extracted_data.get("floor", "Không xác định"),
            "4_price": extracted_data.get("price", "Chưa rõ"),
            "5_is_private_bathroom": extracted_data.get("is_private_bathroom", "Chưa rõ"),
            "6_appliances": extracted_data.get("appliances", "Chưa rõ"),
            "7_allow_pets": extracted_data.get("allow_pets", "Chưa rõ"),
            "8_media_urls": media_urls,
            "9_move_in_date": extracted_data.get("move_in_date", "Vào ở ngay"),
            "10_has_balcony": extracted_data.get("has_balcony", "Chưa rõ"),
            "11_has_window": extracted_data.get("has_window", "Chưa rõ"),
            "12_status": extracted_data.get("status", "TRỐNG"),
            # --- CÁC TRƯỜNG THÔNG TIN QUẢN LÝ MỚI BỔ SUNG ---
            "zalo_user_id": zalo_user_id,
            "landlord_phone": phone,
            "created_at": created_at,
            "updated_at": now_str,
            "raw_data": extracted_data
        }

        # 5. Upsert vào Qdrant với wait=True để ép đồng bộ ngay lập tức
        qdrant_client.upsert(
            collection_name=COLLECTION_NAME,
            points=[
                PointStruct(
                    id=final_point_id,
                    vector=vector,
                    payload=payload
                )
            ],
            wait=True
        )
        print(f"✅ [QDRANT UPSERT SUCCESS]: ID = {final_point_id} | User = {zalo_user_id} | SĐT = {phone}")
        return True

    except Exception as e:
        print("❌ [QDRANT UPSERT EXCEPTION]:", e)
        return False


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
                "12_status": new_status,
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


# --- 6. TƯƠNG TÁC ZALO OA API (HIỂN THỊ HÌNH ẢNH TRỰC TIẾP LÊN KHUNG CHÁT) ---
def upload_image_to_zalo(image_url_or_path: str) -> str:
    """Upload ảnh lên Zalo Server để lấy attachment_id giúp hiển thị trực tiếp ảnh trên khung chat Zalo"""
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
            attachment_id = res_data.get("data", {}).get("attachment_id", "")
            print(f"✅ [ZALO UPLOAD SUCCESS]: attachment_id = {attachment_id}")
            return attachment_id
        else:
            print(f"❌ [ZALO UPLOAD FAILED]: {res_data.get('message')}")
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
                    extracted_data=extracted,
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
                    extracted_data=extracted,
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


# --- 10. MAIN ENTRY POINT ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"🚀 Server đang chạy tại cổng {port}...")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
    
# --- SCHEMAS CHO CÁC REQUEST TỪ WEB ---
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

# --- 1. API LẤY DANH SÁCH TẤT CẢ PHÒNG (READ) ---
@app.get("/api/rooms")
def get_all_rooms(limit: int = 100):
    try:
        # Scroll để lấy danh sách bản ghi từ Qdrant
        records, _ = qdrant_client.scroll(
            collection_name=COLLECTION_NAME,
            limit=limit,
            with_payload=True,
            with_vectors=False
        )
        results = []
        for r in records:
            p = r.payload
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

# --- 2. API THÊM / SỬA PHÒNG TỪ WEB ---
@app.post("/api/rooms")
def create_or_update_room_from_web(data: RoomCreateUpdateSchema, point_id: Optional[str] = None):
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
        point_id=point_id, # None = Thêm mới, có ID = Chỉnh sửa
        zalo_user_id="ADMIN_WEB",
        landlord_phone=data.landlord_phone
    )
    
    if success:
        return {"status": "success", "message": "Thao tác thành công!"}
    raise HTTPException(status_code=500, detail="Không thể lưu thông tin vào Qdrant.")

# --- 3. API XÓA PHÒNG TỪ WEB (DELETE) ---
@app.delete("/api/rooms/{point_id}")
def delete_room_from_web(point_id: str):
    try:
        qdrant_client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=[point_id],
            wait=True
        )
        return {"status": "success", "message": f"Đã xóa thành công phòng ID {point_id}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi khi xóa: {str(e)}")
        
        
templates = Jinja2Templates(directory="templates")

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    """Đường dẫn truy cập trang Web Admin: https://your-domain.com/admin"""
    return templates.TemplateResponse(
        request=request, 
        name="admin.html"
    )
    
    
import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
import io

# --- API UPLOAD & XỬ LÝ FILE EXCEL / CSV ---
@app.post("/api/rooms/upload-excel")
async def upload_rooms_from_excel(file: UploadFile = File(...)):
    # 1. Kiểm tra định dạng file
    if not (file.filename.endswith(".xlsx") or file.filename.endswith(".xls") or file.filename.endswith(".csv")):
        raise HTTPException(status_code=400, detail="Chỉ hỗ trợ định dạng file .xlsx, .xls hoặc .csv")

    try:
        # 2. Đọc nội dung file vào Pandas DataFrame
        contents = await file.read()
        if file.filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(contents))
        else:
            df = pd.read_excel(io.BytesIO(contents))

        # Thay thế các giá trị NaN/NaT thành chuỗi rỗng
        df = df.fillna("")

        success_count = 0
        failed_count = 0
        error_logs = []

        # 3. Duyệt qua từng dòng trong file Excel
        for index, row in df.iterrows():
            address = str(row.get("Địa chỉ", row.get("address", ""))).strip()
            
            # Bỏ qua dòng thiếu địa chỉ
            if not address or address.lower() in ["null", "none", "nan", ""]:
                failed_count += 1
                error_logs.append(f"Dòng {index + 2}: Thiếu địa chỉ phòng.")
                continue

            room_name = str(row.get("Tên phòng", row.get("room_name", "Phòng trọ"))).strip()
            phone = str(row.get("SĐT Chủ nhà", row.get("landlord_phone", "Chưa rõ"))).strip()
            
            # Xử lý mảng ảnh Media (phân cách bằng dấu phẩy nếu có)
            raw_media = str(row.get("Media URLs", row.get("media_urls", "")))
            media_urls = [url.strip() for url in raw_media.split(",") if url.strip()] if raw_media else []

            extracted_data = {
                "address": address,
                "room_name": room_name,
                "floor": str(row.get("Tầng", row.get("floor", "Chưa rõ"))),
                "price": str(row.get("Giá", row.get("price", "Chưa rõ"))),
                "is_private_bathroom": str(row.get("WC Khép kín", row.get("is_private_bathroom", "Chưa rõ"))),
                "appliances": str(row.get("Đồ đạc", row.get("appliances", "Chưa rõ"))),
                "allow_pets": str(row.get("Thú cưng", row.get("allow_pets", "Chưa rõ"))),
                "move_in_date": str(row.get("Ngày ở", row.get("move_in_date", "Vào ở ngay"))),
                "has_balcony": str(row.get("Ban công", row.get("has_balcony", "Chưa rõ"))),
                "has_window": str(row.get("Cửa sổ", row.get("has_window", "Chưa rõ"))),
                "status": str(row.get("Trạng thái", row.get("status", "TRỐNG"))).upper(),
                "landlord_phone": phone
            }

            # 4. Lưu vào Qdrant DB (Dùng point_id=None để đảm bảo luôn tạo/cập nhật chuẩn xác theo UUID5 địa chỉ)
            ok = upsert_room_to_db(
                extracted_data=extracted_data,
                media_urls=media_urls,
                point_id=None,
                zalo_user_id="EXCEL_IMPORT",
                landlord_phone=phone
            )

            if ok:
                success_count += 1
            else:
                failed_count += 1
                error_logs.append(f"Dòng {index + 2}: Lỗi khi tạo Vector/Lưu Qdrant.")

        return {
            "status": "success",
            "message": f"Nhập file hoàn tất: Thành công {success_count} phòng, thất bại {failed_count} phòng.",
            "details": {
                "success_count": success_count,
                "failed_count": failed_count,
                "errors": error_logs
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi khi xử lý file Excel: {str(e)}")
        
        
# Đường dẫn tới file Excel có sẵn trên server
TEMPLATE_FILE_PATH = os.path.join(os.path.dirname(__file__), "templates", "Mau_Nhap_Danh_Sach_Phong.xlsx")

@app.get("/api/rooms/download-template")
def download_excel_template():
    # Kiểm tra xem file có tồn tại trên server không
    if not os.path.exists(TEMPLATE_FILE_PATH):
        raise HTTPException(
            status_code=404, 
            detail="File mẫu không tồn tại trên hệ thống server!"
        )
    
    # Trả về file trực tiếp cho trình duyệt tải xuống
    return FileResponse(
        path=TEMPLATE_FILE_PATH,
        filename="Mau_Nhap_Danh_Sach_Phong.xlsx", # Tên file khi tải về máy khách
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )