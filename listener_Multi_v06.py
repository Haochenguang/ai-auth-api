import time
import re
import email
from email.header import decode_header
import threading
from imapclient import IMAPClient
from flask import Flask, request, jsonify

# ================= 🔧 配置区 =================
FEISHU_IMAP_SERVER = 'imap.feishu.cn'

# 在这里填入你要监听的所有飞书邮箱
ACCOUNTS = [
    {"email": "SBF_AI_01@superbonfire.com", "password": "3bo8gE496FeIloAm"},
    {"email": "SBF_AI_02@superbonfire.com", "password": "qjc6s8seOHInCqKo"},
    {"email": "SBF_AI_03@superbonfire.com", "password": "0guuq9iMlyMIW1lS"},
    {"email": "SBF_AI_04@superbonfire.com", "password": "zZlbbc43a5zUmsvQ"},
    {"email": "SBF_AI_05@superbonfire.com", "password": "bsIZsi0QWdD72Aib"},
]

# ⬇️ 并发上限配置
PLATFORM_LIMITS = {
    #'seedance': 1,      
    'lovart': 2,        
    # 'midjourney': 2,    
    'chatgpt': 5,       
    'jimeng': 10,       
    'keling': 10        
}

# ⬇️ 🎯 核心新增：中英文搜索雷达（在这里添加中文识别词）
PLATFORM_KEYWORDS = {
    #'seedance': ['seedance'],
    'lovart': ['lovart'],
   # 'midjourney': ['midjourney'],
    'chatgpt': ['chatgpt', 'openai'], # OpenAI 的邮件里可能没有 chatgpt 字眼，加上 openai 防漏
    'jimeng': ['jimeng', '即梦'],
    'keling': ['keling', '可灵']
}
# ============================================

TARGET_PLATFORMS = list(PLATFORM_LIMITS.keys())
EMAIL_LIST = [acc["email"] for acc in ACCOUNTS]

# 记录最新验证码的临时仓库
code_storage = {platform: None for platform in TARGET_PLATFORMS}

# 独立计时锁二维账本 [邮箱][平台]
lock_storage = {
    email_acc: {
        platform: {"owners": {}} for platform in TARGET_PLATFORMS
    } for email_acc in EMAIL_LIST
}

LOCK_DURATION = 30 * 60  # 每个人独立的锁定时间（30 分钟）

app = Flask(__name__)

def parse_verification_code_with_context(body, keyword):
    """
    智能解析验证码：围绕着“触发词(比如'即梦')”去寻找数字
    """
    body_lower = body.lower()
    
    # 1. 尝试匹配关键字后面临近的 4-6 位数字
    match = re.search(rf"{keyword}[^0-9]{{0,40}}\b(\d{{4,6}})\b", body_lower)
    if match:
        return match.group(1)
        
    # 2. 如果没找到，先清理掉常见的干扰数字
    clean_body = re.sub(r'\b106\d+\b', '', body)
    clean_body = re.sub(r'\d{4}-\d{2}-\d{2}', '', clean_body) # 过滤日期
    clean_body = re.sub(r'\d{2}:\d{2}', '', clean_body)       # 过滤时间
    
    # 3. 兜底寻找剩下文本中的 4-6 位数字
    match = re.search(r'\b\d{4,6}\b', clean_body)
    return match.group(0) if match else None

