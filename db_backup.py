"""SQLite 数据库自动备份到 GitHub（防 Render 重新部署丢数据）"""
import os
import base64
import json
import urllib.request
import urllib.error
import threading
import time
import sqlite3
from models import DB_PATH

# GitHub 配置（从环境变量读取）
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "ll136897/kitchen-panel")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
DB_FILE_PATH = "kitchen.db"  # 仓库中的路径

# 备份锁（防止并发）
_backup_lock = threading.Lock()
_last_backup = 0  # 上次备份时间戳


def _github_api(method, path, data=None):
    """调用 GitHub API"""
    if not GITHUB_TOKEN:
        return None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DB_FILE_PATH}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # 文件不存在
        print(f"[backup] GitHub API 错误: {e.code} {e.reason}")
        return None
    except Exception as e:
        print(f"[backup] GitHub API 异常: {e}")
        return None


def backup_to_github():
    """把当前 kitchen.db 推到 GitHub"""
    global _last_backup
    if not GITHUB_TOKEN:
        return False
    if not os.path.exists(DB_PATH):
        return False

    if not _backup_lock.acquire(blocking=False):
        print("[backup] 已有备份任务在跑，跳过")
        return False
    try:
        # 读取数据库文件
        with open(DB_PATH, "rb") as f:
            content = base64.b64encode(f.read()).decode()

        # 查询现有文件的 sha（更新需要）
        info = _github_api("GET", "")
        sha = info.get("sha") if info else None

        data = {
            "message": "auto: backup kitchen.db",
            "content": content,
            "branch": GITHUB_BRANCH,
        }
        if sha:
            data["sha"] = sha

        result = _github_api("PUT", "", data)
        if result:
            _last_backup = time.time()
            print(f"[backup] 已备份到 GitHub (sha: {result.get('content',{}).get('sha','')[:8]})")
            return True
        return False
    except Exception as e:
        print(f"[backup] 备份失败: {e}")
        return False
    finally:
        _backup_lock.release()


def restore_from_github():
    """从 GitHub 拉取 kitchen.db 恢复"""
    if not GITHUB_TOKEN:
        print("[restore] 未设置 GITHUB_TOKEN，跳过")
        return False
    info = _github_api("GET", "")
    if not info:
        print("[restore] GitHub 上无备份文件，跳过")
        return False

    try:
        # 下载文件内容
        download_url = info.get("download_url")
        if download_url:
            req = urllib.request.Request(download_url)
            req.add_header("Authorization", f"token {GITHUB_TOKEN}")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
        else:
            # 用 base64 content
            data = base64.b64decode(info.get("content", ""))

        if len(data) < 100:
            print("[restore] 备份文件太小，可能无效，跳过")
            return False

        # 写入临时文件再替换
        tmp_path = DB_PATH + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(data)
        # 验证 SQLite 完整性
        try:
            conn = sqlite3.connect(tmp_path)
            conn.execute("PRAGMA integrity_check")
            conn.close()
        except Exception as e:
            print(f"[restore] 备份文件损坏: {e}")
            os.remove(tmp_path)
            return False

        # 替换
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        os.rename(tmp_path, DB_PATH)
        print(f"[restore] 已从 GitHub 恢复数据库 ({len(data)} bytes)")
        return True
    except Exception as e:
        print(f"[restore] 恢复失败: {e}")
        return False


def backup_async():
    """异步备份（不阻塞请求）"""
    thread = threading.Thread(target=backup_to_github, daemon=True)
    thread.start()


def start_auto_backup(interval=600):
    """启动定时备份（默认10分钟）"""
    def _loop():
        while True:
            time.sleep(interval)
            backup_to_github()
    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    print(f"[backup] 定时备份已启动，间隔 {interval}s")
