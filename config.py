import os


class Config:
    ROOM_INDEX = os.getenv("ROOM_INDEX", "rooms_v01")
    APP_ID = os.getenv("ZALO_OA_ID", "")
    SECRET_KEY = os.getenv("SESSION_SECRET", "")
