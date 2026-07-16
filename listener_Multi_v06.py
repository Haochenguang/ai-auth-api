import time
import re
import email
from email.header import decode_header
import threading
import html  # ⭐ 新增：专门用于解析和卸妆网页邮件的官方利器
from imapclient import IMAPClient
from flask import Flask, request, jsonify

# ================= 🔧 配置区 =================
FEISHU_IMAP_SERVER = 'imap.feishu.cn'

ACCOUNTS = [
    {"email": "SBF_AI_01@superbonfire.com", "password": "3bo8gE496FeIloAm"},
    {"email": "SBF_AI_02@superbonfire.com", "password": "qjc6s8seOHInCqKo"},
    {"email": "SBF_AI_03@superbonfire.com", "password": "0guuq9iMlyMIW1lS"},
    {"email": "SBF_AI_04@superbonfire.com", "password": "zZlbbc43a5zUmsvQ"},
    {"email": "SBF_AI_05@superbonfire.com", "password": "bsIZsi0QWdD72Aib"},
]

PLATFORM_LIMITS = {
    #'seedance': 1,      
    'lovart': 2,        
    #'midjourney': 2,    
    'chatgpt': 5,       
    'jimeng': 10,       
    'keling': 10        
}

PLATFORM_KEYWORDS = {
    #'seedance': ['seedance'],
    'lovart': ['lovart'],
    #'midjourney': ['midjourney'],
    'chatgpt': ['chatgpt', 'openai'], 
    'jimeng': ['jimeng', '即梦'],
    'keling': ['keling', '可灵']
}
# ============================================

TARGET_PLATFORMS = list(PLATFORM_LIMITS.keys())
EMAIL_LIST = [acc["email"] for acc in ACCOUNTS]

code_storage = {platform: None for platform in TARGET_PLATFORMS}
lock_storage = {email_acc: {platform: {"owners": {}} for platform in TARGET_PLATFORMS} for email_acc in EMAIL_LIST}

LOCK_DURATION = 30 * 60  

app = Flask(__name__)

def parse_verification_code_with_context(text, keywords):
    """
    智能解析验证码：先卸妆，再搜索。完美免疫 HTML 邮件中的 CSS/色号 干扰！
    """
    # --- 1. 终极 HTML 卸妆术 ---
    # 删掉 CSS 和 JS 块，屏蔽所有诸如 #333333 这种隐藏颜色代码
    clean_text = re.sub(r'<style.*?>.*?</style>', ' ', text, flags=re.IGNORECASE|re.DOTALL)
    clean_text = re.sub(r'<script.*?>.*?</script>', ' ', clean_text, flags=re.IGNORECASE|re.DOTALL)
    
    # 扒掉所有 HTML 标签 (<...>)
    clean_text = re.sub(r'<[^>]+>', ' ', clean_text)
    
    # 将网页转义符（如 &nbsp;）翻译成真实文字
    clean_text = html.unescape(clean_text)
    
    # 把文本彻底展平（将多个换行和空格压缩成一个空格）
    clean_text = re.sub(r'\s+', ' ', clean_text)
    clean_text_lower = clean_text.lower()
    
    # --- 2. 匹配逻辑 ---
    for kw in keywords:
        match = re.search(rf"{kw}[^0-9]{{0,40}}?(?<!\d)(\d{{4,6}})(?!\d)", clean_text_lower)
        if match:
            return match.group(1)
            
    # --- 3. 强力净化区（排雷） ---
    filtered_text = re.sub(r'(?<!\d)106\d+(?!\d)', ' ', clean_text)               
    filtered_text = re.sub(r'(?i)uid\s*[:：]?\s*\d+', ' ', filtered_text)       
    filtered_text = re.sub(r'\d{4}-\d{2}-\d{2}', ' ', filtered_text)           
    filtered_text = re.sub(r'\d{2}:\d{2}(:\d{2})?', ' ', filtered_text)         
    # 顺手过滤掉邮件底部的版权年份声明 (如 © 2024, 2026)，防止误抓
    filtered_text = re.sub(r'(?i)(copyright|©)\s*\d{4}', ' ', filtered_text)

    # --- 4. 兜底寻找真正的验证码 ---
    match = re.search(r'(?<!\d)\d{4,6}(?!\d)', filtered_text)
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
                            
                            full_text = subject_str + " \n " + body
                            full_text_lower = full_text.lower()

                            for platform in TARGET_PLATFORMS:
                                keywords = PLATFORM_KEYWORDS.get(platform, [platform])
                                
                                platform_matched = False
                                for kw in keywords:
                                    if kw in sender or kw in full_text_lower:
                                        platform_matched = True
                                        break
                                
                                if platform_matched:
                                    code = parse_verification_code_with_context(full_text, keywords)
                                    if code:
                                        print(f"【💥 捕获验证码】邮箱: {email_account} | 平台: {platform} | 码: {code}")
                                        code_storage[platform] = {"code": code, "email": email_account}
                                        
                            server.add_flags(uid, '\\Seen')
                        server.idle()
        except Exception as e:
            print(f"[❌ 异常] 邮箱 {email_account} 监听中断，原因: {e}。10秒后重试...")
            time.sleep(10)

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
    current_occupancy = len(lock_info["owners"])
    
    return jsonify({"status": "success", "code": code})

if __name__ == "__main__":
    print("==================== 安全验证中心 (卸妆抗干扰版) ====================")
    for acc in ACCOUNTS:
        t = threading.Thread(target=monitor_single_account, args=(acc["email"], acc["password"]))
        t.daemon = True
        t.start()
        time.sleep(1)
    app.run(host='0.0.0.0', port=5000)
