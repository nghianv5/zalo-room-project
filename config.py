import os

class Config:
    ELASTICSEARCH_URL = "https://zalo-room-db-f3a4ff.es.asia-southeast1.gcp.elastic.cloud:443"
    ELASTICSEARCH_PASSWORD = "SnFhazNaNEIxY1hHWFpLdmk5STI6SXlVUDZyM1NPZEVUZHhnSEVCX0pnQQ=="
    ROOM_INDEX = "phong_tro_index"
    
    # Hãy đảm bảo CHỈ CÓ chuỗi ký tự Key gốc, KHÔNG CÓ dấu cách ở hai đầu
    GEMINI_API_KEY = "AQ.Ab8RN6JItD6PUypL-p2xUWZDUiQWtDB50bNdzFUL5TkX4pDDYQ" # <-- Điền chính xác Key của bạn vào đây (Ví dụ: "AIzaSyAbCdXyZ...")