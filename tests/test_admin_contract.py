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
    assert "Cập nhật: ${formatDateTimeVN(o.updated_at)}" in html


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
    assert "direct_room_code = extract_room_code_for_media(message_text) if update_command else None" in services_source
    assert 'extracted["room_code"] = direct_room_code' in services_source
    assert 'action = "ADD_ROOM"' in services_source
    assert "valid_address or (update_command and direct_room_code)" in services_source


def test_natural_language_my_rooms_intent_is_not_tenant_search():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    assert "def is_my_rooms_request" in services_source
    assert "def format_my_rooms_overview" in services_source
    assert "def send_my_rooms_overview" in services_source
    assert "if is_my_rooms_request(message_text):" in services_source
    assert 'action == "LIST_MY_ROOMS"' in services_source
    assert '"LIST_MY_ROOMS": Dùng khi người dùng muốn xem/liệt kê/quản lý' in services_source
    assert "get_rooms_by_landlord_phone(landlord_phone, limit=50)" in services_source


def test_gemini_cost_optimizations_are_enabled():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    env_source = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "GEMINI_TEXT_RETRIES" in services_source
    assert 'models_to_try = [GEMINI_TEXT_MODEL]' in services_source
    assert "def _log_gemini_usage" in services_source
    assert 'gemini:embedding:' in services_source
    assert "GEMINI_EMBED_CACHE_TTL" in services_source
    assert "MAX_HISTORY_MESSAGES = 8" in services_source
    assert "top_k=MAX_SEARCH_ROOMS" in services_source
    assert "prompt_format_rooms" not in services_source
    assert "raw_text_search" not in services_source
    assert "def can_update_amenities_without_ai" in services_source
    assert "if can_update_amenities_without_ai(message_text):" in services_source
    assert "GEMINI_TEXT_MODEL=gemini-2.5-flash" in env_source


def test_malformed_gemini_json_is_validated_before_business_logic():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    assert "def normalize_gemini_json_text" in services_source
    assert "json.loads(response_text)" in services_source
    assert "except json.JSONDecodeError" in services_source
    assert "GEMINI JSON INVALID" in services_source
    assert "Gemini không trả JSON hợp lệ sau khi retry" in services_source
    assert '"is_valid_search": false' in services_source
    assert '"min_price": 0' in services_source
    assert '"max_price": 0' in services_source
    assert '"is_valid_search": true/false' not in services_source
    assert '"ai_reply": "Mô tả chi tiết dạng văn bản đẹp mắt..."' not in services_source
    assert "def repair_common_gemini_json_errors" in services_source
    assert "repaired_text = repair_common_gemini_json_errors(response_text)" in services_source
    assert "Sửa JSON dưới đây thành JSON hợp lệ" in services_source
    assert "class GeminiOutputError" in services_source
    assert "except GeminiOutputError:" in services_source


def test_gemini_json_calls_disable_unneeded_afc_and_thinking():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    assert "def build_gemini_generation_config" in services_source
    assert 'automatic_config(disable=True)' in services_source
    assert 'thinking_config(thinking_budget=0)' in services_source
    assert 'contents=current_prompt' in services_source
    assert 'config=build_gemini_generation_config(' in services_source
    assert "if listing_intent:" in services_source
    assert '"action": "ADD_ROOM"' in services_source
    assert "Tin đăng rõ ràng được xử lý nội bộ" in services_source


def test_zalo_media_urls_are_public_and_invalid_images_do_not_break_text():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    env_source = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "https://your-domain.com/static/icon.png" not in services_source
    assert "def get_public_server_domain" in services_source
    assert "def get_zalo_request_image_url" in services_source
    assert "def is_valid_zalo_media_url" in services_source
    assert 'res_data.get("error") == -201' in services_source
    assert "request_image_url = get_zalo_request_image_url(public_base_url)" in services_source
    assert "request_image_url = get_zalo_request_image_url(public_request_base_url)" in main_source
    assert "your-render-service.onrender.com" in env_source


def test_missing_room_price_is_user_input_error_not_system_exception():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    assert 'ROOM_PRICE_REQUIRED = "ROOM_PRICE_REQUIRED"' in services_source
    assert "def is_missing_required_room_price" in services_source
    assert 'return ROOM_PRICE_REQUIRED' in services_source
    assert 'elif message == ROOM_PRICE_REQUIRED:' in services_source
    assert "save_pending_room(user_id, data_to_save)" in services_source
    assert "Thông tin phòng đã được lưu tạm" in services_source
    assert "Lỗi: 'price' không được để trống hoặc null!" not in services_source
    price_check = services_source.index('if is_missing_required_room_price(data.get("price"))')
    embedding_call = services_source.index("vector = (", price_check)
    assert price_check < embedding_call


