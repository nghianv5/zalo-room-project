# Zalo Room App – bản nâng cấp an toàn

Ứng dụng giữ nguyên luồng Zalo tìm phòng, đăng phòng, OTP, đặt lịch, nhập Excel và trang quản trị. Bản này bổ sung phiên đăng nhập ký HMAC, phân quyền/chủ sở hữu, media nhiều tệp qua Redis, xóa mềm, phân trang, chống webhook trùng, giới hạn OTP/login, nhật ký và bản sao dữ liệu phòng trong PostgreSQL.

## Triển khai

1. Sao chép `.env.example` thành `.env` và điền toàn bộ giá trị.
2. Bắt buộc thay `SESSION_SECRET`, `ADMIN_INITIAL_PASSWORD`, các khóa Zalo/Qdrant/Gemini/Cloudinary đã từng xuất hiện trong source cũ.
3. Đặt file mẫu Excel tại `templates/Mau_Nhap_Danh_Sach_Phong.xlsx` nếu muốn dùng chức năng tải mẫu.
4. Build: `docker build -t zalo-room-app .`
5. Chạy container với các biến môi trường của nền tảng triển khai.

Không chạy nhiều worker có scheduler riêng. Khóa Redis đã hạn chế refresh token trùng, nhưng mô hình ổn định nhất vẫn là một worker web hoặc tách scheduler thành tiến trình riêng.

## Kiểm thử

Chạy `python -m pytest -q`. Endpoint `/health` kiểm tra PostgreSQL, Redis và Qdrant.

## Thay đổi dữ liệu

Hai bảng `room_records` và `audit_logs` được tự tạo khi khởi động. Qdrant vẫn là nguồn tìm kiếm để không thay đổi chức năng cốt lõi; `room_records` là bản sao phục vụ sao lưu và phục hồi. Xóa phòng trên web đổi trạng thái thành `ĐÃ XÓA`, không xóa vật lý.
