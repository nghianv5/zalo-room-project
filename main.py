import os
import json
import requests
import uuid
import time
import urllib.parse
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from google import genai
from google.genai import types

# Tích hợp Cloudinary để lưu media vĩnh viễn
import cloudinary
import cloudinary.uploader

app = FastAPI()

# --- 0. TẠO THƯ MỤC VÀ MOUNT STATIC FILES (LƯU VĨNH VIỄN NỘI BỘ NẾU KHÔNG DÙNG CLOUD) ---
MEDIA_DIR = "static/media"
os.makedirs(MEDIA_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Cấu hình Cloudinary nếu có biến môi trường CLOUDINARY_URL
CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL")
if CLOUDINARY_URL:
    cloudinary.config(cloudinary_url=CLOUDINARY_URL)
    print("✅ [MEDIA] Đã cấu hình Cloudinary thành công!")
else:
    print("⚠️ [MEDIA] Không tìm thấy CLOUDINARY_URL. Sử dụng lưu trữ nội bộ (/static/media/).")

# --- 1. CẤU HÌNH VÀ KHỞI TẠO SDK GEMINI MỚI ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

COLLECTION_NAME = "rooms_v5"

@app.get("/")
async def root():
    return {"status": "running", "message": "Zalo Property Management Chatbot is active!"}

# --- 2. KHỞI TẠO CẤU TRÚC KẾT NỐI QDRANT CLOUD ---
qdrant_client = QdrantClient(
    url=os.environ.get("QDRANT_URL"),
    api_key=os.environ.get("QDRANT_API_KEY")
)

try:
    if not qdrant_client.collection_exists(collection_name=COLLECTION_NAME):
        print(f"⚠️ Không tìm thấy collection '{COLLECTION_NAME}'. Đang tạo mới...")
        qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=768,
                distance=Distance.COSINE
            )
        )
        print(f"✅ Tạo thành công collection '{COLLECTION_NAME}' (768 dims)!")
except Exception as qdrant_init_error:
    print("❌ Lỗi khi khởi tạo Collection Qdrant:", qdrant_init_error)

# --- 3. HÀM TẢI VÀ LƯU HÌNH CỐ ĐỊNH (PERMANENT MEDIA STORAGE) ---
def save_media_file(zalo_media_url: str, is_video: bool = False) -> str:
    """
    Tải file từ link Zalo tạm thời và lưu lên Cloudinary hoặc Server nội bộ.
    Trả về URL cố định vĩnh viễn.
    """
    if not zalo_media_url:
        return ""

    resource_type = "video" if is_video else "image"

    # Cách 1: Upload thẳng lên Cloudinary nếu đã cấu hình
    if CLOUDINARY_URL:
        try:
            res = cloudinary.uploader.upload(
                zalo_media_url,
                resource_type=resource_type,
                folder="zalo_room_media"
            )
            permanent_url = res.get("secure_url")
            print(f"☁️ [Cloudinary Upload] Thành công: {permanent_url}")
            return permanent_url
        except Exception as e:
            print(f"❌ [Cloudinary Error] Lỗi upload lên Cloud, chuyển sang lưu nội bộ: {e}")

    # Cách 2: Tải về và lưu trữ tại Server cục bộ (Static folder)
    try:
        response = requests.get(zalo_media_url, timeout=15, stream=True)
        if response.status_code == 200:
            ext = ".mp4" if is_video else ".jpg"
            filename = f"{uuid.uuid4().hex}{ext}"
            filepath = os.path.join(MEDIA_DIR, filename)

            with open(filepath, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            # Thay SERVER_DOMAIN bằng domain public của bạn (ví dụ: ngrok hoặc domain VPS)
            server_domain = os.environ.get("SERVER_DOMAIN", "http://localhost:8000")
            local_url = f"{server_domain}/static/media/{filename}"
            print(f"💾 [Local Saved] Thành công: {local_url}")
            return local_url
    except Exception as e:
        print(f"❌ [Local Save Error] Không thể tải file từ Zalo: {e}")

    # Fallback: Trả về URL Zalo nguyên bản nếu cả 2 phương án thất bại
    return zalo_media_url

# --- 4. HÀM EMBEDDING VÀ DATABASE QDRANT ---
def get_text_embedding(text: str, retries: int = 3, delay: int = 2):
    if not GEMINI_API_KEY or not text or not text.strip():
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": text}]}
    }

    for attempt in range(retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            res_json = response.json()

            if "embedding" in res_json and "values" in res_json["embedding"]:
                return res_json["embedding"]["values"]
            if "embeddings" in res_json and len(res_json["embeddings"]) > 0:
                if "values" in res_json["embeddings"][0]:
                    return res_json["embeddings"][0]["values"]
        except Exception as e:
            print(f"❌ [EMBEDDING] Lỗi kết nối lần {attempt + 1}: {e}")

        if attempt < retries - 1:
            time.sleep(delay)

    return None

def generate_room_id(address: str, room_name: str) -> str:
    unique_string = f"addr:{address.strip().lower()}_room:{room_name.strip().lower()}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, unique_string))

def search_rooms_from_db(query_text: str):
    query_vector = get_text_embedding(query_text)
    if not query_vector:
        return []

    try:
        response = qdrant_client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            limit=4
        )
        return [point.payload for point in response.points if point.payload]
    except Exception as e:
        print("❌ Lỗi khi truy vấn Qdrant:", e)
        return []

