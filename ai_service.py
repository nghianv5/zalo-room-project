#import google.generativeai as genai
#import json
#from config import Config
#
## Cấu hình trực tiếp API Key và ép xóa ký tự thừa ở 2 đầu chuỗi
#genai.configure(api_key=Config.GEMINI_API_KEY.strip())
#
#def analyze_zalo_message(message: str) -> dict:
#    # Viết Prompt trực tiếp, sử dụng phép nối chuỗi để tránh lỗi dấu ngoặc nhọn của Python
#    prompt = """
#Bạn là trợ lý AI chuyên nghiệp cho hệ thống quản lý bất động sản và phòng trọ tại Việt Nam. 
#Nhiệm vụ của bạn là đọc kỹ tin nhắn của chủ nhà và trích xuất thông tin chính xác thành cấu trúc JSON.
#
#Yêu cầu bóc tách thông tin cực kỳ nghiêm ngặt:
#1. dia_chi (string hoặc null): Trích xuất địa chỉ đầy đủ (Ví dụ: "số 45 Lê Văn Lương").
#2. ten_phong (string hoặc null)
#3. tang (integer hoặc null)
#4. gia_thue (integer hoặc null): Phải phân tích kỹ các cụm từ chỉ giá của Việt Nam (Ví dụ: "4 triệu rưỗi" = 4500000, "3tr5" = 3500000, "5 triệu" = 5000000, "4.2tr" = 4200000). Luôn quy đổi ra con số cụ thể hệ VND, không để dạng chữ.
#5. khep_kin (boolean hoặc null): Nếu bài viết ghi "khép kín", "vệ sinh riêng", "vệ sinh trong phòng", "wc riêng" thì bắt buộc là true. Nếu không nhắc gì hoặc wc chung thì là false.
#6. dieu_hoa (boolean hoặc null): Có chữ "điều hòa", "máy lạnh" -> true.
#7. nong_lanh (boolean hoặc null): Có chữ "nóng lạnh", "bình nóng lạnh" -> true.
#8. may_giat (boolean hoặc null): Có chữ "máy giặt" -> true.
#9. cho_nuoi_thu_cung (boolean hoặc null): Có chữ "nuôi chó", "nuôi mèo", "thú cưng" -> true. Nếu cấm thì -> false.
#10. thoi_gian_o (string hoặc null): Ví dụ: "Đầu tháng sau", "Ở ngay".
#11. co_ban_cong (boolean hoặc null): Có chữ "ban công" -> true.
#12. co_cua_so (boolean hoặc null): Có chữ "cửa sổ" -> true.
#
#🚨 QUY TẮC BÁO THIẾU THÔNG TIN (thong_tin_thieu):
#Bạn chỉ được phép coi là thiếu và cho vào danh sách "thong_tin_thieu" khi và chỉ khi các trường cốt lõi sau có giá trị là NULL hoặc không thể tìm thấy bất kỳ manh mối nào trong văn bản:
#- "dia_chi"
#- "gia_thue"
#- "khep_kin"
#
#Nếu các trường trên ĐÃ CÓ thông tin (dù được viết bằng văn bản nói như '4 triệu rưỗi' hay 'khép kín'), bạn PHẢI tự chuyển đổi sang kiểu dữ liệu tương ứng (Số/Boolean) và KHÔNG ĐƯỢC XẾP CHÚNG VÀO "thong_tin_thieu".
#
#Phản hồi bắt buộc ĐÚNG định dạng JSON sau, tuyệt đối không thêm ký tự hay từ ngữ nào khác ngoài JSON:
#{
#  "data": {
#    "dia_chi": null,
#    "ten_phong": null,
#    "tang": null,
#    "gia_thue": null,
#    "khep_kin": null,
#    "dieu_hoa": null,
#    "nong_lanh": null,
#    "may_giat": null,
#    "cho_nuoi_thu_cung": null,
#    "thoi_gian_o": null,
#    "co_ban_cong": null,
#    "co_cua_so": null
#  },
#  "thong_tin_thieu": []
#}
#
#Tin nhắn cần xử lý:
#""" + f'"{message}"'
#
#    try:
#        model = genai.GenerativeModel('gemini-2.5-flash')
#        response = model.generate_content(
#            prompt,
#            generation_config={"response_mime_type": "application/json"}
#        )
#        return json.loads(response.text)
#
#        
#    except Exception as e:
#        # In lỗi thật ra màn hình đen Uvicorn để lập trình viên theo dõi
#        print("\n[LỖI HỆ THỐNG AI CRASH]:", str(e), "\n")
#        # Trả về mã lỗi chung để file main.py nhận biết
#        return {"data": {}, "thong_tin_thieu": ["loi_he_thong_ai"]}
#    