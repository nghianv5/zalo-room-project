import os
import json
import requests
from fastapi import FastAPI, Request
from elasticsearch import Elasticsearch
from google import genai
from google.genai import types  # CẦN THIẾT: Để cấu hình chuẩn MIME Type JSON
from config import Config
import logging

app = FastAPI()

ROOM_INDEX = Config.ROOM_INDEX

# --- 1. KHỞI TẠO CÁC KẾT NỐI ---

# Cấu hình Client Gemini an toàn qua Header chống lỗi 401 trên môi trường Cloud
api_key_env = os.environ.get("GEMINI_API_KEY")
client = genai.Client(
    http_options={'headers': {'x-goog-api-key': api_key_env}} if api_key_env else None
)

# Lấy các cấu hình môi trường cho Elasticsearch
elastic_url = os.environ.get("ELASTICSEARCH_URL") or getattr(Config, "ELASTICSEARCH_URL", None)
elastic_api_key = os.environ.get("ELASTIC_API_KEY") or getattr(Config, "ELASTIC_API_KEY", None)

try:
    if elastic_api_key and elastic_url:
        # Nếu chạy trên Render sử dụng API Key
        es = Elasticsearch(
            elastic_url,
            api_key=elastic_api_key
        )
    elif elastic_url:
        # Nếu chạy dưới máy tính cá nhân (localhost)
        es = Elasticsearch(elastic_url)
    else:
        raise ValueError("Chưa cấu hình ELASTICSEARCH_URL trong môi trường hoặc file Config.")
    
    # Kiểm tra thực tế bằng một lệnh đọc thử thông tin hệ thống
    info = es.info()
    print(f" Kết nối Elasticsearch trực tuyến thành công! Phiên bản: {info['version']['number']}")

except Exception as e:
    logging.error(f"❌ Thất bại khi kết nối hoặc chứng thực Elasticsearch: {e}")
    print("⚠ Hệ thống sẽ chạy tạm thời ở chế độ KHÔNG CÓ DATABASE để tránh sập Server.")
    es = None 


# --- 2. HÀM BỔ TRỢ ELASTICSEARCH ---
def insert_room_to_es(room_data: dict):
    if not es:
        print("❌ Elasticsearch chưa được kết nối. Bỏ qua thao tác lưu.")
        return False
    try:
        response = es.index(index=ROOM_INDEX, document=room_data)
        print("-> Đã lưu phòng vào Elasticsearch:", response.get("result"))
        return True
    except Exception as e:
        print("Lỗi khi thêm phòng vào ES:", e)
        return False

def search_rooms_from_es(user_query: str):
    if not es:
        print("❌ Elasticsearch chưa được kết nối. Không thể tìm kiếm.")
        return []
    try:
        query_body = {
            "query": {
                "multi_match": {
                    "query": user_query,
                    "fields": ["title", "description", "address", "district"]
                }
            },
            "size": 3
        }
        response = es.search(index=ROOM_INDEX, body=query_body)
        hits = response['hits']['hits']
        
        rooms_list = []
        for hit in hits:
            source = hit['_source']
            rooms_list.append({
                "Tiêu đề": source.get("title"),
                "Giá": source.get("price"),
                "Địa chỉ": source.get("address"),
                "Mô tả": source.get("description")
            })
        return rooms_list
    except Exception as e:
        print("Lỗi truy vấn Elasticsearch:", e)
        return []

# Hàm tự động đổi Refresh Token lấy Access Token mới tinh từ Zalo
def refresh_zalo_access_token():
    app_id = Config.APP_ID
    secret_key = Config.SECRET_KEY
    refresh_token = os.environ.get("ZALO_REFRESH_TOKEN")
    
    url = "https://oauth.zalo.me/v3.0/oa/access_token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "secret_key": secret_key
    }
    payload = {
        "app_id": app_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }
    
    try:
        response = requests.post(url, headers=headers, data=payload)
        res_data = response.json()
        
        if "access_token" in res_data:
            new_access_token = res_data["access_token"]
            new_refresh_token = res_data.get("refresh_token", refresh_token)
            
            os.environ["ZALO_ACCESS_TOKEN"] = new_access_token
            os.environ["ZALO_REFRESH_TOKEN"] = new_refresh_token
            
            print("🔄 Tự động gia hạn Zalo Access Token thành công!")
            return new_access_token
        else:
            print("❌ Lỗi gia hạn Token từ API Zalo:", res_data)
            return None
    except Exception as e:
        print("❌ Lỗi kết nối khi cố gắng gia hạn Token:", e)
        return None


