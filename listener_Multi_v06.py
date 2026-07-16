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

# ⬇️ 差异化并发核心：在这里自由配置【每个独立账号】的最高人数上限！
PLATFORM_LIMITS = {
    #'seedance': 1,      
    'lovart': 2,        
    #'midjourney': 2,    
    'chatgpt': 5,       
    'jimeng': 10,       
    'keling': 10        
}

# ⬇️ 🎯 中英文搜索雷达：添加所有的识别触发词
PLATFORM_KEYWORDS = {
    #'seedance': ['seedance'],
    'lovart': ['lovart'],
   # 'midjourney': ['midjourney'],
    'chatgpt': ['chatgpt', 'openai'], 
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
    智能解析验证码：修复中文与数字紧贴导致的边界识别失效问题
    """
    body_lower = body.lower()
    
    # 1. 尝试匹配关键字后面临近的 4-6 位数字
    match = re.search(rf"{keyword}[^0-9]{{0,40}}?(?<!\d)(\d{{4,6}})(?!\d)", body_lower)
    if match:
        return match.group(1)
        
    # 2. 如果没找到，进行深度净化，排除所有干扰数字
    clean_body = re.sub(r'(?<!\d)106\d+(?!\d)', '', body)               # 过滤长串服务号
    
    # ⭐【新增强力过滤】：彻底抹除类似 "UID: 10108"、"uid：12345" 等干扰项
    clean_body = re.sub(r'(?i)\buid\s*[:：]?\s*\d+\b', '', clean_body)
    
    clean_body = re.sub(r'\d{4}-\d{2}-\d{2}', '', clean_body)           # 过滤日期
    clean_body = re.sub(r'\d{2}:\d{2}(:\d{2})?', '', clean_body)         # 过滤时间 (含带秒的 16:35:35)
    
    # 3. 兜底寻找剩下文本中的 4-6 位数字
    match = re.search(r'(?<!\d)\d{4,6}(?!\d)', clean_body)
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
                                keywords = PLATFORM_KEYWORDS.get(platform, [platform])
                                is_forwarded = any(k in sender or k in subject_str or k in body_lower for k in ["forward", "sms", "短信", "转发", "phone"])
                                
                                matched_kw = None
                                for kw in keywords:
                                    if (kw in sender) or (is_forwarded and (kw in body_lower or kw in subject_str)):
                                        matched_kw = kw
                                        break
                                
                                if matched_kw:
                                    code = parse_verification_code_with_context(body, matched_kw)
                                    if code:
                                        print(f"【💥 捕获验证码】邮箱: {email_account} | 平台: {platform} (触发词:{matched_kw}) | 码: {code}")
                                        code_storage[platform] = {"code": code, "email": email_account}
                                        
                            server.add_flags(uid, '\\Seen')
                        server.idle()
        except Exception as e:
            print(f"[❌ 异常] 邮箱 {email_account} 监听中断，原因: {e}。10秒后重试...")
            time.sleep(10)

# ----- 核心桥接与独立计时防顶号 API 接口 -----
@app.route('/api/get_code', methods=['GET'])
def get_code_api():
    platform = request.args.get('platform')
    mac = request.args.get('mac')
    
    if not mac:
        return jsonify({"status": "error", "message": "非法的设备！"})
    if platform not in PLATFORM_LIMITS:
        return jsonify({"status": "error", "message": "未知的 AI 平台！"})

    current_time = time.time()
    
    # 1. 🧹 自动清理
    for email_acc in lock_storage:
        owners = lock_storage[email_acc][platform]["owners"]
        expired_macs = [m for m, t in owners.items() if current_time >= t]
        for m in expired_macs:
            del owners[m]
            print(f"[名额释放] 终端 {m} 在账号 {email_acc} 的 {platform} 独占已结束。")

    # 2. 检查验证码
    latest_data = code_storage.get(platform)
    if not latest_data:
        return jsonify({"status": "error", "message": "未收到最新验证码，请先在 AI 平台点击发送！"})

    target_email = latest_data["email"]
    code = latest_data["code"]
    max_users_allowed = PLATFORM_LIMITS[platform]
    
    lock_info = lock_storage[target_email][platform]

    # 3. 🛑 拦截满员
    if mac not in lock_info["owners"] and len(lock_info["owners"]) >= max_users_allowed:
        earliest_expire = min(lock_info["owners"].values())
        remaining_minutes = int((earliest_expire - current_time) / 60) + 1
        
        print(f"[拦截记录] 拒绝终端 {mac}。{target_email} 的 {platform} 已满员。")
        return jsonify({
            "status": "error", 
            "message": f"当前账号 ({target_email}) 已有 {max_users_allowed} 人使用，名额已满！\n(请换一个没人用的公司邮箱账号发送验证码，\n或等待 {remaining_minutes} 分钟)。"
        })

    # 4. 授权下发
    code_storage[platform] = None  
    lock_info["owners"][mac] = current_time + LOCK_DURATION
    current_occupancy = len(lock_info["owners"])
    
    print(f"[授权成功] {platform} 发给 {mac}。占用账号: {target_email} ({current_occupancy}/{max_users_allowed})。")
    return jsonify({"status": "success", "code": code})

if __name__ == "__main__":
    print("==================== 安全验证中心 (终极无敌完全避雷版) ====================")
    
    for acc in ACCOUNTS:
        t = threading.Thread(target=monitor_single_account, args=(acc["email"], acc["password"]))
        t.daemon = True
        t.start()
        time.sleep(1)
        
    print("\n[*] 正在启动本地 API 服务，端口 5000...")
    app.run(host='0.0.0.0', port=5000)
