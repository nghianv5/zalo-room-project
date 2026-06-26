from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import uuid
from database import init_db, save_room, search_rooms, update_status
#from ai_service import analyze_zalo_message
from fastapi.responses import JSONResponse  # <-- THÊM DÒNG NÀY VÀO
from elasticsearch import Elasticsearch
from config import Config
import logging
import json
# Import thư viện mới tinh của Google
from google import genai

# Khởi tạo Client (Nó sẽ tự động bốc biến môi trường GOOGLE_API_KEY trên Render)
client = genai.Client()
import requests
import os

app = FastAPI(title="Hệ thống Zalo Bot Phòng Trọ Big Data")

UTF8_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
    
# Kết nối tới Elasticsearch cluster 
# 1. Tự động đọc thông tin từ Render (nếu không có thì mặc định chạy localhost để bạn vẫn test được ở máy nhà)
elastic_url = Config.ELASTICSEARCH_URL
elastic_api_key = Config.ELASTIC_API_KEY

# --- KHỞI TẠO GEMINI AI ---
#genai.configure(api_key=os.environ.get("GOOGLE_API_KEY"))
#model = genai.GenerativeModel("gemini-1.5-flash")
es = None

try:
    if elastic_api_key:
        # Nếu chạy trên Render sử dụng API Key
        es = Elasticsearch(
            elastic_url,
            api_key=elastic_api_key
        )
    else:
        # Nếu chạy dưới máy tính cá nhân (localhost)
        es = Elasticsearch(elastic_url)
    
    # Kiểm tra thực tế bằng một lệnh đọc thử thông tin hệ thống (chắc chắn hơn lệnh ping)
    info = es.info()
    print(f" Kết nối Elasticsearch trực tuyến thành công! Phiên bản: {info['version']['number']}")

except Exception as e:
    logging.error(f"❌ Thất bại khi kết nối hoặc chứng thực Elasticsearch: {e}")
    print("⚠ Hệ thống sẽ chạy tạm thời ở chế độ KHÔNG CÓ DATABASE để tránh sập Server.")
    es = None # Gán lại bằng None để các hàm sau không gọi lỗi

INDEX_NAME = Config.ROOM_INDEX

# Giả lập bộ nhớ đệm tạm thời để chờ chủ nhà bấm "Xác nhận" trên Zalo
pending_sessions = {}

# Khởi tạo database khi ứng dụng bắt đầu chạy
@app.on_event("startup")
def startup_event():
    init_db()

# --- MODEL ĐỊNH NGHĨA DỮ LIỆU ĐẦU VÀO ---
class ZaloMessage(BaseModel):
    user_id: str
    message: str
    media_urls: Optional[List[str]] = []

class SearchRequest(BaseModel):
    vi_tri: Optional[str] = None
    gia_toi_da: Optional[int] = None
    can_khep_kin: Optional[bool] = None
    can_dieu_hoa: Optional[bool] = None

# ==================== BOT 1: BOT THU THẬP (CHỦ NHÀ) ====================