def test_empty_zalo_media_is_user_input_error_without_admin_alert():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    env_source = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "if not media_bytes:" in services_source
    assert "Zalo trả về tệp rỗng" in services_source
    assert "io.BytesIO(bytes(media_bytes))" in services_source
    assert "Ảnh/video bạn gửi đang rỗng" in services_source
    assert '"Cloudinary upload thất bại; chuyển lưu local", e, "save_media_file", notify=False' in services_source
    assert "None if is_video else payload.get(\"thumbnailUrl\")" in main_source
    assert 'media_items.append({"url": "", "is_video": event_name == "user_send_video"})' in main_source
    assert "MAX_IMAGE_UPLOAD_MB=15" in env_source
    assert "MAX_VIDEO_UPLOAD_MB=80" in env_source


def test_admin_rooms_show_all_records_with_server_pagination():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "page: int = 1" in main_source
    assert "page_size: int = 25" in main_source
    assert "while True:" in main_source
    assert 'limit=256' in main_source
    assert '"total": total' in main_source
    assert '"total_pages": total_pages' in main_source
    assert 'id="roomPaginationInfo"' in html
    assert 'id="roomPageSize"' in html
    assert 'id="roomPrevBtn"' in html
    assert 'id="roomNextBtn"' in html
    assert "function changeRoomPage(delta)" in html
    assert "function updateRoomPagination()" in html
    assert 'page_size:$(\'roomPageSize\')?.value||"25"' in html
    assert "limit:\"100\"" not in html


def test_admin_room_delete_is_permanent_in_qdrant_and_postgres():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    route_start = main_source.index('@app.delete("/api/rooms/{point_id}")')
    route_end = main_source.index('@app.post("/api/rooms")', route_start)
    delete_route = main_source[route_start:route_end]
    assert "db: Session = Depends(get_db)" in delete_route
    assert "qdrant_client.delete(" in delete_route
    assert "qdrant_models.PointIdsList(points=[point_id])" in delete_route
    assert "db.delete(mirror_room)" in delete_route
    assert '"ROOM_PERMANENT_DELETE"' in delete_route
    assert "qdrant_client.set_payload" not in delete_route
    assert '"ROOM_SOFT_DELETE"' not in delete_route
    assert "Xóa vĩnh viễn phòng này" in html
    assert "Xóa vĩnh viễn ${ids.length} phòng" in html
    assert "xóa vĩnh viễn bản ghi khỏi cả Qdrant" in readme


def test_admin_can_select_and_delete_many_rooms_in_one_request():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "class DeleteSelectedRoomsSchema" in services_source
    assert '@app.post("/api/admin/rooms/delete-selected")' in main_source
    assert "RoomRecord.id.in_(room_ids)" in main_source
    assert "qdrant_models.PointIdsList(points=qdrant_ids)" in main_source
    assert '"ROOM_DELETE_SELECTED"' in main_source
    assert 'id="deleteSelectedBtn"' in html
    assert "Xóa các phòng đã chọn" in html
    assert 'api("/api/admin/rooms/delete-selected"' in html
    assert "body:JSON.stringify({room_ids:ids})" in html
    assert 'id="deleteAllRoomsBtn"' not in html
    assert "function deleteAllRooms()" not in html
    assert "class DeleteAllRoomsSchema" not in services_source
    assert '@app.post("/api/admin/rooms/delete-all")' not in main_source


def test_room_media_is_grouped_by_room_and_all_media_is_displayed():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    env_source = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "def apply_media_limit" in services_source
    assert "return values if limit <= 0 else values[:limit]" in services_source
    assert '_normalise_room_media(room, 0)' in services_source
    assert "Đã hiển thị {len(room_media)} ảnh/video của phòng {room_code} ở phía trên" not in services_source
    assert "remaining_media" not in services_source
    assert "total_media_sent" not in services_source
    assert "📸 Ảnh/video phòng ${val(r.room_code)}" in html
    assert "<video src=" in html
    assert "/\\/video\\/upload\\//i" in html
    assert "MAX_MEDIA_PER_ROOM=0" in env_source
    assert "MAX_SEARCH_MEDIA_PER_ROOM=0" in env_source


