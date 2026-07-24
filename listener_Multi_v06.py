import os
import time
import re
import email
import requests
from email.header import decode_header
import threading
import html
import sqlite3
from imapclient import IMAPClient
from flask import Flask, request, jsonify, send_file, redirect
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
    'lovart': 2,        
    'chatgpt': 5,       
    'jimeng': 10,       
    'keling': 10,  
    'Pixmax': 10
}

PLATFORM_KEYWORDS = {
    'lovart': ['lovart'],
    'chatgpt': ['chatgpt', 'openai'], 
    'jimeng': ['jimeng', '即梦'],
    'keling': ['keling', '可灵', '快手科技'],
    'Pixmax': ['Pixmax', 'mail.pixmax.cn']
}

# 源账号：定义一个显示映射字典（可以将邮箱转换为你想要的任何文本）
DISPLAY_ACCOUNT_MAP = {
    'jimeng': '18611560059',
    'keling': '13810954214'
    # 其他平台如果没有定义，会默认显示原本的邮箱
}

# ================= 飞书开放平台配置 =================
# 🛡️ 安全优化：通过环境变量获取密钥，若未设置则读取默认测试值或报错
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aaea3c76e123dbd1")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "你的正式环境Secret")
FEISHU_REDIRECT_URI = os.environ.get("FEISHU_REDIRECT_URI", "https://你的真实公网域名/api/feishu/callback")

# 临时存放客户端扫码状态的内存池
feishu_login_tickets = {} 
# =================================================

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

def parse_verification_code_with_context(text, keywords, platform_name=""):
    # 🚨 清除所有不可见“零宽字符”
    clean_text = re.sub(r'[\u200b\u200c\u200d\uFEFF]', '', text)
    
    # 清理样式和脚本
    clean_text = re.sub(r'<style.*?>.*?</style>', ' ', clean_text, flags=re.IGNORECASE|re.DOTALL)
    clean_text = re.sub(r'<script.*?>.*?</script>', ' ', clean_text, flags=re.IGNORECASE|re.DOTALL)
    
    # 剥离所有 HTML 标签，被标签隔开的内容会变成空格
    plain_text = re.sub(r'<[^>]+>', ' ', clean_text)
    plain_text = html.unescape(plain_text)
    
    # ★ 核心方案：专门捕获被空格/换行打散的 6 位连续数字 (例如 "6  4  5  9  4  3")
    spaced_match = re.search(r'(?<!\d)(\d)\s+(\d)\s+(\d)\s+(\d)\s+(\d)\s+(\d)(?!\d)', plain_text)
    if spaced_match:
        return "".join(spaced_match.groups()) # 重新组合成 "645943"
        
    # 如果不是被打散的，走常规合并空格逻辑
    clean_text_normal = re.sub(r'\s+', ' ', plain_text).lower()

    # 常规关键词精准匹配
    for kw in keywords:
        # 放宽距离限制，从 150 提升到 250
        match = re.search(rf"{kw}.{{0,250}}?(?<!\d)(\d{{6}})(?!\d)", clean_text_normal)
        if match: return match.group(1)
        
    # 常规暴力提取（过滤干扰项）
    filtered_text = re.sub(r'(?<!\d)106\d+(?!\d)', ' ', clean_text_normal)               
    filtered_text = re.sub(r'(?i)uid\s*[:：]?\s*\d+', ' ', filtered_text)       
    filtered_text = re.sub(r'\d{4}-\d{2}-\d{2}', ' ', filtered_text)           
    filtered_text = re.sub(r'\d{2}:\d{2}(:\d{2})?', ' ', filtered_text)         
    filtered_text = re.sub(r'(?i)(copyright|©)\s*\d{4}', ' ', filtered_text)
    
    match = re.search(r'(?<!\d)(\d{6})(?!\d)', filtered_text)
    return match.group(1) if match else None

# ================= 📧 核心监听与去重提取引擎 =================
def monitor_single_account(email_account, app_password):
    processed_uids = set() 
    
    while True:
        try:
            with IMAPClient(FEISHU_IMAP_SERVER, ssl=True) as server:
                server.login(email_account, app_password)
                server.select_folder('INBOX')
                print(f"[✔] 邮箱 {email_account} 开始主动轮询监听 (抗掉线模式)...")
                
                existing_messages = server.search('ALL')
                if existing_messages:
                    processed_uids.update(existing_messages[-20:]) 
                
                # 🚀 彻底抛弃不稳定的 server.idle()，改为死循环主动搜索
                while True:
                    time.sleep(3)  # 每 3 秒拉取一次，防止被飞书判定为攻击
                    
                    messages = server.search('ALL')
                    if not messages:
                        continue
                        
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
                                    code = parse_verification_code_with_context(full_text, keywords, platform_name=platform)
                                    if code:
                                        print(f"【💥 捕获】{email_account} | {platform} | {code}")
                                        code_storage[platform] = {"code": code, "email": email_account}
                                        
                                        try:
                                            server.add_flags([uid], '\\Seen')
                                        except Exception as e:
                                            print(f"标记已读失败: {e}")
                                        
        except Exception as e: 
            print(f"[网络异常] IMAP 轮询断开，5秒后重连... {e}")
            time.sleep(5)