@app.post("/webhook/bot1-thu-thap")
def webhook_bot1(payload: ZaloMessage):
    # 🌟 BƯỚC MỚI: BẮT LỆNH XÁC NHẬN TỪ CHỦ NHÀ
    user_message = payload.message.strip()
    
    # TRƯỜNG HỢP 1: CHỦ NHÀ XÁC NHẬN ĐÚNG
    if user_message.upper().startswith("XACNHAN_"):
        session_id = user_message.split("_")[1]
        
        try:
            # 🔄 Cập nhật trạng thái phòng thành 'con_trong' trên Elasticsearch
            es.update(
                index=INDEX_NAME,
                id=session_id,
                body={"doc": {"trang_thai": "con_trong"}}
            )
            return JSONResponse(
                content={
                    "status": "success",
                    "message": f"🎉 Tuyệt vời! Hệ thống đã kích hoạt thành công phòng trọ mã [{session_id}] vào kho Big Data và tự động đồng bộ lên bản đồ tìm kiếm công cộng!"
                },
                headers=UTF8_HEADERS
            )
        except Exception as e:
            return JSONResponse(
                status_code=404,
                content={"status": "error", "message": f"❌ Không tìm thấy phiên làm việc mã [{session_id}] hoặc phiên đã hết hạn."},
                headers=UTF8_HEADERS
            )
            
    # TRƯỜNG HỢP 2: CHỦ NHÀ ẤN HỦY
    if user_message.upper().startswith("HUY_") or user_message.upper() == "HUY":
        # Nếu họ gõ HUY_mã_phòng thì xóa đích danh, gõ HUY thông thường thì chỉ thông báo
        if "_" in user_message:
            session_id = user_message.split("_")[1]
            try:
                es.delete(index=INDEX_NAME, id=session_id)
            except:
                pass
        return JSONResponse(
            content={
                "status": "canceled",
                "message": "❌ Đã hủy phiên làm việc và xóa dữ liệu tạm. Anh/chị vui lòng gửi lại tin nhắn thông tin phòng mới để phân tích lại từ đầu."
            },
            headers=UTF8_HEADERS
        )

    if user_message.upper().startswith("XACNHAN_"):
        session_id = user_message.split("_")[1]
        
        # Thao tác lưu chính thức dữ liệu từ bộ nhớ tạm/Elasticsearch vào kho chính
        # (Ở đây bạn viết logic chuyển trạng thái phòng từ 'cho_duyet' sang 'da_duyet')
        
        return JSONResponse(
            content={
                "status": "success",
                "message": f"🎉 Tuyệt vời! Hệ thống đã lưu trữ thành công phòng trọ mã [{session_id}] vào kho Big Data và tự động đồng bộ lên bản đồ tìm kiếm!"
            },
            headers=UTF8_HEADERS
        )
        
    if user_message.upper() == "HUY":
        return JSONResponse(
            content={
                "status": "canceled",
                "message": "❌ Đã hủy phiên làm việc. Anh/chị vui lòng gửi lại tin nhắn thông tin phòng mới để hệ thống phân tích lại từ đầu."
            },
            headers=UTF8_HEADERS
        )
    
    
    """Tiếp nhận tin nhắn đăng phòng từ chủ nhà gửi qua Zalo"""
    # 1. Gọi AI phân tích và bóc tách câu chữ tự do
    ai_result = analyze_zalo_message(payload.message)
    
    room_data = ai_result.get("data", {})
    missing_fields = ai_result.get("thong_tin_thieu", [])
    
    # Bổ sung các trường hệ thống không do AI trích xuất (Ảnh, trạng thái trống)
    room_data["media_urls"] = payload.media_urls
    room_data["trang_thai"] = "con_trong" 
    
    # Nếu phát hiện lỗi hệ thống AI từ bước trước, báo lỗi kỹ thuật thay vì bảo thiếu tin
    if "loi_he_thong_ai" in missing_fields:
        return {
            "status": "error",
            "message": "🤖 Hệ thống đang bận hoặc cấu hình API Key chưa chính xác. Vui lòng liên hệ Admin để kiểm tra lại hệ thống!"
        }

    # 2. Kiểm tra nếu thiếu thông tin cốt lõi và dịch sang tiếng Việt thân thiện
    if missing_fields:
        # Tạo một từ điển để dịch tên trường sang tiếng Việt
        dictionary = {
            "dia_chi": "Địa chỉ phòng",
            "ten_phong":"Tên phòng",
                    "tang": "Tầng bao nhiêu",
                    "gia_thue": "Giá thuê phòng",
                    "khep_kin": "Thông tin phòng khép kín hay không",
                    "tien_nghi": {
                        "properties": {
                            "dieu_hoa": "Có điều hoà không?",
                            "nong_lanh": "Có nóng lạnh không?",
                            "may_giat": "Có máy giặt không?"
                        }
                    },
                    "cho_nuoi_thu_cung": "Có cho nuôi thú cưng k?",
                    "media_urls": "Chưa có ảnh hoặc video phòng",
                    "thoi_gian_o": "Thời gian ở bao lâu",
                    "co_ban_cong": "Có ban công hay không?",
                    "co_cua_so": "Có cửa sổ không?",
                    "trang_thai":"Còn trống hay đã cho thuê"  # "con_trong" hoặc "da_cho_thue"
        }
        
        # Chuyển đổi các trường bị thiếu sang tiếng Việt
        vietnamese_fields = [dictionary.get(field, field) for field in missing_fields]
        fields_text = ", ".join(vietnamese_fields)
        
        
        return JSONResponse(
            content={
                "status": "warning",
                "message": f"🤖 Cảm ơn anh/chị! Bài đăng đang thiếu thông tin quan trọng: *[{fields_text}]*."
            },
            headers={"Content-Type": "application/json; charset=utf-8"} # <-- Ép header trả về UTF-8
        )
    
    # 3. Lưu vào bộ nhớ tạm thời, tạo phiên xác nhận (Session)
    session_id = str(uuid.uuid4())[:8]
    pending_sessions[session_id] = room_data
    
    # 4. Gửi lại thông tin tổng hợp cho người dùng xác nhận
    summary_msg = (
        f"🤖 HỆ THỐNG ĐÃ TỰ ĐỘNG PHÂN TÍCH THÀNH CÔNG!\n"
        f"📍 Địa chỉ: {room_data.get('dia_chi')}\n"
        f"💰 Giá thuê: {room_data.get('gia_thue'):,} VNĐ\n"
        f"🚪 Khép kín: {'Có' if room_data.get('khep_kin') else 'Không'}\n"
        f"❄️ Điều hòa: {'Có' if room_data.get('dieu_hoa') else 'Không'}\n"
        f"----------------------\n"
        f"Vui lòng gửi lệnh phản hồi để xác nhận:\n"
        f"👉 Nhập 'XACNHAN_{session_id}' nếu thông tin ĐÚNG.\n"
        f"👉 Nhập 'HUY' để nhập lại."
    )
    
    # 4. Nếu đầy đủ thông tin, tiến hành cấp ID và chuẩn bị lưu
    session_id = str(uuid.uuid4())[:8]  # Tạo mã phiên ngắn gọn
    
    room_data = ai_result.get("data", {})
    room_data["id"] = session_id
    room_data["media_urls"] = data.media_urls
    room_data["user_id"] = data.user_id
    room_data["trang_thai"] = "pending"  # 🚨 TRẠNG THÁI CHỜ DUYỆT

    # 💾 Đẩy dữ liệu tạm thời này vào Elasticsearch
    es.index(index=INDEX_NAME, id=session_id, document=room_data)

    return JSONResponse(
        content={
            "status": "summary",
            "session_id": session_id,
            "message": f"🤖 HỆ THỐNG ĐÃ TỰ ĐỘNG PHÂN TÍCH THÀNH CÔNG!\n📍 Địa chỉ: {room_data['dia_chi']}\n💰 Giá thuê: {room_data['gia_thue']:,} VNĐ\n🚪 Khép kín: {'Có' if room_data['khep_kin'] else 'Không'}\n❄️ Điều hòa: {'Có' if room_data['dieu_hoa'] else 'Không'}\n----------------------\nVui lòng gửi lệnh phản hồi để xác nhận:\n👉 Nhập 'XACNHAN_{session_id}' nếu thông tin ĐÚNG.\n👉 Nhập 'HUY' để nhập lại."
        },
        headers=UTF8_HEADERS
    )
    return {"status": "summary", "session_id": session_id, "message": summary_msg}


