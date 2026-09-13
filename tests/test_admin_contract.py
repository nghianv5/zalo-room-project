import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _schema_fields(class_name: str):
    tree = ast.parse((ROOT / "services.py").read_text(encoding="utf-8"))
    schema = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return [
        node.target.id for node in schema.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]


def test_admin_room_form_and_table_cover_schema_fields():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    fields = _schema_fields("RoomCreateUpdateSchema")
    assert len(fields) == 25
    assert not [field for field in fields if not re.search(rf'id=["\']{field}["\']', html)]
    assert not [field for field in fields if not re.search(rf'r\.{field}\b', html)]


def test_admin_order_crud_routes_and_form_fields_exist():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    for route in (
        '@app.get("/api/admin/orders")',
        '@app.post("/api/admin/orders")',
        '@app.put("/api/admin/orders/{order_id}")',
        '@app.delete("/api/admin/orders/{order_id}")',
    ):
        assert route in main_source
    for element_id in (
        "oCode", "oTenantPhone", "oTenantZalo",
        "oLandlordPhone", "oLandlordZalo", "oViewing",
    ):
        assert f'id="{element_id}"' in html


def test_landlord_user_can_only_view_and_update_owned_orders():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "def _normalise_principal_phone" in main_source
    assert "OrderRoom.landlord_phone == _normalise_principal_phone(user)" in main_source
    assert '@app.patch("/api/orders/{order_id}/status")' in main_source
    assert 'data.status not in {"ĐÃ XEM", "ĐÃ THUÊ"}' in main_source
    assert 'user: Principal = Depends(require_admin)' in main_source
    assert '$("ordersTab").classList.remove("hidden")' in html
    assert '$("addOrderBtn").classList.toggle("hidden",role!=="SUPER_ADMIN")' in html
    assert "function updateOrderStatus(id,status)" in html


def test_order_status_and_updated_at_are_persisted_and_displayed():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert 'status = Column(String, nullable=False, default="CHỜ XEM")' in services_source
    assert "updated_at = Column(DateTime, default=vietnam_now, onupdate=vietnam_now)" in services_source
    assert "ALTER TABLE order_room ADD COLUMN IF NOT EXISTS status VARCHAR" in services_source
    assert "ALTER TABLE order_room ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP" in services_source
    assert '"status": order.status' in main_source
    assert '"updated_at": vietnam_datetime_iso(order.updated_at)' in main_source
    assert 'id="oStatus"' in html
    assert "Cập nhật: ${val(o.updated_at)}" in html


def test_order_filters_cover_status_phones_and_room_code():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert 'order_status: Optional[str] = Query(default=None, alias="status")' in main_source
    assert "query = query.filter(OrderRoom.status == normalized_status)" in main_source
    assert 'id="ofCode"' in html
    assert 'id="ofTenant"' in html
    assert 'id="ofLandlord"' in html
    assert 'id="ofStatus"' in html
    assert '["status","ofStatus"]' in html


def test_super_admin_login_reuses_existing_admin_super_record():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "def _get_super_admin_account" in main_source
    assert 'UserWeb.user_id == "ADMIN_SUPER"' in main_source
    assert "admin_acc = _get_super_admin_account(db, admin_username)" in main_source
    assert "except IntegrityError:" in main_source
    assert "db.rollback()" in main_source
    assert 'if user.role == "SUPER_ADMIN":' in main_source


def test_central_error_reporting_is_configured_and_protected():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    config_source = (ROOT / "config.py").read_text(encoding="utf-8")
    reporter_source = (ROOT / "error_reporting.py").read_text(encoding="utf-8")
    assert "configure_error_reporting(send_zalo_message)" in main_source
    assert "@app.exception_handler(Exception)" in main_source
    assert "@app.exception_handler(RequestValidationError)" in main_source
    assert '@app.get("/api/admin/logs/download")' in main_source
    assert "user: Principal = Depends(require_admin)" in main_source
    assert "ZALO_ADMIN_ID" in config_source
    assert "ZALO_ERROR_ALERT_COOLDOWN_SECONDS" in config_source
    assert "RotatingFileHandler" in reporter_source
    assert "def _redact" in reporter_source
    assert "threading.Thread" in reporter_source


def test_zalo_refresh_uses_app_id_not_oa_id_and_root_supports_head():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    config_source = (ROOT / "config.py").read_text(encoding="utf-8")
    assert 'ZALO_APP_ID = os.environ.get("ZALO_APP_ID")' in services_source
    assert "app_id_clean = str(ZALO_APP_ID).strip()" in services_source
    assert '"app_id": app_id_clean' in services_source
    assert "ZALO_APP_ID phải là App ID dạng số" in services_source
    assert 'APP_ID = os.getenv("ZALO_APP_ID", "").strip()' in config_source
    assert '@app.api_route("/", methods=["GET", "HEAD"])' in main_source


