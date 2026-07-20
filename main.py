import os
import json
import requests
import uuid
import time
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
PENDING_MEDIA_CACHE = {}
CACHE_TTL_SECONDS = 600  # Bộ nhớ đệm tự hủy sau 10 phút nếu người dùng không nhắn tiếp

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
            return res.get("secure_url", "")
        except Exception as e:
            print(f"❌ [Cloudinary Error]: {e}")

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
            return f"{server_domain}/static/media/{filename}"
    except Exception as e:
        print(f"❌ [Local Save Error]: {e}")

    return zalo_media_url

# --- 3. VECTOR EMBEDDING & TƯƠNG TÁC QDRANT ---
def get_text_embedding(text: str) -> list:
    """Hàm bổ trợ lấy Vector Embedding chuẩn 3072-dim từ Gemini"""
    try:
        # Cách 1: Thử gọi với model text-embedding-004 chuẩn + output_dimensionality
        emb_res = gemini_client.models.embed_content(
            model="models/gemini-embedding-001",
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=3072)
        )
        
        if hasattr(emb_res, 'embedding') and hasattr(emb_res.embedding, 'values'):
            return [float(x) for x in emb_res.embedding.values]
            
    except Exception as e:
        print(f"⚠️ [EMBEDDING FALLBACK]: Lỗi lần 1 ({e}), thử cấu hình fallback...")
        


def generate_room_id(address: str, room_name: str) -> str:
    unique_string = f"addr:{address.strip().lower()}_room:{room_name.strip().lower()}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, unique_string))

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
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{address}_{room_name}"))

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
        print(f"✅ [QDRANT UPSERTED SUCCESS]: ID = {point_id} | Địa chỉ = {address}")
        return True

    except Exception as e:
        print("❌ [QDRANT UPSERT EXCEPTION]:", e)
        return False

# --- 4. TƯƠNG TÁC ZALO OA API ---
def upload_image_to_zalo(image_url_or_path: str) -> str:
    """
    Tải file ảnh lên Zalo Server để lấy attachment_id dùng cho tin nhắn Media.
    """
    access_token = os.environ.get("ZALO_ACCESS_TOKEN")
    if not access_token or not image_url_or_path:
        return ""

    url = "https://openapi.zalo.me/v2.0/oa/upload/image"
    headers = {"access_token": access_token}

    try:
        if image_url_or_path.startswith("http"):
            res = requests.get(image_url_or_path, timeout=10)
            img_bytes = res.content
        else:
            with open(image_url_or_path, "rb") as f:
                img_bytes = f.read()

        files = {'file': ('image.jpg', img_bytes, 'image/jpeg')}
        response = requests.post(url, headers=headers, files=files, timeout=15)
        res_data = response.json()

        if res_data.get("error") == 0:
            attachment_id = res_data.get("data", {}).get("attachment_id", "")
            print(f"✅ [ZALO UPLOAD] Thành công attachment_id: {attachment_id}")
            return attachment_id
        else:
            print(f"❌ [ZALO UPLOAD ERROR]: {res_data.get('message')}")
    except Exception as e:
        print("❌ [ZALO UPLOAD EXCEPTION]:", e)

    return ""

def send_zalo_message(user_id: str, ai_reply: str, media_urls: list = None):
    """
    Gửi tin nhắn Zalo:
    - Nếu không có media_urls: Gửi tin nhắn văn bản thông thường.
    - Nếu có media_urls: Tải ảnh đầu tiên lên Zalo và gửi tin nhắn đính kèm hiển thị khung ảnh trực tiếp.
    """
    access_token = os.environ.get("ZALO_ACCESS_TOKEN")
    if not access_token:
        return False

    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    headers = {
        "Content-Type": "application/json",
        "access_token": access_token
    }

    # Kiểm tra xem có hình ảnh để gửi đính kèm không
    attachment_id = None
    if media_urls and len(media_urls) > 0:
        # Lấy hình ảnh đầu tiên để hiển thị làm khung đính kèm
        attachment_id = upload_image_to_zalo(media_urls[0])

    # Nếu upload ảnh thành công, gửi theo cấu trúc Template Media
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
        # Nếu không có ảnh hoặc upload thất bại, gửi tin nhắn văn bản thông thường
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

# --- 5. HÀM QUẢN LÝ PENDING MEDIA CACHE ---
def get_and_clear_pending_media(user_id: str) -> list:
    """Lấy danh sách media đang chờ của user và xóa bộ nhớ đệm."""
    if user_id in PENDING_MEDIA_CACHE:
        cached_data = PENDING_MEDIA_CACHE.pop(user_id)
        if time.time() - cached_data["timestamp"] <= CACHE_TTL_SECONDS:
            return cached_data["urls"]
    return []

def add_pending_media(user_id: str, new_urls: list):
    """Lưu tạm danh sách media của user vào bộ nhớ đệm."""
    current_urls = []
    if user_id in PENDING_MEDIA_CACHE:
        if time.time() - PENDING_MEDIA_CACHE[user_id]["timestamp"] <= CACHE_TTL_SECONDS:
            current_urls = PENDING_MEDIA_CACHE[user_id]["urls"]

    merged_urls = list(dict.fromkeys(current_urls + new_urls))
    PENDING_MEDIA_CACHE[user_id] = {
        "urls": merged_urls,
        "timestamp": time.time()
    }

