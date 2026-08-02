import os
import json
import requests
import uuid
import time
import io
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
from datetime import datetime
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "123456")

# --- API ĐĂNG NHẬP & ĐỔI MẬT KHẨU ---
class AdminLoginSchema(BaseModel):
    username: str
    password: str

class AdminChangePasswordSchema(BaseModel):
    old_password: str
    new_password: str

@app.post("/api/admin/login")
def admin_login(data: AdminLoginSchema):
    current_user = os.environ.get("ADMIN_USERNAME", ADMIN_USERNAME)
    current_pass = os.environ.get("ADMIN_PASSWORD", ADMIN_PASSWORD)

    if data.username == current_user and data.password == current_pass:
        return {"status": "success", "message": "Đăng nhập thành công!"}
    
    raise HTTPException(status_code=401, detail="Tài khoản hoặc mật khẩu không chính xác!")

@app.post("/api/admin/change-password")
def change_admin_password(data: AdminChangePasswordSchema):
    current_pass = os.environ.get("ADMIN_PASSWORD", ADMIN_PASSWORD)
    if data.old_password != current_pass:
        raise HTTPException(status_code=400, detail="Mật khẩu hiện tại không chính xác!")

    os.environ["ADMIN_PASSWORD"] = data.new_password
    return {
        "status": "success", 
        "message": "Đã cập nhật mật khẩu mới thành công!"
    }

# --- CẤU HÌNH MEDIA ---
MEDIA_DIR = "static/media"
os.makedirs(MEDIA_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL")
if CLOUDINARY_URL:
    cloudinary.config(cloudinary_url=CLOUDINARY_URL)

# --- CẤU HÌNH GEMINI & QDRANT ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

COLLECTION_NAME = "rooms_v16_structured"
VECTOR_SIZE = 768

qdrant_client = QdrantClient(
    url=os.environ.get("QDRANT_URL"),
    api_key=os.environ.get("QDRANT_API_KEY")
)

try:
    if not qdrant_client.collection_exists(collection_name=COLLECTION_NAME):
        qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
        )
        print(f"✅ Tạo mới collection '{COLLECTION_NAME}' thành công!")
except Exception as e:
    print("❌ Lỗi khởi tạo Qdrant:", e)

# --- SCHEMA DỮ LIỆU PHÒNG TRỌ CHUẨN ĐẦY ĐỦ THEO 8 MỤC YÊU CẦU ---
class RoomCreateUpdateSchema(BaseModel):
    # 1. Địa chỉ
    address: str
    # 2. Tên phòng
    room_name: Optional[str] = "Phòng trọ"
    # 3. Giá thuê
    price: Optional[str] = "Chưa rõ"
    # 4. Thông tin phòng chi tiết
    floor: Optional[str] = "Chưa rõ"                  
    is_private_bathroom: Optional[str] = "Chưa rõ"   
    has_ac: Optional[str] = "Chưa rõ"                 
    has_heater: Optional[str] = "Chưa rõ"             
    has_washer: Optional[str] = "Chưa rõ"             
    allow_pets: Optional[str] = "Chưa rõ"             
    has_balcony: Optional[str] = "Chưa rõ"            
    has_window: Optional[str] = "Chưa rõ"             
    has_fingerprint_lock: Optional[str] = "Chưa rõ"   
    parking_info: Optional[str] = "Chưa rõ"          
    max_occupants: Optional[str] = "Chưa rõ"         
    other_amenities: Optional[str] = "Chưa rõ"       
    # 5. Phí dịch vụ
    service_fees: Optional[str] = "Chưa rõ"           
    # 6. Ảnh, video phòng
    media_urls: Optional[List[str]] = []
    # 7. Thời gian khách vào ở được
    move_in_date: Optional[str] = "Vào ở ngay"
    # 8. Trạng thái phòng trống hay đã cho thuê
    status: Optional[str] = "TRỐNG"
    # Thông tin liên hệ
    landlord_phone: Optional[str] = "Chưa rõ"

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
    return []

