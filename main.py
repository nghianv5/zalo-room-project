import os
import io
import re
import random
from datetime import datetime, timezone, timedelta
import bcrypt
import pandas as pd
import uvicorn
from typing import Optional
from fastapi import FastAPI, Request, BackgroundTasks, UploadFile, File, HTTPException, Depends, Query, status
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from qdrant_client.http import models as qdrant_models
from services import *
from security import Principal, create_session_token, enforce_rate_limit, get_current_user, require_admin, verify_zalo_webhook
import sys
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR
import logging
from config import Config
from error_reporting import configure_error_reporting, install_process_exception_hooks, report_error

# Khởi tạo Scheduler
scheduler = BackgroundScheduler(timezone=VN_TZ)
scheduler.add_job(cron_refresh_zalo_job, 'interval', hours=6, next_run_time=datetime.now(VN_TZ), id='refresh_zalo_job', replace_existing=True) # 6 giờ chạy 1 lần

logging.basicConfig(level=logging.INFO)
logging.getLogger('apscheduler').setLevel(logging.DEBUG)
configure_error_reporting(send_zalo_message)
install_process_exception_hooks()


def _scheduler_error_listener(event) -> None:
    if event.exception:
        report_error("Tác vụ nền Scheduler thất bại", event.exception, f"scheduler:{event.job_id}")


scheduler.add_listener(_scheduler_error_listener, EVENT_JOB_ERROR)

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


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    report_error(
        f"HTTP {exc.status_code}: {exc.detail}",
        context=f"{request.method} {request.url.path}",
        notify=exc.status_code >= 500 or Config.ZALO_NOTIFY_HTTP_4XX,
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    summary = "; ".join(
        f"{'.'.join(map(str, item.get('loc', [])))}: {item.get('msg', 'invalid')}"
        for item in exc.errors()[:10]
    )
    report_error(
        f"Dữ liệu request không hợp lệ: {summary}",
        exc,
        f"{request.method} {request.url.path}",
        notify=Config.ZALO_NOTIFY_HTTP_4XX,
    )
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    report_error("Exception API chưa được xử lý", exc, f"{request.method} {request.url.path}")
    return JSONResponse(status_code=500, content={"detail": "Lỗi hệ thống nội bộ."})



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


@app.api_route("/", methods=["GET", "HEAD"])
def root_status():
    return {"status": "ok", "service": "zalo-room-app"}

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    zalo_oa_url = os.getenv("ZALO_OA_URL", "").strip()
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"ZALO_OA_URL": zalo_oa_url},
    )

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


@app.get("/api/admin/logs/download")
def download_error_log(user: Principal = Depends(require_admin)):
    log_path = os.path.abspath(Config.LOG_FILE_PATH)
    if not os.path.isfile(log_path):
        raise HTTPException(status_code=404, detail="Chưa có file log lỗi.")
    return FileResponse(log_path, media_type="text/plain", filename="app-error.log")

@app.get("/zalo_verifierCjNXTBZqO5H_qBfhZTypOtR2daEQj4iKE3Wn.html", response_class=PlainTextResponse)
async def verify_zalo_specific_file():
    return "200"