def test_zalo_phone_share_uses_render_and_webhook_https_fallbacks():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    env_source = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert 'def get_public_server_domain(fallback_url: str = "")' in services_source
    assert 'os.getenv("RENDER_EXTERNAL_URL", "")' in services_source
    assert 'def get_zalo_request_image_url(fallback_url: str = "")' in services_source
    assert 'public_base_url: str = ""' in services_source
    assert "get_zalo_request_image_url(public_base_url)" in services_source
    assert "public_request_base_url = str(request.base_url)" in main_source
    assert "get_zalo_request_image_url(public_request_base_url)" in main_source
    assert "public_base_url=public_request_base_url" in main_source
    assert "RENDER_EXTERNAL_URL=" in env_source


def test_detailed_vietnamese_location_search_is_accent_tolerant():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    env_source = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "def normalize_location_search" in services_source
    assert "unicodedata.normalize(\"NFD\"" in services_source
    assert "def get_location_match_level" in services_source
    assert 'str(location_search or "").split(",", 1)[0]' in services_source
    assert "accepted_level = best_level if best_level >= 2 else 1" in services_source
    assert "rooms.sort(key=parse_price_safe, reverse=True)" in services_source
    search_start = services_source.index("def search_rooms_with_filter(")
    search_end = services_source.index("def refresh_zalo_tokens", search_start)
    search_source = services_source[search_start:search_end]
    assert "MatchText(text=location_search)" not in search_source
    assert "get_text_embedding(query_text)" not in search_source
    assert "SEARCH_CANDIDATE_LIMIT=2000" in env_source


def test_room_view_and_report_are_on_zalo_while_admin_manages_reports():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "class RoomReport(Base)" in services_source
    assert '__tablename__ = "room_reports"' in services_source
    assert "class RoomReportCreateSchema" not in services_source
    assert '@app.post("/api/rooms/{point_id}/reports")' not in main_source
    assert '@app.get("/api/admin/room-reports")' in main_source
    assert 'id="roomViewModal"' not in html
    assert 'id="roomReportModal"' not in html
    assert "function viewRoom(id)" not in html
    assert "function openRoomReport(id)" not in html
    assert "def start_zalo_room_report" in services_source
    assert "def submit_zalo_room_report" in services_source
    assert '"ROOM_REPORT_CREATE_ZALO"' in services_source
    assert '📅 Đặt lịch xem phòng: nhắn “XEM PHÒNG {normalized_code}”' in services_source
    assert '🚩 Report phòng: nhắn “REPORT PHÒNG {normalized_code}”' in services_source
    assert "report_match = re.search(" in main_source
    assert "get_pending_zalo_room_report" in main_source
    assert "clear_pending_zalo_room_report" in main_source
    assert "submit_zalo_room_report(" in main_source
    assert "booking_match = booking_candidate" in main_source
    assert "process_room_booking(tenant_zalo_id=str(sender_id)" in main_source


def test_zalo_room_media_is_sent_before_room_text():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    search_start = services_source.index("def send_zalo_search_results")
    search_end = services_source.index("def _refresh_zalo_token_after_invalid", search_start)
    search_source = services_source[search_start:search_end]
    assert "format_room_search_message(room, position, include_action_instructions=False)" in search_source
    assert "media_urls=room_media" in search_source
    assert "media_first=True" in search_source
    assert "combine_first_media=True" not in search_source
    message_start = services_source.index("def send_zalo_message")
    message_end = services_source.index("def write_audit_log", message_start)
    message_source = services_source[message_start:message_end]
    assert "media_first: bool = False" in message_source
    assert "def send_media_items(items: list)" in message_source
    assert message_source.index("if media_first and unique_media:") < message_source.index(
        "for idx, chunk in enumerate(text_chunks):"
    )
    assert "def send_zalo_room_action_buttons" in services_source
    assert '"template_type": "button"' in services_source
    assert '"type": "oa.query.show"' in services_source
    assert '"payload": f"XEM PHÒNG {normalized_code}"' in services_source
    assert '"payload": f"REPORT PHÒNG {normalized_code}"' in services_source
    assert "send_zalo_room_action_buttons(user_id, room_code)" in search_source
    assert "include_action_instructions=False" in search_source
    assert "return send_zalo_message(user_id, fallback_text)" in services_source


