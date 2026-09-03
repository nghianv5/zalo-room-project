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
from services import *
from security import Principal, create_session_token, enforce_rate_limit, get_current_user, require_admin, verify_zalo_webhook
import sys
from apscheduler.schedulers.background import BackgroundScheduler
import logging

# Khởi tạo Scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(cron_refresh_zalo_job, 'interval', hours=6, next_run_time=datetime.now(), id='refresh_zalo_job', replace_existing=True) # 6 giờ chạy 1 lần

logging.basicConfig(level=logging.INFO)
logging.getLogger('apscheduler').setLevel(logging.DEBUG)

# ĐỊNH NGHĨA LIFESPAN TRƯỚC
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Code chạy khi SERVER KHỞI ĐỘNG ---
    print("🚀 [SCHEDULER] Bắt đầu chạy BackgroundScheduler...", flush=True)
    scheduler.start()
    
    yield  # Ứng dụng hoạt động ở đây
    
    # --- Code chạy khi SERVER TẮT ---
    print("🛑 [SCHEDULER] Dừng BackgroundScheduler...", flush=True)
    scheduler.shutdown()

# TRUYỀN LIFESPAN VÀO FASTAPI APP
app = FastAPI(lifespan=lifespan)



BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# --- MIDDLEWARE & STATIC FILES ---
allowed_origins = [x.strip() for x in os.getenv("ALLOWED_ORIGINS", "http://localhost:8000").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html", context={"ZALO_OA_ID": ZALO_OA_ID or ""})

@app.get("/health")
def health_check():
    checks = {"database": False, "redis": False, "qdrant": False}
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        checks["database"] = True
    except Exception:
        pass
    try:
        checks["redis"] = bool(redis_client.ping())
    except Exception:
        pass
    try:
        checks["qdrant"] = qdrant_client.collection_exists(COLLECTION_NAME)
    except Exception:
        pass
    if not all(checks.values()):
        raise HTTPException(status_code=503, detail=checks)
    return {"status": "ok", "checks": checks}

@app.get("/zalo_verifierCjNXTBZqO5H_qBfhZTypOtR2daEQj4iKE3Wn.html", response_class=PlainTextResponse)
async def verify_zalo_specific_file():
    return "200"

# --- AUTH ROUTES ---
@app.post("/api/user/register")
async def register_user(data: RegisterModel, request: Request, db: Session = Depends(get_db)):
    enforce_rate_limit(redis_client, f"register:{request.client.host if request.client else 'unknown'}", 10, 3600)
    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Mật khẩu phải có tối thiểu 8 ký tự.")
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
def delete_room_from_web(point_id: str, user: Principal = Depends(get_current_user)):
    try:
        records = qdrant_client.retrieve(collection_name=COLLECTION_NAME, ids=[point_id])
        if not records or not records[0].payload:
            raise HTTPException(status_code=404, detail="Không tìm thấy phòng.")
        owner = records[0].payload.get("landlord_phone")
        if user.role != "SUPER_ADMIN" and owner != user.username:
            raise HTTPException(status_code=403, detail="Bạn không có quyền xóa phòng này.")
        qdrant_client.set_payload(collection_name=COLLECTION_NAME, payload={"status": "ĐÃ XÓA", "deleted_at": datetime.now(VN_TZ).isoformat()}, points=[point_id], wait=True)
        write_audit_log(user.username, "ROOM_SOFT_DELETE", point_id)
        return {"status": "success", "message": "Đã chuyển phòng vào thùng rác!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rooms")
async def save_or_update_room(
    data: RoomCreateUpdateSchema, 
    point_id: Optional[str] = None,
    user: Principal = Depends(get_current_user)
):
    try:
        # Chuyển dữ liệu schema sang dict
        room_dict = data.model_dump() if hasattr(data, "model_dump") else data.dict()
        
        if user.role == "SUPER_ADMIN":
            room_dict["landlord_phone"] = format_national_phone(room_dict.get("landlord_phone"))
        else:
            room_dict["landlord_phone"] = user.username
        if point_id:
            records = qdrant_client.retrieve(collection_name=COLLECTION_NAME, ids=[point_id])
            if not records or not records[0].payload:
                raise HTTPException(status_code=404, detail="Không tìm thấy phòng.")
            if user.role != "SUPER_ADMIN" and records[0].payload.get("landlord_phone") != user.username:
                raise HTTPException(status_code=403, detail="Bạn không có quyền sửa phòng này.")
        # Nếu chưa có mã phòng 6 ký tự, tự động tạo mới
        if not room_dict.get("room_code"):
            room_dict["room_code"] = generate_unique_room_code()

        # Gọi hàm upsert dữ liệu vào Qdrant DB
        success = upsert_room_to_db(
            data=room_dict, 
            point_id=point_id, 
            media_urls=room_dict.get("media_urls", []),
            type_process = "NOT_EXCEL"
        )
        
        if success == "SUCCESS":
            write_audit_log(user.username, "ROOM_UPDATE" if point_id else "ROOM_CREATE", point_id, {"room_code": room_dict.get("room_code")})
            return {"status": "success", "message": "Lưu thông tin phòng thành công!"}
        else:
            raise HTTPException(status_code=400, detail=f"Không thể ghi dữ liệu: {success}")
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/login")
def api_login(data: UnifiedLoginSchema, request: Request, db: Session = Depends(get_db)):
    enforce_rate_limit(redis_client, f"login:{request.client.host if request.client else 'unknown'}", 20, 900)
    phone_val = getattr(data, 'phone', None) or getattr(data, 'username', None)
    if not phone_val:
        raise HTTPException(status_code=400, detail="Vui lòng nhập số điện thoại hoặc tên đăng nhập!")

    phone_input = str(phone_val).strip()
    password_input = data.password.strip()

    admin_username = os.getenv("ADMIN_USERNAME", "adminpro")
    if phone_input == admin_username:
        admin_acc = db.query(UserWeb).filter(UserWeb.phone == admin_username).first()
        if not admin_acc:
            initial_password = os.getenv("ADMIN_INITIAL_PASSWORD", "")
            if initial_password and password_input == initial_password:
                salt = bcrypt.gensalt()
                hashed_pw = bcrypt.hashpw(initial_password.encode('utf-8'), salt).decode('utf-8')
                new_admin = UserWeb(phone=admin_username, password=hashed_pw, user_id="ADMIN_SUPER")
                db.add(new_admin)
                db.commit()
                return {"status": "success", "role": "SUPER_ADMIN", "username": admin_username, "access_token": create_session_token(admin_username, "SUPER_ADMIN")}
            raise HTTPException(status_code=401, detail="Mật khẩu Admin không chính xác!")

        if bcrypt.checkpw(password_input.encode('utf-8'), admin_acc.password.encode('utf-8')):
            return {"status": "success", "role": "SUPER_ADMIN", "username": admin_username, "access_token": create_session_token(admin_username, "SUPER_ADMIN")}
        raise HTTPException(status_code=401, detail="Mật khẩu Admin không chính xác!")

    clean_phone = format_national_phone(phone_input)
    account = db.query(UserWeb).filter(UserWeb.phone == clean_phone).first()
    
    if not account or not account.password:
        raise HTTPException(status_code=400, detail="Số điện thoại chưa được đăng ký hoặc chưa khởi tạo mật khẩu!")

    if not bcrypt.checkpw(password_input.encode('utf-8'), account.password.encode('utf-8')):
        raise HTTPException(status_code=401, detail="Mật khẩu không chính xác!")

    return {"status": "success", "role": "USER", "username": account.phone, "access_token": create_session_token(account.phone, "USER")}


@app.post("/api/admin/change-password")
async def change_password(payload: AdminChangePasswordSchema, db: Session = Depends(get_db), user: Principal = Depends(get_current_user)):
    old_password = payload.old_password.strip() if payload.old_password else ""
    new_password = payload.new_password.strip() if payload.new_password else ""
    username = user.username

    if not old_password or not new_password or not username:
        raise HTTPException(status_code=400, detail="Vui lòng nhập đầy đủ thông tin!")

    target_username = username if username == os.getenv("ADMIN_USERNAME", "adminpro") else format_national_phone(username)
    user_account = db.query(UserWeb).filter(UserWeb.phone == target_username).first()

    if not user_account or not user_account.password:
        raise HTTPException(status_code=400, detail="Tài khoản không tồn tại hoặc chưa tạo mật khẩu!")

    if not bcrypt.checkpw(old_password.encode('utf-8'), user_account.password.encode('utf-8')):
        raise HTTPException(status_code=400, detail="Mật khẩu cũ không chính xác!")

    salt = bcrypt.gensalt()
    user_account.password = bcrypt.hashpw(new_password.encode('utf-8'), salt).decode('utf-8')
    user_account.updated_at = datetime.utcnow()
    db.commit()
    write_audit_log(user.username, "PASSWORD_CHANGE")

    return {"status": "success", "message": "Đổi mật khẩu thành công!"}

@app.post("/api/rooms/upload-excel")
async def upload_excel_rooms(file: UploadFile = File(...), user: Principal = Depends(get_current_user)):
    if not file.filename.endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(status_code=400, detail="Vui lòng tải lên tệp .xlsx, .xls hoặc .csv!")

    contents = await file.read()
    try:
        df = pd.read_csv(io.BytesIO(contents)) if file.filename.endswith(".csv") else pd.read_excel(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không thể đọc tệp: {str(e)}")

    success_count = 0
    failed_rows_details = []

    BATCH_SIZE = 15
    rows = df.to_dict(orient="records")

    for i in range(0, len(rows), BATCH_SIZE):
        batch_rows = rows[i:i + BATCH_SIZE]
        batch_results = ai_validate_and_extract_room_batch(batch_rows)

        # Sử dụng enumerate để lấy chỉ số offset của từng bản ghi trong batch
        for offset, validated_data in enumerate(batch_results):
            # Tính số dòng chính xác trong file Excel
            current_excel_row = i + offset + 2

            # Kiểm tra an toàn: nếu validated_data là list, lấy phần tử đầu tiên
            if isinstance(validated_data, list) and len(validated_data) > 0:
                validated_data = validated_data[0]

            if not validated_data or not isinstance(validated_data, dict):
                print(f"❌ Dòng {current_excel_row}: AI không phân tích được dữ liệu.")
                failed_rows_details.append({"row": current_excel_row, "reason": "AI không phân tích được dữ liệu"})
                continue
                

            extracted = validated_data.get("extracted_data", {})
            if not isinstance(extracted, dict):
                print(f"❌ Dòng {current_excel_row}: Dữ liệu AI trích xuất sai định dạng.")
                failed_rows_details.append({"row": current_excel_row, "reason": "Dữ liệu AI sai định dạng"})
                continue

            raw_address = str(extracted.get("address") or "").strip()

            if not raw_address or raw_address.lower() in ["[chưa cập nhật]", "none", "null", "chưa rõ", ""]:
                print(f"❌ Đăng ký thành công đến dòng {current_excel_row - 1}. Lỗi từ dòng {current_excel_row}: Thiếu hoặc sai địa chỉ.")
                failed_rows_details.append({"row": current_excel_row, "reason": "Thiếu hoặc sai địa chỉ"})
                continue

            # Kiểm tra lưu DB (hàm trả về None/"" nếu thành công, trả về string lỗi nếu thất bại)
            excel_owner_phone = extracted.get("landlord_phone") if user.role == "SUPER_ADMIN" else user.username
            extracted["landlord_phone"] = excel_owner_phone
            message = upsert_room_to_db(data=extracted, current_excel_row=current_excel_row, type_process="EXCEL", landlord_phone=excel_owner_phone)
            if message == "SUCCESS":
                success_count += 1
            else:
                print(f"❌ Đăng ký thành công đến dòng {current_excel_row - 1}. Lỗi từ dòng {current_excel_row}: Lỗi DB - {message}")
                failed_rows_details.append({"row": current_excel_row, "reason": str(message)})

    return {
        "status": "success",
        "message": f"AI đã xử lý xong! Thành công: {success_count} phòng; lỗi: {len(failed_rows_details)} dòng.",
        "errors": failed_rows_details[:100]
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
    username: Optional[str] = None,
    limit: int = 50,
    offset: Optional[str] = None,
    include_deleted: bool = False,
    user: Principal = Depends(get_current_user)
):
    must_conditions = []
    must_not_conditions = []
    effective_username = username if user.role == "SUPER_ADMIN" else user.username
    if effective_username and effective_username != os.getenv("ADMIN_USERNAME", "adminpro"):
        must_conditions.append(qdrant_models.FieldCondition(key="landlord_phone", match=qdrant_models.MatchValue(value=effective_username)))
    if not include_deleted:
        must_not_conditions.append(qdrant_models.FieldCondition(key="status", match=qdrant_models.MatchValue(value="ĐÃ XÓA")))

    if status:
        must_conditions.append(qdrant_models.FieldCondition(key="status", match=qdrant_models.MatchValue(value=status)))

    if min_price is not None or max_price is not None:
        price_range = {}
        if min_price is not None: price_range["gte"] = min_price
        if max_price is not None: price_range["lte"] = max_price
        must_conditions.append(qdrant_models.FieldCondition(key="price", range=qdrant_models.Range(**price_range)))

    if from_date or to_date:
        time_range = {}
        if from_date:
            dt_from = datetime.strptime(from_date, "%Y-%m-%d")
            time_range["gte"] = VN_TZ.localize(dt_from).timestamp()
        if to_date:
            dt_to = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            time_range["lte"] = VN_TZ.localize(dt_to).timestamp()
        must_conditions.append(qdrant_models.FieldCondition(key="move_in_timestamp", range=qdrant_models.Range(**time_range)))

    query_filter = qdrant_models.Filter(must=must_conditions, must_not=must_not_conditions)
    safe_limit = min(max(limit, 1), 100)
    records, next_offset = qdrant_client.scroll(collection_name=COLLECTION_NAME, scroll_filter=query_filter, limit=safe_limit, offset=offset)

    results = []
    for rec in records:
        if rec.payload:
            payload_data = rec.payload
            payload_data["id"] = rec.id
            results.append(payload_data)
            
    return {"data": results, "next_offset": str(next_offset) if next_offset else None}

# --- WEBHOOK ZALO ---
@app.post("/webhook/zalo")
async def zalo_webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        await verify_zalo_webhook(request)
        data = await request.json()
        event_id = str(data.get("event_id") or data.get("timestamp") or "")
        if event_id:
            if not redis_client.set(f"zalo_event:{event_id}", "1", nx=True, ex=86400):
                return {"status": "duplicate"}
        event_name = str(data.get("event_name", "")).strip()
        sender_id = data.get("sender", {}).get("id")

        if not sender_id:
            return {"status": "ignored"}
            
        domain_host = str(request.base_url)
        
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
                        result_upfile = process_excel_file(file_url, sender_id)
                        send_zalo_message(sender_id, result_upfile)

                    background_tasks.add_task(handle_excel)
                    return {"status": "success"}

        if event_name == "user_send_text":
            message_obj = data.get("message", {})
            raw_message = message_obj.get("text", "")
            clean_message = raw_message.strip().lower()
            
            # 🎯 BẮT TRƯỜNG HỢP: Zalo gửi thông tin dưới dạng tin nhắn văn bản user_send_text
            # Check xem đây có phải là tin nhắn tự động gửi thông tin từ phía Zalo Client không
            if "gửi thông tin cho oa" in clean_message or "số điện thoại:" in clean_message:
                
                # 🔍 Trích xuất Số điện thoại bằng Regex từ chuỗi text (Bắt các số 10 chữ số bắt đầu bằng 0 hoặc 84)
                phone_match = re.search(r'(?:số điện thoại|sđt|phone):\s*(\+?84[35789][0-9]{8}|0[35789][0-9]{8})\b', raw_message, re.IGNORECASE)
                
                # Dự phòng: Nếu Regex trên trật, lấy luôn chuỗi 10 số bắt đầu bằng 0 trong tin nhắn
                if not phone_match:
                    phone_match = re.search(r'\b(0[35789][0-9]{8})\b', raw_message)

                if phone_match:
                    extracted_phone = format_national_phone(phone_match.group(1))
                    
                    try:
                        # Lưu vào Database
                        saved_user = save_or_update_user_web(
                            db=db,
                            zalo_user_id=str(sender_id),
                            phone=extracted_phone
                        )
                        pending_room = get_pending_room(str(sender_id))
                        if pending_room:
                            pending_media = get_pending_media(str(sender_id))
                            result = upsert_room_to_db(pending_room, media_urls=pending_media, type_process="NOT_EXCEL", landlord_phone=extracted_phone)
                            if result == "SUCCESS":
                                clear_pending_room(str(sender_id))
                                get_get_and_clear_pending_media(str(sender_id))
                        print(f"✅ [SUCCESS] Đã bắt thành công SĐT từ text: {extracted_phone} (User ID: {sender_id})")
                        
                        # Phản hồi lại cho khách
                        send_zalo_message(
                            str(sender_id),
                            f"✅ Cảm ơn bạn! Hệ thống đã ghi nhận thành công Số điện thoại: {extracted_phone}."
                        )
                        return {"status": "success", "phone": extracted_phone}
                    
                    except Exception as e:
                        print(f"❌ [DB ERROR]: {e}")
                        return {"status": "error", "message": str(e)}
                    
            if "otp" in clean_message:
                enforce_rate_limit(redis_client, f"otp:{sender_id}", 5, 900)
                phone_match = re.search(r'(0[35789][0-9]{8})', clean_message)
                if phone_match:
                    phone_number = format_national_phone(phone_match.group(1))
                    otp = str(random.randint(100000, 999999))
                    now = datetime.utcnow()
                    expired_at = now + timedelta(minutes=5)

                    existing_phone_record = db.query(UserWeb).filter(UserWeb.phone == phone_number).first()
                    existing_user_record = db.query(UserWeb).filter(UserWeb.user_id == sender_id).first()

                    if existing_phone_record and existing_phone_record.user_id != sender_id:
                        send_zalo_message(sender_id, "Số điện thoại này đã liên kết với một tài khoản khác. Vui lòng liên hệ quản trị viên để xác minh.")
                        return {"status": "phone_already_linked"}

                    if existing_user_record:
                        existing_user_record.phone = phone_number
                        existing_user_record.otp = otp
                        existing_user_record.expired_at = expired_at
                        existing_user_record.updated_at = now
                    else:
                        new_record = UserWeb(
                            id=sender_id,
                            user_id=sender_id,
                            phone=phone_number,
                            otp=otp,
                            expired_at=expired_at,
                            updated_at=now
                        )
                        db.add(new_record)

                    db.commit()
                    reply_text = f"Mã OTP xác thực của bạn là: {otp}\nMã có hiệu lực trong 5 phút (đến {expired_at.strftime('%H:%M:%S')}). Vui lòng không chia sẻ mã này."
                    send_zalo_message(user_id=sender_id, ai_reply=reply_text)
                    return {"status": "success", "message": "OTP generated and sent"}
                else:
                    reply_text = "Cú pháp không đúng! Vui lòng nhắn theo cú pháp: OTP <Số thoại điện> (Ví dụ: OTP 0333593681)"
                    send_zalo_message(user_id=sender_id, ai_reply=reply_text)
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
                reply_msg = process_room_booking(tenant_zalo_id=str(sender_id), room_code=room_code, raw_message = raw_message, db=db )
                
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
            def handle_zalo_message():
                task_db = SessionLocal()
                try:
                    process_zalo_ai_logic(text, media_items, sender_id, task_db)
                finally:
                    task_db.close()
            background_tasks.add_task(handle_zalo_message)

    except HTTPException:
        raise
    except Exception as e:
        print("❌ [Webhook Exception]:", e)

    return {"status": "success"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
    
    