def upsert_room_to_db(room_data: dict, media_urls: list):
    address = room_data.get("address") or "Chưa rõ địa chỉ"
    room_name = room_data.get("room_name") or "Phòng mặc định"

    point_id = generate_room_id(address, room_name)

    # Nếu phòng đã tồn tại, ghép thêm các media_urls mới vào danh sách cũ
    existing_media = []
    try:
        existing_points = qdrant_client.retrieve(collection_name=COLLECTION_NAME, ids=[point_id])
        if existing_points and len(existing_points) > 0:
            existing_media = existing_points[0].payload.get("media_urls", [])
    except Exception:
        pass

    # Gộp danh sách ảnh/video không trùng lặp
    all_media_urls = list(dict.fromkeys(existing_media + media_urls))

    search_context = (
        f"Phòng {room_name} tại địa chỉ {address}. "
        f"Giá thuê: {room_data.get('price', 'Chưa rõ')}. "
        f"Tầng: {room_data.get('floor', 'Chưa rõ')}. "
        f"Khép kín: {room_data.get('is_closed')}. "
        f"Tiện ích: Điều hòa ({room_data.get('has_air_conditioner')}), Nóng lạnh ({room_data.get('has_water_heater')}), Máy giặt ({room_data.get('has_washing_machine')}). "
        f"Cho nuôi chó mèo: {room_data.get('allow_pets')}. "
        f"Ban công: {room_data.get('has_balcony')}, Cửa sổ: {room_data.get('has_window')}. "
        f"Ngày vào ở: {room_data.get('available_date')}. "
        f"Mô tả thêm: {room_data.get('description', '')}"
    )

    vector = get_text_embedding(search_context)
    if not vector:
        print("❌ [Qdrant] Hủy cập nhật do không tạo được vector embedding!")
        return False

    payload = {
        "address": address,
        "room_name": room_name,
        "floor": room_data.get("floor"),
        "price": room_data.get("price"),
        "is_closed": room_data.get("is_closed"),
        "amenities": {
            "has_air_conditioner": room_data.get("has_air_conditioner", False),
            "has_water_heater": room_data.get("has_water_heater", False),
            "has_washing_machine": room_data.get("has_washing_machine", False)
        },
        "allow_pets": room_data.get("allow_pets"),
        "media_urls": all_media_urls,
        "available_date": room_data.get("available_date"),
        "has_balcony": room_data.get("has_balcony"),
        "has_window": room_data.get("has_window"),
        "description": room_data.get("description", ""),
        "updated_at": int(time.time())
    }

    try:
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
        print(f"✅ [Qdrant] Upsert thành công phòng '{room_name}' - Tổng media: {len(all_media_urls)}")
        return True
    except Exception as e:
        print("❌ [Qdrant] Lỗi khi Upsert phòng trọ:", e)
        return False

