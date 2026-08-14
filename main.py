import os
import io
import re
import random
from datetime import datetime, timezone, timedelta
import bcrypt
import pandas as pd
import uvicorn
from typing import Optional
from fastapi import FastAPI, Request, BackgroundTasks, UploadFile, File, HTTPException, Depends, status
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from qdrant_client.http import models as qdrant_models


app = FastAPI()
# Import toàn bộ các Service, Helper & Models từ service.py
from services import *

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# --- MIDDLEWARE & STATIC FILES ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html")

@app.get("/zalo_verifierCjNXTBZqO5H_qBfhZTypOtR2daEQj4iKE3Wn.html", response_class=PlainTextResponse)
async def verify_zalo_specific_file():
    return "200"

# --- AUTH ROUTES ---
@app.post("/api/user/register")
async def register_user(data: RegisterModel, db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    clean_phone = format_national_phone(data.phone)
    account = db.query(UserWeb).filter(UserWeb.phone == clean_phone).first()

    if not account:
        raise HTTPException(status_code=400, detail="Số điện thoại chưa yêu cầu mã OTP!")
    if account.password and not account.otp:
        raise HTTPException(status_code=400, detail="Số điện thoại này đã được đăng ký tài khoản!")
    if not account.otp:
        raise HTTPException(status_code=400, detail="Mã OTP không hợp lệ hoặc đã được sử dụng!")

    expired_at_utc = account.expired_at.replace(tzinfo=timezone.utc) if account.expired_at.tzinfo is None else account.expired_at
    if now > expired_at_utc:
        account.otp = None
        db.commit()
        raise HTTPException(status_code=400, detail="Mã OTP đã hết hạn (quá 5 phút). Vui lòng lấy mã mới!")

    if account.otp != data.otp:
        raise HTTPException(status_code=400, detail="Mã OTP không chính xác!")

    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(data.password.encode('utf-8'), salt).decode('utf-8')
    account.password = hashed_password
    account.otp = None
    account.updated_at = datetime.utcnow()
    db.commit()

    return {"status": "success", "message": "Đăng ký tài khoản thành công!"}
    
# --- ROOM MANAGEMENT ROUTES ---
@app.delete("/api/rooms/{point_id}")
def delete_room_from_web(point_id: str):
    try:
        qdrant_client.delete(collection_name=COLLECTION_NAME, points_selector=[point_id], wait=True)
        return {"status": "success", "message": "Đã xóa!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rooms")
async def save_or_update_room(
    data: RoomCreateUpdateSchema, 
    point_id: Optional[str] = None
):
    try:
        # Chuyển dữ liệu schema sang dict
        room_dict = data.model_dump() if hasattr(data, "model_dump") else data.dict()
        
        # Nếu chưa có mã phòng 6 ký tự, tự động tạo mới
        if not room_dict.get("room_code"):
            room_dict["room_code"] = generate_unique_room_code()

        # Gọi hàm upsert dữ liệu vào Qdrant DB
        success = upsert_room_to_db(
            data=room_dict, 
            point_id=point_id, 
            zalo_user_id="ADMIN_WEB",
            media_urls=room_dict.get("media_urls", [])
        )
        
        if success:
            return {"status": "success", "message": "Lưu thông tin phòng thành công!"}
        else:
            raise HTTPException(status_code=400, detail="Không thể ghi dữ liệu phòng vào cơ sở dữ liệu!")
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/login")
def api_login(data: UnifiedLoginSchema, db: Session = Depends(get_db)):
    phone_val = getattr(data, 'phone', None) or getattr(data, 'username', None)
    if not phone_val:
        raise HTTPException(status_code=400, detail="Vui lòng nhập số điện thoại hoặc tên đăng nhập!")

    phone_input = str(phone_val).strip()
    password_input = data.password.strip()

    if phone_input == "adminpro":
        admin_acc = db.query(UserWeb).filter(UserWeb.phone == "adminpro").first()
        if not admin_acc:
            if password_input == "123456":
                salt = bcrypt.gensalt()
                hashed_pw = bcrypt.hashpw("123456".encode('utf-8'), salt).decode('utf-8')
                new_admin = UserWeb(phone="adminpro", password=hashed_pw, user_id="ADMIN_SUPER")
                db.add(new_admin)
                db.commit()
                return {"status": "success", "role": "SUPER_ADMIN", "username": "adminpro"}
            raise HTTPException(status_code=401, detail="Mật khẩu Admin không chính xác!")

        if bcrypt.checkpw(password_input.encode('utf-8'), admin_acc.password.encode('utf-8')):
            return {"status": "success", "role": "SUPER_ADMIN", "username": "adminpro"}
        raise HTTPException(status_code=401, detail="Mật khẩu Admin không chính xác!")

    clean_phone = format_national_phone(phone_input)
    account = db.query(UserWeb).filter(UserWeb.phone == clean_phone).first()
    
    if not account or not account.password:
        raise HTTPException(status_code=400, detail="Số điện thoại chưa được đăng ký hoặc chưa khởi tạo mật khẩu!")

    if not bcrypt.checkpw(password_input.encode('utf-8'), account.password.encode('utf-8')):
        raise HTTPException(status_code=401, detail="Mật khẩu không chính xác!")

    return {"status": "success", "role": "USER", "username": account.phone}


@app.post("/api/admin/change-password")
async def change_password(payload: AdminChangePasswordSchema, db: Session = Depends(get_db)):
    old_password = payload.old_password.strip() if payload.old_password else ""
    new_password = payload.new_password.strip() if payload.new_password else ""
    username = payload.username.strip() if payload.username else ""

    if not old_password or not new_password or not username:
        raise HTTPException(status_code=400, detail="Vui lòng nhập đầy đủ thông tin!")

    target_username = username if username == "adminpro" else format_national_phone(username)
    user_account = db.query(UserWeb).filter(UserWeb.phone == target_username).first()

    if not user_account or not user_account.password:
        raise HTTPException(status_code=400, detail="Tài khoản không tồn tại hoặc chưa tạo mật khẩu!")

    if not bcrypt.checkpw(old_password.encode('utf-8'), user_account.password.encode('utf-8')):
        raise HTTPException(status_code=400, detail="Mật khẩu cũ không chính xác!")

    salt = bcrypt.gensalt()
    user_account.password = bcrypt.hashpw(new_password.encode('utf-8'), salt).decode('utf-8')
    user_account.updated_at = datetime.utcnow()
    db.commit()

    return {"status": "success", "message": "Đổi mật khẩu thành công!"}

@app.post("/api/rooms/upload-excel")
async def upload_excel_rooms(file: UploadFile = File(...)):
    if not file.filename.endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(status_code=400, detail="Vui lòng tải lên tệp .xlsx, .xls hoặc .csv!")

    contents = await file.read()
    try:
        df = pd.read_csv(io.BytesIO(contents)) if file.filename.endswith(".csv") else pd.read_excel(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không thể đọc tệp: {str(e)}")

    df.fillna("Chưa rõ", inplace=True)
    success_count, fail_count, ai_rejected_count = 0, 0, 0

    for _, row in df.iterrows():
        validated_data = ai_validate_and_extract_room(row.to_dict())
        if not validated_data or not validated_data.get("address"):
            ai_rejected_count += 1
            continue

        if upsert_room_to_db(data=validated_data, zalo_user_id="EXCEL_AI_IMPORT"):
            success_count += 1
        else:
            fail_count += 1

    return {
        "status": "success",
        "message": f"AI đã xử lý xong! Thành công: {success_count} phòng. (Bỏ qua {ai_rejected_count} dòng lỗi, {fail_count} lỗi DB)."
    }

@app.get("/api/rooms/download-template")
def download_room_template():
    template_path = os.path.join(BASE_DIR, "templates", "Mau_Nhap_Danh_Sach_Phong.xlsx")
    if not os.path.exists(template_path):
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp mẫu!")
    return FileResponse(
        path=template_path,
        filename="Mau_Nhap_Danh_Sach_Phong.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@app.get("/api/admin/rooms")
async def get_rooms_filter(
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    status: Optional[str] = None,
    username: Optional[str] = None
):
    must_conditions = []
    if username and username != "adminpro":
        must_conditions.append(qdrant_models.FieldCondition(key="landlord_phone", match=qdrant_models.MatchValue(value=username)))

    if status:
        must_conditions.append(qdrant_models.FieldCondition(key="status", match=qdrant_models.MatchValue(value=status)))

    if min_price is not None or max_price is not None:
        price_range = {}
        if min_price is not None: price_range["gte"] = min_price
        if max_price is not None: price_range["lte"] = max_price
        must_conditions.append(qdrant_models.FieldCondition(key="price_num", range=qdrant_models.Range(**price_range)))

    if from_date or to_date:
        time_range = {}
        if from_date:
            dt_from = datetime.strptime(from_date, "%Y-%m-%d")
            time_range["gte"] = VN_TZ.localize(dt_from).timestamp()
        if to_date:
            dt_to = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            time_range["lte"] = VN_TZ.localize(dt_to).timestamp()
        must_conditions.append(qdrant_models.FieldCondition(key="move_in_timestamp", range=qdrant_models.Range(**time_range)))

    query_filter = qdrant_models.Filter(must=must_conditions) if must_conditions else None
    records, _ = qdrant_client.scroll(collection_name=COLLECTION_NAME, scroll_filter=query_filter, limit=200)

    results = []
    for rec in records:
        if rec.payload:
            payload_data = rec.payload
            payload_data["id"] = rec.id
            results.append(payload_data)
            
    return results

# --- WEBHOOK ZALO ---
@app.post("/webhook/zalo")
async def zalo_webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        event_name = str(data.get("event_name", "")).strip()
        sender_id = data.get("user_id_by_app") or data.get("sender", {}).get("id")

        if not sender_id:
            return {"status": "ignored"}

        if "user_follow_oa" in event_name:
            send_zalo_message(sender_id, "👋 Chào mừng bạn! Hãy gửi thông tin hoặc hình ảnh phòng trọ để bắt đầu nhé!")
            return {"status": "success"}

        if event_name == "user_send_file":
            attachments = data.get("message", {}).get("attachments", [])
            for att in attachments:
                file_payload = att.get("payload", {})
                file_url = file_payload.get("url")
                file_name = str(file_payload.get("name", "")).lower()

                if file_url and (file_name.endswith(".xlsx") or file_name.endswith(".xls")):
                    send_zalo_message(sender_id, "📥 Em đã nhận file Excel! Đang tiến hành cập nhật dữ liệu phòng...")
                    
                    def handle_excel():
                        count = process_excel_file(file_url, sender_id)
                        send_zalo_message(sender_id, f"✅ Đã nhập/cập nhật thành công {count} phòng từ file Excel vào hệ thống!")

                    background_tasks.add_task(handle_excel)
                    return {"status": "success"}
        print(f"event_name: {event_name}")

        if event_name == "user_send_text":
            user_id = data.get("sender", {}).get("id")
            raw_message = data.get("message", {}).get("text", "")
            clean_message = raw_message.strip().lower()

            # 🎯 BẮT TRƯỜNG HỢP: Zalo gửi thông tin dưới dạng tin nhắn văn bản user_send_text
            if "gửi thông tin cho OA" in clean_message or "Số điện thoại:" in clean_message:
                print(f"user_submit_info")
                # Bóc tách phone từ các cấu trúc JSON trả về của Zalo
                info_dict = data.get("info", {}) or data.get("message", {})
                user_phone = (
                    info_dict.get("phone") 
                    or info_dict.get("address", {}).get("phone")
                    or data.get("data", {}).get("phone")
                )

                # Trường hợp Zalo gửi Phone dưới dạng số nguyên (Int) hoặc Chuỗi
                if user_phone:
                    clean_phone = format_national_phone(str(user_phone))
                    
                    try:
                        # 🎯 Lưu hoặc Cập nhật User vào DB user_web
                        saved_user = save_or_update_user_web(
                            db=db,
                            zalo_user_id=str(sender_id),
                            phone=clean_phone
                        )
                        print(f"✅ [SUCCESS] Đã lưu thành công User: {saved_user.phone} vào user_web")
                        
                        # Gửi tin nhắn xác nhận cho User
                        send_zalo_message(
                            str(sender_id), 
                            "✅ Xác thực số điện thoại thành công! Bạn có thể tiếp tục thao tác trên Zalo."
                        )
                        return {"status": "success"}
                    except Exception as err:
                        print(f"❌ [DATABASE ERROR] Không thể lưu user: {err}")
                        return {"status": "error", "message": str(err)}
                else:
                    print(f"⚠️ [WARNING] Không tìm thấy trường phone trong payload: {data}")
                    return {"status": "missing_phone"}
                    
            if "otp" in clean_message:
                phone_match = re.search(r'(0[3|5|7|8|9][0-9]{8})', clean_message)
                if phone_match:
                    phone_number = format_national_phone(phone_match.group(1))
                    otp = str(random.randint(100000, 999999))
                    now = datetime.utcnow()
                    expired_at = now + timedelta(minutes=5)

                    existing_phone_record = db.query(UserWeb).filter(UserWeb.phone == phone_number).first()
                    existing_user_record = db.query(UserWeb).filter(UserWeb.user_id == user_id).first()

                    if existing_phone_record and existing_phone_record.user_id != user_id:
                        db.delete(existing_phone_record)
                        db.commit()

                    if existing_user_record:
                        existing_user_record.phone = phone_number
                        existing_user_record.otp = otp
                        existing_user_record.expired_at = expired_at
                        existing_user_record.updated_at = now
                    else:
                        new_record = UserWeb(
                            id=user_id,
                            user_id=user_id,
                            phone=phone_number,
                            otp=otp,
                            expired_at=expired_at,
                            updated_at=now
                        )
                        db.add(new_record)

                    db.commit()
                    reply_text = f"Mã OTP xác thực của bạn là: {otp}\nMã có hiệu lực trong 5 phút (đến {expired_at.strftime('%H:%M:%S')}). Vui lòng không chia sẻ mã này."
                    send_zalo_message(user_id=user_id, ai_reply=reply_text)
                    return {"status": "success", "message": "OTP generated and sent"}
                else:
                    reply_text = "Cú pháp không đúng! Vui lòng nhắn theo cú pháp: OTP <Số thoại điện> (Ví dụ: OTP 0333593681)"
                    send_zalo_message(user_id=user_id, ai_reply=reply_text)
                    return {"status": "invalid_syntax"}
            # 2. Xử lý Đặt lịch xem phòng theo mã phòng 6 ký tự
            booking_match = re.search(r'(?:đặt lịch|xem phòng|mã phòng|đặt phòng)\s*([a-zA-Z0-9]{6})\b', clean_message, re.IGNORECASE)
            if booking_match:
                # 🎯 Lấy SĐT khách từ DB bằng sender_id chuẩn hóa chuỗi
                tenant_phone = get_phone_by_user_id(db, str(sender_id))

                # 🚨 Nếu chưa xác thực SĐT -> Yêu cầu chia sẻ lại SĐT
                if not tenant_phone or tenant_phone in ["Chưa xác thực SĐT", "Chưa cập nhật", ""]:
                    send_zalo_request_phone(str(sender_id))
                    return {"status": "phone_required"}

                room_code = booking_match.group(1).upper()
                
                # 🎯 Gọi xử lý tạo đơn trong order_room (Sử dụng sender_id đồng nhất)
                reply_msg = process_room_booking(tenant_zalo_id=str(sender_id), room_code=room_code, db=db)
                
                send_zalo_message(user_id=str(sender_id), ai_reply=reply_msg)
                return {"status": "success", "message": "Processed room booking"}
            #
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

                background_tasks.add_task(process_zalo_ai_logic, text, media_items, sender_id, db)

    except Exception as e:
        print("❌ [Webhook Exception]:", e)

    return {"status": "success"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)