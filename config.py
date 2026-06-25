import os

class Config:
    ELASTICSEARCH_URL = "https://zalo-room-db-f3a4ff.es.asia-southeast1.gcp.elastic.cloud:443"
    ELASTIC_API_KEY = "aktZczNwNEIxY1hHWFpLdmk5YjM6NHA4ekZtQU5BM3J3WmNfbzhEZnhqQQ=="
    
    ROOM_INDEX = "phong_tro_index"
    
    # Hãy đảm bảo CHỈ CÓ chuỗi ký tự Key gốc, KHÔNG CÓ dấu cách ở hai đầu
    GEMINI_API_KEY = "AQ.Ab8RN6JItD6PUypL-p2xUWZDUiQWtDB50bNdzFUL5TkX4pDDYQ" # <-- Điền chính xác Key của bạn vào đây (Ví dụ: "AIzaSyAbCdXyZ...")
    
    ZALO_ACCESS_TOKEN = "Lm8HCHGQ0LzN1HOn41m4M5CA6pGG2byZEtKy6HWjGIHSQ2TDQ31mV2rPRqid905WB59eRJPF8bm_2Jn0UH1yI3rQRI4eFo4rTbHEEsjDF58r1KXTTaGmM4aBGrPGSZD0RWWaVauMQbDI3WL8L5bmPMac4GDiPra6Mty_FruYSLORP60yAoK_LG5INcyb5a1f3miYG4HeVMvMVWf95JP4MZCr9NSFVs5C9MOJKcuQV3X-V1KEHHiV7NfCQX1m0Jb_TcbQPrqCC4nWVLLTSZmpJr1SMprx0r46ELH4FmKj5rLRRs96IGuXQWn2LNCL6091Dm8SB2vqOXza3J0ELczrQMuI80T4TqmqPXOECIPs7LSM7MrzLaC1I787Ib1nUGv9Ope4PqfITa5cFZznH74XLaOhPtXqQNbWSoudNn021ry"