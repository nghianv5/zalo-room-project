import os
import json
import requests
import uuid
import time
from typing import Dict, List
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from google import genai
from google.genai import types

# Tích hợp Cloudinary nếu có
import cloudinary
import cloudinary.uploader

app = FastAPI()

# --- 0. BỘ NHỚ ĐỆM TẠM THỜI (PENDING MEDIA CACHE) ---
# Bộ nhớ đệm lưu ảnh/video đang chờ ghép với thông tin phòng: {user_id: {"urls": [...], "timestamp": float}}
PENDING_MEDIA_CACHE: Dict[str, dict] = {}
CACHE_TTL_SECONDS = 600  # Bộ nhớ đệm tự hủy sau 10 phút (600 giây) nếu không có tin nhắn text đi kèm

MEDIA_DIR = "static/media"
os.makedirs(MEDIA_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL")
if CLOUDINARY_URL:
    cloudinary.config(cloudinary_url=CLOUDINARY_URL)

# --- 1. CẤU HÌNH GEMINI & QDRANT ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

COLLECTION_NAME = "rooms_v6"  # Khởi tạo chuẩn 3072 dims

qdrant_client = QdrantClient(
    url=os.environ.get("QDRANT_URL"),
    api_key=os.environ.get("QDRANT_API_KEY")
)

try:
    if not qdrant_client.collection_exists(collection_name=COLLECTION_NAME):
        qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=3072,  # Chuẩn 3072 chiều của Gemini Embedding
                distance=Distance.COSINE
            )
        )
        print(f"✅ Tạo mới collection '{COLLECTION_NAME}' (3072 dims) thành công!")
except Exception as e:
    print("❌ Lỗi khởi tạo Qdrant:", e)


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

    # Fallback lưu tại server nội bộ
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


# --- 3. VECTOR EMBEDDING & TƯƠNG TÁC QDRANT ---
def get_text_embedding(text: str, retries: int = 3, delay: int = 2):
    """
    Tạo Vector Embedding sử dụng model gemini-embedding-001 chính xác từ API Key.
    """
    if not GEMINI_API_KEY:
        print("❌ [EMBEDDING] Thiếu GEMINI_API_KEY!")
        return None

    # Dùng chuẩn model: gemini-embedding-001
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    payload = {
        "model": "models/gemini-embedding-001",
        "content": {
            "parts": [
                {"text": text}
            ]
        }
    }

    for attempt in range(retries):
        try:
            if not text or not text.strip():
                return None

            response = requests.post(url, headers=headers, json=payload, timeout=10)
            res_json = response.json()

            # Lấy danh sách giá trị vector trả về
            if "embedding" in res_json and "values" in res_json["embedding"]:
                return res_json["embedding"]["values"]
            
            print(f"⚠️ [EMBEDDING] Lần thử {attempt + 1} phản hồi: {res_json}")

        except Exception as e:
            print(f"❌ [EMBEDDING] Lỗi kết nối lần {attempt + 1}: {e}")

        if attempt < retries - 1:
            time.sleep(delay)

    return None


def generate_content_with_retry(prompt: str, mime_type: str = "application/json", retries: int = 3) -> str:
    """
    Hàm gọi Gemini API an toàn chống lỗi 503 UNAVAILABLE / 429 Quá tải.
    Tự động retry với exponential backoff và chuyển sang model dự phòng.
    """
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
                    wait_time = 2 ** attempt
                    print(f"⚠️ [GEMINI 503/429]: Model '{model_name}' đang quá tải. Thử lại lần {attempt + 1}/{retries} sau {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    print(f"❌ [GEMINI ERROR]: Model '{model_name}' gặp lỗi không phải 503: {e}")
                    break

        print(f"🔄 [GEMINI FALLBACK]: Model '{model_name}' thất bại. Chuyển sang model tiếp theo...")

    return ""


# --- 4. CÁC HÀM XỬ LÝ DATABASE QDRANT ---
def search_rooms_from_db(query_text: str):
    query_vector = get_text_embedding(query_text)
    if not query_vector:
        print("⚠️ [Qdrant Search] Không thể tạo embedding cho nội dung tìm kiếm.")
        return []

    try:
        response = qdrant_client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            limit=3
        )

        rooms = []
        for point in response.points:
            if point.payload:
                rooms.append(point.payload)
        return rooms
    except Exception as e:
        print("❌ Lỗi khi truy vấn Qdrant:", e)
        return []


