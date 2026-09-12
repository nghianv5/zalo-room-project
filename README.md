# Zalo Room App – bản nâng cấp an toàn

Ứng dụng giữ nguyên luồng Zalo tìm phòng, đăng phòng, OTP, đặt lịch, nhập Excel và trang quản trị. Bản này bổ sung phiên đăng nhập ký HMAC, phân quyền/chủ sở hữu, media nhiều tệp qua Redis, xóa mềm, phân trang, chống webhook trùng, giới hạn OTP/login, nhật ký và bản sao dữ liệu phòng trong PostgreSQL.

## Triển khai

1. Sao chép `.env.example` thành `.env` và điền toàn bộ giá trị.
2. Bắt buộc thay `SESSION_SECRET`, `ADMIN_INITIAL_PASSWORD`, các khóa Zalo/Qdrant/Gemini/Cloudinary đã từng xuất hiện trong source cũ.
3. Đặt file mẫu Excel tại `templates/Mau_Nhap_Danh_Sach_Phong.xlsx` nếu muốn dùng chức năng tải mẫu.
4. Build: `docker build -t zalo-room-app .`
5. Chạy container với các biến môi trường của nền tảng triển khai.

Không chạy nhiều worker có scheduler riêng. Khóa Redis đã hạn chế refresh token trùng, nhưng mô hình ổn định nhất vẫn là một worker web hoặc tách scheduler thành tiến trình riêng.

`ZALO_APP_ID` phải là App ID của ứng dụng liên kết OA trên Zalo Developers. Không dùng `ZALO_OA_ID` hoặc phần số trong link OA thay cho App ID. `ZALO_SECRET_KEY` phải thuộc cùng ứng dụng với `ZALO_APP_ID`; refresh token cũng phải được cấp cho đúng cặp ứng dụng/OA đó. Route `/` hỗ trợ cả GET và HEAD để health check của Render nhận HTTP 200.

## Kiểm thử

Chạy `python -m pytest -q`. Endpoint `/health` kiểm tra PostgreSQL, Redis và Qdrant.

## Log lỗi và cảnh báo Zalo Admin

Lỗi được ghi vào `logs/app.log` theo cơ chế xoay vòng và exception nghiêm trọng được gửi tới Zalo Admin. `ZALO_ADMIN_ID` phải là **Zalo User ID của tài khoản quản trị đã từng tương tác với OA**, không phải OA ID hoặc App ID. Cấu hình bằng `ZALO_ADMIN_ID`, `ZALO_ERROR_ALERTS_ENABLED`, `ZALO_NOTIFY_HTTP_4XX`, `ZALO_ERROR_ALERT_COOLDOWN_SECONDS`, `LOG_FILE_PATH`, `LOG_MAX_BYTES` và `LOG_BACKUP_COUNT`. Mặc định không gửi lỗi HTTP 4xx để tránh spam do đăng nhập sai hoặc request sai; đặt `ZALO_NOTIFY_HTTP_4XX=true` nếu muốn nhận cả nhóm này. Super Admin có thể tải log hiện tại tại `GET /api/admin/logs/download` bằng access token. Token, mật khẩu, API key và mật khẩu PostgreSQL được che trước khi ghi/gửi.

Render dùng filesystem tạm nếu chưa gắn Persistent Disk, vì vậy file log có thể mất sau lần deploy/restart; cảnh báo Zalo vẫn được gửi theo thời gian thực.

## Thay đổi dữ liệu

Hai bảng `room_records` và `audit_logs` được tự tạo khi khởi động. Qdrant vẫn là nguồn tìm kiếm để không thay đổi chức năng cốt lõi; `room_records` là bản sao phục vụ sao lưu và phục hồi. Xóa phòng trên web đổi trạng thái thành `ĐÃ XÓA`, không xóa vật lý.

`landlord_phone` là trường bắt buộc cho mọi lần tạo/cập nhật phòng từ web, Zalo và Excel. Giá trị phải là số điện thoại Việt Nam 10 chữ số hợp lệ. Các bản ghi Qdrant cũ từng thiếu trường này cần được bổ sung trước khi cập nhật lại.

Khi tìm phòng, hệ thống không in đường dẫn `media_urls` trong tin nhắn. Mỗi phòng được gửi thành một cụm riêng; ảnh đầu tiên được ghép cùng request với nội dung phòng, các ảnh tiếp theo nằm ngay sau đó. Nếu OA không hỗ trợ payload kết hợp, hệ thống tự fallback sang nội dung rồi ảnh để không làm gián đoạn phản hồi. Các đường gạch dài thừa cũng được tự loại bỏ. `MAX_SEARCH_ROOMS` giới hạn số phòng hiển thị (mặc định 5), `MAX_SEARCH_MEDIA_PER_ROOM` giới hạn media mỗi phòng (mặc định 3), và `MAX_SEARCH_MEDIA` giới hạn tổng media của một lượt (mặc định 10).

Tìm kiếm Qdrant dùng timeout 30 giây và retry 3 lần theo mặc định. Nếu vector search vẫn lỗi hoặc timeout, hệ thống fallback sang `scroll` với cùng bộ lọc trạng thái, địa chỉ và giá. Có thể chỉnh bằng `QDRANT_TIMEOUT_SECONDS` và `QDRANT_SEARCH_RETRIES`.

## Quản trị phòng và đặt phòng

Trang `/admin` hiển thị đầy đủ 24 trường của `RoomCreateUpdateSchema`, gồm mã phòng, giường, tủ quần áo, media và các trường hệ thống liên quan. Form thêm/sửa phòng dùng cùng bộ trường với API và tiếp tục áp dụng phân quyền chủ sở hữu hiện có.

Tài khoản `SUPER_ADMIN` có tab **Đặt phòng** với các API:

- `GET /api/admin/orders`: xem và lọc đơn.
- `POST /api/admin/orders`: thêm đơn.
- `PUT /api/admin/orders/{order_id}`: sửa đơn.
- `DELETE /api/admin/orders/{order_id}`: xóa đơn.

API đặt phòng kiểm tra mã phòng trong Qdrant, chuẩn hóa số điện thoại, chống trùng người thuê/mã phòng và ghi audit log. User thường chỉ xem đơn có `landlord_phone` trùng số đăng nhập và chỉ cập nhật trạng thái sang `ĐÃ XEM` hoặc `ĐÃ THUÊ`; Super Admin giữ quyền thêm, sửa và xóa toàn bộ đơn.
