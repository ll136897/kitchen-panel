"""云端自保活 —— 让 Render 免费实例在营业时段不进入休眠。

为什么要做这个（背景）：
1. Render 免费实例「15 分钟没有入站请求」就休眠，唤醒要 30-60 秒。
   备餐时点开别的套餐就卡在等待，系统在关键时刻帮不上忙。
2. 免费版**没有"关闭休眠"的开关**，只能靠"定时打请求"保持唤醒。
3. 之前只依赖 GitHub Actions 的 cron，但 GitHub 官方明确：
   「schedule 事件在高负载时可能延迟，**最坏情况下可能不运行**」——
   实测提交后近 2 小时一次没跑。所以不能把可靠性押在它身上。

本模块的做法：
  服务**自己**定时请求**自己的公网地址**（/api/ping）。
  这个请求会经过 Render 的入口，对 Render 而言就是一次真实的**入站请求**，
  自然就把"15 分钟空闲计时"重置了。
  → 不依赖 GitHub、不依赖用户的手机、不需要任何第三方服务。

省额度：
  免费额度 750 实例小时/月，而一个月 720-744 小时 → 24 小时保活会吃光。
  所以**只在营业时段保活**（默认北京时间 08:00-24:00 ≈ 480 小时/月，留足余量），
  凌晨让它正常休眠（那时也没人用）。
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

SETTINGS_KEY = "keepalive_config"

# 默认配置
DEFAULT_CFG = {
    "enabled": True,          # 是否开启云端自保活
    "start_hour": 8,          # 营业开始（北京时间，含）
    "end_hour": 24,           # 营业结束（北京时间，不含；24 = 到午夜）
    "interval_min": 8,        # 心跳间隔（分钟）。要 < 15（Render 的空闲阈值），留余量
    "url": "",                # 留空则自动用 RENDER_EXTERNAL_URL 或下面的兜底地址
}

FALLBACK_URL = "https://kitchen-panel.onrender.com"
BJ = timezone(timedelta(hours=8))

_lock = threading.Lock()
_state = {
    "boot_ts": time.time(),     # 进程启动时间（用来判断"有没有睡过"：睡过就会重启进程，这个时间会变新）
    "last_ts": 0.0,             # 上次心跳时间（用于限速）
    "count": 0,                 # 累计成功心跳次数
    "last": None,               # 上次结果 {at, ok, http, ms, target, err}
    "started": False,
    "stop": False,
}


def _bj_now():
    return datetime.now(BJ)


def load_cfg():
    """读配置（数据库里存的优先，没有就用默认值）"""
    cfg = dict(DEFAULT_CFG)
    db = None
    try:
        import models
        db = models.get_db()
        row = db.execute("SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
        if row and row["value"]:
            cfg.update(json.loads(row["value"]))
    except Exception:
        pass
    finally:
        # 这个函数每 60 秒被后台线程调一次，**必须关连接**，否则几小时后会耗尽文件句柄
        try:
            if db is not None:
                db.close()
        except Exception:
            pass
    if not cfg.get("url"):
        cfg["url"] = (os.environ.get("RENDER_EXTERNAL_URL") or FALLBACK_URL).rstrip("/")
    return cfg


def save_cfg(patch):
    """局部更新配置（只覆盖传进来的字段）"""
    cfg = load_cfg()
    for k in ("enabled", "start_hour", "end_hour", "interval_min", "url"):
        if k in patch and patch[k] is not None and patch[k] != "":
            cfg[k] = patch[k]
    if "enabled" in patch:
        cfg["enabled"] = bool(patch["enabled"]) if not isinstance(patch["enabled"], str) \
            else patch["enabled"].lower() in ("1", "true", "yes", "on")
    for k in ("start_hour", "end_hour", "interval_min"):
        try:
            cfg[k] = int(float(cfg[k]))
        except Exception:
            cfg[k] = DEFAULT_CFG[k]
    cfg["start_hour"] = max(0, min(23, cfg["start_hour"]))
    cfg["end_hour"] = max(0, min(24, cfg["end_hour"]))
    cfg["interval_min"] = max(2, min(14, cfg["interval_min"]))
    try:
        import models
        db = models.get_db()
        try:
            db.execute(
                "INSERT INTO settings (key, value, note) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (SETTINGS_KEY, json.dumps(cfg, ensure_ascii=False), "云端自保活配置"))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass
    return cfg


def in_window(cfg=None, now=None):
    """当前是否在营业时段（北京时间）"""
    cfg = cfg or load_cfg()
    h = (now or _bj_now()).hour
    s, e = int(cfg["start_hour"]), int(cfg["end_hour"])
    if s == e:
        return False
    if s < e:
        return s <= h < e
    return h >= s or h < e      # 跨夜（例如 20 点 → 次日 2 点）


def ping_once(url=None, timeout=100):
    """打一次自己的 /api/ping。返回结果字典。"""
    cfg = load_cfg()
    target = (url or cfg["url"]).rstrip("/") + "/api/ping"
    t0 = time.time()
    res = {"at": _bj_now().strftime("%Y-%m-%d %H:%M:%S"), "target": target,
           "ok": False, "http": 0, "ms": 0, "err": ""}
    try:
        req = urllib.request.Request(target, headers={"User-Agent": "kitchen-panel-keepalive"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res["http"] = r.status
            res["ok"] = (r.status == 200)
    except urllib.error.HTTPError as e:
        res["http"] = e.code
        res["err"] = "HTTP %s" % e.code
    except Exception as e:
        res["err"] = str(e)[:120]
    res["ms"] = int((time.time() - t0) * 1000)
    return res


def _loop():
    """后台循环：每分钟看一次，到点/跨窗口就心跳"""
    while not _state["stop"]:
        try:
            cfg = load_cfg()
            if cfg["enabled"] and in_window(cfg):
                due = cfg["interval_min"] * 60
                if time.time() - _state["last_ts"] >= due:
                    res = ping_once(cfg["url"])
                    with _lock:
                        _state["last"] = res
                        _state["last_ts"] = time.time()
                        if res["ok"]:
                            _state["count"] += 1
        except Exception as e:
            with _lock:
                _state["last"] = {"at": _bj_now().strftime("%Y-%m-%d %H:%M:%S"),
                                  "ok": False, "err": str(e)[:120], "ms": 0, "http": 0,
                                  "target": ""}
                _state["last_ts"] = time.time()
        time.sleep(60)


def start_self_keepalive():
    """启动后台保活线程（幂等；测试可用 KEEPALIVE_DISABLE=1 关掉）"""
    if os.environ.get("KEEPALIVE_DISABLE"):
        print("[keepalive] 已被 KEEPALIVE_DISABLE 关闭（测试环境）")
        return False
    with _lock:
        if _state["started"]:
            return True
        _state["started"] = True
    t = threading.Thread(target=_loop, name="self-keepalive", daemon=True)
    t.start()
    cfg = load_cfg()
    print("[keepalive] 自保活已启动：%s | 时段 %s:00-%s:00(北京) | 每 %s 分钟"
          % (cfg["url"], cfg["start_hour"], cfg["end_hour"], cfg["interval_min"]))
    return True


def status():
    """给前端/排查用的状态"""
    cfg = load_cfg()
    with _lock:
        st = dict(_state)
    boot = datetime.fromtimestamp(st["boot_ts"], BJ)
    up_h = round((time.time() - st["boot_ts"]) / 3600.0, 2)
    return {
        "enabled": cfg["enabled"],
        "in_window": in_window(cfg),
        "window": "%s:00-%s:00" % (cfg["start_hour"], cfg["end_hour"]),
        "start_hour": cfg["start_hour"],
        "end_hour": cfg["end_hour"],
        "interval_min": cfg["interval_min"],
        "url": cfg["url"],
        "count": st["count"],
        "last": st["last"],
        "boot_at": boot.strftime("%Y-%m-%d %H:%M:%S"),
        # 这个数字很关键：一直增长 = 这段时间**从没睡过**（睡过会重启进程、数字归零）
        "uptime_hours": up_h,
        "now_bj": _bj_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
