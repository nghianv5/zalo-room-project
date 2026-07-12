import os
import json
import requests
import uuid
from fastapi import FastAPI, Request, BackgroundTasks
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from google import genai
from google.genai import types  
from config import Config
import logging
from fastapi.responses import HTMLResponse # <-- Nhớ thêm import này ở đầu file nếu chưa có

app = FastAPI()
@app.get("/")
async def root():
    return {"status": "running", "message": "Zalo Room Chatbot AI is active!"}

# Tên bảng lưu trữ phòng trọ trên Qdrant
COLLECTION_NAME = "rooms_v3"
# --- 1. KHỞI TẠO CÁC KẾT NỐI (GEMINI & QDRANT) ---

# Khởi tạo Client Gemini an toàn
api_key_env = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key_env) if api_key_env else None

# 1. Khởi tạo client kết nối Qdrant
qdrant_client = QdrantClient(
    url=os.environ.get("QDRANT_URL"),
    api_key=os.environ.get("QDRANT_API_KEY")
)

# 2. Sửa đoạn tự động tạo bảng (Nâng size lên 3072)
try:
    if not qdrant_client.collection_exists(collection_name=COLLECTION_NAME):
        print(f"⚠ Không tìm thấy collection '{COLLECTION_NAME}'. Đang tạo mới...")
        qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=3072,  # <--- SỬA LÊN 3072 ĐỂ KHỚP VỚI VECTOR EMBEDDING MỚI
                distance=Distance.COSINE
            )
        )
        print(f" Tạo thành công collection '{COLLECTION_NAME}' (3072 dims)!")
except Exception as qdrant_init_error:
    print("❌ Lỗi khi khởi tạo kiểm tra Collection Qdrant:", qdrant_init_error)

# Lấy cấu hình môi trường cho Qdrant Cloud (Thay thế Elasticsearch)
qdrant_url = os.environ.get("QDRANT_URL")
qdrant_api_key = os.environ.get("QDRANT_API_KEY")

try:
    if not qdrant_client.collection_exists(collection_name=COLLECTION_NAME):
        print(f"⚠ Không tìm thấy collection '{COLLECTION_NAME}'. Đang tạo mới...")
        qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=3072,  # <--- SỬA THÀNH 3072 ĐỂ KHỚP VỚI TEXT-EMBEDDING-004
                distance=Distance.COSINE
            )
        )
        print(f" Tạo thành công collection '{COLLECTION_NAME}' (3072 dims)!")
except Exception as qdrant_init_error:
    print("❌ Lỗi khi khởi tạo kiểm tra Collection Qdrant:", qdrant_init_error)
    
    
    
# --- 2. HÀM TẠO VECTOR EMBEDDING BẰNG GEMINI ---
def get_text_embedding(text: str):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        # Ép về cổng v1beta để text-embedding-004 hoạt động mượt qua HTTP thuần
        url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={api_key}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "content": {"parts": [{"text": text}]},
            "outputDimensionality": 768  # Ép chặt Google trả về đúng 768 chiều khớp với Qdrant
        }
        
        response = requests.post(url, headers=headers, json=payload)
        res_json = response.json()
        
        if "embedding" in res_json and "values" in res_json["embedding"]:
            return res_json["embedding"]["values"]
        return None
    except Exception as e:
        print("❌ Lỗi tạo Embedding trực tiếp:", e)
        return None


# --- 3. CÁC HÀM XỬ LÝ DATABASE QDRANT ---
def insert_room_to_db(room_data: dict):
    if not qd_client:
        print("❌ Qdrant chưa được kết nối. Bỏ qua thao tác lưu.")
        return False
    try:
        # Gom thông tin phòng trọ làm giàu ngữ nghĩa cho Vector
        combined_text = f"{room_data.get('title', '')} {room_data.get('address', '')} {room_data.get('description', '')}"
        vector = get_text_embedding(combined_text)
        
        if not vector:
            return False
            
        point_id = str(uuid.uuid4())
        qd_client.upsert(
            collection_name=COLLECTION_NAME,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=room_data  # Lưu dữ liệu gốc (Tiêu đề, Giá, Địa chỉ, Mô tả)
                )
            ]
        )
        print("-> Đã lưu phòng vào Qdrant Cloud thành công!")
        return True
    except Exception as e:
        print("Lỗi khi thêm phòng vào Qdrant:", e)
        return False

def search_rooms_from_db(query_text: str):
    # Khối tạo embedding từ văn bản của bạn ở đây...
    query_vector = get_text_embedding(query_text)
    if not query_vector:
        return []
        
    try:
        # SỬA TẠI ĐÂY: Thay client.search bằng client.query_points
        response = qdrant_client.query_points(
            collection_name="rooms", # Tên collection của bạn
            query=query_vector,
            limit=3
        )
        
        # Trích xuất kết quả từ cấu trúc mới
        rooms = []
        for point in response.points:
            rooms.append(point.payload)
        return rooms
        
    except Exception as e:
        print("❌ Lỗi truy vấn Qdrant:", e)
        return []


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
            print(f"❌ Zalo OAuth không trả về JSON. Mã: {response.status_code}. Thô: {response.text[:200]}")
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
        print("❌ Thiếu Zalo Access Token")
        return
        
    url = "https://openapi.zalo.me/v3.0/oa/message" # URL CHUẨN KHÔNG ĐƯỢC THAY ĐỔI
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
        return response.json()
    except Exception as e:
        print("❌ Lỗi gọi API Zalo:", e)
        return None

