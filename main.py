import os
import json
import requests
import uuid
import time
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
from pydantic import BaseModel
from datetime import datetime
from fastapi.templating import Jinja2Templates

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# --- 0. THƯ MỤC MEDIA & CLOUDINARY ---
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


# --- 2. TẠO VECTOR EMBEDDING ---
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


# --- 6. LUỒNG XỬ LÝ AI ZALO (CHỈ CÒN TÌM KIẾM PHÒNG) ---
def process_zalo_ai_logic(
    message_text: str, 
    user_id: str = "SYSTEM",
):
    try:
        relevant_rooms = search_rooms_by_vector(message_text, top_k=5)

        system_prompt = f"""
Bạn là Trợ lý AI Chuyên Tư vấn và Tìm kiếm Phòng trọ trên Zalo.

MỤC TIÊU DUY NHẤT:
Hỗ trợ khách hàng TÌM KIẾM phòng trọ theo nhu cầu của họ (`SEARCH_ROOM`).

LƯU Ý QUAN TRỌNG:
- Kênh Zalo CHỈ DÀNH CHO KHÁCH TÌM PHÒNG.
- Mọi thao tác QUẢN LÝ PHÒNG từ Chủ nhà (như Đăng phòng mới, Sửa thông tin, Đổi trạng thái Đã cho thuê/Còn trống) KHÔNG ĐƯỢC THỰC HIỆN TRÊN ZALO.
- Nếu người dùng gửi yêu cầu thêm/sửa/xóa phòng hoặc đổi trạng thái phòng, hãy trả về `action`: "MANAGEMENT_NOT_ALLOWED".

DỮ LIỆU ĐẦU VÀO:
- Tin nhắn người dùng: "{message_text}"
- Danh sách phòng gợi ý từ database: {json.dumps(relevant_rooms, ensure_ascii=False)}

TRẢ VỀ DUY NHẤT 1 CHUỖI JSON ĐÚNG CẤU TRÚC:
{{
    "action": "SEARCH_ROOM" | "MANAGEMENT_NOT_ALLOWED",
    "ai_reply": "Nội dung phản hồi chi tiết, thân thiện cho khách hàng..."
}}
"""

        raw_text = generate_content_with_retry(system_prompt, mime_type="application/json")
        if not raw_text:
            raise Exception("Gemini không phản hồi dữ liệu.")

        result_data = json.loads(raw_text)
        action = result_data.get("action")
        ai_reply = result_data.get("ai_reply", "Dạ em có thể giúp gì cho anh/chị trong việc tìm phòng ạ?")

        urls_to_send = []

        if action == "SEARCH_ROOM" and relevant_rooms:
            for room in relevant_rooms:
                m_urls = room.get("8_media_urls") or room.get("media_urls")
                if m_urls and len(m_urls) > 0:
                    urls_to_send = m_urls
                    break

        elif action == "MANAGEMENT_NOT_ALLOWED":
            ai_reply = "Dạ hiện tại kênh Zalo chỉ hỗ trợ Khách hàng Tìm kiếm phòng trọ ạ!\n\nNếu anh/chị là Chủ nhà muốn **Đăng phòng, Chỉnh sửa thông tin hoặc Cập nhật trạng thái phòng**, vui lòng đăng nhập vào **Trang Quản trị Web Admin** để thực hiện nhé!"

    except Exception as err:
        print("❌ [AI Logic Exception]:", err)
        ai_reply = "Dạ hệ thống đang bận một chút, anh/chị vui lòng thử lại sau giây lát nhé!"

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
            send_zalo_message(sender_id, "👋 Chào mừng bạn! Bạn đang cần tìm phòng trọ ở khu vực nào, giá khoảng bao nhiêu ạ?")
            return {"status": "success"}

        if event_name in ["user_send_text", "user_send_image", "user_send_file", "user_send_video"]:
            message_obj = data.get("message", {})
            text = message_obj.get("text", "")

            background_tasks.add_task(process_zalo_ai_logic, text, sender_id)
    
    except Exception as e:
        print("❌ [Webhook Exception]:", e)

    return {"status": "success"}


# --- 8. API DÀNH CHO DASHBOARD WEB ADMIN ---
@app.get("/api/rooms")
def get_all_rooms(limit: int = 100):
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
                "status": p.get("12_status"),
                "landlord_phone": p.get("landlord_phone")
            })
        return JSONResponse(content={"status": "success", "data": results})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- 9. MAIN ENTRY POINT ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"🚀 Server Zalo Tìm Phòng đang chạy tại cổng {port}...")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)