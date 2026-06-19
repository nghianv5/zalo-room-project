import os

class Config:
    ELASTICSEARCH_URL = "https://zalo-room-db-f3a4ff.es.asia-southeast1.gcp.elastic.cloud:443"
    ELASTIC_API_KEY = "aktZczNwNEIxY1hHWFpLdmk5YjM6NHA4ekZtQU5BM3J3WmNfbzhEZnhqQQ=="
    
    ROOM_INDEX = "phong_tro_index"
    
    # Hãy đảm bảo CHỈ CÓ chuỗi ký tự Key gốc, KHÔNG CÓ dấu cách ở hai đầu
    GEMINI_API_KEY = "AQ.Ab8RN6JItD6PUypL-p2xUWZDUiQWtDB50bNdzFUL5TkX4pDDYQ" # <-- Điền chính xác Key của bạn vào đây (Ví dụ: "AIzaSyAbCdXyZ...")
    
    ZALO_ACCESS_TOKEN = "qGuwUjUh6GRH2JrwqwqS0TK6C7_Jkqjwdr0BT9lmUmdQJYqreALWBVq48ZMonMOowp8K28toOIlZL3Stlh5wIxvkMqZYhZnzlrGHMxYrJYRg7W8EghzCSAbg6c_mmMPHWpGYKVt_L6MU9NXipj0bKx8RL6lgxov2kWmCDzdjJp6O4sbBdleNVDyYH4Y4wMb9z2WILfgBR6d9VpbDXwr8JjOB272lqt14m70gMOoeQdVtJ3nIyUr5PlCP4q7rrNyvioaTBidHPLMR04TAz_8KOh8gE5ZDZ4X7cMeJPCAGPN2DIG09ZhH02izZ71xBgaOoiKC6FkMtM0NY4mqxhj5eFg5o2aJnbYb3iqfyVEIw0LkrR5HypeeiVsKsanHDqxCV1W"