def monitor_single_account(email_account, app_password):
    while True:
        try:
            with IMAPClient(FEISHU_IMAP_SERVER, ssl=True) as server:
                server.login(email_account, app_password)
                server.select_folder('INBOX')
                print(f"[✔] 邮箱 {email_account} 开始监听...")
                server.idle()
                while True:
                    responses = server.idle_check(timeout=1740) 
                    if responses:
                        server.idle_done() 
                        messages = server.search('UNSEEN')
                        for uid, message_data in server.fetch(messages, 'RFC822').items():
                            msg = email.message_from_bytes(message_data[b'RFC822'])
                            sender = msg.get("From", "").lower()
                            subject = msg.get("Subject", "")
                            
                            try:
                                decoded_subject = ""
                                for part, encoding in decode_header(subject):
                                    if isinstance(part, bytes):
                                        decoded_subject += part.decode(encoding or 'utf-8', errors='ignore')
                                    else:
                                        decoded_subject += part
                                subject_str = decoded_subject.lower()
                            except Exception:
                                subject_str = str(subject).lower()

                            body = ""
                            if msg.is_multipart():
                                for part in msg.walk():
                                    if part.get_content_type() in ["text/plain", "text/html"]:
                                        payload = part.get_payload(decode=True)
                                        if payload: body += payload.decode(errors='ignore')
                            else:
                                payload = msg.get_payload(decode=True)
                                if payload: body = payload.decode(errors='ignore')
                            
                            body_lower = body.lower()

                            # 🎯 智能多语言匹配逻辑
                            for platform in TARGET_PLATFORMS:
                                # 去字典里拿这个平台的所有搜索词，没有的话就默认用它自己的英文名
                                keywords = PLATFORM_KEYWORDS.get(platform, [platform])
                                is_forwarded = any(k in sender or k in subject_str or k in body_lower for k in ["forward", "sms", "短信", "转发", "phone"])
                                
                                matched_kw = None
                                for kw in keywords:
                                    # 只要发件人、正文或主题里命中任意一个词（比如命中了“即梦”），就判定成功
                                    if (kw in sender) or (is_forwarded and (kw in body_lower or kw in subject_str)):
                                        matched_kw = kw
                                        break
                                
                                if matched_kw:
                                    # 把命中的中文词传给解析器，让它去这个中文词附近找验证码
                                    code = parse_verification_code_with_context(body, matched_kw)
                                    if code:
                                        print(f"【💥 捕获验证码】邮箱: {email_account} | 平台: {platform} (触发词:{matched_kw}) | 码: {code}")
                                        code_storage[platform] = {"code": code, "email": email_account}
                                        
                            server.add_flags(uid, '\\Seen')
                        server.idle()
        except Exception as e:
            print(f"[❌ 异常] 邮箱 {email_account} 监听中断，原因: {e}。10秒后重试...")
            time.sleep(10)

# ----- API 接口部分保持完全不变 -----
@app.route('/api/get_code', methods=['GET'])
def get_code_api():
    platform = request.args.get('platform')
    mac = request.args.get('mac')
    
    if not mac:
        return jsonify({"status": "error", "message": "非法的设备！"})
    if platform not in PLATFORM_LIMITS:
        return jsonify({"status": "error", "message": "未知的 AI 平台！"})

    current_time = time.time()
    
    for email_acc in lock_storage:
        owners = lock_storage[email_acc][platform]["owners"]
        expired_macs = [m for m, t in owners.items() if current_time >= t]
        for m in expired_macs:
            del owners[m]

    latest_data = code_storage.get(platform)
    if not latest_data:
        return jsonify({"status": "error", "message": "未收到最新验证码，请先在 AI 平台点击发送！"})

    target_email = latest_data["email"]
    code = latest_data["code"]
    max_users_allowed = PLATFORM_LIMITS[platform]
    
    lock_info = lock_storage[target_email][platform]

    if mac not in lock_info["owners"] and len(lock_info["owners"]) >= max_users_allowed:
        earliest_expire = min(lock_info["owners"].values())
        remaining_minutes = int((earliest_expire - current_time) / 60) + 1
        return jsonify({
            "status": "error", 
            "message": f"当前账号 ({target_email}) 已有 {max_users_allowed} 人使用，名额已满！\n(请换一个没人用的公司邮箱账号发送验证码，\n或等待 {remaining_minutes} 分钟)。"
        })

    code_storage[platform] = None 
    lock_info["owners"][mac] = current_time + LOCK_DURATION
    
    return jsonify({"status": "success", "code": code})

if __name__ == "__main__":
    print("==================== 安全验证中心 (多语言中文雷达版) ====================")
    
    for acc in ACCOUNTS:
        t = threading.Thread(target=monitor_single_account, args=(acc["email"], acc["password"]))
        t.daemon = True
        t.start()
        time.sleep(1)
        
    app.run(host='0.0.0.0', port=5000)
