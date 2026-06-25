import os

class Config:
    ELASTICSEARCH_URL = "https://zalo-room-db-f3a4ff.es.asia-southeast1.gcp.elastic.cloud:443"
    ELASTIC_API_KEY = "aktZczNwNEIxY1hHWFpLdmk5YjM6NHA4ekZtQU5BM3J3WmNfbzhEZnhqQQ=="
    
    ROOM_INDEX = "phong_tro_index"
    
    # Hãy đảm bảo CHỈ CÓ chuỗi ký tự Key gốc, KHÔNG CÓ dấu cách ở hai đầu
    GEMINI_API_KEY = "AQ.Ab8RN6JItD6PUypL-p2xUWZDUiQWtDB50bNdzFUL5TkX4pDDYQ" # <-- Điền chính xác Key của bạn vào đây (Ví dụ: "AIzaSyAbCdXyZ...")
    
    ZALO_ACCESS_TOKEN = "6o4OIOE3J2GR9Xz9XuL98nyf4ao7aoXOVIeFGUNv1oyFG5qQbwnP26PRSGMtXcTkS2L02k7SKZX-U6ChlujeFMDJF3UYY00oQIrJFvN3MWTPCNiOX_De4m8FG2djrWu76XfP6jkxKHyrNGiJfOS_5KDL51sZiXG8Ma4sR9-wEda4Q1jI_BWQGmrSC7h0-JX_03KMS_Z30aqt0ZDeoCGdLIOu0cpid0W16qix2-IB07ePM5CBW_nlOM8OQLgEqtz34GrI9ex6NXb2OrCglePgD7nDGJsuYKGPHrbUFSAIUWiiQ41YofX1VnTJHLICgs9XG5rfNeU4O5PWIMKokv0o6553EmEEcIygLpqz7CQP734yVpuROaseb6DSXvjA90"