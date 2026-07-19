import os
import json
import requests
import uuid
import time
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from config import Config
import google.generativeai as genai

app = FastAPI()

# --- 1. CẤU HÌNH VÀ KHỞI TẠO GEMINI API ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    print("✅ Đã cấu hình thành công Gemini API Key!")
else:
    print("❌ Thiếu GEMINI_API_KEY trong biến môi trường!")

# Khởi tạo mô hình Gemini Chat / Bóc tách dữ liệu
gemini_model = genai.GenerativeModel("gemini-2.5-flash")


genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

print("🔍 Danh sách các model hỗ trợ embedContent:")
for m in genai.list_models():
    if 'embedContent' in m.supported_generation_methods:
        print("  ->", m.name)

@app.get("/")
async def root():
    return {"status": "running", "message": "Zalo Room Chatbot AI is active!"}

# Tên bảng lưu trữ phòng trọ trên Qdrant
COLLECTION_NAME = "rooms_v3"

# --- 2. KHỞI TẠO CÁC KẾT NỐI (QDRANT CLOUD) ---
qdrant_client = QdrantClient(
    url=os.environ.get("QDRANT_URL"),
    api_key=os.environ.get("QDRANT_API_KEY")
)

# Khởi tạo bảng với size=768
try:
    if not qdrant_client.collection_exists(collection_name=COLLECTION_NAME):
        print(f"⚠ Không tìm thấy collection '{COLLECTION_NAME}'. Đang tạo mới...")
        qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=768,  
                distance=Distance.COSINE
            )
        )
        print(f"✅ Tạo thành công collection '{COLLECTION_NAME}' (768 dims)!")
except Exception as qdrant_init_error:
    print("❌ Lỗi khi khởi tạo kiểm tra Collection Qdrant:", qdrant_init_error)

# --- 3. HÀM TẠO VECTOR EMBEDDING BẰNG GEMINI API ---
# --- 3. HÀM TẠO VECTOR EMBEDDING QUA REST API (CHỐNG LỖI 404 TRUYỀN KIẾP) ---
def get_text_embedding(text: str, retries: int = 3, delay: int = 2):
    """
    Tạo Vector Embedding 768 chiều trực tiếp qua REST API của Gemini.
    Không dùng SDK genai.embed_content để tránh lỗi 404 version endpoint.
    """
    if not GEMINI_API_KEY:
        print("❌ [EMBEDDING] Thiếu GEMINI_API_KEY!")
        return None

    # Endpoint chuẩn hóa của Google Gemini Embedding
    url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    payload = {
        "model": "models/text-embedding-004",
        "content": {
            "parts": [
                {"text": text}
            ]
        },
        "taskType": "RETRIEVAL_DOCUMENT"
    }

    for attempt in range(retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            res_json = response.json()

            # Nếu thành công nhận về danh sách giá trị vector
            if "embedding" in res_json and "values" in res_json["embedding"]:
                return res_json["embedding"]["values"]
            
            print(f"⚠️ [EMBEDDING] Lần thử {attempt + 1} phản hồi không như kỳ vọng: {res_json}")

        except Exception as e:
            print(f"❌ [EMBEDDING] Lỗi kết nối tại lần thử {attempt + 1}: {e}")

        if attempt < retries - 1:
            time.sleep(delay)

    return None

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

def insert_room_to_db(room_data: dict):
    print(f"📥 [Qdrant] Tiến hành thêm phòng mới vào DB: {room_data}")
    try:
        description = f"Phòng trọ tại {room_data.get('address', 'Chưa rõ địa chỉ')}. " \
                      f"Giá: {room_data.get('price', 'Chưa rõ giá')}. " \
                      f"Diện tích: {room_data.get('area', 'Chưa rõ')} m2. " \
                      f"Mô tả chi tiết: {room_data.get('description', '')}"
                      
        vector = get_text_embedding(description)
        if not vector:
            print("❌ [Qdrant] Không thể tạo embedding cho phòng mới sau nhiều lần thử, hủy lưu!")
            return False
            
        point_id = str(uuid.uuid4())
        
        qdrant_client.upsert(
            collection_name=COLLECTION_NAME,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=room_data
                )
            ]
        )
        print(f"✅ [Qdrant] Đã lưu phòng mới thành công với ID: {point_id}")
        return True
    except Exception as e:
        print("❌ [Qdrant] Lỗi khi thêm phòng trọ vào cơ sở dữ liệu:", e)
        return False

