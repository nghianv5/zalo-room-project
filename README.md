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

`SERVER_DOMAIN` phải là origin HTTPS công khai của Web Service Render, ví dụ `https://zalo-room-app.onrender.com`, không phải URL PostgreSQL. Tệp `/static/icon_zalo_room.png` phải mở được công khai và trả HTTP 200. Nếu thiếu `SERVER_DOMAIN`, source lần lượt dùng `RENDER_EXTERNAL_URL` và HTTPS base URL của webhook làm dự phòng. Hệ thống loại URL local/không HTTPS, bỏ qua media bị Zalo trả lỗi `-201` nhưng vẫn gửi nội dung phòng, và không lưu lại URL media tạm của webhook khi quá trình lưu bền vững thất bại.

## Tối ưu chi phí Gemini

Ứng dụng mặc định chỉ dùng `gemini-2.5-flash`, retry tối đa 2 lần, cache embedding trong Redis 7 ngày và giữ 8 tin nhắn lịch sử gần nhất. Kết quả tìm phòng được định dạng bằng code nên không gọi Gemini lần hai. Có thể điều chỉnh bằng `GEMINI_TEXT_MODEL`, `GEMINI_TEXT_RETRIES`, `GEMINI_EMBED_RETRIES`, `GEMINI_EMBED_CACHE_TTL`, `GEMINI_MAX_OUTPUT_TOKENS` và `GEMINI_EXCEL_MAX_OUTPUT_TOKENS`. Log `GEMINI USAGE` cho biết token đầu vào, đầu ra và tổng token theo từng tác vụ.

## Kiểm thử

Chạy `python -m pytest -q`. Endpoint `/health` kiểm tra PostgreSQL, Redis và Qdrant.

## Log lỗi và cảnh báo Zalo Admin

Lỗi được ghi vào `logs/app.log` theo cơ chế xoay vòng và exception nghiêm trọng được gửi tới Zalo Admin. Thời gian trong file log và tin Zalo dùng múi giờ Việt Nam `Asia/Ho_Chi_Minh` (UTC+7). `ZALO_ADMIN_ID` phải là **Zalo User ID của tài khoản quản trị đã từng tương tác với OA**, không phải OA ID hoặc App ID. Cấu hình bằng `ZALO_ADMIN_ID`, `ZALO_ERROR_ALERTS_ENABLED`, `ZALO_NOTIFY_HTTP_4XX`, `ZALO_ERROR_ALERT_COOLDOWN_SECONDS`, `LOG_FILE_PATH`, `LOG_MAX_BYTES` và `LOG_BACKUP_COUNT`. Mặc định không gửi lỗi HTTP 4xx để tránh spam do đăng nhập sai hoặc request sai; đặt `ZALO_NOTIFY_HTTP_4XX=true` nếu muốn nhận cả nhóm này. Super Admin có thể tải log hiện tại tại `GET /api/admin/logs/download` bằng access token. Token, mật khẩu, API key và mật khẩu PostgreSQL được che trước khi ghi/gửi.

Render dùng filesystem tạm nếu chưa gắn Persistent Disk, vì vậy file log có thể mất sau lần deploy/restart; cảnh báo Zalo vẫn được gửi theo thời gian thực.

## Thay đổi dữ liệu

Hai bảng `room_records` và `audit_logs` được tự tạo khi khởi động. Qdrant vẫn là nguồn tìm kiếm để không thay đổi chức năng cốt lõi; `room_records` là bản sao dữ liệu phòng. Xóa phòng trên web là xóa vĩnh viễn bản ghi khỏi cả Qdrant và `room_records`; `audit_logs` chỉ giữ mã phòng, chủ sở hữu và người thực hiện để phục vụ kiểm tra. Đơn đặt phòng lịch sử không bị xóa theo phòng.

Trang Admin cho phép tích chọn một hoặc nhiều phòng rồi bấm **Xóa các phòng đã chọn**. Nút và API xóa toàn bộ phòng đã được loại bỏ để tránh xóa nhầm; thao tác xóa nhiều gửi một yêu cầu duy nhất, kiểm tra quyền với toàn bộ danh sách trước khi xóa khỏi Qdrant và PostgreSQL.

Luồng Zalo ưu tiên nhận dạng câu đăng phòng của chủ nhà như `tôi cần cho thuê phòng ở ...` là `ADD_ROOM`, không chuyển nhầm sang tìm phòng khi câu có địa chỉ và giá. Hệ thống bổ sung trực tiếp địa chỉ, giá, diện tích và các tiện ích được nói rõ nếu kết quả AI bỏ sót.