def upsert_room_to_db(data: dict, point_id: str = None, zalo_user_id: str = "SYSTEM") -> bool:
    try:
        address = str(data.get("address", "")).strip()
        room_name = str(data.get("room_name", "Phòng trọ")).strip()
        phone = str(data.get("landlord_phone", "Chưa rõ")).strip()

        if not address:
            return False

        # Gom văn bản tổng hợp để làm vector hóa tìm kiếm
        text_to_embed = f"Địa chỉ: {address} Tên phòng: {room_name} Giá: {data.get('price', '')} Đồ đạc: AC:{data.get('has_ac')}, Heater:{data.get('has_heater')}, Washer:{data.get('has_washer')}, {data.get('other_amenities', '')} Phí dịch vụ: {data.get('service_fees', '')}"
        vector = get_text_embedding(text_to_embed)
        if not vector:
            return False

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        created_at = now_str

        if point_id:
            new_point_id = point_id
            try:
                old_records = qdrant_client.retrieve(collection_name=COLLECTION_NAME, ids=[point_id])
                if old_records and old_records[0].payload:
                    created_at = old_records[0].payload.get("created_at", now_str)
            except Exception:
                pass
        else:
            new_point_id = str(uuid.uuid4())

        media_list = data.get("media_urls", [])
        if isinstance(media_list, str):
            media_list = [x.strip() for x in media_list.split(",") if x.strip()]

        payload = {
            "1_address": address,
            "2_room_name": room_name,
            "3_price": str(data.get("price", "Chưa rõ")),
            "4_floor": str(data.get("floor", "Chưa rõ")),
            "4_is_private_bathroom": str(data.get("is_private_bathroom", "Chưa rõ")),
            "4_has_ac": str(data.get("has_ac", "Chưa rõ")),
            "4_has_heater": str(data.get("has_heater", "Chưa rõ")),
            "4_has_washer": str(data.get("has_washer", "Chưa rõ")),
            "4_allow_pets": str(data.get("allow_pets", "Chưa rõ")),
            "4_has_balcony": str(data.get("has_balcony", "Chưa rõ")),
            "4_has_window": str(data.get("has_window", "Chưa rõ")),
            "4_has_fingerprint_lock": str(data.get("has_fingerprint_lock", "Chưa rõ")),
            "4_parking_info": str(data.get("parking_info", "Chưa rõ")),
            "4_max_occupants": str(data.get("max_occupants", "Chưa rõ")),
            "4_other_amenities": str(data.get("other_amenities", "Chưa rõ")),
            "5_service_fees": str(data.get("service_fees", "Chưa rõ")),
            "6_media_urls": media_list,
            "7_move_in_date": str(data.get("move_in_date", "Vào ở ngay")),
            "8_status": str(data.get("status", "TRỐNG")),
            "landlord_phone": phone,
            "zalo_user_id": zalo_user_id,
            "created_at": created_at,
            "updated_at": now_str
        }

        qdrant_client.upsert(
            collection_name=COLLECTION_NAME,
            points=[PointStruct(id=new_point_id, vector=vector, payload=payload)],
            wait=True
        )
        return True
    except Exception as e:
        print("❌ [QDRANT UPSERT EXCEPTION]:", e)
        return False

# --- API ROUTES ---
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html")