def test_error_alert_and_log_use_vietnam_timezone():
    reporter_source = (ROOT / "error_reporting.py").read_text(encoding="utf-8")
    assert 'ZoneInfo("Asia/Ho_Chi_Minh")' in reporter_source
    assert "Thời gian Việt Nam:" in reporter_source
    assert "datetime.fromtimestamp(record.created, tz=_VIETNAM_TIMEZONE)" in reporter_source
    assert "Thời gian UTC:" not in reporter_source


def test_system_scheduler_and_database_use_vietnam_timezone():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    docker_source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    env_source = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert 'APP_TIMEZONE = "Asia/Ho_Chi_Minh"' in services_source
    assert 'os.environ["TZ"] = APP_TIMEZONE' in services_source
    assert "def vietnam_now()" in services_source
    assert "def vietnam_datetime_iso" in services_source
    assert "default=datetime.utcnow" not in services_source
    assert "datetime.utcnow()" not in services_source
    assert "datetime.utcnow()" not in main_source
    assert "BackgroundScheduler(timezone=VN_TZ)" in main_source
    assert "CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Ho_Chi_Minh'" in services_source
    assert '"created_at": vietnam_datetime_iso(order.created_at)' in main_source
    assert "TZ=Asia/Ho_Chi_Minh" in docker_source
    assert "TZ=Asia/Ho_Chi_Minh" in env_source


def test_zalo_media_is_attached_only_after_owned_room_confirmation():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "def get_rooms_by_landlord_phone" in services_source
    assert "def format_room_media_choices" in services_source
    assert "def build_room_media_choices" in services_source
    assert "def extract_room_code_for_media" in services_source
    assert "def attach_media_to_owned_room" in services_source
    assert 'key="landlord_phone"' in services_source
    assert 'key="room_code"' in services_source
    assert '"ROOM_MEDIA_UPDATE"' in services_source
    assert "selected_room_code = extract_room_code_for_media(message_text) if pending_urls else None" in services_source
    assert "attach_media_to_owned_room(phone, selected_room_code, pending_urls)" in services_source
    assert "clear_pending_media(user_id)" in services_source
    assert "build_room_media_choices(extracted_phone, len(pending_media))" in main_source


def test_zalo_room_update_is_partial_and_owner_scoped():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    assert "def merge_room_partial_update" in services_source
    assert "def keep_only_explicit_room_updates" in services_source
    assert "def infer_explicit_boolean_room_updates" in services_source
    assert "def normalize_room_address" in services_source
    assert "is_room_update_command(message_text)" in services_source
    assert "point_id=existing_point_id" in services_source
    assert 'key="landlord_phone"' in services_source
    assert 'match=qdrant_models.MatchValue(value=safe_phone)' in services_source
    assert '"has_balcony": ("ban công",)' in services_source
    assert '"cổng vân tay"' in services_source
    assert "explicit.update(infer_explicit_boolean_room_updates(message_text))" in services_source
    assert "has_fridge: Optional[str]" in services_source
    assert '"has_fridge": ("tủ lạnh", "tủ mát")' in services_source
    assert '"parking_info": ("chỗ để xe", "nơi để xe", "bãi xe", "để xe")' in services_source
    assert 'data["has_fridge"] = "Có"' in services_source
    assert "Không tìm thấy đúng phòng thuộc SĐT của bạn để cập nhật" in services_source
    assert "Đã cập nhật thông tin phòng" in services_source


def test_excel_dialog_resets_previous_result_before_reopen():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert 'onclick="openExcelModal()"' in html
    assert "function resetExcelDialog()" in html
    assert '$("uploadMsg").textContent=""' in html
    assert '$("uploadMsg").className="mb-3 hidden rounded p-2 text-sm"' in html
    assert '$("excelFile").value=""' in html


def test_excel_super_admin_owner_phone_comes_from_database_when_missing():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "db: Session = Depends(get_db)" in main_source
    assert 'get_phone_by_user_id(db, "ADMIN_SUPER")' in main_source
    assert "excel_owner_phone = user.username" in main_source


def test_admin_user_registration_flow_is_available():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert '@app.post("/api/user/register")' in main_source
    assert 'onclick="switchToRegister()"' in html
    assert "function switchToRegister()" in html
    assert "function switchToLogin()" in html
    assert "async function handleRegister()" in html
    assert 'api("/api/user/register"' in html
    for element_id in (
        "registerModal", "regPhone", "regOTP", "regPass",
        "registerMsg", "registerButton", "zaloQR", "openZaloOA", "zaloQRMsg",
    ):
        assert f'id="{element_id}"' in html
    assert "function configureZaloQR()" in html
    assert "function openConfiguredZaloOA()" in html
    assert "ZALO_OA_URL | tojson" in html