Các cách nói thông dụng như `cho thuê phòng ở...`, `cần cho thuê phòng...`, `đăng phòng...`, `rao phòng...`, `có phòng cần người thuê...` cũng được ưu tiên là đăng phòng. Sau khi Admin xóa nhiều phòng, danh sách được tải lại từ trang đầu với cache bị vô hiệu hóa để dữ liệu đã xóa biến mất ngay.

Mỗi phòng trên Admin có nút **Xem phòng** để mở toàn bộ dữ liệu/media dạng chỉ đọc và nút **Report phòng**. Hộp thoại report khóa mã phòng, tên phòng, địa chỉ; người dùng chỉ nhập nội dung phản ánh. Report được lưu vào PostgreSQL `room_reports`, giới hạn 2.000 ký tự và có API quản trị `GET /api/admin/room-reports`.

Super Admin có tab **Report phòng** để lọc theo mã phòng, người gửi và trạng thái; có thể chuyển report giữa `MỚI`, `ĐANG XỬ LÝ`, `ĐÃ XỬ LÝ`, `BỎ QUA` hoặc xóa report. Câu tìm phòng có đủ địa chỉ và giá được parser nội bộ ưu tiên xử lý trước kết quả Gemini, ví dụ `xem phòng phan thị hành, phú thọ hoà, tân phú 6tr` được hiểu là địa chỉ `phan thị hành, phú thọ hoà, tân phú`, giá từ 0 đến 6.000.000 đồng.

`landlord_phone` là trường bắt buộc cho mọi lần tạo/cập nhật phòng từ web, Zalo và Excel. Giá trị phải là số điện thoại Việt Nam 10 chữ số hợp lệ. Các bản ghi Qdrant cũ từng thiếu trường này cần được bổ sung trước khi cập nhật lại.

Khi tìm phòng, hệ thống không in đường dẫn `media_urls`. Mỗi phòng được gửi thành một cụm riêng theo thứ tự: thông tin phòng, tiêu đề ghi đúng mã phòng, toàn bộ ảnh/video, rồi thông báo kết thúc cụm trước khi chuyển sang phòng tiếp theo. Admin cũng hiển thị gallery riêng trong từng dòng phòng; ảnh và video được phân loại, video có trình phát. `MAX_MEDIA_PER_ROOM=0`, `MAX_SEARCH_MEDIA_PER_ROOM=0` và `MAX_SEARCH_MEDIA=0` nghĩa là không giới hạn media; `MAX_SEARCH_ROOMS` vẫn giới hạn số phòng của một lượt tìm để tránh gửi quá nhiều phòng không liên quan.

Tìm kiếm Qdrant dùng timeout 30 giây và retry 3 lần theo mặc định. Nếu vector search vẫn lỗi hoặc timeout, hệ thống fallback sang `scroll` với cùng bộ lọc trạng thái, địa chỉ và giá. Có thể chỉnh bằng `QDRANT_TIMEOUT_SECONDS` và `QDRANT_SEARCH_RETRIES`.

Tìm địa chỉ dài được chuẩn hóa dấu tiếng Việt (`hoà/hòa`), dấu câu và khoảng trắng rồi đối chiếu theo các thành phần đường–phường–quận. Hệ thống lọc Qdrant theo trạng thái và giá trước; nếu không có địa chỉ khớp đầy đủ mới fallback theo tên đường. Vì vậy `xem phòng phan thị hành, phú thọ hoà, tân phú 6tr` vẫn tìm được dữ liệu có cách viết tương đương. `SEARCH_CANDIDATE_LIMIT` giới hạn số bản ghi được kiểm tra trong một lượt (mặc định 2.000).

## Quản trị phòng và đặt phòng

Trang `/admin` hiển thị đầy đủ 24 trường của `RoomCreateUpdateSchema`, gồm mã phòng, giường, tủ quần áo, media và các trường hệ thống liên quan. Form thêm/sửa phòng dùng cùng bộ trường với API và tiếp tục áp dụng phân quyền chủ sở hữu hiện có.

Tài khoản `SUPER_ADMIN` có tab **Đặt phòng** với các API:

- `GET /api/admin/orders`: xem và lọc đơn.
- `POST /api/admin/orders`: thêm đơn.
- `PUT /api/admin/orders/{order_id}`: sửa đơn.
- `DELETE /api/admin/orders/{order_id}`: xóa đơn.

API đặt phòng kiểm tra mã phòng trong Qdrant, chuẩn hóa số điện thoại, chống trùng người thuê/mã phòng và ghi audit log. User thường chỉ xem đơn có `landlord_phone` trùng số đăng nhập và chỉ cập nhật trạng thái sang `ĐÃ XEM` hoặc `ĐÃ THUÊ`; Super Admin giữ quyền thêm, sửa và xóa toàn bộ đơn.
