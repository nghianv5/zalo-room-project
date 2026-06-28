import os
import json
import requests
from fastapi import FastAPI, Request
from elasticsearch import Elasticsearch
from google import genai
from config import Config

app = FastAPI()
elastic_url = Config.ELASTICSEARCH_URL
elastic_api_key = Config.ELASTIC_API_KEY
ROOM_INDEX = Config.ROOM_INDEX
# --- 1. KHỞI TẠO CÁC KẾT NỐI ---

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))


# --- 2. HÀM BỔ TRỢ ELASTICSEARCH ---
def insert_room_to_es(room_data: dict):
    try:
        response = es.index(index=ROOM_INDEX, document=room_data)
        print("-> Đã lưu phòng vào Elasticsearch:", response.get("result"))
        return True
    except Exception as e:
        print("Lỗi khi thêm phòng vào ES:", e)
        return False

def search_rooms_from_es(user_query: str):
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


# --- 3. HÀM GỬI TIN NHẮN ZALO ĐÍNH KÈM NÚT BẤM (CẤU TRÚC CHUẨN V3) ---
def send_zalo_message(recipient_id: str, text: str):
    token = os.environ.get("ZALO_ACCESS_TOKEN")
    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    headers = {"access_token": token, "Content-Type": "application/json"}
    
    payload = {
        "recipient": {
            "user_id": recipient_id
        },
        "message": {
            "text": text,
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "transaction",
                    "elements": [{
                        "title": "Hệ Thống Phòng Trọ AI",
                        "subtitle": "Chọn chức năng nhanh bên dưới:"
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
        print("--- KẾT QUẢ TRẢ VỀ TỪ ZALO API V3 ---", response.json())
        return response.json()
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
            welcome_text = "👋 Xin chào! Chào mừng bạn đến với Hệ Thống Tư Vấn Phòng Trọ Tự Động.\n\nHãy nhấn vào các nút chức năng bên dưới để bắt đầu trải nghiệm nhé!"
            send_zalo_message(recipient_id, welcome_text)
            return {"status": "success"}
            
        # SỰ KIỆN 2: Khách nhắn tin chữ / Bấm nút
        if "user_send_text" in event_name:
            recipient_id = data.get("user_id_by_app") or data["sender"]["id"]
            user_message = data["message"]["text"]
            
            print(f"-> Nhận tin nhắn từ khách: {user_message}")
            ai_reply = "🤖 Hệ thống đang bận xử lý, bạn vui lòng thử lại sau vài phút nhé!"
            
            # Khởi tạo prompt định tuyến ý định cho Gemini
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
                # --- ĐÃ SỬA: Ép đúng 1 tham số vị trí duy nhất là model, còn lại dùng keyword ---
                router_response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=router_prompt,
                    config={'response_mime_type': 'application/json'}
                )
                
                intent_data = json.loads(router_response.text.strip())
                action = intent_data.get("action")
                extracted = intent_data.get("extracted_data", {})
                
                # CHỨC NĂNG 1: THÊM PHÒNG
                if action == "ADD_ROOM":
                    print("-> Nhận diện: Chức năng THÊM PHÒNG TRỌ.")
                    success = insert_room_to_es(extracted)
                    if success:
                        ai_reply = f"✅ Đã thêm phòng trọ thành công vào hệ thống!\n📍 Địa chỉ: {extracted.get('address', 'Chưa rõ')}\n💰 Giá: {extracted.get('price', 'Chưa rõ')}\n🤖 Cảm ơn bạn đã cập nhật dữ liệu."
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
                    
                    # --- ĐÃ SỬA TIẾP: Đồng bộ cú pháp gọi model chuẩn ở khối reply ---
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