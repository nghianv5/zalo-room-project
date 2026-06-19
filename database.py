from elasticsearch import Elasticsearch
from config import Config


elastic_url = Config.ELASTICSEARCH_URL
elastic_api_key = Config.ELASTIC_API_KEY

# Khởi tạo biến es chuẩn cho toàn bộ hệ thống xài chung
if elastic_api_key:
    es = Elasticsearch(
        elastic_url,
        api_key=elastic_api_key
    )
else:
    es = Elasticsearch(elastic_url)

def init_db():
    # Kiểm tra an toàn trước khi chạy lệnh gọi đến database
    if es is None:
        print("⚠ Không có kết nối Database hợp lệ. Bỏ qua init_db.")
        return
        
    try:
        # Giữ nguyên đoạn code cũ của bạn ở đây
        # Ví dụ: if not es.indices.exists(index=Config.ROOM_INDEX):
        pass 
    except Exception as e:
        print(f"⚠ Lỗi khi khởi tạo index: {e}")
        
def init_db():
    if not es.indices.exists(index=Config.ROOM_INDEX):
        # Định nghĩa kiểu dữ liệu (Mapping) tối ưu cho Elasticsearch
        mapping = {
            "mappings": {
                "properties": {
                    "dia_chi": {"type": "text", "analyzer": "standard"},
                    "ten_phong": {"type": "keyword"},
                    "tang": {"type": "integer"},
                    "gia_thue": {"type": "long"},
                    "khep_kin": {"type": "boolean"},
                    "tien_nghi": {
                        "properties": {
                            "dieu_hoa": {"type": "boolean"},
                            "nong_lanh": {"type": "boolean"},
                            "may_giat": {"type": "boolean"}
                        }
                    },
                    "cho_nuoi_thu_cung": {"type": "boolean"},
                    "media_urls": {"type": "keyword"},
                    "thoi_gian_o": {"type": "keyword"},
                    "co_ban_cong": {"type": "boolean"},
                    "co_cua_so": {"type": "boolean"},
                    "trang_thai": {"type": "keyword"}  # "con_trong" hoặc "da_cho_thue"
                }
            }
        }
        es.indices.create(index=Config.ROOM_INDEX, body=mapping)
        print("-> Đã khởi tạo thành công Kho dữ liệu Elasticsearch!")
    else:
        print("-> Kho dữ liệu Elasticsearch đã sẵn sàng.")

def save_room(room_id: str, data: dict):
    return es.index(index=Config.ROOM_INDEX, id=room_id, body=data, refresh=True)

def update_status(room_id: str, status: str):
    return es.update(index=Config.ROOM_INDEX, id=room_id, body={"doc": {"trang_thai": status}}, refresh=True)

def search_rooms(query_body: dict):
    response = es.search(index=Config.ROOM_INDEX, body=query_body)
    return [hit["_source"] | {"id": hit["_id"]} for hit in response["hits"]["hits"]]