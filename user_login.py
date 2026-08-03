import random

# --- BỘ NHỚ TẠM LƯU MÃ OTP KHÔI PHỤC MẬT KHẨU ---
FORGOT_PASSWORD_OTP_CACHE: Dict[str, dict] = {} # { "phone": {"otp": "123456", "expire": timestamp} }

# --- SCHEMAS ---
class UserLoginSchema(BaseModel):
    phone: str
    password: str

class RequestOTPSchema(BaseModel):
    phone: str

class ResetPasswordSchema(BaseModel):
    phone: str
    otp: str
    new_password: str


# --- HÀM BỔ TRỢ: TÌM ZALO USER ID TỪ SĐT TRONG QDRANT ---
def get_zalo_id_by_phone(phone: str) -> Optional[str]:
    try:
        filter_phone = models.Filter(
            must=[models.FieldCondition(key="landlord_phone", match=models.MatchValue(value=phone))]
        )
        records, _ = qdrant_client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=filter_phone,
            limit=1
        )
        if records and records[0].payload:
            return records[0].payload.get("zalo_user_id")
    except Exception as e:
        print("❌ Lỗi tìm zalo_user_id:", e)
    return None


# --- 1. API ĐĂNG NHẬP USER ---
@app.post("/api/user/login")
def user_login(data: UserLoginSchema):
    phone = data.phone.strip()
    password = data.password.strip()

    # Tìm user trong Qdrant hoặc DB của bạn
    # Ví dụ minh họa: Kiểm tra khớp SĐT và Mật khẩu
    filter_user = models.Filter(
        must=[
            models.FieldCondition(key="landlord_phone", match=models.MatchValue(value=phone)),
            models.FieldCondition(key="password", match=models.MatchValue(value=password))
        ]
    )
    records, _ = qdrant_client.scroll(collection_name=COLLECTION_NAME, scroll_filter=filter_user, limit=1)

    if not records:
        raise HTTPException(status_code=401, detail="Số điện thoại hoặc mật khẩu không chính xác!")

    return {"status": "success", "message": "Đăng nhập thành công!", "phone": phone}


# --- 2. API YÊU CẦU GỬI OTP QUA ZALO OA ---
@app.post("/api/user/request-reset-otp")
def request_reset_otp(data: RequestOTPSchema):
    phone = data.phone.strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Vui lòng nhập số điện thoại!")

    # Tìm zalo_user_id tương ứng
    zalo_id = get_zalo_id_by_phone(phone)
    if not zalo_id or zalo_id in ["SYSTEM", "ADMIN_WEB"]:
        raise HTTPException(
            status_code=404, 
            detail="Không tìm thấy tài khoản Zalo liên kết với SĐT này! Hãy nhắn tin cho Zalo OA trước."
        )

    # Sinh mã OTP 6 số
    otp_code = str(random.randint(100000, 999999))
    FORGOT_PASSWORD_OTP_CACHE[phone] = {
        "otp": otp_code,
        "expire": time.time() + 300  # Hạn 5 phút
    }

    # Gửi qua Zalo OA bằng hàm send_zalo_message có sẵn
    msg = f"🔒 [XÁC NHẬN KHÔI PHỤC MẬT KHẨU]\nMã OTP của bạn là: {otp_code}\nHiệu lực trong 5 phút. Vui lòng không tiết lộ mã này cho ai!"
    sent_success = send_zalo_message(user_id=zalo_id, ai_reply=msg)

    if not sent_success:
        print(f"⚠️ [DEV DEBUG OTP]: {otp_code}") # In console phục vụ test
        # Nếu gửi thất bại do Zalo token hết hạn hoặc chưa quan tâm OA
        return {
            "status": "warning", 
            "message": "Đã tạo OTP nhưng không thể gửi tin Zalo (Dev mode check Console terminal).",
            "debug_otp": otp_code
        }

    return {"status": "success", "message": "Mã OTP khôi phục mật khẩu đã được gửi đến Zalo của bạn!"}


# --- 3. API XÁC NHẬN OTP & ĐỔI MẬT KHẨU MỚI ---
@app.post("/api/user/reset-password")
def reset_password(data: ResetPasswordSchema):
    phone = data.phone.strip()
    otp = data.otp.strip()
    new_pass = data.new_password.strip()

    if phone not in FORGOT_PASSWORD_OTP_CACHE:
        raise HTTPException(status_code=400, detail="Mã OTP không tồn tại hoặc đã hết hạn!")

    cached = FORGOT_PASSWORD_OTP_CACHE[phone]
    if time.time() > cached["expire"]:
        del FORGOT_PASSWORD_OTP_CACHE[phone]
        raise HTTPException(status_code=400, detail="Mã OTP đã hết hạn! Vui lòng yêu cầu lại.")

    if cached["otp"] != otp:
        raise HTTPException(status_code=400, detail="Mã OTP không đúng!")

    # Cập nhật mật khẩu mới vào DB (Qdrant)
    try:
        filter_user = models.Filter(
            must=[models.FieldCondition(key="landlord_phone", match=models.MatchValue(value=phone))]
        )
        records, _ = qdrant_client.scroll(collection_name=COLLECTION_NAME, scroll_filter=filter_user)
        
        for rec in records:
            qdrant_client.set_payload(
                collection_name=COLLECTION_NAME,
                payload={"password": new_pass},
                points=[rec.id]
            )

        # Xóa OTP đã dùng
        del FORGOT_PASSWORD_OTP_CACHE[phone]
        return {"status": "success", "message": "Đặt lại mật khẩu thành công! Vui lòng đăng nhập lại."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi cập nhật mật khẩu: {str(e)}")


# --- ROUTE RENDER GIAO DIỆN ---
@app.get("/login-user", response_class=HTMLResponse)
def login_user_page(request: Request):
    return templates.TemplateResponse(request=request, name="login_user.html")