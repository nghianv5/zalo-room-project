# 1. Dùng Python 3.10 phiên bản slim để tối ưu dung lượng nhẹ và build nhanh
FROM python:3.10-slim

# 2. Ngăn Python tạo file .pyc và bật log trực tiếp ra console
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 3. Tạo thư mục làm việc trong container
WORKDIR /app

# 4. Cài đặt các thư viện hệ thống cần thiết cho C-extensions (psycopg2, pandas)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt-get/lists/*

# 5. COPY REQUIREMENTS TRƯỚC (Đây là chìa khóa để tận dụng Build Cache)
COPY requirements.txt .

# 6. Cài đặt các thư viện Python (Chỉ chạy lại khi requirements.txt thay đổi)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 7. Copy toàn bộ mã nguồn ứng dụng vào container
COPY . .

RUN addgroup --system app && adduser --system --ingroup app app && \
    mkdir -p /app/static/media && chown -R app:app /app

USER app

# 8. Mở port 8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

# 9. Lệnh khởi chạy uvicorn app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