def test_move_in_date_defaults_to_empty_and_supports_natural_vietnamese_dates():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    assert 'move_in_date: Optional[str] = None' in services_source
    assert "def normalize_move_in_date" in services_source
    assert "def extract_move_in_date_from_text" in services_source
    assert 'return "", None' in services_source
    assert 'extracted["move_in_date"] = extract_move_in_date_from_text(message_text)' in services_source
    assert 'move_in_date_str = "Vào ở ngay"' not in services_source
    assert 'move_in_timestamp = now_vn.timestamp()' not in services_source


def test_excel_import_preserves_phone_and_code_as_text():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    assert 'pd.read_excel(io.BytesIO(contents), dtype=str)' in main_source
    assert 'pd.read_csv(io.BytesIO(contents), dtype=str)' in main_source
    assert 'pd.read_excel(temp_file, dtype=str)' in services_source


def test_standard_excel_bypasses_gemini_and_duplicate_key_is_address_plus_name():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    assert "def extract_standard_excel_rows" in services_source
    assert "standard_rows = extract_standard_excel_rows(df)" in main_source
    assert "standard_rows = extract_standard_excel_rows(df)" in services_source
    assert '"processing_mode": "AI_FALLBACK" if used_ai else "DIRECT_NO_AI"' in main_source
    assert "skip_ai_embedding=not used_ai" in main_source
    assert "skip_ai_embedding=not used_ai" in services_source
    assert "if skip_ai_embedding" in services_source
    assert "def find_duplicate_room_id(address: str, room_name: str)" in services_source
    assert 'elif type_process == "EXCEL":' in services_source
    assert "existing_id = find_duplicate_room_id(address_clean, room_name)" in services_source
    assert "Phòng bị trùng địa chỉ và tên phòng" in services_source


def test_excel_validation_errors_are_returned_and_rendered_in_admin_modal():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "def validate_excel_room_data" in services_source
    assert "validation_errors = validate_excel_room_data(extracted)" in main_source
    assert '"success_count": success_count' in main_source
    assert '"error_count": error_count' in main_source
    assert 'result_status = "error"' in main_source
    assert "const rowErrors=Array.isArray(d.errors)?d.errors:[]" in html
    assert '"field_errors": validation_errors' in main_source
    assert "Array.isArray(item.field_errors)" in html
    assert "Chi tiết lỗi:" in html
    assert "whitespace-pre-line" in html
    assert 'resultStatus==="partial"' in html
    assert 'console.error("Lỗi tải Excel:"' not in html
    assert 'print(f"❌ Dòng {current_excel_row}' not in main_source


def test_admin_timestamps_are_formatted_as_vietnamese_date_time():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "formatDateTimeVN=value=>" in html
    assert 'timeZone:"Asia/Ho_Chi_Minh"' in html
    assert 'second:"2-digit"' in html
    assert "r.move_in_timestamp" not in html
    assert "${formatDateTimeVN(r.created_at)}" in html
    assert "${formatDateTimeVN(r.updated_at)}" in html
    assert "${formatDateTimeVN(item.created_at)}" in html
    assert "${formatDateTimeVN(o.viewing_time)}" in html


def test_natural_search_overrides_invalid_gemini_extraction():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    assert "def extract_natural_room_search" in services_source
    assert 'action = "SEARCH_ROOM"' in services_source
    assert "search_params.update(natural_search)" in services_source
    assert 'natural_search.get("max_price", 0) > 0' in services_source
    assert 'print(f"📩 [ZALO RES Part' not in services_source
    direct_start = services_source.index("direct_search = extract_natural_room_search(message_text)")
    gemini_start = services_source.index('system_prompt = f"""', direct_start)
    assert direct_start < gemini_start
    assert "send_zalo_search_results(user_id, search_results)" in services_source[direct_start:gemini_start]


def test_landlord_listing_intent_has_priority_over_room_search():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    assert "def is_room_listing_request" in services_source
    assert "not listing_intent" in services_source
    assert 'action = "ADD_ROOM"' in services_source
    assert "apply_direct_room_listing_fallbacks(extracted, message_text)" in services_source
    assert 'extracted["address"] = address_match.group(1)' in services_source
    assert 'extracted["price"] = number *' in services_source
    assert 'extracted["room_size"]' in services_source
    assert '"wardrobe": ("tủ quần áo", "tủ áo", "giường tủ")' in services_source
    assert 'r"^(?:(?:toi|minh|em|anh|chi)\\s+)?cho thue' in services_source