# --- AUTH ROUTES ---
@app.post("/api/user/register")
async def register_user(data: RegisterModel, request: Request, db: Session = Depends(get_db)):
    enforce_rate_limit(redis_client, f"register:{request.client.host if request.client else 'unknown'}", 10, 3600)
    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Mật khẩu phải có tối thiểu 8 ký tự.")
    now = vietnam_now()
    clean_phone = format_national_phone(data.phone)
    account = db.query(UserWeb).filter(UserWeb.phone == clean_phone).first()

    if not account:
        raise HTTPException(status_code=400, detail="Số điện thoại chưa yêu cầu mã OTP!")
    if account.password and not account.otp:
        raise HTTPException(status_code=400, detail="Số điện thoại này đã được đăng ký tài khoản!")
    if not account.otp:
        raise HTTPException(status_code=400, detail="Mã OTP không hợp lệ hoặc đã được sử dụng!")

    expired_at_vn = account.expired_at
    if expired_at_vn and expired_at_vn.tzinfo is not None:
        expired_at_vn = expired_at_vn.astimezone(VN_TZ).replace(tzinfo=None)
    if not expired_at_vn or now > expired_at_vn:
        account.otp = None
        db.commit()
        raise HTTPException(status_code=400, detail="Mã OTP đã hết hạn (quá 5 phút). Vui lòng lấy mã mới!")

    if account.otp != data.otp:
        raise HTTPException(status_code=400, detail="Mã OTP không chính xác!")

    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(data.password.encode('utf-8'), salt).decode('utf-8')
    account.password = hashed_password
    account.otp = None
    account.updated_at = vietnam_now()
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
    except HTTPException:
        raise
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
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _get_super_admin_account(db: Session, admin_username: str) -> Optional[UserWeb]:
    """Ưu tiên khóa ổn định ADMIN_SUPER; phone chỉ là tương thích dữ liệu cũ."""
    account = db.query(UserWeb).filter(UserWeb.user_id == "ADMIN_SUPER").first()
    if account:
        return account
    return db.query(UserWeb).filter(UserWeb.phone == admin_username).first()


