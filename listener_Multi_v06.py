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
   # 'seedance': 1,      # 每个邮箱允许1人
    'lovart': 2,        # 每个邮箱允许2人
   # 'midjourney': 2,    # 每个邮箱允许2人
    'chatgpt': 5,       # 每个邮箱允许5人
    'jimeng': 10,       # 每个邮箱允许10人
    'keling': 10,       # 每个邮箱允许10人

   
}
# ============================================

TARGET_PLATFORMS = list(PLATFORM_LIMITS.keys())
EMAIL_LIST = [acc["email"] for acc in ACCOUNTS]

# 🧠 记忆升级：不仅存验证码，还要存这个码是【哪个邮箱】收到的
code_storage = {platform: None for platform in TARGET_PLATFORMS}

# 📒 账本升级：二维独立账本 [邮箱][平台]
lock_storage = {
    email_acc: {
        platform: {"owners": {}} for platform in TARGET_PLATFORMS
    } for email_acc in EMAIL_LIST
}

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
                                        # 记录验证码时，绑定来源邮箱
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
    
    # 1. 🧹 自动清理【所有邮箱】下到期的名额
    for email_acc in lock_storage:
        owners = lock_storage[email_acc][platform]["owners"]
        expired_macs = [m for m, t in owners.items() if current_time >= t]
        for m in expired_macs:
            del owners[m]
            print(f"[名额释放] 终端 {m} 在账号 {email_acc} 的 {platform} 独占已结束。")

    # 2. 检查是否有最新的验证码
    latest_data = code_storage.get(platform)
    if not latest_data:
        return jsonify({"status": "error", "message": "未收到最新验证码，请先在 AI 平台点击发送！"})

    # 解析当前验证码是属于哪个邮箱的
    target_email = latest_data["email"]
    code = latest_data["code"]
    max_users_allowed = PLATFORM_LIMITS[platform]
    
    # 定位到这个特定邮箱的账本
    lock_info = lock_storage[target_email][platform]

    # 3. 🛑 核心拦截逻辑（只针对当前这个邮箱）
    if mac not in lock_info["owners"] and len(lock_info["owners"]) >= max_users_allowed:
        earliest_expire = min(lock_info["owners"].values())
        remaining_minutes = int((earliest_expire - current_time) / 60) + 1
        
        print(f"[拦截记录] 拒绝终端 {mac}。{target_email} 的 {platform} 已满员。")
        return jsonify({
            "status": "error", 
            "message": f"当前账号 ({target_email}) 已有 {max_users_allowed} 人使用，名额已满！\n(请换一个没人用的公司邮箱账号发送验证码，\n或等待 {remaining_minutes} 分钟)。"
        })

    # 4. 正常下发逻辑
    code_storage[platform] = None  # 验证码用完即毁，防止重复提取
    lock_info["owners"][mac] = current_time + LOCK_DURATION
    current_occupancy = len(lock_info["owners"])
    
    print(f"[授权成功] {platform} 发给 {mac}。占用账号: {target_email} ({current_occupancy}/{max_users_allowed})。")
    return jsonify({"status": "success", "code": code})

if __name__ == "__main__":
    print("==================== 安全验证中心 (独立账号池版) ====================")
    
    for acc in ACCOUNTS:
        t = threading.Thread(target=monitor_single_account, args=(acc["email"], acc["password"]))
        t.daemon = True
        t.start()
        time.sleep(1)
        
    print("\n[*] 正在启动本地 API 服务，端口 5000...")
    app.run(host='0.0.0.0', port=5000)