# --- 6. LUỒNG XỬ LÝ CHÍNH VỚI AI ---
def process_zalo_ai_logic(user_id: str, message_text: str, media_items: list):
    incoming_media_urls = []
    for item in media_items:
        saved_url = save_media_file(item["url"], is_video=item.get("is_video", False))
        if saved_url:
            incoming_media_urls.append(saved_url)

    pending_urls = get_and_clear_pending_media(user_id)
    all_current_media = list(dict.fromkeys(pending_urls + incoming_media_urls))

    # Trường hợp 1: Chỉ gửi ảnh lẻ
    if not message_text.strip() and all_current_media:
        add_pending_media(user_id, all_current_media)
        reply = f"📸 Em đã nhận {len(all_current_media)} hình ảnh/video của bạn rồi ạ!\n\n👉 Bạn vui lòng gửi thêm địa chỉ cụ thể để em hoàn tất đăng bài nhé!"
        send_zalo_message(user_id, reply, media_urls=all_current_media)
        return

    urls_to_send = all_current_media

    try:
        # 1. Tìm kiếm phòng trong CSDL Qdrant trước
        found_rooms = []
        is_greeting = any(kw in message_text.lower() for kw in ["chào", "hi", "hello", "bắt đầu"])
        if not is_greeting and message_text.strip():
            found_rooms = search_rooms_from_db(message_text)

        # 2. Xây dựng Prompt chi tiết cho Gemini
        system_prompt = f"""
Bạn là Trợ lý AI Quản lý và Tư vấn Phòng trọ trên Zalo.
Hãy phân tích tin nhắn người dùng và dữ liệu liên quan để trả về kết quả JSON.

Tin nhắn người dùng: "{message_text}"
Hình ảnh kèm theo: {json.dumps(all_current_media)}
Dữ liệu phòng tìm thấy trong CSDL (Khớp nhất): {json.dumps(found_rooms, ensure_ascii=False)}

YÊU CẦU XỬ LÝ:
1. Phân loại "action":
   - "ADD_ROOM": Người dùng muốn ĐĂNG/CHO THUÊ phòng mới.
   - "SEARCH_ROOM": Người dùng ĐI TÌM/HỎI THÔNG TIN phòng trọ.

2. Nếu "action" == "SEARCH_ROOM":
   - Nếu tìm thấy dữ liệu trong CSDL (`found_rooms` có dữ liệu): Soạn `ai_reply` trình bày CHI TIẾT thông tin phòng (Giá, Tầng, Tiện ích, Cho nuôi thú cưng không,...).
   - Nếu KHÔNG tìm thấy dữ liệu: Soạn `ai_reply` báo chưa tìm thấy phòng phù hợp tại khu vực/địa chỉ đó và hẹn hỗ trợ sau.

3. Nếu "action" == "ADD_ROOM":
   - Trích xuất "address". Nếu có địa chỉ -> báo đã lưu thành công. Nếu thiếu địa chỉ -> nhắc khách bổ sung.

ĐỊNH DẠNG TRẢ VỀ (DUY NHẤT 1 CHUỖI JSON):
{{
    "action": "ADD_ROOM" hoặc "SEARCH_ROOM",
    "extracted_data": {{ ... }},
    "ai_reply": "Nội dung trả lời CHI TIẾT gửi cho khách"
}}
"""

        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=system_prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )

        raw_text = response.text.strip()
        result_data = json.loads(raw_text)
        
        ai_reply = result_data.get("ai_reply", "Dạ em đã ghi nhận thông tin, em sẽ kiểm tra và báo lại ngay ạ!")
        action = result_data.get("action")

        if action == "ADD_ROOM":
            print("action ADD_ROOM")
            extracted = result_data.get("extracted_data", {})
            address = extracted.get("address")
            if address and address.strip().lower() not in ["null", "none", "chưa rõ địa chỉ", "chưa rõ"]:
                print("upsert_room_to_db")
                upsert_room_to_db(extracted, all_current_media)
            elif all_current_media:
                add_pending_media(user_id, all_current_media)
                print("add_pending_media")

        elif action == "SEARCH_ROOM" and found_rooms:
            print("action SEARCH_ROOM")
            # Lấy danh sách ảnh của các phòng tìm được trong CSDL để gửi đính kèm Zalo
            db_media = []
            for room in found_rooms:
                db_media.extend(room.get("media_urls", []))
            if db_media:
                urls_to_send = db_media

    except Exception as err:
        print("❌ [AI Logic Error]:", err)
        ai_reply = "Dạ hệ thống đang bận một chút, bạn vui lòng đợi trong giây lát hoặc nhắn lại giúp em nhé!"

    # Gửi tin nhắn trả lời hoàn chỉnh kèm hình ảnh (nếu có)
    send_zalo_message(user_id, ai_reply, media_urls=urls_to_send)

# --- 7. WEBHOOK RECEIVER ---
@app.post("/webhook/zalo")
async def zalo_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        event_name = str(data.get("event_name", "")).strip()
        sender_id = data.get("user_id_by_app") or data.get("sender", {}).get("id")

        if not sender_id:
            return {"status": "ignored"}

        if "user_follow_oa" in event_name:
            send_zalo_message(sender_id, "👋 Chào mừng bạn! Gửi thông tin hoặc hình ảnh phòng trọ để bắt đầu nhé!")
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

            background_tasks.add_task(process_zalo_ai_logic, sender_id, text, media_items)

    except Exception as e:
        print("❌ [Webhook Error]:", e)

    return {"status": "success"}