def _set_account_password(account: UserWeb, raw_password: str) -> None:
    account.password = bcrypt.hashpw(raw_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    account.updated_at = vietnam_now()


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
        admin_acc = _get_super_admin_account(db, admin_username)
        initial_password = os.getenv("ADMIN_INITIAL_PASSWORD", "")
        if not admin_acc:
            if initial_password and password_input == initial_password:
                new_admin = UserWeb(phone=admin_username, user_id="ADMIN_SUPER")
                _set_account_password(new_admin, initial_password)
                db.add(new_admin)
                try:
                    db.commit()
                    admin_acc = new_admin
                except IntegrityError:
                    # Một request khác hoặc dữ liệu cũ đã tạo ADMIN_SUPER trước đó.
                    db.rollback()
                    admin_acc = _get_super_admin_account(db, admin_username)
                    if not admin_acc:
                        raise HTTPException(status_code=409, detail="Tài khoản Admin đã tồn tại nhưng không thể tải dữ liệu.")
            else:
                raise HTTPException(status_code=401, detail="Mật khẩu Admin không chính xác!")

        if not admin_acc.password:
            if not initial_password or password_input != initial_password:
                raise HTTPException(status_code=401, detail="Mật khẩu Admin không chính xác!")
            _set_account_password(admin_acc, initial_password)
            admin_acc.user_id = "ADMIN_SUPER"
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise HTTPException(status_code=409, detail="Dữ liệu tài khoản Admin đang bị trùng.") from exc

        try:
            password_is_valid = bcrypt.checkpw(password_input.encode("utf-8"), admin_acc.password.encode("utf-8"))
        except (TypeError, ValueError):
            password_is_valid = False

        if password_is_valid:
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

    if user.role == "SUPER_ADMIN":
        user_account = _get_super_admin_account(db, os.getenv("ADMIN_USERNAME", "adminpro"))
    else:
        target_username = format_national_phone(username)
        user_account = db.query(UserWeb).filter(UserWeb.phone == target_username).first()

    if not user_account or not user_account.password:
        raise HTTPException(status_code=400, detail="Tài khoản không tồn tại hoặc chưa tạo mật khẩu!")

    if not bcrypt.checkpw(old_password.encode('utf-8'), user_account.password.encode('utf-8')):
        raise HTTPException(status_code=400, detail="Mật khẩu cũ không chính xác!")

    salt = bcrypt.gensalt()
    user_account.password = bcrypt.hashpw(new_password.encode('utf-8'), salt).decode('utf-8')
    user_account.updated_at = vietnam_now()
    db.commit()
    write_audit_log(user.username, "PASSWORD_CHANGE")

    return {"status": "success", "message": "Đổi mật khẩu thành công!"}

@app.post("/api/rooms/upload-excel")
async def upload_excel_rooms(
    file: UploadFile = File(...),
    user: Principal = Depends(get_current_user),
    db: Session = Depends(get_db),
):
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
            if user.role == "SUPER_ADMIN":
                excel_owner_phone = extracted.get("landlord_phone")
                if not excel_owner_phone or str(excel_owner_phone).strip().lower() in {"none", "null", ""}:
                    excel_owner_phone = get_phone_by_user_id(db, "ADMIN_SUPER")
            else:
                excel_owner_phone = user.username

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
        try:
            if from_date:
                dt_from = datetime.strptime(from_date, "%Y-%m-%d")
                time_range["gte"] = VN_TZ.localize(dt_from).timestamp()
            if to_date:
                dt_to = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                time_range["lte"] = VN_TZ.localize(dt_to).timestamp()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Ngày lọc phải có định dạng YYYY-MM-DD.") from exc
        must_conditions.append(qdrant_models.FieldCondition(key="move_in_timestamp", range=qdrant_models.Range(**time_range)))

    query_filter = qdrant_models.Filter(must=must_conditions, must_not=must_not_conditions)
    safe_limit = min(max(limit, 1), 100)
    records, next_offset = qdrant_client.scroll(collection_name=COLLECTION_NAME, scroll_filter=query_filter, limit=safe_limit, offset=offset)

    results = []
    for rec in records:
        if rec.payload:
            payload_data = dict(rec.payload)
            payload_data["id"] = str(rec.id)
            results.append(payload_data)
            
    return {"data": results, "next_offset": str(next_offset) if next_offset else None}


def _serialize_order(order: OrderRoom) -> dict:
    return {
        "id": order.id,
        "tenant_zalo_id": order.tenant_zalo_id,
        "tenant_phone": order.tenant_phone,
        "landlord_zalo_id": order.landlord_zalo_id,
        "landlord_phone": order.landlord_phone,
        "room_code": order.room_code,
        "viewing_time": vietnam_datetime_iso(order.viewing_time),
        "status": order.status,
        "created_at": vietnam_datetime_iso(order.created_at),
        "updated_at": vietnam_datetime_iso(order.updated_at),
    }


def _get_room_by_code(room_code: str) -> dict:
    try:
        records, _ = qdrant_client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=qdrant_models.Filter(
                must=[qdrant_models.FieldCondition(
                    key="room_code",
                    match=qdrant_models.MatchValue(value=room_code),
                )]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Không thể kiểm tra mã phòng: {exc}") from exc
    if not records or not records[0].payload:
        raise HTTPException(status_code=400, detail="Mã phòng không tồn tại.")
    return records[0].payload


def _normalise_principal_phone(user: Principal) -> str:
    phone = format_national_phone(str(user.username or "").strip())
    if not re.fullmatch(r"0[35789][0-9]{8}", phone):
        raise HTTPException(status_code=403, detail="Tài khoản chưa có số điện thoại hợp lệ.")
    return phone


def _prepare_order_data(
    data: OrderRoomCreateUpdateSchema,
    db: Session,
    forced_landlord_phone: Optional[str] = None,
) -> dict:
    order_data = data.model_dump() if hasattr(data, "model_dump") else data.dict()
    room_data = _get_room_by_code(order_data["room_code"])
    room_landlord_phone = format_national_phone(str(room_data.get("landlord_phone") or "").strip())
    if forced_landlord_phone:
        if room_landlord_phone != forced_landlord_phone:
            raise HTTPException(status_code=403, detail="Bạn chỉ được cập nhật đơn của phòng thuộc tài khoản mình.")
        order_data["landlord_phone"] = forced_landlord_phone
        order_data["landlord_zalo_id"] = get_user_id_by_phone(db, forced_landlord_phone)
    else:
        order_data["landlord_phone"] = order_data.get("landlord_phone") or room_landlord_phone
        order_data["landlord_zalo_id"] = order_data.get("landlord_zalo_id") or get_user_id_by_phone(db, order_data.get("landlord_phone"))
    order_data["tenant_zalo_id"] = order_data.get("tenant_zalo_id") or get_user_id_by_phone(db, order_data["tenant_phone"])
    return order_data


@app.get("/api/admin/orders")
def get_admin_orders(
    room_code: Optional[str] = None,
    tenant_phone: Optional[str] = None,
    landlord_phone: Optional[str] = None,
    order_status: Optional[str] = Query(default=None, alias="status"),
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: Principal = Depends(get_current_user),
):
    query = db.query(OrderRoom)
    if user.role != "SUPER_ADMIN":
        query = query.filter(OrderRoom.landlord_phone == _normalise_principal_phone(user))
    if room_code:
        query = query.filter(OrderRoom.room_code.ilike(f"%{room_code.strip()}%"))
    if tenant_phone:
        query = query.filter(OrderRoom.tenant_phone.ilike(f"%{tenant_phone.strip()}%"))
    if landlord_phone and user.role == "SUPER_ADMIN":
        query = query.filter(OrderRoom.landlord_phone.ilike(f"%{landlord_phone.strip()}%"))
    if order_status:
        normalized_status = order_status.strip().upper()
        if normalized_status not in {"CHỜ XEM", "ĐÃ XEM", "ĐÃ THUÊ"}:
            raise HTTPException(status_code=400, detail="Trạng thái lọc không hợp lệ.")
        query = query.filter(OrderRoom.status == normalized_status)
    safe_limit = min(max(limit, 1), 200)
    safe_offset = max(offset, 0)
    total = query.count()
    orders = query.order_by(OrderRoom.created_at.desc()).offset(safe_offset).limit(safe_limit).all()
    return {"data": [_serialize_order(item) for item in orders], "total": total, "limit": safe_limit, "offset": safe_offset}


@app.post("/api/admin/orders")
def create_admin_order(
    data: OrderRoomCreateUpdateSchema,
    db: Session = Depends(get_db),
    user: Principal = Depends(require_admin),
):
    order_data = _prepare_order_data(data, db)
    duplicate = db.query(OrderRoom).filter(
        OrderRoom.tenant_phone == order_data["tenant_phone"],
        OrderRoom.room_code == order_data["room_code"],
    ).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="Số điện thoại này đã đặt phòng này.")
    order = OrderRoom(id=str(uuid.uuid4()), **order_data, created_at=vietnam_now())
    try:
        db.add(order)
        db.commit()
        db.refresh(order)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Không thể thêm đơn đặt phòng.") from exc
    write_audit_log(user.username, "ORDER_CREATE", order.id, {"room_code": order.room_code})
    return {"status": "success", "message": "Thêm đơn đặt phòng thành công.", "data": _serialize_order(order)}


@app.put("/api/admin/orders/{order_id}")
def update_admin_order(
    order_id: str,
    data: OrderRoomCreateUpdateSchema,
    db: Session = Depends(get_db),
    user: Principal = Depends(require_admin),
):
    query = db.query(OrderRoom).filter(OrderRoom.id == order_id)
    order = query.first()
    if not order:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn đặt phòng.")
    order_data = _prepare_order_data(data, db)
    duplicate = db.query(OrderRoom).filter(
        OrderRoom.tenant_phone == order_data["tenant_phone"],
        OrderRoom.room_code == order_data["room_code"],
        OrderRoom.id != order_id,
    ).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="Số điện thoại này đã đặt phòng này.")
    for key, value in order_data.items():
        setattr(order, key, value)
    try:
        db.commit()
        db.refresh(order)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Không thể cập nhật đơn đặt phòng.") from exc
    write_audit_log(user.username, "ORDER_UPDATE", order.id, {"room_code": order.room_code})
    return {"status": "success", "message": "Cập nhật đơn đặt phòng thành công.", "data": _serialize_order(order)}