@app.post("/webhook/bot1-thu-thap/confirm")
def confirm_room(session_id: str, action: str):
    """API giả lập việc chủ nhà bấm nút Xác nhận trên màn hình chat Zalo"""
    if action == "XACNHAN":
        if session_id not in pending_sessions:
            raise HTTPException(status_code=404, detail="Phiên làm việc đã hết hạn hoặc không tồn tại.")
        
        # Lấy dữ liệu tạm ra và lưu chính thức vào Big Data Elasticsearch
        final_data = pending_sessions.pop(session_id)
        room_id = str(uuid.uuid4())[:12]
        
        save_room(room_id, final_data)
        
        return {
            "status": "success",
            "message": f"✅ Tuyệt vời! Phòng trọ mã số [{room_id}] đã được lưu vào kho Big Data và sẵn sàng hiển thị cho khách thuê."
        }
    else:
        pending_sessions.pop(session_id, None)
        return {"status": "cancelled", "message": "❌ Đã hủy bỏ dữ liệu vừa nhập."}


@app.post("/room/change-status")
def change_room_status(room_id: str, status: str):
    """API chuyển đổi trạng thái phòng (ví dụ từ 'con_trong' sang 'da_cho_thue')"""
    if status not in ["con_trong", "da_cho_thue"]:
        raise HTTPException(status_code=400, detail="Trạng thái không hợp lệ.")
    update_status(room_id, status)
    return {"status": "success", "message": f"Đã cập nhật trạng thái phòng {room_id} thành: {status}"}