# --- 3. HÀM GỬI TIN NHẮN ZALO ĐÍNH KÈM NÚT BẤM (CẤU TRÚC CHUẨN V3) ---
def send_zalo_message(recipient_id: str, text: str):
    token = os.environ.get("ZALO_ACCESS_TOKEN")
    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    headers = {"access_token": token, "Content-Type": "application/json"}
    
    # SỬA LỖI PAYLOAD: Tách văn bản biến động (`text`) đưa vào tiêu đề của element 
    # để tránh xung đột cấu trúc tin nhắn hỗn hợp.
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


# --- 4. WEBHOOK XỬ LÝ CHÍNH ---
@app.post("/webhook/zalo")
async def zalo_webhook(request: Request):
    try:
        raw_body = await request.body()
        data = json.loads(raw_body.decode("utf-8"))
        event_name = str(data.get("event_name", "")).strip()
        
        # SỰ KIỆN 1: Khách nhấn Quan tâm OA
        if "user_follow_oa" in event_name:
            recipient_id = data["sender"]["id"]
            welcome_text = "Chào mừng bạn đến với Hệ Thống Tư Vấn Phòng Trọ Tự Động. Hãy chọn chức năng bên dưới nhé!"
            send_zalo_message(recipient_id, welcome_text)
            return {"status": "success"}
            
        # SỰ KIỆN 2: Khách nhắn tin chữ / Bấm nút
        if "user_send_text" in event_name:
            recipient_id = data.get("user_id_by_app") or data["sender"]["id"]
            user_message = data["message"]["text"]
            
            print(f"-> Nhận tin nhắn từ khách: {user_message}")
            ai_reply = "🤖 Hệ thống đang bận xử lý, bạn vui lòng thử lại sau vài phút nhé!"
            
            router_prompt = f"""
            Phân tích tin nhắn sau của người dùng: "{user_message}"
            Xác định hành động thuộc 1 trong 2 loại:
            - "ADD_ROOM": Muốn đăng bài, nạp dữ liệu, thêm phòng trọ mới.
            - "SEARCH_ROOM": Muốn tìm kiếm, thuê phòng, hỏi phòng trống.

            Trả về định dạng JSON duy nhất, không kèm giải thích:
            {{
                "action": "ADD_ROOM" hoặc "SEARCH_ROOM",
                "extracted_data": {{
                    "title": "Tiêu đề phòng trọ nếu có",
                    "price": "Giá phòng nếu có",
                    "address": "Địa chỉ nếu có",
                    "description": "Nội dung chi tiết hoặc từ khóa tìm kiếm"
                }}
            }}
            """
            
            try:
                # SỬA LỖI CONFIG: Sử dụng types.GenerateContentConfig để ép định dạng trả về JSON sạch
                router_response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=router_prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                )
                
                intent_data = json.loads(router_response.text.strip())
                action = intent_data.get("action")
                extracted = intent_data.get("extracted_data", {})
                
                # CHỨC NĂNG 1: THÊM PHÒNG
                if action == "ADD_ROOM":
                    print("-> Nhận diện: Chức năng THÊM PHÒNG TRỌ.")
                    success = insert_room_to_es(extracted)
                    if success:
                        ai_reply = f"✅ Đã thêm phòng trọ thành công vào hệ thống!\n📍 Địa chỉ: {extracted.get('address', 'Chưa rõ')}\n💰 Giá: {extracted.get('price', 'Chưa rõ')}"
                    else:
                        ai_reply = "❌ Hệ thống lưu trữ đang bận, không thể thêm phòng trọ lúc này."
                        
                # CHỨC NĂNG 2: TÌM PHÒNG
                else:
                    print("-> Nhận diện: Chức năng TÌM KIẾM PHÒNG TRỌ.")
                    search_keyword = extracted.get("description") or user_message
                    found_rooms = search_rooms_from_es(search_keyword)
                    
                    reply_prompt = f"""
                    Bạn là trợ lý ảo tư vấn phòng trọ của Zalo OA. Khách hỏi: "{user_message}"
                    Dữ liệu phòng tìm thấy từ database: {json.dumps(found_rooms, ensure_ascii=False)}
                    Hãy soạn một tin nhắn tư vấn ngắn gọn, lịch sự gửi cho khách dựa trên dữ liệu trên. Nếu có 0 phòng, hãy báo hết phòng khéo léo và xin thông tin liên hệ.
                    """
                    
                    ai_search_response = client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=reply_prompt
                    )
                    ai_reply = ai_search_response.text.strip()
                    
            except Exception as ai_error:
                print("Lỗi chi tiết trong khối xử lý Gemini/Elasticsearch:", ai_error)
                ai_reply = "🤖 Đã xảy ra lỗi khi phân tích tin nhắn. Bạn vui lòng thử lại sau!"
            
            # Gửi tin nhắn phản hồi
            send_zalo_message(recipient_id, ai_reply)
            
    except Exception as e:
        print("Lỗi nghiêm trọng tại Webhook tổng:", e)
        
    return {"status": "success"}