# --- 5. HÀM XỬ LÝ CHẠY NGẦM (BACKGROUND TASK) ---
def process_zalo_ai_logic(user_id: str, message_text: str):
    print(f"🔄 [AI] Bắt đầu xử lý luồng chạy ngầm cho User: {user_id}")
    
    # Định nghĩa sẵn câu trả lời mặc định, nếu có sập toàn bộ thì khách vẫn nhận được tin này
    ai_reply = "🤖 Trợ lý AI đang xử lý thông tin, bạn vui lòng nhắn lại rõ hơn nhé!" 
    
    try:
        # 1. Tìm kiếm thông tin từ cơ sở dữ liệu Qdrant
        context = ""
        try:
            rooms = search_rooms_from_db(message_text)
            if rooms:
                context = f"Danh sách phòng trống phù hợp tìm thấy: {rooms}"
        except Exception as qdrant_err:
            print("❌ [AI] Lỗi tìm kiếm Qdrant nhưng bỏ qua để chạy tiếp:", qdrant_err)

        # 2. Thiết lập gọi API Chat Gemini v1 ổn định
        api_key = os.environ.get("GEMINI_API_KEY")
        chat_url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={api_key}"
        chat_headers = {"Content-Type": "application/json"}
        
        combined_prompt = f"""
        Bạn là trợ lý ảo tư vấn phòng trọ thông minh.
        Dữ liệu phòng trọ tìm được: {context}
        Khách hàng nhắn: "{message_text}"
        
        HÃY TRẢ VỀ ĐỊNH DẠNG JSON NGHIÊM NGẶT THEO MẪU SAU, KHÔNG ĐƯỢC THỪA CHỮ NÀO NGOÀI JSON:
        {{
            "action": "REPLY",
            "ai_reply": "Nội dung câu trả lời tư vấn bằng tiếng Việt thân thiện"
        }}
        """
        
        chat_payload = {
            "contents": [{"parts": [{"text": combined_prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"}
        }
        
        print("📡 [AI] Đang gửi yêu cầu sang Gemini API...")
        # Thêm timeout=10 để phòng trường hợp mạng nghẽn không bị treo hàm
        chat_response = requests.post(chat_url, headers=chat_headers, json=chat_payload, timeout=10)
        chat_res_json = chat_response.json()
        
        # 3. Bóc tách dữ liệu an toàn
        if "candidates" in chat_res_json and chat_res_json["candidates"]:
            raw_text = chat_res_json["candidates"][0]["content"]["parts"][0]["text"]
            print("📩 [AI] Văn bản thô nhận từ Gemini:", raw_text)
            
            try:
                # Ép kiểu JSON xem có đúng cấu trúc không
                result_data = json.loads(raw_text.strip())
                ai_reply = result_data.get("ai_reply", ai_reply)
                
                # Nếu là lệnh thêm phòng trọ tự động
                if result_data.get("action") == "ADD_ROOM":
                    extracted = result_data.get("extracted_data", {})
                    insert_room_to_db(extracted)
            except Exception as json_parse_err:
                print("⚠ [AI] Gemini trả về cấu trúc không phải JSON chuẩn. Ép lấy chữ thuần.")
                # Phòng hờ nếu Gemini trả về text thường, ta lấy luôn text đó trả lời khách
                ai_reply = raw_text
        else:
            print("❌ [AI] Khối phản hồi Google lỗi hoặc trống rỗng:", chat_res_json)
            
    except Exception as general_error:
        print("❌ [AI] Lỗi tổng quát trong luồng xử lý:", general_error)
        
    # === VỊ TRÍ CHÍ CHÍ MẠNG ===
    # Đưa hàm gửi tin nhắn ra RIÊNG BIỆT ở cuối cùng, đảm bảo DÙ LỖI HAY KHÔNG LỖI thì vẫn phải bắn tin nhắn về Zalo
    print(f"📤 [AI] Tiến hành gửi phản hồi về Zalo cho khách: {ai_reply}")
    send_zalo_message(user_id, ai_reply)

# --- 5. WEBHOOK XỬ LÝ CHÍNH ---
@app.post("/webhook/zalo")
def zalo_webhook(request_data: dict, background_tasks: BackgroundTasks):
    try:
        try:
            user_id = request_data.get("sender", {}).get("id") or request_data.get("recipient", {}).get("id")
            
            # Hãy chắc chắn bạn đã lấy được nội dung tin nhắn vào một biến, ví dụ: message_text
            message_text = request_data.get("message", {}).get("text", "")
            
            print(f"-> Nhận tin nhắn từ khách: {message_text}")
            
            if user_id and message_text:
                # --- ĐOẠN BẠN CẦN SỬA CHÍNH XÁC ---
                # Phải truyền đủ cả user_id VÀ message_text vào sau tên hàm
                background_tasks.add_task(process_zalo_ai_logic, user_id, message_text)
                # ----------------------------------
                
        except Exception as e:
            print("❌ Lỗi bóc tách webhook Zalo:", e)
        
        event_name = str(data.get("event_name", "")).strip()
        print(f"-> Nhận tin nhắn ")
        # SỰ KIỆN 1: Khách nhấn Quan tâm OA -> Gửi tin text thuần an toàn
        if "user_follow_oa" in event_name:
            recipient_id = data["sender"]["id"]
            welcome_text = "👋 Chào mừng bạn đến với Hệ Thống Tư Vấn Phòng Trọ Tự Động. Hãy gõ nhu cầu phòng trọ hoặc thông tin phòng cho thuê của bạn để trợ lý AI hỗ trợ nhé!"
            send_pure_text(recipient_id, welcome_text)
            return {"status": "success"}
            
        # SỰ KIỆN 2: Khách nhắn tin chữ / Bấm nút chức năng
        if "user_send_text" in event_name:
            recipient_id = data.get("user_id_by_app") or data["sender"]["id"]
            user_message = data["message"]["text"]
            
            print(f"-> Nhận tin nhắn từ khách: {user_message}")
            
            # Đọc trước dữ liệu Qdrant để mớm cho Gemini (Tối ưu hóa thành 1 lần gọi duy nhất)
            found_rooms = search_rooms_from_db(user_message)
            
            # Prompt TẤT CẢ TRONG MỘT
            combined_prompt = f"""
            Bạn là trợ lý ảo thông minh của Hệ thống Zalo OA Tư vấn phòng trọ. 
            Nhiệm vụ của bạn là phân tích tin nhắn của khách và chuẩn bị phản hồi.

            Tin nhắn của khách: "{user_message}"
            Dữ liệu phòng tìm thấy trong Database (nếu có): {json.dumps(found_rooms, ensure_ascii=False)}

            Hãy thực hiện 2 việc:
            1. Phân loại hành động ("action") thành "ADD_ROOM" (nếu muốn đăng/cho thuê phòng) hoặc "SEARCH_ROOM" (nếu muốn tìm/hỏi phòng).
            2. Soạn nội dung phản hồi ("ai_reply"):
               - Nếu là "ADD_ROOM": Xác nhận ghi nhận thành công và hiển thị lại các thông số bạn bóc tách được.
               - Nếu là "SEARCH_ROOM": Dựa trên danh sách mớm từ database để soạn câu trả lời ngắn gọn, lịch sự, có đầy đủ xuống dòng. Nếu không tìm thấy phòng thích hợp, hãy báo hết phòng khéo léo và xin thông tin liên hệ lại sau.

            Trả về định dạng JSON duy nhất, không kèm giải thích:
            {{
                "action": "ADD_ROOM" hoặc "SEARCH_ROOM",
                "extracted_data": {{
                    "title": "Tiêu đề phòng",
                    "price": "Giá",
                    "address": "Địa chỉ",
                    "description": "Mô tả chi tiết"
                }},
                "ai_reply": "Nội dung tin nhắn bạn đã soạn để gửi trực tiếp cho khách hàng"
            }}
            """
            
            try:
                # Gọi Gemini duy nhất một lần
                response = client.models.generate_content(
                    model='gemini-2.5-flash',  # <--- ĐỔI SANG BẢN 8B ĐỂ NÉ GIỚI HẠN 20 CÂU/NGÀY
                    contents=combined_prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
)
                
                result_data = json.loads(response.text.strip())
                action = result_data.get("action")
                ai_reply = result_data.get("ai_reply", "🤖 Trợ lý AI đang cập nhật thông tin, bạn vui lòng đợi chút nhé.")
                
                # Nếu khách muốn Đăng phòng -> Tiến hành lưu ngầm vào Qdrant
                if action == "ADD_ROOM":
                    print("-> Nhận diện: Chức năng THÊM PHÒNG TRỌ.")
                    extracted = result_data.get("extracted_data", {})
                    insert_room_to_db(extracted)
                    
            except Exception as ai_error:
                print("❌ Lỗi chi tiết trong khối xử lý Gemini/Qdrant:", ai_error)
                ai_reply = "🤖 Trợ lý AI đang quá tải lượt xử lý. Bạn vui lòng thử lại yêu cầu sau ít giây nhé!"
            
            # Gửi tin nhắn phản hồi cuối cùng tới Zalo
            send_zalo_message(recipient_id, ai_reply)
            
    except Exception as e:
        print("Lỗi tiếp nhận webhook đầu vào:", e)
        
    return {"status": "success"}
    
    