# ==================== BOT 2: BOT TƯ VẤN (KHÁCH THUÊ) ====================

@app.post("/webhook/bot2-tu_van/search")
def webhook_bot2_search(criteria: SearchRequest):
    """Tự động tạo câu lệnh Query Big Data quét siêu tốc dựa trên nhu cầu của khách hàng"""
    
    # Bộ lọc bắt buộc luôn luôn chỉ tìm phòng CÒN TRỐNG
    must_conditions = [{"term": {"trang_thai": "con_trong"}}]
    
    # Bộ lọc theo vị trí (Full text search thông minh của Elasticsearch)
    if criteria.vi_tri:
        must_conditions.append({"match": {"dia_chi": criteria.vi_tri}})
        
    # Bộ lọc theo khoảng giá
    if criteria.gia_toi_da:
        must_conditions.append({"range": {"gia_thue": {"lte": criteria.gia_toi_da}}})
        
    # Bộ lọc theo các tiện ích
    if criteria.can_khep_kin is not None:
        must_conditions.append({"term": {"khep_kin": criteria.can_khep_kin}})
        
    if criteria.can_dieu_hoa is not None:
        must_conditions.append({"term": {"dieu_hoa": criteria.can_dieu_hoa}})

    # Xây dựng cấu trúc Query chuẩn Elasticsearch DSL
    es_query = {
        "query": {
            "bool": {
                "must": must_conditions
            }
        },
        "size": 10 # Giới hạn lấy 10 phòng tối ưu nhất
    }
    
    results = search_rooms(es_query)
    
    if not results:
        return {"message": "🤖 Rất tiếc, hiện tại không tìm thấy phòng nào phù hợp với yêu cầu của bạn."}
        
    return {
        "message": f"🤖 Tìm thấy {len(results)} phòng phù hợp nhất cho bạn:",
        "rooms": results
    }
    
    
    
from fastapi.responses import HTMLResponse