@app.patch("/api/orders/{order_id}/status")
def update_order_status(
    order_id: str,
    data: OrderRoomStatusUpdateSchema,
    db: Session = Depends(get_db),
    user: Principal = Depends(get_current_user),
):
    query = db.query(OrderRoom).filter(OrderRoom.id == order_id)
    if user.role != "SUPER_ADMIN":
        query = query.filter(OrderRoom.landlord_phone == _normalise_principal_phone(user))
        if data.status not in {"ĐÃ XEM", "ĐÃ THUÊ"}:
            raise HTTPException(status_code=403, detail="User chỉ được cập nhật trạng thái ĐÃ XEM hoặc ĐÃ THUÊ.")
    order = query.first()
    if not order:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn đặt phòng.")
    order.status = data.status
    order.updated_at = vietnam_now()
    try:
        db.commit()
        db.refresh(order)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Không thể cập nhật trạng thái đặt phòng.") from exc
    write_audit_log(user.username, "ORDER_STATUS_UPDATE", order.id, {"status": order.status})
    return {"status": "success", "message": "Cập nhật trạng thái thành công.", "data": _serialize_order(order)}


@app.delete("/api/admin/orders/{order_id}")
def delete_admin_order(
    order_id: str,
    db: Session = Depends(get_db),
    user: Principal = Depends(require_admin),
):
    order = db.query(OrderRoom).filter(OrderRoom.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn đặt phòng.")
    room_code = order.room_code
    try:
        db.delete(order)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Không thể xóa đơn đặt phòng.") from exc
    write_audit_log(user.username, "ORDER_DELETE", order_id, {"room_code": room_code})
    return {"status": "success", "message": "Đã xóa đơn đặt phòng."}

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
                        phone_reply = f"✅ Cảm ơn bạn! Hệ thống đã ghi nhận thành công Số điện thoại: {extracted_phone}. Mời bạn đăng thông tin phòng."
                        if pending_room:
                            pending_media = get_pending_media(str(sender_id))
                            result = upsert_room_to_db(pending_room, media_urls=pending_media, type_process="NOT_EXCEL", landlord_phone=extracted_phone)
                            if result == "SUCCESS":
                                clear_pending_room(str(sender_id))
                                get_get_and_clear_pending_media(str(sender_id))
                                phone_reply = "✅ Đã xác thực SĐT và đăng phòng đang chờ thành công."
                        else:
                            pending_media = get_pending_media(str(sender_id))
                            if pending_media:
                                phone_reply = (
                                    f"✅ Đã xác thực SĐT: {extracted_phone}.\n\n"
                                    f"{build_room_media_choices(extracted_phone, len(pending_media))}"
                                )
                        print(f"✅ [SUCCESS] Đã bắt thành công SĐT từ text: {extracted_phone} (User ID: {sender_id})")
                        
                        # Phản hồi lại cho khách
                        send_zalo_message(
                            str(sender_id),
                            phone_reply
                        )
                        return {"status": "success", "phone": extracted_phone}
                    
                    except Exception as e:
                        report_error("Lưu số điện thoại từ webhook thất bại", e, f"zalo_user:{sender_id}")
                        return {"status": "error", "message": str(e)}
                    
            if "otp" in clean_message:
                enforce_rate_limit(redis_client, f"otp:{sender_id}", 5, 900)
                phone_match = re.search(r'(0[35789][0-9]{8})', clean_message)
                if phone_match:
                    phone_number = format_national_phone(phone_match.group(1))
                    otp = str(random.randint(100000, 999999))
                    now = vietnam_now()
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
                    request_phone_payload = {
                        "recipient": {"user_id": str(sender_id)},
                        "message": {
                            "attachment": {
                                "type": "template",
                                "payload": {
                                    "template_type": "request_user_info",
                                    "elements": [{
                                        "title": "Xác thực số điện thoại",
                                        "subtitle": "Vui lòng chia sẻ số điện thoại Zalo để đặt lịch xem phòng.",
                                        "image_url": f"{os.getenv('SERVER_DOMAIN', str(request.base_url)).rstrip('/')}/static/icon_zalo_room.png"
                                    }]
                                }
                            }
                        }
                    }
                    send_zalo_request(request_phone_payload)
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
                except Exception as exc:
                    report_error("Xử lý tin nhắn Zalo nền thất bại", exc, f"zalo_user:{sender_id}")
                finally:
                    task_db.close()
            background_tasks.add_task(handle_zalo_message)

    except HTTPException:
        raise
    except Exception as e:
        report_error("Webhook Zalo thất bại", e, f"event:{event_name}")

    return {"status": "success"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
    
    
