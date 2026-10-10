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
    """把当前 kitchen.db 推到 GitHub。

    推之前先「合并」：把远端备份里有、本地没有的订单并进本地（只补不覆盖），
    这样"两处同时备份互相覆盖"时也**丢不了单**——这正是同一仓库挂多个实例时的根治手段。
    """
    global _last_backup
    if not GITHUB_TOKEN:
        return False
    if not os.path.exists(DB_PATH):
        return False

    # 先合并（补回被覆盖掉的单），再推；失败不阻断推送
    try:
        _n = merge_remote_into_local()
        if _n and _n > 0:
            _last_merge_added = _n
    except Exception as e:
        print("[backup] 合并步骤跳过：%s" % e)

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


# 与订单关联的子表（都带 order_id），合并时一并补齐
_ORDER_CHILD_TABLES = ("order_packages", "order_costs", "tool_loans", "order_dishes", "prep_checklist")
_last_merge_added = 0


def _download_remote_bytes():
    """下载 GitHub 上当前的 kitchen.db 字节；失败返回 None。"""
    if not GITHUB_TOKEN:
        return None
    info = _github_api("GET", "")
    if not info:
        return None
    try:
        url = info.get("download_url")
        if url:
            rq = urllib.request.Request(url)
            rq.add_header("Authorization", f"token {GITHUB_TOKEN}")
            with urllib.request.urlopen(rq, timeout=30) as r:
                return r.read()
        return base64.b64decode(info.get("content", ""))
    except Exception as e:
        print(f"[merge] 下载远端库失败: {e}")
        return None


def _table_cols(conn, table):
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    except Exception:
        return []


def merge_remote_into_local():
    """把"远端备份里有、本地没有"的订单（及其子表）并入本地库 —— **只补不覆盖**。

    返回并入的订单数；失败返回 -1。这是"备份不再互相覆盖丢单"的核心：
    不做"谁少谁不许推"的一刀切，而是"不丢内容"。
    """
    global _last_merge_added
    data = _download_remote_bytes()
    if not data or len(data) < 100:
        return -1
    tmp = DB_PATH + ".remote.db"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
    except Exception as e:
        print("[merge] 写临时文件失败：%s" % e)
        return -1
    added = 0
    try:
        lc = sqlite3.connect(DB_PATH)
        lc.row_factory = sqlite3.Row
        rc = sqlite3.connect(tmp)
        rc.row_factory = sqlite3.Row
        l_ids = set(r[0] for r in lc.execute("SELECT id FROM orders"))
        r_ids = set(r[0] for r in rc.execute("SELECT id FROM orders"))
        missing = sorted(r_ids - l_ids)
        if missing:
            ocols = _table_cols(rc, "orders")
            for oid in missing:
                row = rc.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
                if not row:
                    continue
                cols = [c for c in ocols if c in row.keys()]
                lc.execute("INSERT OR IGNORE INTO orders (%s) VALUES (%s)" % (
                    ",".join(cols), ",".join("?" * len(cols))), [row[c] for c in cols])
                for t in _ORDER_CHILD_TABLES:
                    tcols = _table_cols(rc, t)
                    if "order_id" not in tcols:
                        continue
                    lcols = _table_cols(lc, t)
                    keep = [c for c in tcols if c in lcols]
                    for cr in rc.execute("SELECT * FROM %s WHERE order_id=?" % t, (oid,)):
                        lc.execute("INSERT INTO %s (%s) VALUES (%s)" % (
                            t, ",".join(keep), ",".join("?" * len(keep))), [cr[c] for c in keep])
                added += 1
            lc.commit()
        lc.close()
        rc.close()
    except Exception as e:
        print("[merge] 合并失败：%s" % e)
        return -1
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    if added:
        _last_merge_added = added
        print("[merge] 已从远端备份并入 %d 单（只补不覆盖）" % added)
    return added


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