# --- 飞书登录 API 1：唤起授权 ---
@app.route('/api/feishu/login')
def feishu_login():
    ticket = request.args.get('ticket')
    if ticket:
        feishu_login_tickets[ticket] = {"status": "pending"}
    # 将 ticket 作为 state 参数传递给飞书，飞书会在回调时原封不动带回来
    auth_url = f"https://open.feishu.cn/open-apis/authen/v1/index?redirect_uri={FEISHU_REDIRECT_URI}&app_id={FEISHU_APP_ID}&state={ticket}"
    return redirect(auth_url)

# --- 飞书登录 API 2：扫码后的回调处理 ---
@app.route('/api/feishu/callback')
def feishu_callback():
    code = request.args.get('code')
    ticket = request.args.get('state') 
    
    if not code or not ticket: return "参数错误", 400

    try:
        # 1. 获取 app_access_token
        app_token_res = requests.post("https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal", 
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).json()
        app_token = app_token_res.get("tenant_access_token")

        # 2. 获取 user_access_token (代表该用户的身份凭证)
        user_token_res = requests.post("https://open.feishu.cn/open-apis/authen/v1/oidc/access_token",
            headers={"Authorization": f"Bearer {app_token}"},
            json={"grant_type": "authorization_code", "code": code}).json()
        user_token = user_token_res.get("data", {}).get("access_token")

        # 3. 获取用户基本信息 (需要 contact:user.base:readonly 权限)
        user_info_res = requests.get("https://open.feishu.cn/open-apis/authen/v1/user_info",
            headers={"Authorization": f"Bearer {user_token}"}).json()
        user_info = user_info_res.get("data", {})
        
        open_id = user_info.get("open_id")
        
        # 提取中文名和英文名，并将它们合并以匹配飞书客户端的显示
        base_name = user_info.get("name", "")
        en_name = user_info.get("en_name", "")
        
        # 加一个防重复判断：如果英文名存在，且和中文名不一样，才拼在一起
        if en_name and en_name != base_name:
            display_name = f"{base_name}{en_name}"
        else:
            display_name = base_name

        if open_id and display_name:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT id FROM users WHERE username=?", (open_id,))
            if not c.fetchone():
                # 首次扫码，自动注册账号
                c.execute("INSERT INTO users (username, password_hash, real_name, last_ip, max_devices) VALUES (?, ?, ?, ?, 1)",
                          (open_id, "FEISHU_OAUTH_DUMMY", display_name, "feishu_web"))
            else:
                # 非首次扫码，强制更新为飞书最新的名字，保持实时同步
                c.execute("UPDATE users SET real_name=? WHERE username=?", (display_name, open_id))
            conn.commit()
            conn.close()

            # 将同步后的最新名字传给客户端
            feishu_login_tickets[ticket] = {"status": "success", "username": open_id, "real_name": display_name}
            
            # ================== 自动关闭网页的 HTML ==================
            return f"""
            <div style="text-align:center; margin-top:150px; font-family:sans-serif;">
                <h1 style="color:#00E676; font-size: 40px;">✔ 授权成功</h1>
                <h2 style="color:#333;">你好，{display_name}</h2>
                <p style="color:#666;" id="close-text">身份已验证，页面将在 3 秒后自动关闭...</p>
            </div>
            <script>
                // 3秒后尝试自动关闭窗口
                setTimeout(function() {{
                    window.close();
                    // 兜底提示：部分浏览器可能会安全拦截自动关闭，给出手动关闭提示
                    document.getElementById('close-text').innerText = "身份已验证，您可以手动关闭本网页返回客户端";
                }}, 3000);
                
                // 倒计时数字显示逻辑
                let count = 3;
                setInterval(function() {{
                    count--;
                    if(count > 0) {{
                        document.getElementById('close-text').innerText = "身份已验证，页面将在 " + count + " 秒后自动关闭...";
                    }}
                }}, 1000);
            </script>
            """
        else:
            return "获取飞书用户信息失败", 500
    except Exception as e:
        return f"授权发生异常: {str(e)}", 500

