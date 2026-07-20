import os
import json
import requests
import uuid
import time
from typing import Dict, List
import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue
from google import genai
from google.genai import types

# Tích hợp Cloudinary
import cloudinary
import cloudinary.uploader

app = FastAPI()

# --- 0. BỘ NHỚ ĐỆM TẠM THỜI (PENDING MEDIA CACHE) ---
PENDING_MEDIA_CACHE: Dict[str, dict] = {}
CACHE_TTL_SECONDS = 600  # 10 phút

MEDIA_DIR = "static/media"
os.makedirs(MEDIA_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL")
if CLOUDINARY_URL:
    cloudinary.config(cloudinary_url=CLOUDINARY_URL)

# --- 1. CẤU HÌNH GEMINI & QDRANT ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

COLLECTION_NAME = "rooms_v7"  # Nâng cấp phiên bản collection cho 12 trường mới

qdrant_client = QdrantClient(
    url=os.environ.get("QDRANT_URL"),
    api_key=os.environ.get("QDRANT_API_KEY")
)

try:
    if not qdrant_client.collection_exists(collection_name=COLLECTION_NAME):
        qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=3072,  # Chuẩn 3072 dimensions cho model text-embedding-004
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


# --- 3. VECTOR EMBEDDING & TƯƠNG TÁC GEMINI SDK ---
def get_text_embedding(text: str, retries: int = 3, delay: int = 2):
    if not gemini_client or not text or not text.strip():
        return None

    for attempt in range(retries):
        try:
            response = gemini_client.models.embed_content(
                model="text-embedding-004",
                contents=text,
            )
            if response and response.embedding and response.embedding.values:
                return response.embedding.values

        except Exception as e:
            print(f"❌ [EMBEDDING] Lỗi tạo vector lần {attempt + 1}: {e}")
            if attempt < retries - 1:
                time.sleep(delay)

    return None


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
                    wait_time = 2 ** attempt
                    print(f"⚠️ [GEMINI 503/429]: Model '{model_name}' quá tải. Thử lại {attempt + 1}/{retries} sau {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    print(f"❌ [GEMINI ERROR]: Model '{model_name}' gặp lỗi: {e}")
                    break

        print(f"🔄 [GEMINI FALLBACK]: Chuyển từ '{model_name}' sang model tiếp theo...")

    return ""


# --- 4. CÁC HÀM XỬ LÝ DATABASE QDRANT (VỚI 12 TRƯỜNG DỮ LIỆU) ---
def search_rooms_from_db(query_text: str):
    query_vector = get_text_embedding(query_text)
    if not query_vector:
        print("⚠️ [Qdrant Search] Không thể tạo embedding cho nội dung tìm kiếm.")
        return []

    try:
        # Tìm kiếm các phòng tương đồng
        response = qdrant_client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            limit=5
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
        # Trích xuất 12 trường chuẩn
        address = extracted_data.get("address", "")
        room_name = extracted_data.get("room_name", "Phòng")
        floor = extracted_data.get("floor", "Không xác định")
        price = extracted_data.get("price", "Chưa rõ")
        is_private_bathroom = extracted_data.get("is_private_bathroom", "Không rõ")
        amenities = extracted_data.get("amenities", "Chưa rõ")
        allow_pets = extracted_data.get("allow_pets", "Không rõ")
        move_in_date = extracted_data.get("move_in_date", "Vào ở ngay")
        has_balcony = extracted_data.get("has_balcony", "Không rõ")
        has_window = extracted_data.get("has_window", "Không rõ")
        status = extracted_data.get("status", "TRỐNG")  # Mặc định là TRỐNG khi đăng bài

        # 1. Chuỗi văn bản đại diện cho Vector
        text_to_vector = f"Địa chỉ: {address}. Phòng: {room_name}, Tầng {floor}. Giá: {price}. Vệ sinh khép kín: {is_private_bathroom}. Thiết bị: {amenities}. Cho nuôi chó mèo: {allow_pets}. Ban công: {has_balcony}, Cửa sổ: {has_window}. Trạng thái: {status}"

        # 2. Tạo vector embedding (3072 dims)
        vector = get_text_embedding(text_to_vector)
        if not vector:
            print("❌ [QDRANT UPSERT CANCELED]: Không tạo được vector embedding!")
            return False

        # 3. Tạo Deterministic UUIDv5 duy nhất theo Địa chỉ + Tên phòng
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{address.strip().lower()}_{str(room_name).strip().lower()}"))

        payload = {
            "address": address,
            "room_name": room_name,
            "floor": floor,
            "price": price,
            "is_private_bathroom": is_private_bathroom,
            "amenities": amenities,
            "allow_pets": allow_pets,
            "media_urls": media_urls,
            "move_in_date": move_in_date,
            "has_balcony": has_balcony,
            "has_window": has_window,
            "status": status,
            "raw_data": extracted_data
        }

        # 4. Lưu/Cập nhật vào Qdrant
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
        print(f"✅ [QDRANT UPSERT SUCCESS]: ID = {point_id} | Địa chỉ = {address} | Trạng thái = {status}")
        return True

    except Exception as e:
        print("❌ [QDRANT UPSERT EXCEPTION]:", e)
        return False


def update_room_status_in_db(address: str, room_name: str, new_status: str = "ĐÃ CHO THUÊ") -> bool:
    """Cập nhật trạng thái phòng khi chủ nhà báo đã cho thuê"""
    try:
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{address.strip().lower()}_{str(room_name).strip().lower()}"))
        
        # Lấy point hiện tại ra để update payload
        points = qdrant_client.retrieve(collection_name=COLLECTION_NAME, ids=[point_id])
        if points:
            payload = points[0].payload
            payload["status"] = new_status
            payload["raw_data"]["status"] = new_status
            
            # Cập nhật lại payload
            qdrant_client.set_payload(
                collection_name=COLLECTION_NAME,
                payload={"status": new_status},
                points=[point_id]
            )
            print(f"🎉 [UPDATE STATUS SUCCESS]: Đã đổi trạng thái phòng {room_name} - {address} thành '{new_status}'")
            return True
    except Exception as e:
        print("❌ [UPDATE STATUS ERROR]:", e)
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


# --- 6. HÀM QUẢN LÝ PENDING MEDIA CACHE ---
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


# --- 7. LUỒNG XỬ LÝ CHÍNH VỚI AI VÀ 12 TRƯỜNG DỮ LIỆU ---
def process_zalo_ai_logic(user_id: str, message_text: str, media_items: list):
    incoming_media_urls = []
    for item in media_items:
        saved_url = save_media_file(item["url"], is_video=item.get("is_video", False))
        if saved_url:
            incoming_media_urls.append(saved_url)

    if not message_text.strip() and incoming_media_urls:
        add_pending_media(user_id, incoming_media_urls)
        total_pending = len(PENDING_MEDIA_CACHE.get(user_id, {}).get("urls", []))
        reply = f"📸 Em đã nhận {len(incoming_media_urls)} hình ảnh/video! (Tổng đã nhận: {total_pending} file)\n\n👉 Anh/Chị gửi thêm thông tin phòng (Địa chỉ đường/phường/quận, Tầng, Giá, Tiện nghi...) để em tạo bài nhé!"
        send_zalo_message(user_id, reply, media_urls=incoming_media_urls)
        return

    pending_urls = get_and_clear_pending_media(user_id)
    all_current_media = list(dict.fromkeys(pending_urls + incoming_media_urls))
    urls_to_send = all_current_media

    try:
        found_rooms = []
        is_greeting = any(kw in message_text.lower() for kw in ["chào", "hi", "hello", "bắt đầu"])
        if not is_greeting and message_text.strip():
            found_rooms = search_rooms_from_db(message_text)

        system_prompt = f"""
Bạn là Trợ lý AI Quản lý và Tư vấn Phòng trọ thông minh trên Zalo.
Phân tích tin nhắn người dùng và trích xuất/xử lý chính xác 12 thông tin phòng trọ.

Tin nhắn người dùng: "{message_text}"
Hình ảnh/Video gửi kèm: {json.dumps(all_current_media)}
Dữ liệu các phòng tìm thấy trong CSDL Qdrant: {json.dumps(found_rooms, ensure_ascii=False)}

YÊU CẦU PHÂN LOẠI "action":
1. "ADD_ROOM": Người dùng đăng/thêm bài cho thuê phòng mới.
2. "SEARCH_ROOM": Người dùng đi tìm phòng trọ.
3. "UPDATE_STATUS": Người dùng thông báo phòng ĐÃ CHO THUÊ / CÓ KHÁCH THUÊ RỒI.

QUY TRÌNH BẮT BUỘC DÀNH CHO 12 TRƯỜNG DỮ LIỆU:
1. `address`: Phải trích xuất ĐẦY ĐỦ nhất có thể dạng: [Số nhà, Đường/Phố], [Phường/Xã], [Quận/Huyện], [Tỉnh/Thành phố].
2. `room_name`: Tên hoặc số phòng (VD: Phòng 301, Phòng tầng 2...).
3. `floor`: Tầng thứ mấy (VD: Tầng 3).
4. `price`: Giá thuê (VD: 3.5 triệu/tháng).
5. `is_private_bathroom`: Khép kín hay dùng chung? (VD: Khép kín).
6. `amenities`: Thiết bị có sẵn (Điều hòa, nóng lạnh, máy giặt...).
7. `allow_pets`: Có cho nuôi chó mèo không? (Có / Không / Thỏa thuận).
8. `media_urls`: Danh sách URL ảnh/video phòng.
9. `move_in_date`: Ngày/Thời gian có thể vào ở (VD: Ở ngay, Đầu tháng sau...).
10. `has_balcony`: Có ban công không? (Có / Không).
11. `has_window`: Có cửa sổ không? (Có / Không).
12. `status`: Trạng thái phòng ("TRỐNG" hoặc "ĐÃ CHO THUÊ").

XỬ LÝ CHI TIẾT THEO ACTION:
- Nếu `action` == "ADD_ROOM":
  Trích xuất 12 trường vào `extracted_data`. Đặt `status` mặc định là "TRỐNG".
  Nếu thiếu địa chỉ rõ ràng -> Soạn `ai_reply` nhắc khách bổ sung địa chỉ chi tiết (Đường, Phường, Quận, Thành phố).

- Nếu `action` == "SEARCH_ROOM":
  Chỉ gợi ý các phòng có `status` == "TRỐNG". Nếu phòng đã cho thuê thì bỏ qua hoặc thông báo rõ.
  Soạn `ai_reply` đầy đủ 12 thông tin phòng cho người tìm trọ dễ xem.

- Nếu `action` == "UPDATE_STATUS":
  Xác định phòng nào được báo đã thuê dựa trên tin nhắn và `found_rooms`. Đặt `status` = "ĐÃ CHO THUÊ".

TRẢ VỀ DUY NHẤT CHUỖI JSON ĐÚNG ĐỊNH DẠNG (KHÔNG DÙNG FENCE CODE BLOCK ```):
{{
    "action": "ADD_ROOM" | "SEARCH_ROOM" | "UPDATE_STATUS",
    "extracted_data": {{
        "address": "Số nhà, Đường, Phường, Quận, Tỉnh/Thành phố",
        "room_name": "Tên phòng",
        "floor": "Tầng bao nhiêu",
        "price": "Giá thuê",
        "is_private_bathroom": "Khép kín hay không",
        "amenities": "Điều hòa, nóng lạnh, máy giặt...",
        "allow_pets": "Cho phép nuôi chó mèo không",
        "move_in_date": "Thời gian vào ở",
        "has_balcony": "Có ban công không",
        "has_window": "Có cửa sổ không",
        "status": "TRỐNG" hoặc "ĐÃ CHO THUÊ"
    }},
    "ai_reply": "Câu trả lời thân thiện gửi lại Zalo cho người dùng"
}}
"""

        raw_text = generate_content_with_retry(system_prompt, mime_type="application/json")
        if not raw_text:
            raise Exception("Gemini không phản hồi dữ liệu.")

        result_data = json.loads(raw_text)
        ai_reply = result_data.get("ai_reply", "Dạ em đã ghi nhận thông tin rồi ạ!")
        action = result_data.get("action")
        extracted = result_data.get("extracted_data", {})

        if action == "ADD_ROOM":
            address = str(extracted.get("address", "")).strip()
            if address and address.lower() not in ["null", "none", "chưa rõ", ""]:
                upsert_room_to_db(extracted, all_current_media)
            else:
                if all_current_media:
                    add_pending_media(user_id, all_current_media)

        elif action == "UPDATE_STATUS":
            address = extracted.get("address", "")
            room_name = extracted.get("room_name", "")
            if address and room_name:
                update_room_status_in_db(address, room_name, new_status="ĐÃ CHO THUÊ")

        elif action == "SEARCH_ROOM" and found_rooms:
            # Lấy ảnh phòng còn TRỐNG để gửi cho khách
            db_media = []
            for room in found_rooms:
                if room.get("status") == "TRỐNG":
                    db_media.extend(room.get("media_urls", []))
            if db_media:
                urls_to_send = db_media

    except Exception as err:
        print("❌ [AI Logic Exception]:", err)
        ai_reply = "Dạ hệ thống đang bận một chút, anh/chị chờ em vài giây rồi nhắn lại giúp em nhé!"

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

        if "user_follow_oa" in event_name:
            send_zalo_message(sender_id, "👋 Chào mừng bạn! Hãy gửi thông tin hoặc hình ảnh phòng trọ để bắt đầu nhé!")
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
        print("❌ [Webhook Exception]:", e)

    return {"status": "success"}


# --- 9. MAIN ENTRY POINT ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"🚀 Server đang chạy tại cổng {port}...")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)