# --- 5. TƯƠNG TÁC ZALO OA API ---
def send_zalo_message(user_id: str, ai_reply: str):
    access_token = os.environ.get("ZALO_ACCESS_TOKEN")
    if not access_token:
        print("❌ [ZALO] Thiếu ZALO_ACCESS_TOKEN!")
        return False

    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    headers = {
        "Content-Type": "application/json",
        "access_token": access_token
    }
    payload = {
        "recipient": {"user_id": user_id},
        "message": {"text": ai_reply}
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        res_data = response.json()

        if res_data.get("error") == 0:
            print("✨ [ZALO SUCCESS]: Đã nhắn tin cho người dùng thành công!")
            return True
        else:
            print(f"❌ [ZALO ERROR]: Gửi tin thất bại: {res_data.get('message')}")
            return False
    except Exception as e:
        print("❌ [ZALO CRITICAL ERROR]: Lỗi kết nối API Zalo:", e)
        return False

# --- 6. LUỒNG XỬ LÝ AI ---
def process_zalo_ai_logic(user_id: str, message_text: str, media_items: list):
    """
    media_items: Danh sách dict chứa {'url': ..., 'is_video': True/False}
    """
    print(f"🔄 [AI processing] User: {user_id} | Text: '{message_text}' | Raw Media Items: {len(media_items)}")

    # Bước 1: Tải và lưu vĩnh viễn tất cả file Media
    permanent_media_urls = []
    for item in media_items:
        saved_url = save_media_file(item["url"], is_video=item.get("is_video", False))
        if saved_url:
            permanent_media_urls.append(saved_url)

    ai_reply = "🤖 Trợ lý AI đang xử lý thông tin, vui lòng chờ trong giây lát!"

    try:
        found_rooms = []
        is_greeting = any(kw in message_text.lower() for kw in ["chào", "hi", "hello", "bắt đầu"])

        if not is_greeting and message_text.strip():
            found_rooms = search_rooms_from_db(message_text)

        system_prompt = f"""
Bạn là Trợ lý AI Quản lý và Tư vấn Phòng trọ chuyên nghiệp trên Zalo.
Hãy phân tích tin nhắn người dùng và dữ liệu liên quan để trả về kết quả JSON.

Tin nhắn gửi tới: "{message_text}"
Danh sách URL đính kèm (Hình ảnh/Video đã lưu vĩnh viễn): {json.dumps(permanent_media_urls)}
Dữ liệu phòng tìm thấy trong CSDL: {json.dumps(found_rooms, ensure_ascii=False)}

YÊU CẦU XỬ LÝ:
1. Xác định "action": 
   - "ADD_ROOM": Nếu người dùng đăng bài phòng trọ, cho thuê hoặc bổ sung thêm ảnh/video/thông tin phòng.
   - "SEARCH_ROOM": Nếu người dùng đang đi tìm phòng, hỏi thông tin phòng trọ.

2. Nếu là "ADD_ROOM", trích xuất đủ 11 thông số sau vào object "extracted_data":
   - "address": Địa chỉ cụ thể (String)
   - "room_name": Tên phòng / Mã phòng / Tên tòa nhà (String)
   - "floor": Tầng số mấy (String/Null)
   - "price": Giá thuê (String)
   - "is_closed": Phòng khép kín có WC riêng không (Boolean)
   - "has_air_conditioner": Có điều hòa không (Boolean)
   - "has_water_heater": Có bình nóng lạnh không (Boolean)
   - "has_washing_machine": Có máy giặt không (Boolean)
   - "allow_pets": Cho phép nuôi chó mèo không (Boolean)
   - "available_date": Thời gian khách vào ở được (String/Null)
   - "has_balcony": Có ban công không (Boolean)
   - "has_window": Có cửa sổ không (Boolean)
   - "description": Tóm tắt các mô tả thêm khác (String)

3. Soạn nội dung "ai_reply":
   - Ngắn gọn, dùng emoji sinh động.
   - Với "ADD_ROOM": Xác nhận đã đăng/cập nhật thông tin phòng thành công, tóm tắt thông số và thông báo đã nhận đủ ảnh/video.
   - Với "SEARCH_ROOM": Tổng hợp danh sách phòng tìm thấy trong CSDL kèm địa chỉ, giá, các tiện ích nổi bật và các link ảnh/video xem phòng.

ĐỊNH DẠNG BẮT BUỘC TRẢ VỀ (JSON duy nhất):
{{
    "action": "ADD_ROOM" hoặc "SEARCH_ROOM",
    "extracted_data": {{ ... các trường thuộc tính ... }},
    "ai_reply": "Câu trả lời soạn sẵn cho khách"
}}
"""

        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=system_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )

        raw_text = response.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]

        result_data = json.loads(raw_text.strip())
        ai_reply = result_data.get("ai_reply", ai_reply)
        action = result_data.get("action")

        if action == "ADD_ROOM":
            extracted = result_data.get("extracted_data", {})
            upsert_room_to_db(extracted, permanent_media_urls)

    except Exception as err:
        print("❌ [AI Logic Error]:", err)

    send_zalo_message(user_id, ai_reply)

# --- 7. WEBHOOK RECEIVER CHÍNH ---
@app.post("/webhook/zalo")
async def zalo_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        event_name = str(data.get("event_name", "")).strip()
        sender_id = data.get("user_id_by_app") or data.get("sender", {}).get("id")

        if not sender_id:
            return {"status": "ignored", "reason": "No sender ID"}

        # SỰ KIỆN 1: Quan tâm OA
        if "user_follow_oa" in event_name:
            welcome_text = "👋 Chào mừng bạn! Trợ lý AI sẵn sàng hỗ trợ bạn tìm phòng trọ hoặc đăng bài cho thuê phòng kèm ảnh/video nhé!"
            send_zalo_message(sender_id, welcome_text)
            return {"status": "success"}

        # SỰ KIỆN 2: Nhắn tin văn bản hoặc đính kèm Ảnh / Video / File
        if event_name in ["user_send_text", "user_send_image", "user_send_file", "user_send_video"]:
            message_obj = data.get("message", {})
            text = message_obj.get("text", "")

            # Bóc tách danh sách Media items
            attachments = message_obj.get("attachments", [])
            media_items = []

            for item in attachments:
                payload = item.get("payload", {})
                media_url = payload.get("url") or payload.get("thumbnailUrl")
                msg_type = item.get("type", "")

                is_video = (msg_type == "video") or ("user_send_video" in event_name)

                if media_url:
                    media_items.append({
                        "url": media_url,
                        "is_video": is_video
                    })

            # Đưa công việc vào Background Tasks để tránh timeout Webhook
            background_tasks.add_task(process_zalo_ai_logic, sender_id, text, media_items)

    except Exception as e:
        print("❌ [Webhook Error]:", e)

    return {"status": "success"}