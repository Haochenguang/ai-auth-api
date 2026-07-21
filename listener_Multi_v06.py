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
from email.utils import parsedate_to_datetime 

# ================= 🔧 核心配置区 =================
ACCOUNTS = [
    {"email": "SBF_AI_01@superbonfire.com", "password": "3bo8gE496FeIloAm"},
    {"email": "SBF_AI_02@superbonfire.com", "password": "qjc6s8seOHInCqKo"},
    {"email": "SBF_AI_03@superbonfire.com", "password": "0guuq9iMlyMIW1lS"},
    {"email": "SBF_AI_04@superbonfire.com", "password": "zZlbbc43a5zUmsvQ"},
    {"email": "SBF_AI_05@superbonfire.com", "password": "bsIZsi0QWdD72Aib"},
]

PLATFORM_LIMITS = {
    'lovart': 2,        
    'chatgpt': 5,       
    'jimeng': 10,       
    'keling': 10        
}

PLATFORM_KEYWORDS = {
    'lovart': ['lovart'],
    'chatgpt': ['chatgpt', 'openai'], 
    'jimeng': ['jimeng', '即梦'],
    'keling': ['keling', '可灵']
}

ADMIN_SECRET = "SuperAdmin2026" 
DEFAULT_INVITE_CODE = "SBF2026"  

DATA_DIR = "data"                
if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
DB_FILE = os.path.join(DATA_DIR, "ai_users.db")

FEISHU_IMAP_SERVER = 'imap.feishu.cn'
TARGET_PLATFORMS = list(PLATFORM_LIMITS.keys())
EMAIL_LIST = [acc["email"] for acc in ACCOUNTS]

code_storage = {platform: None for platform in TARGET_PLATFORMS}
lock_storage = {email_acc: {platform: {"owners": {}} for platform in TARGET_PLATFORMS} for email_acc in EMAIL_LIST}
LOCK_DURATION = 30 * 60  

app = Flask(__name__)

def get_client_ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()

