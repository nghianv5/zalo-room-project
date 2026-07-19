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

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "running", "message": "Zalo Room Chatbot AI is active!"}

# Tên bảng lưu trữ phòng trọ trên Qdrant
COLLECTION_NAME = "rooms_v3"

# --- 1. KHỞI TẠO CÁC KẾT NỐI (QDRANT CLOUD) ---
qdrant_client = QdrantClient(
    url=os.environ.get("QDRANT_URL"),
    api_key=os.environ.get("QDRANT_API_KEY")
)

# Khởi tạo bảng với size=768 (Khớp hoàn toàn với mô hình text-embedding-004)
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

# --- 2. HÀM TẠO VECTOR EMBEDDING BẰNG GEMINI API VỚI CƠ CHẾ RETRY ---
def get_text_embedding(text: str, retries: int = 3, delay: int = 2):
    """
    Tạo Vector Embedding từ văn bản sử dụng mô hình text-embedding-004 qua API v1 chính thức.
    Bổ sung cơ chế tự động thử lại nếu gặp lỗi nghẽn hoặc quota tạm thời từ Google.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("❌ [EMBEDDING] Thiếu GEMINI_API_KEY trong biến môi trường!")
        return None
        
    url = f"https://generativelanguage.googleapis.com/v1/models/text-embedding-004:embedContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": 768  
    }
    
    for attempt in range(retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            res_json = response.json()
            
            if "embedding" in res_json and "values" in res_json["embedding"]:
                return res_json["embedding"]["values"]
            
            print(f"⚠️ [EMBEDDING] Lần thử {attempt + 1} thất bại. Phản hồi API: {res_json}")
            
        except Exception as e:
            print(f"❌ [EMBEDDING] Lỗi kết nối tại lần thử {attempt + 1}: {e}")
            
        if attempt < retries - 1:
            time.sleep(delay) 
            
    return None

# --- 3. CÁC HÀM XỬ LÝ DATABASE QDRANT ---
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

# --- 4. CÁC HÀM GIA HẠN VÀ GỬI TIN NHẮN ZALO ---
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
    
    # Luôn luôn gửi nội dung văn bản tự nhiên do AI tự soạn thảo
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

# --- 5. LUỒNG XỬ LÝ CHẠY NGẦM BẰNG GEMINI 2.5-FLASH ---
def process_zalo_ai_logic(user_id: str, message_text: str):
    print(f"🔄 [AI] Bắt đầu xử lý luồng chạy ngầm cho User: {user_id}")
    ai_reply = "🤖 Trợ lý AI đang bận xử lý hệ thống, bạn vui lòng đợi vài giây rồi nhắn lại nhé!" 
    
    try:
        found_rooms = []
        
        # Chỉ truy vấn tìm phòng trong database nếu người dùng không nhắn tin chào hỏi thông thường
        if not any(kw in message_text.lower() for kw in ["chào", "hi", "hello", "bắt đầu"]):
            found_rooms = search_rooms_from_db(message_text)
        
        api_key = os.environ.get("GEMINI_API_KEY")
        chat_url = f"https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash:generateContent?key={api_key}"
        chat_headers = {"Content-Type": "application/json"}
        
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
        
        chat_payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": combined_prompt + "\n\nTUYỆT ĐỐI CHỈ TRẢ VỀ DỮ LIỆU ĐỊNH DẠNG JSON, KHÔNG CHỨA CÁC ĐÁM KÝ TỰ BAO BỌC KIỂU