# --- 飞书登录 API 3：客户端轮询接口 (带设备码安全校验) ---
@app.route('/api/feishu/check')
def feishu_check():
    ticket = request.args.get('ticket')
    machine_code = request.args.get('machine_code')
    client_ip = get_client_ip()

    ticket_data = feishu_login_tickets.get(ticket)
    if not ticket_data: return jsonify({"status": "pending"})
    
    if ticket_data["status"] == "success":
        username = ticket_data["username"]
        real_name = ticket_data["real_name"]

        # 执行设备校验 (复用原有的硬件绑定逻辑)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT is_locked, max_devices FROM users WHERE username=?", (username,))
        row = c.fetchone()
        
        if row:
            if row[0] == 1: 
                conn.close()
                return jsonify({"status": "error", "message": "账号已被管理员锁定，禁止登录！"})
            
            c.execute("SELECT value FROM settings WHERE key='check_machine_code'")
            check_machine_status = c.fetchone()
            check_machine_code = True if not check_machine_status else (check_machine_status[0] == '1')
            
            if check_machine_code:
                if not machine_code: 
                    conn.close()
                    return jsonify({"status": "error", "message": "无法获取设备网卡地址！"})
                max_dev = row[1]
                c.execute("SELECT machine_code FROM user_devices WHERE username=?", (username,))
                bound_devices = [r[0] for r in c.fetchall()]
                
                if machine_code not in bound_devices:
                    if len(bound_devices) >= max_dev:
                        conn.close()
                        return jsonify({"status": "error", "message": f"设备已达上限 ({max_dev}台)。"})
                    else:
                        c.execute("INSERT INTO user_devices (username, machine_code) VALUES (?, ?)", (username, machine_code))
            
            c.execute("UPDATE users SET last_ip=? WHERE username=?", (client_ip, username))
            conn.commit()
        conn.close()

        # 消费掉这个 ticket，防止重复请求
        del feishu_login_tickets[ticket]
        return jsonify({"status": "success", "username": username, "real_name": real_name})
    
    return jsonify({"status": "pending"})

# ================= 🔌 普通功能 API =================
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
    
    latest_data = code_storage.get(platform)
    if not latest_data: return jsonify({"status": "empty", "message": "未收到最新验证码"})

    target_email, code = latest_data["email"], latest_data["code"]
    max_users = PLATFORM_LIMITS[platform]
    lock_info = lock_storage[target_email][platform]

    if username not in lock_info["owners"] and len(lock_info["owners"]) >= max_users:
        # ✅ 检查是否有可挤占（已过期）的名额
        expired_users = [(u, d) for u, d in lock_info["owners"].items() if current_time >= d["expire"]]
        if expired_users:
            # 找到最早过期的用户，将其踢出
            oldest_user = min(expired_users, key=lambda x: x[1]["expire"])[0]
            del lock_info["owners"][oldest_user]
        else:
            # 没有任何过期名额，全在活跃锁定中
            active_names = [d["real_name"] for d in lock_info["owners"].values()]
            return jsonify({"status": "error", "message": f"({target_email}) 已满员！\n请联系目前使用人: {', '.join(active_names)}"})

    code_storage[platform] = None  
    lock_info["owners"][username] = {"expire": current_time + LOCK_DURATION, "real_name": real_name or username}
    return jsonify({"status": "success", "code": code})

@app.route('/api/get_status', methods=['GET'])
def get_status_api():
    status_report = []
    current_time = time.time()
    for email_acc, platforms in lock_storage.items():
        for plat, info in platforms.items():
            
            # ★ 在这里进行替换：如果该平台在字典中，就用字典里的值，否则用真实邮箱
            display_email = DISPLAY_ACCOUNT_MAP.get(plat, email_acc)
            
            for user, data in info["owners"].items():
                is_expired = current_time >= data["expire"]
                remaining = 0 if is_expired else int((data["expire"] - current_time) / 60) + 1
                status_report.append({
                    "email": display_email, 
                    "platform": plat, 
                    "user": data["real_name"], 
                    "username": user, # 新增用于精准踢人
                    "remaining_minutes": remaining,
                    "is_bumpable": is_expired # 新增状态标记
                })
    return jsonify({"status": "success", "data": status_report})

@app.route('/api/release_lock', methods=['POST'])
def release_lock_api():
    # 用户自己主动退出的接口
    username = request.json.get('username')
    for email_acc, platforms in lock_storage.items():
        for plat, info in platforms.items():
            if username in info["owners"]:
                del info["owners"][username]
    return jsonify({"status": "success", "message": "已主动释放名额"})


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
    # 保留管理员端后台强制制创建测试账号的功能
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

@app.route('/api/admin/kick_session', methods=['POST'])
def admin_kick_session():
    # 管理员踢人的接口
    if not check_admin(request.json): return jsonify({"status": "error", "message": "权限拒绝"}), 403
    username = request.json.get('username')
    for email_acc, platforms in lock_storage.items():
        for plat, info in platforms.items():
            if username in info["owners"]:
                del info["owners"][username]
    return jsonify({"status": "success", "message": f"已将 {username} 从监控会话中踢出！"})    

if __name__ == "__main__":
    for acc in ACCOUNTS:
        threading.Thread(target=monitor_single_account, args=(acc["email"], acc["password"]), daemon=True).start()
        time.sleep(1)
    app.run(host='0.0.0.0', port=5000)
