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
_last_backup = 0   # 上次备份成功的时间戳
_last_ok = None    # 上次备份是否成功（None=还没跑过）
_last_write = 0    # 最后一次数据变更的时间（用来判断"有没有改动还没存下来"）
_dirty = False     # 备份推送期间是否又发生了写入（有就必须在跑完后补存一次）
_pending_timer = None
_pending_lock = threading.Lock()


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
    """把当前 kitchen.db 推到 GitHub（忠实备份本地现状，不回拉任何云端订单）。"""
    global _last_backup, _last_ok
    if not GITHUB_TOKEN:
        return False
    if not os.path.exists(DB_PATH):
        return False

    if not _backup_lock.acquire(blocking=False):
        # ⚠️ 以前这里是"直接跳过"——连续录入时，后几单就这么漏掉了（2026-10-11 丢单事故）
        # 现在改成：记下"还有改动没存"，等正在跑的那次结束后立刻补存一次
        print("[backup] 上一次备份还在跑 -> 已标记，跑完会立刻补存一次")
        schedule_backup(delay=5)
        return False
    try:
        with _pending_lock:
            _dirty = False
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
        _last_ok = bool(result)
        if result:
            _last_backup = time.time()
            print(f"[backup] 已备份到 GitHub (sha: {result.get('content',{}).get('sha','')[:8]})")
            return True
        print("[backup] 推送失败（GitHub 没有返回内容）")
        return False
    except Exception as e:
        _last_ok = False
        print(f"[backup] 备份失败: {e}")
        return False
    finally:
        _backup_lock.release()
        with _pending_lock:
            _again = _dirty
        if _again:
            print("[backup] 备份期间又有新改动 -> 补存一次")
            schedule_backup(delay=2)


def _local_has_data():
    """本地数据库是否已有订单数据（有就别用旧备份覆盖）"""
    if not os.path.exists(DB_PATH):
        return False
    try:
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        conn.close()
        return n > 0
    except Exception:
        return False


def restore_from_github():
    """从 GitHub 拉取 kitchen.db 恢复（本地已有订单数据时跳过，避免旧备份覆盖新数据）"""
    if not GITHUB_TOKEN:
        print("[restore] 未设置 GITHUB_TOKEN，跳过")
        return False
    if _local_has_data():
        print("[restore] 本地已有订单数据，跳过恢复（避免用旧备份覆盖新数据）")
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


def mark_write():
    """任何数据变更后调用：记下"有改动"并预约一次备份。"""
    global _last_write
    _last_write = time.time()
    schedule_backup()


def schedule_backup(delay=3):
    """预约备份：把短时间内的多次写入合并成一次推送（别每写一笔就打一次 GitHub）。
    如果上一次备份正在跑，跑完会自动补存一次 —— 保证最后一次改动一定存得下来。"""
    global _pending_timer, _dirty
    with _pending_lock:
        _dirty = True
        if _pending_timer is not None:
            return True                      # 已经排上了，不必重复排
        if not GITHUB_TOKEN:
            return False
        _pending_timer = threading.Timer(delay, _run_pending)
        _pending_timer.daemon = True
        _pending_timer.start()
        return True


def _run_pending():
    global _pending_timer
    with _pending_lock:
        _pending_timer = None
    try:
        backup_to_github()
    except Exception as e:
        print("[backup] 预约备份出错:", e)


def backup_status():
    """给页面显示用：有没有配 token、上次什么时候存的、现在有没有改动还没存下来。"""
    return {
        "configured": bool(GITHUB_TOKEN),
        "last_backup": _last_backup,
        "last_ok": _last_ok,
        "last_write": _last_write,
        "dirty": bool(_last_write and _last_write > _last_backup),
    }


def backup_async():
    """异步备份（不阻塞请求）"""
    schedule_backup(delay=1)


def start_auto_backup(interval=600):
    """启动定时备份（默认10分钟）"""
    def _loop():
        while True:
            time.sleep(interval)
            backup_to_github()
    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    print(f"[backup] 定时备份已启动，间隔 {interval}s")