def test_media_choice_message_and_admin_reload_after_batch_delete():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert '"Bạn hãy gửi thông tin phòng (tên phòng, địa chỉ, giá...) để tạo phòng mới"' in services_source
    assert '"Hoặc xác nhận phòng cần cập nhật bằng cách nhắn: CẬP NHẬT ẢNH <MÃ PHÒNG>"' in services_source
    assert 'lines.append(f"{index}. 🏠 {code} — {name}\\n   📍 {address}")' in services_source
    assert 'lines.append("\\nVí dụ: CẬP NHẬT ẢNH SP840D")' in services_source
    assert 'cache:"no-store"' in html
    assert "roomPage=1;" in html
    assert "await loadRooms(true);" in html


def test_admin_report_management_screen_and_crud_exist():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "class RoomReportStatusSchema" in services_source
    assert '@app.patch("/api/admin/room-reports/{report_id}/status")' in main_source
    assert '@app.delete("/api/admin/room-reports/{report_id}")' in main_source
    assert "RoomReport.room_code.ilike" in main_source
    assert "RoomReport.reporter_username.ilike" in main_source
    assert 'id="reportsTab"' in html
    assert 'id="reportsPage"' in html
    assert 'id="reportBody"' in html
    assert "function loadReports()" in html
    assert "function updateReportStatus(id,status)" in html
    assert "function deleteReport(id)" in html


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


def test_admin_room_media_update_is_validated_replaced_and_reloaded():
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")

    assert "function parseMediaUrls(value)" in html
    assert "await loadRooms();" in html
    assert "Link ảnh/video không hợp lệ" in html
    assert "replace_media_urls=bool(point_id)" in main_source
    assert "def normalize_room_media_urls" in services_source
    assert "if replace_media_urls:" in services_source


def test_admin_rooms_are_sorted_by_created_time():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'key=lambda item: str(item.get("created_at") or "")' in main_source
    assert 'item.get("updated_at") or item.get("created_at")' not in main_source


def test_room_only_persists_move_in_date_without_timestamp():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")

    assert '"move_in_date": move_in_date_str' in services_source
    assert '"move_in_timestamp": move_in_timestamp' not in services_source
    assert 'key="move_in_timestamp"' not in main_source
    assert 'datetime.strptime(str(item.get("move_in_date") or ""), "%d/%m/%Y")' in main_source
    assert 'payload_data.pop("move_in_timestamp", None)' in main_source
    assert "r.move_in_timestamp" not in html


def test_admin_room_filters_cover_details_and_boolean_checkboxes():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")

    for field_name in (
        "floor", "room_size", "max_occupants", "move_in_date",
        "is_private_bathroom", "has_ac", "has_heater", "has_washer",
        "has_fridge", "bed", "wardrobe", "allow_pets", "has_balcony",
        "has_window", "has_fingerprint_lock", "parking_info",
    ):
        assert f'"{field_name}":' in main_source or f'{field_name}: Optional[str]' in main_source
    assert "const booleanRoomFilterDefinitions=" in html
    assert "function toggleBooleanRoomFilter" in html
    assert 'return"Có"' in html
    assert 'return"Không"' in html
    assert 'p.set("move_in_date",exactMoveInDate)' in html
    assert 'id="fFloor"' in html
    assert 'id="fRoomSize"' in html
    assert 'id="fMaxOccupants"' in html
    assert 'id="fMoveInDate"' in html


def test_unindexed_room_filters_are_applied_after_qdrant_scroll():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = main_source.index('async def get_rooms_filter(')
    route_end = main_source.index('\ndef _serialize_order(', route_start)
    route_source = main_source[route_start:route_end]

    assert "active_exact_room_filters" in route_source
    assert "item.get(field_name)" in route_source
    assert "expected_value" in route_source
    assert 'key=field_name' not in route_source


def test_zalo_search_shows_text_actions_and_caps_results_at_twenty():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    env_source = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert 'MAX_SEARCH_ROOMS = min(20, max(1, int(os.getenv("MAX_SEARCH_ROOMS", "20"))))' in services_source
    assert "MAX_SEARCH_ROOMS=20" in env_source
    assert 'f"📅 Đặt lịch xem phòng: nhắn “XEM PHÒNG {normalized_code}”' in services_source
    assert 'f"🚩 Report phòng: nhắn “REPORT PHÒNG {normalized_code}”' in services_source
    assert "text_sent = send_zalo_message(user_id, fallback_text)" in services_source
    assert "return button_sent or text_sent" in services_source
    assert 'f"Hiển thị {len(rooms)}/{total_found} phòng:"' in services_source
    assert "top_k=MAX_SEARCH_ROOMS" in services_source
    assert "safe_top_k = min(max(int(top_k or MAX_SEARCH_ROOMS), 1), MAX_SEARCH_ROOMS)" in services_source


