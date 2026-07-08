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


app = FastAPI()
@app.get("/")
async def root():
    return {"status": "running", "message": "Zalo Room Chatbot AI is active!"}

# Tên bảng lưu trữ phòng trọ trên Qdrant
ROOM_COLLECTION = "room_collection"

# --- 1. KHỞI TẠO CÁC KẾT NỐI (GEMINI & QDRANT) ---

# Khởi tạo Client Gemini an toàn
api_key_env = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key_env) if api_key_env else None

# Lấy cấu hình môi trường cho Qdrant Cloud (Thay thế Elasticsearch)
qdrant_url = os.environ.get("QDRANT_URL")
qdrant_api_key = os.environ.get("QDRANT_API_KEY")

try:
    if qdrant_url and qdrant_api_key:
        qd_client = QdrantClient(url=qdrant_url, api_key=qdrant_api_key)
        
        # Tự động tạo Collection nếu chưa có trên Qdrant Cloud
        # Model 'text-embedding-004' tạo ra vector 768 chiều
        collections = qd_client.get_collections().collections
        collection_names = [c.name for c in collections]
        
        if ROOM_COLLECTION not in collection_names:
            qd_client.create_collection(
                collection_name=ROOM_COLLECTION,
                vectors_config=VectorParams(size=768, distance=Distance.COSINE),
            )
        print(" Connected to Qdrant Cloud & Collection is ready!")
    else:
        raise ValueError("Chưa cấu hình QDRANT_URL hoặc QDRANT_API_KEY trong môi trường.")
except Exception as e:
    logging.error(f"❌ Thất bại khi kết nối Qdrant: {e}")
    print("⚠ Hệ thống sẽ chạy tạm thời ở chế độ KHÔNG CÓ DATABASE để tránh sập Server.")
    qd_client = None 


# --- 2. HÀM TẠO VECTOR EMBEDDING BẰNG GEMINI ---
def get_text_embedding(text: str):
    """Biến đổi câu chữ thành mảng số vector để tìm kiếm ngữ nghĩa"""
    if not client:
        return None
    try:
        response = client.models.embed_content(
            model="text-embedding-004",
            contents=text
        )
        return response.embeddings[0].values
    except Exception as e:
        print("❌ Lỗi tạo Embedding từ Gemini:", e)
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
            collection_name=ROOM_COLLECTION,
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

def search_rooms_from_db(user_query: str):
    if not qd_client:
        print("❌ Qdrant chưa được kết nối. Không thể tìm kiếm.")
        return []
    try:
        query_vector = get_text_embedding(user_query)
        if not query_vector:
            return []
            
        # Tìm kiếm không gian Vector (Top 3 phòng có nghĩa sát nhất)
        search_result = qd_client.search(
            collection_name=ROOM_COLLECTION,
            query_vector=query_vector,
            limit=3
        )
        
        rooms_list = []
        for hit in search_result:
            source = hit.payload
            rooms_list.append({
                "Tiêu đề": source.get("title"),
                "Giá": source.get("price"),
                "Địa chỉ": source.get("address"),
                "Mô tả": source.get("description")
            })
        return rooms_list
    except Exception as e:
        print("Lỗi truy vấn Qdrant:", e)
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

def send_zalo_message(recipient_id: str, text: str):
    token = os.environ.get("ZALO_ACCESS_TOKEN")
    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    headers = {"access_token": token, "Content-Type": "application/json"}
    
    payload = {
        "recipient": {
            "user_id": recipient_id
        },
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "transaction",
                    "elements": [{
                        "title": "Hệ Thống Phòng Trọ AI",
                        "subtitle": text
                    }],
                    "buttons": [
                        {
                            "title": "➕ Đăng/Thêm Phòng",
                            "type": "oa.query.show",
                            "payload": "Thêm phòng trọ mới tại: "
                        },
                        {
                            "title": "🔍 Tìm Kiếm Phòng",
                            "type": "oa.query.show",
                            "payload": "Tìm phòng trọ tại: "
                        }
                    ]
                }
            }
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        res_json = response.json()
        
        if res_json.get("error") == -216:
            print("⚠️ Phát hiện Token hết hạn! Đang tiến hành tự động gia hạn...")
            new_token = refresh_zalo_access_token()
            if new_token:
                headers["access_token"] = new_token
                response = requests.post(url, headers=headers, json=payload)
                res_json = response.json()
                
        print("--- KẾT QUẢ TRẢ VỀ TỪ ZALO API V3 ---", res_json)
        return res_json
    except Exception as req_err:
        print("Lỗi kết nối Zalo API:", req_err)
        return None


# --- 5. WEBHOOK XỬ LÝ CHÍNH ---
@app.post("/webhook/zalo")
def zalo_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        raw_body = await request.body()
        data = json.loads(raw_body.decode("utf-8"))
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
                    model='gemini-2.5-flash',
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
        print("Lỗi nghiêm trọng tại Webhook tổng:", e)
        
    return {"status": "success"}
    
    
    
    
from fastapi.responses import HTMLResponse # <-- Nhớ thêm import này ở đầu file nếu chưa có

# Thay thế tên file và chuỗi mã chính xác theo file Zalo cấp cho bạn
@app.get("/zalo_verifierCjNXTBZqO5H_qBfhZTypOtR2daEQj4iKE3Wn.html", response_class=HTMLResponse)
async def zalo_verification():
    # Điền đoạn mã bên trong file HTML của Zalo vào đây
    return """<!DOCTYPE html>
<html lang="en">

<head>
    <meta property="zalo-platform-site-verification" content="CjNXTBZqO5H_qBfhZTypOtR2daEQj4iKE3Wn" />
</head>

<body>
CjNXTBZqO5H_qBfhZTypOtR2daEQj4iKE3Wn
</body>

</html>"""