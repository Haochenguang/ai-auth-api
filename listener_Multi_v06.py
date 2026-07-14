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
    {"email": "SBF_AI_05@superbonfire.com", "password": "bsIZsi0QWdD72Aib"},
]

# ⬇️ 差异化并发核心：在这里自由配置每个平台的最高人数上限！
PLATFORM_LIMITS = {
    'seedance': 1,      # 极其严格，仅限1人独占
    'lovart': 2,        # 允许2人同时使用
    'midjourney': 2,    # 允许2人同时使用
    'chatgpt': 5,       # 允许5人同时使用
    'jimeng': 10,       # 允许10人同时使用
    'keling': 10        # 允许10人同时使用
}
# ============================================

# 根据配置表自动生成平台列表
TARGET_PLATFORMS = list(PLATFORM_LIMITS.keys())

# 记录最新验证码的临时仓库
code_storage = {platform: None for platform in TARGET_PLATFORMS}

# 独立计时锁核心
lock_storage = {platform: {"owners": {}} for platform in TARGET_PLATFORMS}
LOCK_DURATION = 30 * 60  # 每个人独立的锁定时间（30 分钟）

app = Flask(__name__)

def parse_verification_code(email_message):
    body = ""
    if email_message.is_multipart():
        for part in email_message.walk():
            if part.get_content_type() in ["text/plain", "text/html"]:
                payload = part.get_payload(decode=True)
                if payload: body += payload.decode(errors='ignore')
    else:
        payload = email_message.get_payload(decode=True)
        if payload: body = payload.decode(errors='ignore')

    match = re.search(r'\b\d{4,6}\b', body)
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
                            
                            for platform in TARGET_PLATFORMS:
                                if platform in sender:
                                    code = parse_verification_code(msg)
                                    if code:
                                        print(f"【💥 捕获验证码】邮箱: {email_account} | 平台: {platform} | 码: {code}")
                                        code_storage[platform] = code
                            server.add_flags(uid, '\\Seen')
                        server.idle()
        except Exception as e:
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
    lock_info = lock_storage.get(platform)
    
    # 动态获取当前请求平台的最大允许人数
    max_users_allowed = PLATFORM_LIMITS[platform]

    # 1. 🧹 自动清理到期名额
    expired_macs = []
    for stored_mac, expire_time in lock_info["owners"].items():
        if current_time >= expire_time:
            expired_macs.append(stored_mac)
            
    for expired_mac in expired_macs:
        del lock_info["owners"][expired_mac]
        print(f"[名额释放] 终端 {expired_mac} 的 {platform} 独占时间已结束，名额回收。")

    # 2. 🛑 核心拦截逻辑
    if mac not in lock_info["owners"] and len(lock_info["owners"]) >= max_users_allowed:
        earliest_expire = min(lock_info["owners"].values())
        remaining_minutes = int((earliest_expire - current_time) / 60) + 1
        
        print(f"[拦截记录] 拒绝终端 {mac}。{platform} 已满员 ({max_users_allowed}/{max_users_allowed})。")
        return jsonify({
            "status": "error", 
            "message": f"账号当前已有 {max_users_allowed} 人同时使用，名额已满！\n为防止互相顶号，暂不予下发。\n最快释放空位预计还需：{remaining_minutes} 分钟。"
        })

    # 3. 正常下发逻辑
    code = code_storage.get(platform)
    if code:
        code_storage[platform] = None
        lock_info["owners"][mac] = current_time + LOCK_DURATION
        current_occupancy = len(lock_info["owners"])
        
        print(f"[授权成功] {platform} 发给 {mac}。当前占用名额：{current_occupancy}/{max_users_allowed}。")
        return jsonify({"status": "success", "code": code})
    else:
        return jsonify({"status": "error", "message": "未收到最新验证码，请先在 AI 平台点击发送！"})

if __name__ == "__main__":
    print("==================== 安全验证中心 ====================")
    
    for acc in ACCOUNTS:
        t = threading.Thread(target=monitor_single_account, args=(acc["email"], acc["password"]))
        t.daemon = True
        t.start()
        time.sleep(1)
        
    print("\n[*] 正在启动本地 API 服务，端口 5000...")
    app.run(host='0.0.0.0', port=5000)
