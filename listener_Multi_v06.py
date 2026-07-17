import os
import time
import re
import email
from email.header import decode_header
import threading
import html
import sqlite3
from imapclient import IMAPClient
from flask import Flask, request, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash

# ================= 🔧 核心配置区 =================
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

COMPANY_INVITE_CODE = "SBF2026"  
DATA_DIR = "data"                
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)
DB_FILE = os.path.join(DATA_DIR, "ai_users.db")

FEISHU_IMAP_SERVER = 'imap.feishu.cn'
TARGET_PLATFORMS = list(PLATFORM_LIMITS.keys())
EMAIL_LIST = [acc["email"] for acc in ACCOUNTS]

code_storage = {platform: None for platform in TARGET_PLATFORMS}
# 锁存储结构升级：保存字典包含过期时间和真实姓名
lock_storage = {email_acc: {platform: {"owners": {}} for platform in TARGET_PLATFORMS} for email_acc in EMAIL_LIST}
LOCK_DURATION = 30 * 60  

app = Flask(__name__)

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            real_name TEXT NOT NULL DEFAULT '未命名',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 兼容老数据，尝试新增 real_name 字段
    try:
        c.execute("ALTER TABLE users ADD COLUMN real_name TEXT DEFAULT '未命名'")
    except sqlite3.OperationalError:
        pass # 如果字段已存在，就会忽略报错
    conn.commit()
    conn.close()
    print("[*] 企业用户数据库 (SQLite3) 挂载并同步完成。")

init_db()

def parse_verification_code_with_context(text, keywords):
    clean_text = re.sub(r'<style.*?>.*?</style>', ' ', text, flags=re.IGNORECASE|re.DOTALL)
    clean_text = re.sub(r'<script.*?>.*?</script>', ' ', clean_text, flags=re.IGNORECASE|re.DOTALL)
    clean_text = re.sub(r'<[^>]+>', ' ', clean_text)
    clean_text = html.unescape(clean_text)
    clean_text = re.sub(r'\s+', ' ', clean_text)
    clean_text_lower = clean_text.lower()
    
    for kw in keywords:
        match = re.search(rf"{kw}[^0-9]{{0,40}}?(?<!\d)(\d{{4,6}})(?!\d)", clean_text_lower)
        if match:
            return match.group(1)
            
    filtered_text = re.sub(r'(?<!\d)106\d+(?!\d)', ' ', clean_text)               
    filtered_text = re.sub(r'(?i)uid\s*[:：]?\s*\d+', ' ', filtered_text)       
    filtered_text = re.sub(r'\d{4}-\d{2}-\d{2}', ' ', filtered_text)           
    filtered_text = re.sub(r'\d{2}:\d{2}(:\d{2})?', ' ', filtered_text)         
    filtered_text = re.sub(r'(?i)(copyright|©)\s*\d{4}', ' ', filtered_text)

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
            print(f"[❌ 异常] {email_account} 断线重连中... ({e})")
            time.sleep(10)

@app.route('/api/register', methods=['POST'])
def register_api():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    real_name = data.get('real_name')  # 新增真实姓名
    invite_code = data.get('invite_code')

    if invite_code != COMPANY_INVITE_CODE:
        return jsonify({"status": "error", "message": "企业邀请码错误！"})
    if not username or not password or not real_name:
        return jsonify({"status": "error", "message": "信息填写不完整！"})

    hashed_password = generate_password_hash(password)
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO users (username, password_hash, real_name) VALUES (?, ?, ?)", (username, hashed_password, real_name))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "注册成功！"})
    except sqlite3.IntegrityError:
        return jsonify({"status": "error", "message": "用户名已存在！"})

@app.route('/api/login', methods=['POST'])
def login_api():
    data = request.json
    username, password = data.get('username'), data.get('password')

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT password_hash, real_name FROM users WHERE username=?", (username,))
    row = c.fetchone()
    conn.close()

    if row and check_password_hash(row[0], password):
        return jsonify({"status": "success", "username": username, "real_name": row[1]})
    return jsonify({"status": "error", "message": "账号或密码错误！"})

@app.route('/api/get_code', methods=['GET'])
def get_code_api():
    platform = request.args.get('platform')
    username = request.args.get('username')
    real_name = request.args.get('real_name', username)
    
    if not username:
        return jsonify({"status": "error", "message": "未提供用户身份标识！"})
    if platform not in PLATFORM_LIMITS:
        return jsonify({"status": "error", "message": "未知的 AI 平台！"})

    current_time = time.time()
    for email_acc in lock_storage:
        owners = lock_storage[email_acc][platform]["owners"]
        # 数据结构变了，循环判断逻辑也要变
        expired_users = [u for u, d in owners.items() if current_time >= d["expire"]]
        for u in expired_users: del owners[u]

    latest_data = code_storage.get(platform)
    if not latest_data:
        return jsonify({"status": "error", "message": "未收到最新验证码，请先在 AI 平台点击发送！"})

    target_email, code = latest_data["email"], latest_data["code"]
    max_users = PLATFORM_LIMITS[platform]
    lock_info = lock_storage[target_email][platform]

    if username not in lock_info["owners"] and len(lock_info["owners"]) >= max_users:
        earliest_expire = min([d["expire"] for d in lock_info["owners"].values()])
        remaining = int((earliest_expire - current_time) / 60) + 1
        return jsonify({
            "status": "error", 
            "message": f"({target_email}) 已满员！请换邮箱或等待 {remaining} 分钟。"
        })

    code_storage[platform] = None  
    # 记录时保存真实姓名
    lock_info["owners"][username] = {"expire": current_time + LOCK_DURATION, "real_name": real_name}
    return jsonify({"status": "success", "code": code})

@app.route('/api/get_status', methods=['GET'])
def get_status_api():
    status_report = []
    current_time = time.time()
    
    for email_acc, platforms in lock_storage.items():
        for plat, info in platforms.items():
            for user, data in info["owners"].items():
                if current_time < data["expire"]:
                    remaining = int((data["expire"] - current_time) / 60)
                    status_report.append({
                        "email": email_acc, "platform": plat, 
                        "user": data["real_name"], "remaining_minutes": remaining
                    })
                    
    return jsonify({"status": "success", "data": status_report})

@app.route('/api/download_db', methods=['GET'])
def download_db_api():
    secret = request.args.get('secret')
    if secret != COMPANY_INVITE_CODE:
        return jsonify({"status": "error", "message": "无权访问！"}), 403

    db_path = os.path.abspath(DB_FILE)
    if os.path.exists(db_path):
        return send_file(db_path, as_attachment=True, download_name="ai_users_backup.db")
    else:
        return jsonify({"status": "error", "message": "数据库文件还未生成！"}), 404

if __name__ == "__main__":
    print("==================== SUPERBONFIRE AI AUTH CENTER ====================")
    for acc in ACCOUNTS:
        t = threading.Thread(target=monitor_single_account, args=(acc["email"], acc["password"]))
        t.daemon = True
        t.start()
        time.sleep(1)
    app.run(host='0.0.0.0', port=5000)