@app.get("/api/rooms")
def get_all_rooms(
    limit: int = 500,
    address: Optional[str] = None,
    room_name: Optional[str] = None,
    status: Optional[str] = None,
    landlord_phone: Optional[str] = None
):
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

            if address and address.lower() not in str(p.get("1_address", "")).lower(): continue
            if room_name and room_name.lower() not in str(p.get("2_room_name", "")).lower(): continue
            if status and p.get("8_status") != status: continue
            if landlord_phone and landlord_phone not in str(p.get("landlord_phone", "")): continue

            results.append({
                "id": r.id,
                "address": p.get("1_address"),
                "room_name": p.get("2_room_name"),
                "price": p.get("3_price"),
                "floor": p.get("4_floor"),
                "is_private_bathroom": p.get("4_is_private_bathroom"),
                "has_ac": p.get("4_has_ac"),
                "has_heater": p.get("4_has_heater"),
                "has_washer": p.get("4_has_washer"),
                "allow_pets": p.get("4_allow_pets"),
                "has_balcony": p.get("4_has_balcony"),
                "has_window": p.get("4_has_window"),
                "has_fingerprint_lock": p.get("4_has_fingerprint_lock"),
                "parking_info": p.get("4_parking_info"),
                "max_occupants": p.get("4_max_occupants"),
                "other_amenities": p.get("4_other_amenities"),
                "service_fees": p.get("5_service_fees"),
                "media_urls": p.get("6_media_urls", []),
                "move_in_date": p.get("7_move_in_date"),
                "status": p.get("8_status"),
                "landlord_phone": p.get("landlord_phone"),
                "zalo_user_id": p.get("zalo_user_id"),
                "created_at": p.get("created_at"),
                "updated_at": p.get("updated_at")
            })
        return {"status": "success", "data": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rooms")
def create_or_update_room(data: RoomCreateUpdateSchema, point_id: Optional[str] = None):
    extracted = data.dict()
    success = upsert_room_to_db(data=extracted, point_id=point_id, zalo_user_id="ADMIN_WEB")
    if success:
        return {"status": "success", "message": "Thao tác thành công!"}
    raise HTTPException(status_code=500, detail="Lỗi lưu dữ liệu.")

@app.delete("/api/rooms/{point_id}")
def delete_room_from_web(point_id: str):
    try:
        qdrant_client.delete(collection_name=COLLECTION_NAME, points_selector=[point_id], wait=True)
        return {"status": "success", "message": "Đã xóa!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- HÀM AI DÙNG GEMINI KIỂM TRA & CHUẨN HÓA DỮ LIỆU ---
def ai_validate_and_extract_room(row_dict: dict) -> Optional[dict]:
    """
    Sử dụng Gemini để kiểm tra, sửa lỗi văn bản và chuẩn hóa dữ liệu hàng từ Excel/CSV
    """
    if not gemini_client:
        # Nếu chưa cấu hình GEMINI_API_KEY thì trả về dữ liệu thô
        return row_dict

    prompt = f"""
    Bạn là một trợ lý AI kiểm định dữ liệu phòng trọ. 
    Hãy phân tích dữ liệu đầu vào từ 1 dòng file Excel sau đây và chuyển thành JSON chuẩn.

    Dữ liệu đầu vào:
    {json.dumps(row_dict, ensure_ascii=False)}

    Yêu cầu chuẩn hóa:
    - "address": Địa chỉ chi tiết (nếu không có địa chỉ cụ thể hoặc là dữ liệu rác, hãy trả về null).
    - "room_name": Tên/số phòng (mặc định "Phòng trọ" nếu thiếu).
    - "price": Giá thuê (ví dụ: "3.5 triệu/tháng").
    - "is_private_bathroom", "has_ac", "has_heater", "has_washer", "allow_pets", "has_balcony", "has_window", "has_fingerprint_lock": Chỉ trả về đúng 1 trong 3 giá trị: "Có", "Không", hoặc "Chưa rõ".
    - "status": Chỉ trả về "TRỐNG" hoặc "ĐÃ CHO THUÊ".

    Trả về kết quả dưới dạng JSON duy nhất, không kèm markdown hay giải thích thêm.
    Format JSON:
    {{
        "address": string hoặc null,
        "room_name": string,
        "price": string,
        "floor": string,
        "is_private_bathroom": string,
        "has_ac": string,
        "has_heater": string,
        "has_washer": string,
        "allow_pets": string,
        "has_balcony": string,
        "has_window": string,
        "has_fingerprint_lock": string,
        "parking_info": string,
        "max_occupants": string,
        "other_amenities": string,
        "service_fees": string,
        "move_in_date": string,
        "status": string,
        "landlord_phone": string,
        "media_urls": list
    }}
    """
    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        if response and response.text:
            cleaned_data = json.loads(response.text)
            return cleaned_data
    except Exception as e:
        print("⚠️ Lỗi AI Validation:", e)
    
    return row_dict

# --- API UPLOAD EXCEL / CSV CÓ AI CHECK ---
@app.post("/api/rooms/upload-excel")
async def upload_excel_rooms(file: UploadFile = File(...)):
    if not file.filename.endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(status_code=400, detail="Vui lòng tải lên tệp định dạng .xlsx, .xls hoặc .csv!")

    contents = await file.read()
    try:
        if file.filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(contents))
        else:
            df = pd.read_excel(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không thể đọc file Excel/CSV: {str(e)}")

    df.fillna("Chưa rõ", inplace=True)
    success_count = 0
    fail_count = 0
    ai_rejected_count = 0

    for _, row in df.iterrows():
        raw_row_dict = row.to_dict()
        
        # 1. Gọi AI Kiểm tra và Chuẩn hóa dữ liệu
        validated_data = ai_validate_and_extract_room(raw_row_dict)

        # 2. Kiểm tra nếu AI phát hiện dữ liệu không hợp lệ (không có địa chỉ)
        if not validated_data or not validated_data.get("address"):
            ai_rejected_count += 1
            continue

        # 3. Tiến hành Lưu vào Database (Qdrant)
        if upsert_room_to_db(data=validated_data, zalo_user_id="EXCEL_AI_IMPORT"):
            success_count += 1
        else:
            fail_count += 1

    return {
        "status": "success",
        "message": f"AI đã xử lý xong! Thành công: {success_count} phòng. (Bỏ qua {ai_rejected_count} dòng dữ liệu lỗi/rác, {fail_count} lỗi lưu DB)."
    }
    
# --- API TẢI FILE EXCEL MẪU ---
@app.get("/api/rooms/download-template")
def download_room_template():
    # Đường dẫn tới file mẫu trong thư mục templates
    template_path = os.path.join(BASE_DIR, "templates", "Mau_Nhap_Danh_Sach_Phong.xlsx")
    
    if not os.path.exists(template_path):
        raise HTTPException(
            status_code=404, 
            detail="Không tìm thấy tệp mẫu 'Mau_Nhap_Danh_Sach_Phong.xlsx' trong thư mục templates!"
        )
    
    return FileResponse(
        path=template_path,
        filename="Mau_Nhap_Danh_Sach_Phong.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    

# --- 9. WEBHOOK RECEIVER ---
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

        # Thêm xử lý khi người dùng gửi File (.xlsx, .csv)
        if event_name == "user_send_file":
            attachments = data.get("message", {}).get("attachments", [])
            for att in attachments:
                file_payload = att.get("payload", {})
                file_url = file_payload.get("url")
                file_name = str(file_payload.get("name", "")).lower()

                # Nếu là file Excel (.xlsx hoặc .xls)
                if file_url and (file_name.endswith(".xlsx") or file_name.endswith(".xls")):
                    send_zalo_message(sender_id, "📥 Em đã nhận file Excel! Đang tiến hành cập nhật dữ liệu phòng...")
                    
                    # Chạy xử lý ngầm (Background Task)
                    def handle_excel():
                        count = process_excel_file(file_url, sender_id)
                        send_zalo_message(sender_id, f"✅ Đã nhập/cập nhật thành công {count} phòng từ file Excel vào hệ thống!")

                    background_tasks.add_task(handle_excel)
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

            background_tasks.add_task(process_zalo_ai_logic, text, media_items, sender_id )
    
    
    except Exception as e:
        print("❌ [Webhook Exception]:", e)

    return {"status": "success"}



if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)