# Thay 'zalo_verifierXXXXX.html' bằng đúng tên file bạn vừa tải từ Zalo về
@app.get("/zalo_verifierCjNXTBZqO5H_qBfhZTypOtR2daEQj4iKE3Wn.html", response_class=HTMLResponse)
async def verify_zalo():
    html_content = """
    <html>
    <head>
        <meta property="zalo-platform-site-verification" content="CjNXTBZqO5H_qBfhZTypOtR2daEQj4iKE3Wn" />
    </head>
    <body></body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)
    
    
    
from fastapi import FastAPI, Request

app = FastAPI()
    
    
    
# --- HÀM BỔ TRỢ 1: TÌM PHÒNG TRÊN ELASTICSEARCH ---
def search_rooms_from_es(user_query: str):
    try:
        query_body = {
            "query": {
                "multi_match": {
                    "query": user_query,
                    "fields": ["title", "description", "address", "district"]
                }
            },
            "size": 3  # Lấy tối đa 3 phòng phù hợp nhất
        }
        response = es.search(INDEX_NAME, body=query_body)
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

# --- SỬA LẠI HÀM GỬI TIN NHẮN: ĐÍNH KÈM NÚT CHỨC NĂNG ---
def send_zalo_message(recipient_id: str, text: str):
    token = os.environ.get("ZALO_ACCESS_TOKEN")
    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    headers = {"access_token": token, "Content-Type": "application/json"}
    
    # Cấu hình Payload chứa tin nhắn văn bản kèm 2 nút bấm ở dưới
    payload = {
        "recipient": {"user_id": recipient_id},
        "message": {
            "text": text,
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "media", # Hoặc sử dụng cấu trúc nút bấm tương tác trực tiếp
                    "elements": [{
                        "media_type": "image",
                        "url": "https://images.unsplash.com/photo-1522771739844-6a9f6d5f14af?w=500", # Ảnh minh họa cho đẹp, bạn có thể đổi link ảnh khác
                        "title": "HỆ THỐNG QUẢN LÝ PHÒNG TRỌ V3",
                        "subtitle": "Vui lòng chọn chức năng bạn muốn thực hiện bên dưới:",
                        "buttons": [
                            {
                                "title": "➕ Đăng/Thêm Phòng Trọ",
                                "image_icon": "",
                                "type": "oa.query.show",
                                "payload": "Thêm phòng trọ mới tại: [Địa chỉ], giá: [Số tiền], mô tả: [Chi tiết phòng]"
                            },
                            {
                                "title": "🔍 Tìm Kiếm Phòng Trọ",
                                "image_icon": "",
                                "type": "oa.query.show",
                                "payload": "Tìm phòng trọ tại khu vực: "
                            }
                        ]
                    }]
                }
            }
        }
    }
    
    # Lưu ý: Nếu muốn nút bấm dạng chữ đơn giản không kèm ảnh (Quick Reply), bạn có thể dùng cấu trúc text dưới đây:
    payload_text_buttons = {
        "recipient": {"user_id": recipient_id},
        "message": {
            "text": text,
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
    
    # Chúng ta sẽ ưu tiên dùng payload_text_buttons cho gọn, nhẹ giao diện chat Zalo
    response = requests.post(url, headers=headers, json=payload_text_buttons)
    print("--- KẾT QUẢ TRẢ VỀ TỪ ZALO API V3 ---", response.json())
    
    
    
# --- 4. WEBHOOK XỬ LÝ CHÍNH ---
@app.post("/webhook/zalo")
async def zalo_webhook(request: Request):
    try:
        raw_body = await request.body()
        data = json.loads(raw_body.decode("utf-8"))
        event_name = str(data.get("event_name", "")).strip()
        
        # SỰ KIỆN 1: Người dùng nhấn Quan tâm OA -> Gửi ngay lời chào kèm Menu nút bấm
        if "user_follow_oa" in event_name:
            recipient_id = data["sender"]["id"]
            welcome_text = "👋 Xin chào! Chào mừng bạn đến với Hệ Thống Tư Vấn Phòng Trọ Tự Động.\n\nHãy nhấn vào các nút chức năng bên dưới để bắt đầu trải nghiệm nhé!"
            send_zalo_message(recipient_id, welcome_text)
            return {"status": "success"}
            
        # SỰ KIỆN 2: Người dùng nhắn tin văn bản (hoặc bấm từ nút chức năng)
        if "user_send_text" in event_name:
            recipient_id = data.get("user_id_by_app") or data["sender"]["id"]
            user_message = data["message"]["text"]
            
            print(f"-> Nhận tin nhắn từ khách: {user_message}")
            ai_reply = "🤖 Hệ thống đang bận xử lý, bạn vui lòng thử lại sau vài phút nhé!"
            
            # Xây dựng Prompt phân loại ý định (Intent Routing) cho Gemini
            router_prompt = f"""
            Phân tích tin nhắn sau của người dùng: "{user_message}"
            Hãy xác định xem người dùng muốn thực hiện chức năng nào trong 2 chức năng sau:
            1. "ADD_ROOM": Người dùng muốn đăng bài, thêm thông tin, nạp dữ liệu một phòng trọ mới (thường đi kèm thông tin giá, địa chỉ, diện tích...).
            2. "SEARCH_ROOM": Người dùng muốn tìm kiếm, thuê phòng, hỏi xem có phòng nào trống không.

            Trả về kết quả dưới dạng JSON duy nhất theo cấu trúc sau, không kèm bất kỳ văn bản giải thích nào khác:
            {{
                "action": "ADD_ROOM" hoặc "SEARCH_ROOM",
                "extracted_data": {{
                    "title": "Tiêu đề phòng trọ nếu có (hoặc để trống)",
                    "price": "Giá phòng nếu có (hoặc để trống)",
                    "address": "Địa chỉ phòng nếu có (hoặc để trống)",
                    "description": "Nội dung mô tả hoặc từ khóa tìm kiếm của người dùng"
                }}
            }}
            """
            
            try:
                # Gọi Gemini xử lý phân luồng
                router_response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=router_prompt,
                    config={'response_mime_type': 'application/json'}
                )
                
                intent_data = json.loads(router_response.text.strip())
                action = intent_data.get("action")
                extracted = intent_data.get("extracted_data", {})
                
                # LUỒNG CHỨC NĂNG 1: THÊM THÔNG TIN PHÒNG TRỌ
                if action == "ADD_ROOM":
                    print("-> Nhận diện: Chức năng THÊM PHÒNG TRỌ.")
                    success = insert_room_to_es(extracted)
                    if success:
                        ai_reply = f"✅ Đã thêm phòng trọ thành công vào hệ thống!\n📍 Địa chỉ: {extracted.get('address', 'Chưa rõ')}\n💰 Giá: {extracted.get('price', 'Chưa rõ')}\n🤖 Cảm ơn bạn đã cập nhật dữ liệu."
                    else:
                        ai_reply = "❌ Hệ thống lưu trữ đang bận, không thể thêm phòng trọ lúc này. Vui lòng thử lại!"
                        
                # LUỒNG CHỨC NĂNG 2: TÌM KIẾM PHÒNG TRỌ
                else:
                    print("-> Nhận diện: Chức năng TÌM KIẾM PHÒNG TRỌ.")
                    search_keyword = extracted.get("description") or user_message
                    found_rooms = search_rooms_from_es(search_keyword)
                    
                    # Đưa kết quả từ Elasticsearch vào Gemini để viết câu trả lời tự nhiên
                    reply_prompt = f"""
                    Bạn là trợ lý ảo tư vấn phòng trọ của Zalo OA. Khách hỏi: "{user_message}"
                    Dữ liệu phòng tìm thấy từ database: {json.dumps(found_rooms, ensure_ascii=False)}
                    Hãy soạn một tin nhắn tư vấn ngắn gọn, lịch sự gửi cho khách dựa trên dữ liệu trên. Nếu có 0 phòng, hãy báo hết phòng khéo léo và xin thông tin liên hệ.
                    """
                    ai_search_response = client.models.generate_content(
                        model='gemini-1.5-flash',
                        contents=reply_prompt
                    )
                    ai_reply = ai_search_response.text.strip()
                    
            except Exception as ai_error:
                print("Lỗi trong khối xử lý Gemini/Elasticsearch:", ai_error)
                ai_reply = "🤖 Đã xảy ra lỗi khi phân tích tin nhắn. Bạn vui lòng thử lại sau!"
            
            # Bắn tin nhắn phản hồi về Zalo (Hàm này tự động đính kèm nút bấm chức năng ở cuối)
            send_zalo_message(recipient_id, ai_reply)
            
    except Exception as e:
        print("Lỗi nghiêm trọng tại Webhook tổng:", e)
        
    return {"status": "success"}