def upsert_room_to_db(extracted_data: dict, media_urls: list) -> bool:
    try:
        address = extracted_data.get("address", "")
        room_name = extracted_data.get("room_name") or extracted_data.get("room_number") or "Phòng"
        price = extracted_data.get("price", "")
        desc = extracted_data.get("description", "")

        # 1. Tạo text để vectorize
        text_to_vector = f"Địa chỉ: {address}. Phòng: {room_name}. Giá: {price}. Chi tiết: {desc}"

        # 2. Lấy vector embedding (3072 dims)
        vector = get_text_embedding(text_to_vector)

        if not vector:
            print("❌ [QDRANT UPSERT CANCELED]: Không tạo được vector embedding!")
            return False

        # 3. Tạo Deterministic UUIDv5
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{address.strip().lower()}_{room_name.strip().lower()}"))

        payload = {
            "address": address,
            "room_name": room_name,
            "price": price,
            "description": desc,
            "media_urls": media_urls,
            "raw_data": extracted_data
        }

        # 4. Upsert vào Qdrant DB
        qdrant_client.upsert(
            collection_name=COLLECTION_NAME,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload
                )
            ]
        )
        print(f"✅ [QDRANT UPSERTED SUCCESS]: ID = {point_id} | Địa chỉ = {address} | Tổng media = {len(media_urls)}")
        return True

    except Exception as e:
        print("❌ [QDRANT UPSERT EXCEPTION]:", e)
        return False


# --- 5. TƯƠNG TÁC ZALO OA API ---
def upload_image_to_zalo(image_url_or_path: str) -> str:
    """
    Tải file ảnh lên Zalo Server để lấy attachment_id dùng cho tin nhắn Media.
    Chống lỗi Connection Refused do tự gọi về localhost.
    """
    access_token = os.environ.get("ZALO_ACCESS_TOKEN")
    if not access_token or not image_url_or_path:
        return ""

    url = "https://openapi.zalo.me/v2.0/oa/upload/image"
    headers = {"access_token": access_token}

    try:
        img_bytes = None

        # Trường hợp 1: URL công khai bên ngoài (Cloudinary, S3...)
        if image_url_or_path.startswith("http") and "localhost" not in image_url_or_path and "127.0.0.1" not in image_url_or_path:
            res = requests.get(image_url_or_path, timeout=10)
            if res.status_code == 200:
                img_bytes = res.content

        # Trường hợp 2: File local hoặc URL chứa localhost/static -> Đọc trực tiếp từ ổ cứng
        else:
            filename = os.path.basename(image_url_or_path)
            local_filepath = os.path.join(MEDIA_DIR, filename)

            if os.path.exists(local_filepath):
                with open(local_filepath, "rb") as f:
                    img_bytes = f.read()
            else:
                print(f"❌ [ZALO UPLOAD]: File không tìm thấy tại ổ cứng local: {local_filepath}")
                return ""

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
            print(f"❌ [ZALO UPLOAD ERROR]: {res_data.get('message')}")

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
        res_data = response.json()

        if res_data.get("error") == 0:
            print("✨ [ZALO SUCCESS]: Đã gửi tin nhắn thành công!")
            return True
        else:
            print(f"❌ [ZALO ERROR]: {res_data.get('message')}")
            return False
    except Exception as e:
        print("❌ [ZALO CRITICAL ERROR]:", e)
        return False


