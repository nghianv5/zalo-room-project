import os

class Config:
    ELASTICSEARCH_URL = "https://zalo-room-db-f3a4ff.es.asia-southeast1.gcp.elastic.cloud:443"
    ELASTIC_API_KEY = "AQ.Ab8RN6KgODYVlRLycHkUsu4byQhC_kjbmcVzrTYwUjb5Vb_VDA"
    
    ROOM_INDEX = "phong_tro_index"
    
    # Hãy đảm bảo CHỈ CÓ chuỗi ký tự Key gốc, KHÔNG CÓ dấu cách ở hai đầu
    GEMINI_API_KEY = "AQ.Ab8RN6JItD6PUypL-p2xUWZDUiQWtDB50bNdzFUL5TkX4pDDYQ" # <-- Điền chính xác Key của bạn vào đây (Ví dụ: "AIzaSyAbCdXyZ...")
    
#    ZALO_ACCESS_TOKEN = "blPRDtKfjYEMyLmtSI-p99B-KJ9nESrlmx8YBsSKh3tEnIzGT7oRAfRoSZucOUmkZUvQFsfjq4ZCppikV4wgETMM6YrU4AL7nSb644Hph7BLxHrYO5xt1jQ655jNFvyFpuuyOtuFWGEOZ1zKV13q0_RFJqTVIAqtp_PEQ71ytpwQfMee0XFNMudBI1qSMDKAZOTeMsedWqVRfGiqHJgQSzAVE2On79OkkfGQFmqozLUoa5iwK1BNQTIONoHW2B1UYv4JJ3ClfY-5X0r1PJAGKUo3EGGB7jeOjgPEHoz-fJlC-n8lN6kZMyV6DoaY6guxmw8TAqCRgaJufICL6nYOUAkv46KP6FiDa9jlK3K7mXQofYz6K2YO0CM42LLL7QGzyiCzMqHph0_ls3j7MbMaKUhk6mPBGjFfsYLnCR0Z"