# --- 5. CÁC HÀM GIA HẠN VÀ GỬI TIN NHẮN ZALO ---
def refresh_zalo_access_token():
    url = "https://oauth.zalo.me/v3.0/oa/access_token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "secret_key": Config.SECRET_KEY
    }
    payload = {
        "app_id": Config.APP_ID,
        "grant_type": "refresh_token",
        "refresh_token": os.environ.get("ZALO_REFRESH_TOKEN")
    }
    try:
        response = requests.post(url, headers=headers, data=payload)
        try:
            res_data = response.json()
        except Exception:
            print(f"❌ Zalo OAuth không trả về JSON. Mã: {response.status_code}.")
            return None

        if "access_token" in res_data:
            os.environ["ZALO_ACCESS_TOKEN"] = res_data["access_token"]
            if "refresh_token" in res_data:
                os.environ["ZALO_REFRESH_TOKEN"] = res_data["refresh_token"]
            print("🔄 Tự động gia hạn Zalo Access Token thành công!")
            return res_data["access_token"]
        else:
            print("❌ Lỗi từ Zalo OAuth JSON:", res_data)
            return None
    except Exception as e:
        print("❌ Lỗi kết nối khi cố gắng gia hạn Token:", e)
        return None

def send_pure_text(recipient_id: str, text: str):
    token = os.environ.get("ZALO_ACCESS_TOKEN")
    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    headers = {"access_token": token, "Content-Type": "application/json"}
    payload = {
        "recipient": {"user_id": recipient_id},
        "message": {"text": text}
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        return response.json()
    except Exception as e:
        print("Lỗi gửi text thuần:", e)
        return None

def send_zalo_message(user_id: str, ai_reply: str):
    access_token = os.environ.get("ZALO_ACCESS_TOKEN")
    if not access_token:
        print("❌ [ZALO] Thiếu Zalo Access Token trong biến môi trường!")
        return None
        
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
        response = requests.post(url, headers=headers, json=payload)
        res_data = response.json()
        
        print(f"📡 [ZALO API RESPONSE]: {res_data}")
        if res_data.get("error") != 0:
            print(f"❌ [ZALO ERROR]: Gửi tin thất bại. Mã lỗi: {res_data.get('error')} - Lý do: {res_data.get('message')}")
        else:
            print("✨ [ZALO SUCCESS]: Đã bắn tin nhắn thành công tới người dùng!")
            
        return res_data
    except Exception as e:
        print("❌ [ZALO CRITICAL ERROR]: Không thể kết nối đến API Zalo:", e)
        return None

# --- 6. LUỒNG XỬ LÝ CHẠY NGẦM BẰNG GEMINI SDK CHUẨN ---
def process_zalo_ai_logic(user_id: str, message_text: str):
    print(f"🔄 [AI] Bắt đầu xử lý luồng chạy ngầm cho User: {user_id}")
    ai_reply = "🤖 Trợ lý AI đang bận xử lý hệ thống, bạn vui lòng đợi vài giây rồi nhắn lại nhé!" 
    
    try:
        found_rooms = []
        
        # Chỉ truy vấn tìm phòng nếu tin nhắn không phải chào hỏi thông thường
        if not any(kw in message_text.lower() for kw in ["chào", "hi", "hello", "bắt đầu"]):
            found_rooms = search_rooms_from_db(message_text)
        
        combined_prompt = f"""
Bạn là trợ lý ảo thông minh của Hệ thống Zalo OA Tư vấn phòng trọ. 
Nhiệm vụ của bạn là phân tích tin nhắn của khách và chuẩn bị phản hồi phù hợp.

Tin nhắn của khách: "{message_text}"
Dữ liệu phòng tìm thấy trong Database (nếu có): {json.dumps(found_rooms, ensure_ascii=False)}

Hãy thực hiện 2 việc:
1. Phân loại hành động ("action") thành "ADD_ROOM" (nếu muốn đăng/cho thuê phòng) hoặc "SEARCH_ROOM" (nếu muốn tìm/hỏi phòng).
2. Soạn nội dung phản hồi ("ai_reply"):
   - Nếu là "ADD_ROOM": Xác nhận ghi nhận thành công và hiển thị lại các thông số bạn bóc tách được (Địa chỉ, Giá, Diện tích, Mô tả).
   - Nếu là "SEARCH_ROOM": Dựa trên danh sách từ database để soạn câu trả lời ngắn gọn, lịch sự, có đầy đủ xuống dòng. Nếu không tìm thấy phòng thích hợp, hãy báo hết phòng khéo léo và xin thông tin để liên hệ lại sau.

HÃY TRẢ VỀ ĐỊNH DẠNG JSON NGHIÊM NGẶT THEO MẪU SAU, KHÔNG ĐƯỢC CHỨA CÁC ĐÁM KÝ TỰ BAO BỌC KIỂU ```json VÀ TUYỆT ĐỐI KHÔNG VIẾT CHỮ NÀO NGOÀI KHỐI JSON:
{{
    "action": "ADD_ROOM" hoặc "SEARCH_ROOM",
    "extracted_data": {{
        "title": "Tiêu đề phòng",
        "price": "Giá",
        "address": "Địa chỉ",
        "area": "Diện tích số m2",
        "description": "Mô tả chi tiết"
    }},
    "ai_reply": "Nội dung tin nhắn bạn đã soạn để gửi trực tiếp cho khách hàng"
}}
"""
        
        print("📡 [AI] Đang gửi yêu cầu sang Gemini API...")
        
        # Gọi Gemini qua SDK chính thức thay vì dùng requests.post thủ công
        response = gemini_model.generate_content(
            combined_prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        
        raw_text = response.text
        print("📩 [AI] Văn bản thô nhận từ Gemini:", raw_text)
        
        try:
            clean_text = raw_text.strip()
            if clean_text.startswith("```json"):
                clean_text = clean_text[7:]
            if clean_text.endswith("```"):
                clean_text = clean_text[:-3]
            
            result_data = json.loads(clean_text.strip())
            ai_reply = result_data.get("ai_reply", ai_reply)
            action = result_data.get("action")
            
            if action == "ADD_ROOM":
                print("-> Nhận diện hành động: THÊM PHÒNG TRỌ.")
                extracted = result_data.get("extracted_data", {})
                insert_room_to_db(extracted)
        except Exception as json_parse_err:
            print("⚠ [AI] Phản hồi lỗi cấu trúc JSON, ép lấy văn bản thuần:", json_parse_err)
            ai_reply = raw_text

    except Exception as general_error:
        print("❌ [AI] Lỗi tổng quát trong luồng xử lý ngầm:", general_error)
        
    print(f"📤 [AI] Tiến hành bắn phản hồi về Zalo: {ai_reply}")
    send_zalo_message(user_id, ai_reply)

# --- 7. WEBHOOK TIẾP NHẬN CHÍNH ---
@app.post("/webhook/zalo")
def zalo_webhook(request_data: dict, background_tasks: BackgroundTasks):
    try:
        event_name = str(request_data.get("event_name", "")).strip()
        
        # SỰ KIỆN 1: Khách nhấn Quan tâm OA
        if "user_follow_oa" in event_name:
            recipient_id = request_data.get("sender", {}).get("id")
            if recipient_id:
                welcome_text = "👋 Chào mừng bạn đến với Hệ Thống Tư Vấn Phòng Trọ Tự Động. Hãy gõ nhu cầu phòng trọ hoặc thông tin phòng cho thuê của bạn để trợ lý AI hỗ trợ nhé!"
                send_pure_text(recipient_id, welcome_text)
            return {"status": "success"}
            
        # SỰ KIỆN 2: Khách nhắn tin chữ / Bấm nút chức năng
        if "user_send_text" in event_name:
            user_id = request_data.get("user_id_by_app") or request_data.get("sender", {}).get("id")
            user_message = request_data.get("message", {}).get("text", "")
            
            print(f"📥 -> Nhận tin nhắn từ khách hàng: {user_message}")
            
            if user_id and user_message:
                background_tasks.add_task(process_zalo_ai_logic, user_id, user_message)
                    
    except Exception as e:
        print("❌ Lỗi tiếp nhận webhook đầu vào:", e)
        
    return {"status": "success"}
    
@app.get("/check-models")
def check_gemini_models():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"error": "Thiếu GEMINI_API_KEY"}
    
    # Gọi trực tiếp REST API hỏi Google xem Key này dùng được những Model nào
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        
        # Lọc ra danh sách các model hỗ trợ embedContent
        embed_models = []
        if "models" in data:
            for m in data["models"]:
                methods = m.get("supportedGenerationMethods", [])
                if "embedContent" in methods or "batchEmbedContents" in methods:
                    embed_models.append({
                        "name": m.get("name"),
                        "displayName": m.get("displayName"),
                        "methods": methods
                    })
        return {
            "total_models_found": len(data.get("models", [])),
            "available_embedding_models": embed_models,
            "raw_response": data
        }
    except Exception as e:
        return {"error": str(e)}