# --- 6. HÀM QUẢN LÝ PENDING MEDIA CACHE ---
def get_and_clear_pending_media(user_id: str) -> list:
    """Lấy toàn bộ danh sách media đang chờ của user và giải phóng bộ nhớ đệm."""
    if user_id in PENDING_MEDIA_CACHE:
        cached_data = PENDING_MEDIA_CACHE.pop(user_id)
        if time.time() - cached_data["timestamp"] <= CACHE_TTL_SECONDS:
            print(f"🚚 [CACHE RETRIEVED]: Lấy thành công {len(cached_data['urls'])} media chờ cho user {user_id}")
            return cached_data["urls"]
        else:
            print(f"⏳ [CACHE EXPIRED]: Media chờ của user {user_id} đã hết hạn TTL 10 phút.")
    return []


def add_pending_media(user_id: str, new_urls: list):
    """Lưu thêm danh sách media của user vào bộ nhớ đệm tạm thời."""
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
    print(f"📦 [CACHE STORED]: Đã lưu tạm {len(new_urls)} media cho user {user_id}. Tổng đang chờ: {len(merged_urls)}")


# --- 7. LUỒNG XỬ LÝ CHÍNH VỚI AI ---
def process_zalo_ai_logic(user_id: str, message_text: str, media_items: list):
    # Step 1: Lưu và chuyển đổi tất cả media nhận được ở lượt nhắn này
    incoming_media_urls = []
    for item in media_items:
        saved_url = save_media_file(item["url"], is_video=item.get("is_video", False))
        if saved_url:
            incoming_media_urls.append(saved_url)

    # Step 2: Trường hợp KHÔNG CÓ TEXT (Chủ phòng chỉ gửi ảnh/video lẻ)
    if not message_text.strip() and incoming_media_urls:
        add_pending_media(user_id, incoming_media_urls)
        # Đếm tổng số ảnh đang chờ hiện tại để báo lại cho chủ phòng
        total_pending = len(PENDING_MEDIA_CACHE.get(user_id, {}).get("urls", []))
        reply = f"📸 Em đã nhận {len(incoming_media_urls)} hình ảnh/video! (Tổng đã nhận: {total_pending} file)\n\n👉 Anh/Chị nhắn thêm địa chỉ cụ thể, tên phòng và giá thuê để em hoàn tất đăng bài nhé!"
        send_zalo_message(user_id, reply, media_urls=incoming_media_urls)
        return

    # Step 3: Lấy toàn bộ ảnh đang chờ từ các lượt nhắn trước ra để gộp chung với tin nhắn text này
    pending_urls = get_and_clear_pending_media(user_id)
    all_current_media = list(dict.fromkeys(pending_urls + incoming_media_urls))

    urls_to_send = all_current_media

    try:
        # 1. Tìm kiếm phòng trong CSDL Qdrant
        found_rooms = []
        is_greeting = any(kw in message_text.lower() for kw in ["chào", "hi", "hello", "bắt đầu"])
        if not is_greeting and message_text.strip():
            found_rooms = search_rooms_from_db(message_text)

        # 2. Xây dựng System Prompt chuẩn cho Gemini
        system_prompt = f"""
Bạn là Trợ lý AI Quản lý và Tư vấn Phòng trọ chuyên nghiệp trên Zalo.
Hãy phân tích tin nhắn người dùng và dữ liệu liên quan để trả về kết quả JSON.

Tin nhắn người dùng: "{message_text}"
Hình ảnh/Video kèm theo (đã lưu): {json.dumps(all_current_media)}
Dữ liệu phòng tìm thấy trong CSDL: {json.dumps(found_rooms, ensure_ascii=False)}

YÊU CẦU XỬ LÝ:
1. Phân loại "action":
   - "ADD_ROOM": Người dùng muốn ĐĂNG/CHO THUÊ phòng mới.
   - "SEARCH_ROOM": Người dùng ĐI TÌM/HỎI THÔNG TIN phòng trọ.

2. Nếu "action" == "SEARCH_ROOM":
   - Nếu tìm thấy dữ liệu (`found_rooms` có dữ liệu): Soạn `ai_reply` trình bày CHI TIẾT thông tin phòng (Giá, Địa chỉ, Tiện ích...).
   - Nếu KHÔNG tìm thấy dữ liệu: Soạn `ai_reply` báo chưa tìm thấy phòng phù hợp tại khu vực đó.

3. Nếu "action" == "ADD_ROOM":
   - Trích xuất thông tin vào object `extracted_data` gồm: `address`, `room_name`, `price`, `description`.
   - Nếu có địa chỉ rõ ràng -> `ai_reply` xác nhận đã lưu phòng thành công kèm số lượng ảnh.
   - Nếu THIẾU địa chỉ -> `ai_reply` nhắc khách bổ sung thêm địa chỉ cụ thể.

ĐỊNH DẠNG TRẢ VỀ (DUY NHẤT 1 CHUỖI JSON, KHÔNG DÙNG FENCE CODE BLOCK):
{{
    "action": "ADD_ROOM" hoặc "SEARCH_ROOM",
    "extracted_data": {{
        "address": "Địa chỉ phòng",
        "room_name": "Tên hoặc số phòng",
        "price": "Giá thuê",
        "description": "Mô tả tiện ích"
    }},
    "ai_reply": "Nội dung trả lời gửi lại cho khách"
}}
"""

        # 3. Gọi Gemini với cơ chế Retry & Fallback
        raw_text = generate_content_with_retry(system_prompt, mime_type="application/json")

        if not raw_text:
            raise Exception("Gemini không phản hồi dữ liệu sau khi retry.")

        result_data = json.loads(raw_text)
        ai_reply = result_data.get("ai_reply", "Dạ em đã ghi nhận thông tin rồi ạ!")
        action = result_data.get("action")

        # 4. Xử lý Logic sau khi AI phân tích
        if action == "ADD_ROOM":
            extracted = result_data.get("extracted_data", {})
            address = str(extracted.get("address", "")).strip().lower()

            # Kiểm tra xem có địa chỉ hợp lệ không
            if address and address not in ["null", "none", "chưa rõ địa chỉ", "chưa rõ", ""]:
                print("✅ [ADD_ROOM]: Tiến hành Upsert vào Qdrant...")
                upsert_room_to_db(extracted, all_current_media)
            else:
                # Nếu thiếu địa chỉ, trả toàn bộ ảnh lại cache chờ khách nhắn địa chỉ bổ sung
                if all_current_media:
                    add_pending_media(user_id, all_current_media)
                    print("⚠️ [ADD_ROOM MISSING ADDRESS]: Đưa ảnh quay lại cache chờ địa chỉ bổ sung.")

        elif action == "SEARCH_ROOM" and found_rooms:
            # Lấy danh sách media của các phòng tìm được trong DB để gửi lại đính kèm cho khách
            db_media = []
            for room in found_rooms:
                db_media.extend(room.get("media_urls", []))
            if db_media:
                urls_to_send = db_media

    except Exception as err:
        print("❌ [AI Logic Exception]:", err)
        ai_reply = "Dạ hệ thống đang xử lý một chút, bạn chờ em vài giây rồi nhắn lại giúp em nhé!"

    # Gửi câu trả lời hoàn chỉnh lại cho người dùng trên Zalo
    send_zalo_message(user_id, ai_reply, media_urls=urls_to_send)


# --- 8. WEBHOOK RECEIVER ---
@app.post("/webhook/zalo")
async def zalo_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        event_name = str(data.get("event_name", "")).strip()
        sender_id = data.get("user_id_by_app") or data.get("sender", {}).get("id")

        if not sender_id:
            return {"status": "ignored"}

        # Sự kiện người dùng quan tâm OA
        if "user_follow_oa" in event_name:
            send_zalo_message(sender_id, "👋 Chào mừng bạn! Hãy gửi thông tin hoặc hình ảnh phòng trọ để bắt đầu nhé!")
            return {"status": "success"}

        # Các sự kiện tin nhắn từ người dùng
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

            # Đưa xử lý AI vào Background Task để phản hồi webhook ngay lập tức (HTTP 200)
            background_tasks.add_task(process_zalo_ai_logic, sender_id, text, media_items)

    except Exception as e:
        print("❌ [Webhook Exception]:", e)

    return {"status": "success"}