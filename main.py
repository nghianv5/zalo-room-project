import os
import json
import requests
import uuid
import time
import io
import random
from typing import Dict, List, Optional
import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks, UploadFile, File, HTTPException, Depends, Header, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from google import genai
from google.genai import types
import cloudinary
import cloudinary.uploader
import pandas as pd
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
import re
from qdrant_client.http import models
import pytz
from datetime import datetime, timedelta, timezone, date
import bcrypt
# 1. Thêm declarative_base vào import
from sqlalchemy import Column, Integer, String, DateTime, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from fastapi import FastAPI, Request, Depends
from sqlalchemy.orm import Session
import phonenumbers
from phonenumbers import NumberParseException
import services

@app.post("/api/user/register")
async def register_user(data: RegisterModel, db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    # Chuẩn hóa SĐT trước khi query
    clean_phone = format_national_phone(data.phone)

    # 1. Tìm bản ghi theo SĐT đã chuẩn hóa
    account = db.query(UserWeb).filter(UserWeb.phone == clean_phone).first()

    if not account:
        raise HTTPException(
            status_code=400, 
            detail="Số điện thoại chưa yêu cầu mã OTP!"
        )

    # 2. Kiểm tra xem tài khoản này đã có mật khẩu (đã đăng ký) chưa
    if account.password and not account.otp:
        raise HTTPException(
            status_code=400, 
            detail="Số điện thoại này đã được đăng ký tài khoản!"
        )

    # 3. Kiểm tra OTP có tồn tại không
    if not account.otp:
        raise HTTPException(
            status_code=400, 
            detail="Mã OTP không hợp lệ hoặc đã được sử dụng!"
        )

    # 4. Kiểm tra thời hạn OTP (quá 5 phút)
    expired_at_utc = account.expired_at.replace(tzinfo=timezone.utc) if account.expired_at.tzinfo is None else account.expired_at
    if now > expired_at_utc:
        account.otp = None  # Xóa OTP hết hạn
        db.commit()
        raise HTTPException(
            status_code=400, 
            detail="Mã OTP đã hết hạn (quá 5 phút). Vui lòng lấy mã mới!"
        )

    # 5. Kiểm tra mã OTP gửi lên có khớp không
    if account.otp != data.otp:
        raise HTTPException(status_code=400, detail="Mã OTP không chính xác!")

    # 6. Mã hóa mật khẩu (Hash password)
    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(data.password.encode('utf-8'), salt).decode('utf-8')

    # 7. Cập nhật tài khoản: Lưu mật khẩu & XÓA OTP
    account.password = hashed_password
    account.otp = None  # Xóa trường OTP
    account.updated_at = datetime.utcnow()

    db.commit()

    return {
        "status": "success",
        "message": "Đăng ký tài khoản thành công!"
    }


# --- API ĐĂNG NHẬP DÙNG BẢNG user_web ---
@app.post("/api/login")
def unified_login(data: UnifiedLoginSchema, db: Session = Depends(get_db)):
    
    # Lấy phone hoặc username tùy theo dữ liệu gửi lên
    phone_val = getattr(data, 'phone', None) or getattr(data, 'username', None)

    # Kiểm tra xem người dùng có nhập thông tin hay không
    if not phone_val:
        raise HTTPException(
            status_code=400, 
            detail="Vui lòng nhập số điện thoại hoặc tên đăng nhập!"
        )

    # Ép kiểu chuỗi và loại bỏ khoảng trắng an toàn
    phone_input = str(phone_val).strip()
    password_input = data.password.strip()

    if not phone_input or not password_input:
        raise HTTPException(status_code=400, detail="Vui lòng nhập đầy đủ tài khoản/SĐT và mật khẩu!")

    # 1. TRƯỜNG HỢP ADMINPRO (Super Admin)
    if phone_input == "adminpro":
        # Tìm adminpro trong bảng user_web
        admin_acc = db.query(UserWeb).filter(UserWeb.phone == "adminpro").first()
        
        # Nếu chưa có account adminpro trong DB -> Khởi tạo mặc định
        if not admin_acc:
            if password_input == "123456":
                salt = bcrypt.gensalt()
                hashed_pw = bcrypt.hashpw("123456".encode('utf-8'), salt).decode('utf-8')
                new_admin = UserWeb(
                    phone="adminpro",
                    password=hashed_pw,
                    user_id="ADMIN_SUPER"
                )
                db.add(new_admin)
                db.commit()
                return {"status": "success", "role": "SUPER_ADMIN", "username": "adminpro"}
            else:
                raise HTTPException(status_code=401, detail="Mật khẩu Admin không chính xác!")

        # Kiểm tra mật khẩu Admin
        if bcrypt.checkpw(password_input.encode('utf-8'), admin_acc.password.encode('utf-8')):
            return {"status": "success", "role": "SUPER_ADMIN", "username": "adminpro"}
        else:
            raise HTTPException(status_code=401, detail="Mật khẩu Admin không chính xác!")

    # 2. TRƯỜNG HỢP USER THƯỜNG (SĐT)
    clean_phone = format_national_phone(phone_input)
    account = db.query(UserWeb).filter(UserWeb.phone == clean_phone).first()
    
    if not account or not account.password:
        raise HTTPException(status_code=400, detail="Số điện thoại chưa được đăng ký hoặc chưa khởi tạo mật khẩu!")

    # Xác thực mật khẩu Hash
    if not bcrypt.checkpw(password_input.encode('utf-8'), account.password.encode('utf-8')):
        raise HTTPException(status_code=401, detail="Mật khẩu không chính xác!")

    return {"status": "success", "role": "USER", "username": account.phone}



@app.post("/api/user/login")
def user_login(data: LoginUserSchema, db: Session = Depends(get_db)):
    clean_phone = format_national_phone(data.phone)
    
    account = db.query(UserWeb).filter(UserWeb.phone == clean_phone).first()
    if not account or not account.password:
        raise HTTPException(status_code=400, detail="Tài khoản không tồn tại hoặc chưa hoàn tất đăng ký!")

    if not bcrypt.checkpw(data.password.encode('utf-8'), account.password.encode('utf-8')):
        raise HTTPException(status_code=401, detail="Mật khẩu không chính xác!")

    return {"status": "success", "message": "Đăng nhập thành công!", "phone": account.phone}

class RequestOTPModel(BaseModel):
    phone: str


@app.post("/api/user/request-register-otp")
async def request_register_otp(data: RequestOTPModel, db: Session = Depends(get_db)):
    # 1. Chuẩn hóa SĐT ngay từ đầu
    raw_phone = data.phone
    clean_phone = format_national_phone(raw_phone)
    
    if not clean_phone or len(clean_phone) < 9:
        raise HTTPException(status_code=400, detail="Số điện thoại không hợp lệ")

    otp = str(random.randint(100000, 999999))
    now = datetime.now(timezone.utc)
    expired_at = now + timedelta(minutes=5)

    # 2. Xóa các OTP cũ của SĐT đã được chuẩn hóa này
    db.query(UserWeb).filter(UserWeb.phone == clean_phone).delete()

    # 3. Lưu bản ghi mới với clean_phone
    new_otp = UserWeb(
        phone=clean_phone,
        otp=otp,
        created_at=now,
        expired_at=expired_at
    )
    db.add(new_otp)
    db.commit()

    # 4. Gửi OTP
    result = send_otp_via_zalo_oa(
        user_zalo_id=clean_phone,
        otp=otp,
        access_token=ZALO_ACCESS_TOKEN
    )
    
    if result.get("error") != 0:
        return {"success": False, "message": "Gửi OTP thất bại", "details": result}
    return {"message": "Mã OTP đã được gửi thành công!"}


@app.post("/api/admin/change-password")
async def change_password(
    payload: AdminChangePasswordSchema, 
    db: Session = Depends(get_db)
):
    # Lấy thông tin trực tiếp từ payload
    old_password = payload.old_password.strip() if payload.old_password else ""
    new_password = payload.new_password.strip() if payload.new_password else ""
    username = payload.username.strip() if payload.username else ""

    if not old_password or not new_password:
        raise HTTPException(
            status_code=400, 
            detail="Vui lòng nhập đầy đủ mật khẩu cũ và mật khẩu mới!"
        )

    # Lấy username từ payload
    if not username:
        raise HTTPException(
            status_code=400, 
            detail="Không tìm thấy thông tin tài khoản cần đổi mật khẩu!"
        )

    # Nếu không phải tài khoản adminpro thì chuẩn hóa định dạng SĐT
    target_username = username if username == "adminpro" else format_national_phone(username)
    
    # Truy vấn tài khoản trong cơ sở dữ liệu
    user_account = db.query(UserWeb).filter(UserWeb.phone == target_username).first()

    if not user_account:
        raise HTTPException(
            status_code=440, 
            detail="Tài khoản không tồn tại trên hệ thống!"
        )

    if not user_account.password:
        raise HTTPException(
            status_code=400, 
            detail="Tài khoản này chưa tạo mật khẩu!"
        )

    # 3. Kiểm tra mật khẩu cũ (So sánh bằng bcrypt tương tự như hàm login)
    if not bcrypt.checkpw(old_password.encode('utf-8'), user_account.password.encode('utf-8')):
        raise HTTPException(
            status_code=400, 
            detail="Mật khẩu cũ không chính xác!"
        )

    # 4. Băm (Hash) mật khẩu mới bằng bcrypt
    salt = bcrypt.gensalt()
    hashed_new_password = bcrypt.hashpw(new_password.encode('utf-8'), salt).decode('utf-8')

    # 5. Cập nhật mật khẩu mới và thời gian update vào PostgreSQL
    user_account.password = hashed_new_password
    user_account.updated_at = datetime.utcnow()

    # Commit thay đổi vào CSDL PostgreSQL
    db.commit()

    return {
        "status": "success", 
        "message": "Đổi mật khẩu thành công!"
    }

# --- API ROUTES ---
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html")



@app.get("/api/rooms")
def get_rooms(
    current_user: UserWeb = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # 1. Nếu là Admin, cho phép xem toàn bộ phòng trọ
    if getattr(current_user, "role", None) == "ADMIN":
        return db.query(Room).all()

    # 2. Lấy SĐT user đăng nhập và chuẩn hóa theo chuẩn quốc tế (E.164 không dấu +)
    raw_phone = str(current_user.phone).strip() if current_user.phone else ""
    if not raw_phone:
        return []

    user_phone = format_national_phone(raw_phone)

    # 3. Nếu DB đã chuẩn hóa toàn bộ landlordphone về dạng 84... / chuẩn quốc tế:
    # Chỉ cần so sánh trực tiếp =
    rooms = db.query(Room).filter(Room.landlordphone == user_phone).all()

    # TRƯỜNG HỢP DỰ PHÒNG: Nếu dữ liệu landlordphone trong DB vẫn còn lẫn lộn dạng 0x và 84x,
    # bạn có thể dùng phone_variants chuẩn hóa như sau:
    if not rooms:
        # Tạo biến thể 0x tương ứng để fallback tìm dữ liệu cũ trong DB
        phone_variants = [user_phone]
        if user_phone.startswith("84") and len(user_phone) == 11:
            phone_variants.append("0" + user_phone[2:])
        
        rooms = db.query(Room).filter(
            func.trim(Room.landlordphone).in_(phone_variants)
        ).all()
    
    return rooms



@app.delete("/api/rooms/{point_id}")
def delete_room_from_web(point_id: str):
    try:
        qdrant_client.delete(collection_name=COLLECTION_NAME, points_selector=[point_id], wait=True)
        return {"status": "success", "message": "Đã xóa!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- API UPLOAD EXCEL / CSV CÓ AI CHECK ---
@app.post("/api/rooms/upload-excel")
async def upload_excel_rooms(file: UploadFile = File(...)):
    print("upload_excel_rooms")
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


@app.get("/api/admin/rooms")
async def get_rooms_filter(
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    status: Optional[str] = None,
    username: Optional[str] = None # Nhận username để phân quyền[cite: 4]
):
    must_conditions = []

    # 🔒 Phân quyền: Nếu không phải adminpro thì chỉ lấy data của SĐT đó[cite: 4]
    if username and username != "adminpro":
        must_conditions.append(
            models.FieldCondition(
                key="landlord_phone",
                match=models.MatchValue(value=username)
            )
        )

    # 1. Lọc theo trạng thái phòng
    if status:
        must_conditions.append(
            models.FieldCondition(key="status", match=models.MatchValue(value=status))
        )

    # 2. Lọc theo khoảng giá
    if min_price is not None or max_price is not None:
        price_range = {}
        if min_price is not None: price_range["gte"] = min_price
        if max_price is not None: price_range["lte"] = max_price
        must_conditions.append(
            models.FieldCondition(key="price_num", range=models.Range(**price_range))
        )

    # 3. Lọc theo khoảng thời gian
    if from_date or to_date:
        time_range = {}
        if from_date:
            dt_from = datetime.strptime(from_date, "%Y-%m-%d")
            time_range["gte"] = VN_TZ.localize(dt_from).timestamp()
        if to_date:
            dt_to = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            time_range["lte"] = VN_TZ.localize(dt_to).timestamp()

        must_conditions.append(
            models.FieldCondition(key="move_in_timestamp", range=models.Range(**time_range))
        )

    query_filter = models.Filter(must=must_conditions) if must_conditions else None
    
    records, _ = qdrant_client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=query_filter,
        limit=200
    )

    results = []
    for rec in records:
        if rec.payload:
            payload_data = rec.payload
            payload_data["id"] = rec.id
            results.append(payload_data)
            
    return results

# --- 9. WEBHOOK RECEIVER ---
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
                    
        # Bắt sự kiện người dùng gửi tin nhắn cho OA
        if event_name == "user_send_text":
            user_id = data.get("sender", {}).get("id")
            raw_message = data.get("message", {}).get("text", "")
            
            # 1. Chuyển toàn bộ tin nhắn về chữ thường để BỎ PHÂN BIỆT CHỮ HOA/THƯỜNG
            clean_message = raw_message.strip().lower()
            
            # 2. Kiểm tra xem tin nhắn có chứa từ khóa 'otp' hay không
            if "otp" in clean_message:
                # Dùng Regex tìm số điện thoại trong tin nhắn (định dạng 10 chữ số bắt đầu bằng 0)
                phone_match = re.search(r'(0[3|5|7|8|9][0-9]{8})', clean_message)
                
                # --- TRONG ROUTE WEBHOOK (/api/zalo/webhook) ---
                if phone_match:
                    phone_number = format_national_phone(phone_match.group(1))
                    
                    otp = str(random.randint(100000, 999999))
                    now = datetime.utcnow()
                    expired_at = now + timedelta(minutes=5)

                    # 1. Kiểm tra xem SĐT này đã tồn tại trong DB chưa
                    existing_phone_record = db.query(UserWeb).filter(UserWeb.phone == phone_number).first()
                    
                    # 2. Kiểm tra xem Zalo User ID này đã tồn tại trong DB chưa
                    existing_user_record = db.query(UserWeb).filter(UserWeb.user_id == user_id).first()

                    if existing_phone_record and existing_phone_record.user_id != user_id:
                        # TRƯỜNG HỢP: SĐT đã được đăng ký bởi 1 Zalo ID khác trước đó.
                        # Giải pháp: Xóa liên kết cũ để gán SĐT này cho Zalo ID mới (đảm bảo 1 SĐT - 1 ZID)
                        db.delete(existing_phone_record)
                        db.commit()

                    if existing_user_record:
                        # TRƯỜNG HỢP: Zalo ID này đã tồn tại (dù đổi SĐT mới hay dùng lại SĐT cũ)
                        existing_user_record.phone = phone_number
                        existing_user_record.otp = otp
                        existing_user_record.expired_at = expired_at
                        existing_user_record.updated_at = now
                    else:
                        # TRƯỜNG HỢP: Cả Zalo ID và SĐT đều hoàn toàn mới
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
                    
                    # 5. Phản hồi lại tin nhắn chứa mã OTP cho người dùng
                    reply_text = f"Mã OTP xác thực của bạn là: {otp}\nMã có hiệu lực trong 5 phút (đến {expired_at.strftime('%H:%M:%S')}). Vui lòng không chia sẻ mã này."
                    send_zalo_message(user_id=user_id, ai_reply = reply_text)
                    
                    return {"status": "success", "message": "OTP generated and sent"}
                
                else:
                    # Trường hợp nhắn 'otp' nhưng quên không kèm Số điện thoại
                    reply_text = "Cú pháp không đúng! Vui lòng nhắn theo cú pháp: OTP <Số điện thoại> (Ví dụ: OTP 0333593681)"
                    send_zalo_message(user_id=user_id, ai_reply = reply_text)
                    return {"status": "invalid_syntax"}
        
        
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
    
# Viết route tĩnh trực tiếp cho đúng file Zalo yêu cầu
@app.get("/zalo_verifierCjNXTBZqO5H_qBfhZTypOtR2daEQj4iKE3Wn.html", response_class=PlainTextResponse)
async def verify_zalo_specific_file():
    # Zalo yêu cầu nội dung bên trong file html khớp với chuỗi xác minh của họ
    return "200"