# ================= 🛡️ 数据库初始化 =================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            real_name TEXT NOT NULL DEFAULT '未命名',
            last_ip TEXT DEFAULT '未知',
            is_locked INTEGER DEFAULT 0,
            max_devices INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    for col, definition in [('real_name', "TEXT DEFAULT '未命名'"), ('last_ip', "TEXT DEFAULT '未知'"), ('is_locked', "INTEGER DEFAULT 0"), ('max_devices', "INTEGER DEFAULT 1")]:
        try: c.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError: pass 
        
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('invite_code', ?)", (DEFAULT_INVITE_CODE,))
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('check_machine_code', '1')")
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_devices (
            username TEXT,
            machine_code TEXT,
            UNIQUE(username, machine_code)
        )
    ''')
    
    conn.commit()
    conn.close()
    print("[*] 数据库初始化完成。")

init_db()

def parse_verification_code_with_context(text, keywords):
    clean_text = re.sub(r'<style.*?>.*?</style>', ' ', text, flags=re.IGNORECASE|re.DOTALL)
    clean_text = re.sub(r'<script.*?>.*?</script>', ' ', clean_text, flags=re.IGNORECASE|re.DOTALL)
    clean_text = re.sub(r'<[^>]+>', ' ', clean_text)
    clean_text = html.unescape(clean_text)
    clean_text = re.sub(r'\s+', ' ', clean_text)
    clean_text_lower = clean_text.lower()
    
    for kw in keywords:
        match = re.search(rf"{kw}.{{0,150}}?(?<!\d)(\d{{4,6}})(?!\d)", clean_text_lower)
        if match: return match.group(1)
        
    filtered_text = re.sub(r'(?<!\d)106\d+(?!\d)', ' ', clean_text)               
    filtered_text = re.sub(r'(?i)uid\s*[:：]?\s*\d+', ' ', filtered_text)       
    filtered_text = re.sub(r'\d{4}-\d{2}-\d{2}', ' ', filtered_text)           
    filtered_text = re.sub(r'\d{2}:\d{2}(:\d{2})?', ' ', filtered_text)         
    filtered_text = re.sub(r'(?i)(copyright|©)\s*\d{4}', ' ', filtered_text)
    match = re.search(r'(?<!\d)\d{4,6}(?!\d)', filtered_text)
    return match.group(0) if match else None

# ================= 📧 核心监听与去重提取引擎 =================
def monitor_single_account(email_account, app_password):
    processed_uids = set() 
    
    while True:
        try:
            with IMAPClient(FEISHU_IMAP_SERVER, ssl=True) as server:
                server.login(email_account, app_password)
                server.select_folder('INBOX')
                print(f"[✔] 邮箱 {email_account} 开始监听...")
                
                existing_messages = server.search('ALL')
                if existing_messages:
                    processed_uids.update(existing_messages[-20:]) 
                
                server.idle()
                
                while True:
                    responses = server.idle_check(timeout=1740) 
                    if responses:
                        server.idle_done() 
                        
                        messages = server.search('ALL')
                        recent_messages = messages[-5:] if len(messages) >= 5 else messages
                        
                        for uid in recent_messages:
                            if uid in processed_uids:
                                continue 
                            
                            processed_uids.add(uid)
                            
                            message_data = server.fetch([uid], 'RFC822')
                            for _, data in message_data.items():
                                msg = email.message_from_bytes(data[b'RFC822'])
                                
                                sender = msg.get("From", "").lower()
                                subject = msg.get("Subject", "")
                                try:
                                    decoded_subject = ""
                                    for part, encoding in decode_header(subject):
                                        if isinstance(part, bytes): decoded_subject += part.decode(encoding or 'utf-8', errors='ignore')
                                        else: decoded_subject += part
                                    subject_str = decoded_subject.lower()
                                except: subject_str = str(subject).lower()

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
                                
                                for platform in TARGET_PLATFORMS:
                                    keywords = PLATFORM_KEYWORDS.get(platform, [platform])
                                    if any(kw in sender or kw in full_text.lower() for kw in keywords):
                                        code = parse_verification_code_with_context(full_text, keywords)
                                        if code:
                                            print(f"【💥 捕获】{email_account} | {platform} | {code}")
                                            code_storage[platform] = {"code": code, "email": email_account}
                                            
                                            try:
                                                server.add_flags([uid], '\\Seen')
                                            except Exception as e:
                                                print(f"标记已读失败: {e}")
                                            
                        server.idle()
        except Exception as e: 
            print(f"[网络异常] IMAP 监听断开，10秒后重连... {e}")
            time.sleep(10)

# ================= 🔌 普通客户端 API =================
@app.route('/api/register', methods=['POST'])
def register_api():
    data = request.json
    username, password, real_name, invite_code = data.get('username'), data.get('password'), data.get('real_name'), data.get('invite_code')
    client_ip = get_client_ip()

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key='invite_code'")
    current_invite = c.fetchone()[0]

    if invite_code != current_invite: return jsonify({"status": "error", "message": "企业邀请码错误或已失效！"})
    if not username or not password or not real_name: return jsonify({"status": "error", "message": "信息填写不完整！"})

    hashed_password = generate_password_hash(password)
    try:
        c.execute("INSERT INTO users (username, password_hash, real_name, last_ip, max_devices) VALUES (?, ?, ?, ?, 1)", (username, hashed_password, real_name, client_ip))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "注册成功！"})
    except sqlite3.IntegrityError:
        return jsonify({"status": "error", "message": "用户名已存在！"})

@app.route('/api/login', methods=['POST'])
def login_api():
    data = request.json
    username, password = data.get('username'), data.get('password')
    machine_code = data.get('machine_code')
    client_ip = get_client_ip()

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT password_hash, real_name, is_locked, max_devices FROM users WHERE username=?", (username,))
    row = c.fetchone()
    
    if row and check_password_hash(row[0], password):
        if row[2] == 1: 
            return jsonify({"status": "error", "message": "账号已被管理员锁定，禁止登录！"})
        
        c.execute("SELECT value FROM settings WHERE key='check_machine_code'")
        check_machine_status = c.fetchone()
        check_machine_code = True if not check_machine_status else (check_machine_status[0] == '1')
        
        if check_machine_code:
            if not machine_code: return jsonify({"status": "error", "message": "环境异常：无法获取设备物理网卡地址！"})
            max_dev = row[3]
            c.execute("SELECT machine_code FROM user_devices WHERE username=?", (username,))
            bound_devices = [r[0] for r in c.fetchall()]
            
            if machine_code not in bound_devices:
                if len(bound_devices) >= max_dev:
                    conn.close()
                    return jsonify({"status": "error", "message": f"登录被拒绝：该账号绑定的电脑数量已达上限 ({max_dev}台)。\n请在原办公电脑使用，或联系管理员解绑！"})
                else:
                    c.execute("INSERT INTO user_devices (username, machine_code) VALUES (?, ?)", (username, machine_code))
        
        c.execute("UPDATE users SET last_ip=? WHERE username=?", (client_ip, username))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "username": username, "real_name": row[1]})
        
    conn.close()
    return jsonify({"status": "error", "message": "账号或密码错误！"})

@app.route('/api/get_code', methods=['GET'])
def get_code_api():
    platform, username, real_name = request.args.get('platform'), request.args.get('username'), request.args.get('real_name')
    if not username: return jsonify({"status": "error", "message": "未提供身份标识！"})
    if platform not in PLATFORM_LIMITS: return jsonify({"status": "error", "message": "未知的 AI 平台！"})

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT is_locked FROM users WHERE username=?", (username,))
    lock_status = c.fetchone()
    conn.close()
    if not lock_status or lock_status[0] == 1:
        return jsonify({"status": "error", "message": "您的账号已被管理员冻结，请求拦截！"})

    current_time = time.time()
    
    # ⚠️ 【移除旧逻辑】：不再在这里自动清空过期记录，保留给大屏展示和挤占判断

    latest_data = code_storage.get(platform)
    if not latest_data: return jsonify({"status": "empty", "message": "未收到最新验证码"})

    target_email, code = latest_data["email"], latest_data["code"]
    max_users = PLATFORM_LIMITS[platform]
    
    # ⭐ 新增：统计当前平台跨所有邮箱的活跃用户
    all_owners = []
    user_exists = False
    for e_acc in lock_storage:
        for u, d in list(lock_storage[e_acc][platform]["owners"].items()):
            if u == username:
                user_exists = True
            else:
                all_owners.append({"email": e_acc, "username": u, "data": d})

    # ⭐ 新增：挤占核心算法
    if not user_exists and len(all_owners) >= max_users:
        # 寻找处于“闲置状态”的记录
        expired_owners = [x for x in all_owners if current_time >= x["data"]["expire"]]
        if expired_owners:
            # 找到闲置时间最长的（排位最靠前的），直接踢出
            to_kick = min(expired_owners, key=lambda x: x["data"]["expire"])
            del lock_storage[to_kick["email"]][platform]["owners"][to_kick["username"]]
        else:
            # 如果名额满了，且所有名额都在锁定保护期，拒绝请求并弹窗
            locked_names = [x["data"]["real_name"] for x in all_owners]
            return jsonify({
                "status": "error", 
                "message": f"名额已满且均在锁定中！\n当前占用人：{', '.join(locked_names)}\n请线下联系使用人释放。"
            })

    code_storage[platform] = None  
    
    # 安全起见，如果同一个人跨了邮箱，先清除旧记录
    for e_acc in lock_storage:
        if username in lock_storage[e_acc][platform]["owners"]:
            del lock_storage[e_acc][platform]["owners"][username]
            
    # 正式分配名额并重置计时
    lock_storage[target_email][platform]["owners"][username] = {"expire": current_time + LOCK_DURATION, "real_name": real_name or username}
    return jsonify({"status": "success", "code": code})

@app.route('/api/release_code', methods=['POST'])
def release_code_api():
    """⭐ 新增：用户主动退出并释放名额的接口"""
    username = request.json.get('username')
    released = False
    for email_acc in lock_storage:
        for platform in lock_storage[email_acc]:
            if username in lock_storage[email_acc][platform]["owners"]:
                del lock_storage[email_acc][platform]["owners"][username]
                released = True
    if released:
        return jsonify({"status": "success", "message": "已成功退出，名额已释放！"})
    return jsonify({"status": "error", "message": "未找到您的占用记录。"})

@app.route('/api/get_status', methods=['GET'])
def get_status_api():
    status_report = []
    current_time = time.time()
    for email_acc, platforms in lock_storage.items():
        for plat, info in platforms.items():
            for user, data in info["owners"].items():
                # ⭐ 变更：不再过滤时间，过期记录也将被传回前端供其渲染闲置状态
                remaining = int((data["expire"] - current_time) / 60)
                status_report.append({
                    "email": email_acc, "platform": plat, 
                    "user": data["real_name"], "remaining_minutes": remaining
                })
    return jsonify({"status": "success", "data": status_report})


# ================= 🛡️ 超级管理员专属 API 接口区 =================
def check_admin(data):
    return data.get('admin_secret') == ADMIN_SECRET

@app.route('/api/admin/users', methods=['POST'])
def admin_get_users():
    if not check_admin(request.json): return jsonify({"status": "error", "message": "权限拒绝"}), 403
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT u.id, u.username, u.real_name, u.last_ip, u.is_locked, u.created_at, u.max_devices, 
               (SELECT COUNT(*) FROM user_devices WHERE username=u.username) as bound_count 
        FROM users u
    """)
    users = [{"id": r[0], "username": r[1], "real_name": r[2], "last_ip": r[3], "is_locked": bool(r[4]), "created_at": r[5], "max_devices": r[6], "bound_count": r[7]} for r in c.fetchall()]
    conn.close()
    return jsonify({"status": "success", "data": users})

@app.route('/api/admin/user/toggle_lock', methods=['POST'])
def admin_toggle_lock():
    if not check_admin(request.json): return jsonify({"status": "error", "message": "权限拒绝"}), 403
    username = request.json.get('username')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT is_locked FROM users WHERE username=?", (username,))
    current_status = c.fetchone()[0]
    new_status = 0 if current_status == 1 else 1
    c.execute("UPDATE users SET is_locked=? WHERE username=?", (new_status, username))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": f"用户 {username} 已{'锁定' if new_status else '解锁'}"})

@app.route('/api/admin/user/set_max_devices', methods=['POST'])
def admin_set_max_devices():
    if not check_admin(request.json): return jsonify({"status": "error", "message": "权限拒绝"}), 403
    username, max_devices = request.json.get('username'), request.json.get('max_devices')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET max_devices=? WHERE username=?", (max_devices, username))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": f"用户 {username} 设备上限已修改为 {max_devices} 台"})

@app.route('/api/admin/user/clear_devices', methods=['POST'])
def admin_clear_devices():
    if not check_admin(request.json): return jsonify({"status": "error", "message": "权限拒绝"}), 403
    username = request.json.get('username')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM user_devices WHERE username=?", (username,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": f"用户 {username} 的所有绑定电脑已解绑！下一次登录的电脑将自动绑定。"})

@app.route('/api/admin/user/delete', methods=['POST'])
def admin_delete_user():
    if not check_admin(request.json): return jsonify({"status": "error", "message": "权限拒绝"}), 403
    username = request.json.get('username')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE username=?", (username,))
    c.execute("DELETE FROM user_devices WHERE username=?", (username,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": f"用户 {username} 已永久删除"})

@app.route('/api/admin/user/add', methods=['POST'])
def admin_add_user():
    if not check_admin(request.json): return jsonify({"status": "error", "message": "权限拒绝"}), 403
    username, password, real_name = request.json.get('username'), request.json.get('password'), request.json.get('real_name')
    hashed_password = generate_password_hash(password)
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO users (username, password_hash, real_name, last_ip, max_devices) VALUES (?, ?, ?, ?, 1)", (username, hashed_password, real_name, "后台创建"))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "添加成功！"})
    except sqlite3.IntegrityError:
        return jsonify({"status": "error", "message": "用户名已存在！"})

@app.route('/api/admin/settings', methods=['POST'])
def admin_get_set_settings():
    if not check_admin(request.json): return jsonify({"status": "error", "message": "权限拒绝"}), 403
    
    new_invite = request.json.get('new_invite_code')
    new_check_machine = request.json.get('check_machine_code') 
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    msg = []
    if new_invite:
        c.execute("UPDATE settings SET value=? WHERE key='invite_code'", (new_invite,))
        msg.append(f"邀请码已修改为: {new_invite}")
    
    if new_check_machine is not None:
        c.execute("UPDATE settings SET value=? WHERE key='check_machine_code'", (str(new_check_machine),))
        status_text = "开启" if new_check_machine == '1' else "关闭"
        msg.append(f"设备码校验功能已: {status_text}")
        
    if msg: conn.commit()
    
    c.execute("SELECT value FROM settings WHERE key='invite_code'")
    current_invite = c.fetchone()
    current_invite = current_invite[0] if current_invite else DEFAULT_INVITE_CODE
    
    c.execute("SELECT value FROM settings WHERE key='check_machine_code'")
    current_machine = c.fetchone()
    current_machine = current_machine[0] if current_machine else '1'
    
    conn.close()
    return jsonify({"status": "success", "message": " | ".join(msg) if msg else "获取成功", "invite_code": current_invite, "check_machine_code": current_machine})

if __name__ == "__main__":
    for acc in ACCOUNTS:
        threading.Thread(target=monitor_single_account, args=(acc["email"], acc["password"]), daemon=True).start()
        time.sleep(1)
    app.run(host='0.0.0.0', port=5000)