def test_room_posting_blocks_cover_web_excel_and_zalo_with_admin_bypass():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")

    assert "class RoomPostingBlock(Base):" in services_source
    assert 'block_type == "PHONE"' in services_source
    assert 'block_type == "ADDRESS"' in services_source
    assert "def normalize_block_address" in services_source
    assert "def find_room_posting_block" in services_source
    assert "not existing_id\n            and not bypass_posting_block" not in services_source
    assert "not bypass_posting_block" in services_source
    assert "format_room_posting_block_message(posting_block)" in services_source
    assert "bypass_posting_block=str(user_id) == Config.ZALO_ADMIN_ID" in services_source
    assert "bypass_posting_block=str(sender_id) == Config.ZALO_ADMIN_ID" in main_source
    assert 'bypass_posting_block=user.role == "SUPER_ADMIN"' in main_source
    assert '@app.get("/api/admin/room-post-blocks")' in main_source
    assert '@app.post("/api/admin/room-post-blocks")' in main_source
    assert '@app.patch("/api/admin/room-post-blocks/{block_id}")' in main_source
    assert '@app.delete("/api/admin/room-post-blocks/{block_id}")' in main_source
    assert 'id="postingBlocksTab"' in html
    assert 'id="postingBlocksPage"' in html
    assert "async function loadPostingBlocks()" in html
    assert "async function savePostingBlock(event)" in html
    assert "async function deletePostingBlock(id)" in html
    assert "Chặn cả tạo mới và cập nhật phòng" in html


def test_blocked_rooms_are_visible_on_web_but_locked_everywhere_else():
    services_source = (ROOT / "services.py").read_text(encoding="utf-8")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")

    assert "def get_room_access_block" in services_source
    assert "def format_room_access_block_message" in services_source
    assert "candidate_rooms = [room for room in candidate_rooms if not get_room_access_block(room, rules)]" in services_source
    assert "room.get(\"posting_blocked\")" in services_source
    assert "access_block = get_room_access_block(room_data)" in services_source
    assert "không thể xem, đặt lịch hoặc report" in services_source
    assert 'room["posting_blocked"] = bool(access_block)' in main_source
    assert 'room["can_manage"] = user.role == "SUPER_ADMIN" or not bool(access_block)' in main_source
    assert "def _serialize_order_with_room_access" in main_source
    assert "Phòng đang bị khóa nên không thể thao tác quản lý đặt phòng" in main_source
    assert "Một hoặc nhiều phòng đang bị khóa nên không thể thao tác" in main_source
    assert "Phòng đang bị khóa. User chỉ được xem thông tin" in html
    assert 'o.can_manage===false' in html
    assert '.roomCheck:not(:disabled)' in html


def test_order_list_does_not_depend_on_qdrant_room_lookup():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    serializer_start = main_source.index("def _serialize_order_with_room_access")
    serializer_end = main_source.index("\ndef _get_room_by_code", serializer_start)
    serializer_source = main_source[serializer_start:serializer_end]

    assert "_get_room_by_code" not in serializer_source
    assert "room_payloads.get(code)" in serializer_source
    assert 'room_data.setdefault("landlord_phone", order.landlord_phone or "")' in serializer_source
    assert "db.query(RoomRecord).filter(RoomRecord.room_code.in_(room_codes)).all()" in main_source
    assert "active_block_rules = get_active_room_posting_blocks(db)" in main_source
    assert "OrderRoom.landlord_phone.in_(phone_variants)" in main_source
    assert 'f"+84{principal_phone[1:]}"' in main_source


def test_blocked_user_or_address_orders_are_hidden_from_regular_users():
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert 'candidate_orders = ordered_query.all() if user.role != "SUPER_ADMIN" else []' in main_source
    assert "if not get_room_access_block(room_data, active_block_rules):" in main_source
    assert "visible_orders.append(item)" in main_source
    assert "total = len(visible_orders)" in main_source
    assert "orders = visible_orders[safe_offset:safe_offset + safe_limit]" in main_source
    assert "else:\n        total = query.count()" in main_source
