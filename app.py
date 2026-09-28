"""烤肉店后厨备餐系统 - Flask 主应用"""
import math
import os
from flask import Flask, request, jsonify, render_template, g
from models import get_db, init_db
from parser import parse_order_text, match_packages_in_db
from calculator import calc_order_requirements, calc_dashboard, preview_parse, calc_merged_prep, calc_prep_urgency

app = Flask(__name__, template_folder="templates", static_folder="static")

# /api/version 的缓存：{sha: 最新改动标题}，避免每次开页面都去 git fetch（见 api_version）
_VERSION_MEMO = {}

# 模块加载时初始化数据库（保证 gunicorn 多 worker 也能跑）
init_db()

# 尝试从 GitHub 恢复数据库（防止重新部署丢数据）
try:
    from db_backup import restore_from_github, backup_async, start_auto_backup
    restored = restore_from_github()
    if restored:
        print("[init] 已从 GitHub 恢复数据库")
        # 关键：恢复会整体替换 kitchen.db（用的是"上次那份备份"的表结构），
        # 所以必须**再跑一次表结构迁移**，否则新版本新增的字段在恢复后不复存在，
        # 新代码一查就报 "no such column"。init_db 是幂等的，重跑安全。
        init_db()
        print("[init] 恢复后已重新执行表结构迁移")
except Exception as _e:
    print(f"[init] restore_from_github skipped: {_e}")

try:
    from seed import seed_data
    seed_data()
except Exception as _e:
    print(f"[init] seed_data skipped: {_e}")

# seed 造完食材后**必须再回填一次**采购口径：
# init_db 跑迁移时表还是空的，回填等于没跑，于是这些食材的 purchase_unit 会一直是空
# （表现：备餐页按"公斤/斤"显示不出来、只回退到"份"）。幂等，只补空的。
try:
    from models import backfill_purchase
    _db0 = get_db()
    backfill_purchase(_db0.cursor())
    _db0.commit()
    _db0.close()
except Exception as _e:
    print(f"[init] backfill after seed skipped: {_e}")

# 启动定时备份（10分钟）
try:
    start_auto_backup(600)
except Exception as _e:
    print(f"[init] auto_backup skipped: {_e}")

# 启动云端自保活：营业时段定时请求自己的 /api/ping，让 Render 免费实例不进入休眠。
# 为什么不只靠 GitHub Actions 的定时任务：GitHub 官方明确 schedule 事件在高峰期
# 可能延迟、**最坏情况根本不运行**（实测提交后近 2 小时一次都没跑）。
try:
    from keepalive import start_self_keepalive
    start_self_keepalive()
except Exception as _e:
    print(f"[init] self_keepalive skipped: {_e}")


@app.before_request
def _keepalive_watchdog():
    """每个请求都顺带看护一下保活线程（很便宜：先比较时间戳，不到点不做任何事）。

    为什么要在请求里兜一层：**线程不会被 fork 继承**，worker 里的线程可能一开始就没有；
    也可能因为任何原因死掉。只要还有任何请求（后厨手机每 5 分钟的保活心跳、有人开页面），
    就能把线程补回来、并把该打的心跳补上，不至于悄无声息地失效。
    ⚠️ 必须排除 /api/ping 自己 —— 它就是我们自己打进来的，否则会无限递归。
    """
    try:
        if request.path == "/api/ping":
            return
        import keepalive
        keepalive.ensure_thread()
        keepalive.maybe_ping_now()
    except Exception:
        pass

# 迁移：去掉"刷子"工具（用户要求菜单无刷子，兼容已有数据库）
try:
    _db = get_db()
    _cur = _db.cursor()
    _cur.execute("SELECT id FROM tools WHERE name = '刷子'")
    _row = _cur.fetchone()
    if _row:
        _bid = _row["id"]
        _cur.execute("DELETE FROM package_tools WHERE tool_id = ?", (_bid,))
        _cur.execute("DELETE FROM tool_loans WHERE tool_id = ?", (_bid,))
        _cur.execute("DELETE FROM tools WHERE id = ?", (_bid,))
        _db.commit()
        print(f"[migrate] 已删除刷子工具(id={_bid})及关联记录")
    _db.close()
except Exception as _e:
    print(f"[migrate] 删除刷子跳过: {_e}")

# 迁移：拆分"卡式炉/烤盘"为"卡式炉"和"烤盘"两个独立工具（兼容已有数据库）
try:
    _db = get_db()
    _cur = _db.cursor()
    _cur.execute("SELECT id FROM tools WHERE name = '卡式炉/烤盘'")
    _row = _cur.fetchone()
    if _row:
        _old_id = _row["id"]
        # 1. 新增两个新工具（卡式炉+烤盘），库存/阈值/成本按合理拆分
        _cur.execute("SELECT stock, threshold, cost FROM tools WHERE id = ?", (_old_id,))
        _old = _cur.fetchone()
        _old_stock = _old["stock"] if _old else 15
        _old_thr = _old["threshold"] if _old else 3
        _cur.execute("INSERT INTO tools (name, stock, threshold, cost) VALUES (?,?,?,?)",
                     ("卡式炉", _old_stock, _old_thr, 65.0))
        _cur.execute("INSERT INTO tools (name, stock, threshold, cost) VALUES (?,?,?,?)",
                     ("烤盘", _old_stock, _old_thr, 15.0))
        _new_stove = _cur.execute("SELECT id FROM tools WHERE name='卡式炉'").fetchone()["id"]
        _new_pan = _cur.execute("SELECT id FROM tools WHERE name='烤盘'").fetchone()["id"]
        # 2. 给每个套餐添加两条 package_tools 关联，per_package 等于原值
        _cur.execute("SELECT package_id, per_package FROM package_tools WHERE tool_id = ?", (_old_id,))
        for _r in _cur.fetchall():
            _cur.execute("INSERT INTO package_tools (package_id, tool_id, per_package) VALUES (?,?,?)",
                         (_r["package_id"], _new_stove, _r["per_package"]))
            _cur.execute("INSERT INTO package_tools (package_id, tool_id, per_package) VALUES (?,?,?)",
                         (_r["package_id"], _new_pan, _r["per_package"]))
        # 3. 删除旧"卡式炉/烤盘"的 package_tools 关联和工具记录
        _cur.execute("DELETE FROM package_tools WHERE tool_id = ?", (_old_id,))
        _cur.execute("DELETE FROM tool_loans WHERE tool_id = ?", (_old_id,))
        _cur.execute("DELETE FROM tools WHERE id = ?", (_old_id,))
        _db.commit()
        print(f"[migrate] 已拆分'卡式炉/烤盘'(id={_old_id})为卡式炉(id={_new_stove})+烤盘(id={_new_pan})")
    _db.close()
except Exception as _e:
    print(f"[migrate] 拆分卡式炉/烤盘跳过: {_e}")

# 迁移：重命名食材"应季水果三样" → "应季水果"（用户指定）
try:
    _db = get_db()
    _cur = _db.cursor()
    _cur.execute("SELECT id FROM ingredients WHERE name = '应季水果三样'")
    _row = _cur.fetchone()
    if _row:
        _cur.execute("UPDATE ingredients SET name = '应季水果' WHERE id = ?", (_row["id"],))
        _db.commit()
        print(f"[migrate] 已重命名食材'应季水果三样'(id={_row['id']})为'应季水果'")
    _db.close()
except Exception as _e:
    print(f"[migrate] 重命名应季水果三样跳过: {_e}")

# 迁移：修复历史订单 booking_date/booking_time 解析错误（旧 parser 对"9月11日12点"等格式解析失败）
# 重新用 raw_text 解析，更新非标准 YYYY-MM-DD 的 booking_date
try:
    import re as _re
    _db = get_db()
    _cur = _db.cursor()
    _cur.execute("SELECT id, raw_text, booking_date, booking_time, meal_time FROM orders")
    _fixed = 0
    _fixed_mt = 0
    for _r in _cur.fetchall():
        _bd = _r["booking_date"] or ""
        _needs_update = False
        _new_bd = _bd
        _new_bt = _r["booking_time"] or ""
        _new_mt = _r["meal_time"] or ""
        # 标准 YYYY-MM-DD 且不等于"待定"之类垃圾值则跳过日期解析
        if not _re.match(r"^\d{4}-\d{2}-\d{2}$", _bd) and _r["raw_text"]:
            from parser import parse_order_text as _pot
            _parsed = _pot(_r["raw_text"])
            _new_bd = _parsed.get("booking_date", "")
            _new_bt = _parsed.get("booking_time", "") or _new_bt
            _new_mt = _parsed.get("meal_time", "") or _new_mt
            if _new_bd:
                _needs_update = True
        # 同时清洗历史脏 meal_time（即使日期正常）
        if _r["raw_text"]:
            from parser import parse_order_text as _pot, _clean_meal_time as _cmt
            _parsed2 = _pot(_r["raw_text"])
            _cleaned_mt = _parsed2.get("meal_time", "")
            if _cleaned_mt and _cleaned_mt != _r["meal_time"]:
                _new_mt = _cleaned_mt
                _needs_update = True
        if _needs_update:
            _cur.execute("UPDATE orders SET booking_date=?, booking_time=?, meal_time=? WHERE id=?",
                         (_new_bd, _new_bt, _new_mt, _r["id"]))
            _fixed += 1
            if _new_mt != (_r["meal_time"] or ""):
                _fixed_mt += 1
    if _fixed:
        _db.commit()
        print(f"[migrate] 已修复 {_fixed} 个订单（其中 {_fixed_mt} 个 meal_time 被清洗）")
    _db.close()
except Exception as _e:
    print(f"[migrate] 修复订单 booking_date/meal_time 跳过: {_e}")

# 迁移：7-8人餐 把"韩式蘸酱"替换为"酸辣烤肉汁"，与其他套餐小料顺序一致
try:
    _db = get_db()
    _cur = _db.cursor()
    _cur.execute("SELECT id FROM ingredients WHERE name='韩式蘸酱'")
    _hjj = _cur.fetchone()
    _cur.execute("SELECT id FROM ingredients WHERE name='酸辣烤肉汁'")
    _slj = _cur.fetchone()
    if _hjj and _slj:
        _hjj_id = _hjj["id"]
        _slj_id = _slj["id"]
        # 7-8人餐的韩式蘸酱关联改为酸辣烤肉汁（per_package 调整为50，与其他套餐一致）
        _cur.execute("""UPDATE package_ingredients
                        SET ingredient_id=?, per_package=50
                        WHERE ingredient_id=? AND package_id IN (SELECT id FROM packages WHERE name='7-8人餐')""",
                     (_slj_id, _hjj_id))
        _affected = _cur.rowcount
        if _affected:
            _db.commit()
            print(f"[migrate] 已把 7-8人餐 的韩式蘸酱({_affected}条)替换为酸辣烤肉汁")
    _db.close()
except Exception as _e:
    print(f"[migrate] 替换韩式蘸酱跳过: {_e}")


@app.before_request
def before():
    g.db = get_db()


@app.teardown_request
def teardown(exc):
    db = getattr(g, "db", None)
    if db is not None:
        db.close()


# 数据变更后自动备份到 GitHub（POST/PUT/DELETE 且非备份接口）
@app.after_request
def auto_backup(response):
    if request.method in ("POST", "PUT", "DELETE") and "/api/backup" not in request.path:
        try:
            from db_backup import backup_async
            backup_async()
        except Exception:
            pass
    return response


@app.route("/api/backup", methods=["POST"])
def manual_backup():
    """手动触发备份"""
    from db_backup import backup_to_github
    ok = backup_to_github()
    if ok:
        return jsonify({"ok": True, "msg": "备份成功"})
    return jsonify({"ok": False, "msg": "备份失败（未配置 GITHUB_TOKEN 或网络错误）"})


# ===== 保活端点（专给"定时保活"和页面心跳用）=====
@app.route("/api/ping")
def api_ping():
    """极轻量：不查数据库、不做任何计算，只证明"服务是醒着的"。

    为什么要专门做一个：
    1. Render 免费实例「15 分钟没有入站请求」就会休眠，唤醒要 30-60 秒。
    2. **别拿 /robots.txt 当保活地址**——服务休眠时 Render 自己拦截这个路径并回
       disallow-all，请求根本到不了我们的服务，也就唤不醒它。
    3. 心跳/定时任务打这里最省资源（不碰 SQLite）。
    """
    import time as _t
    up = 0
    try:
        from keepalive import _state as _kst
        up = int(_t.time() - _kst["boot_ts"])
    except Exception:
        pass
    # up = 进程已连续运行的秒数。它一直涨说明**这段时间从没睡过**（睡过会重启进程、归零）
    resp = jsonify({"ok": True, "t": int(_t.time()), "up": up})
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/api/keepalive/status", methods=["GET"])
def keepalive_status():
    """云端自保活状态。uptime_hours 一直增长 = 这段时间没睡过。"""
    import keepalive
    return jsonify({"ok": True, "data": keepalive.status()})


@app.route("/api/keepalive/config", methods=["GET", "POST"])
def keepalive_config():
    """读/改自保活配置：enabled、start_hour、end_hour、interval_min"""
    import keepalive
    if request.method == "GET":
        return jsonify({"ok": True, "data": keepalive.load_cfg()})
    data = request.get_json(force=True) or {}
    cfg = keepalive.save_cfg(data)
    return jsonify({"ok": True, "data": cfg, "status": keepalive.status()})


@app.route("/api/backup/status", methods=["GET"])
def backup_status():
    """查询备份配置状态"""
    from db_backup import GITHUB_TOKEN, _last_backup
    import time as _time
    return jsonify({
        "configured": bool(GITHUB_TOKEN),
        "last_backup": _last_backup,
        "last_backup_ago": f"{int(_time.time() - _last_backup)}s" if _last_backup else None,
    })


@app.route("/api/version", methods=["GET"])
def api_version():
    """当前部署的版本信息。
    用途：确认"线上到底跑的是哪一版"——看部署时间即可，
    不用去比对 GitHub 上的提交标题（那里最新一条通常是自动备份 kitchen.db 的提交，容易被误以为没更新）。
    """
    import os as _os
    import datetime as _dt
    here = _os.path.dirname(_os.path.abspath(__file__))
    deployed_at = ""
    for fn in ("app.py", "parser.py", "calculator.py"):
        try:
            mt = _os.path.getmtime(_os.path.join(here, fn))
            t = _dt.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")
            if not deployed_at or t > deployed_at:
                deployed_at = t
        except Exception:
            pass
    sha = ""
    latest = ""
    try:
        import subprocess as _sp
        import re as _re
        sha = _sp.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=here,
                               stderr=_sp.DEVNULL).decode().strip()

        def _log_latest():
            """最新一条**非自动备份**的提交标题 —— 直接看出这一版包含什么改动"""
            try:
                out = _sp.check_output(["git", "log", "-n", "60", "--pretty=%s"], cwd=here,
                                       stderr=_sp.DEVNULL).decode("utf-8", "replace").splitlines()
            except Exception:
                return ""
            for ln in out:
                ln = (ln or "").strip()
                if ln and not ln.lower().startswith("auto:"):
                    return ln
            return ""

        if sha and sha in _VERSION_MEMO:
            latest = _VERSION_MEMO[sha]
        else:
            latest = _log_latest()
            if not latest:
                # Render 是浅克隆（--depth 1），历史里只剩一条 auto: 备份提交
                # → 拉深一点再读。用 blob:none（不要文件内容，只要提交信息），
                #   仓库里有 kitchen.db 这种二进制大文件，blobless 快很多。
                for _args in (["fetch", "--depth=30", "--filter=blob:none", "origin"],
                              ["fetch", "--depth=30", "origin"]):
                    try:
                        _sp.check_output(["git"] + _args, cwd=here,
                                         stderr=_sp.DEVNULL, timeout=60)
                        latest = _log_latest()
                        if latest:
                            break
                    except Exception:
                        continue
            if not latest:
                # 兜底：直接问 GitHub 公共接口（仓库是公开的，不需要令牌）
                try:
                    import urllib.request as _u
                    import json as _json
                    _repo = "ll136897/kitchen-panel"
                    try:
                        _origin = _sp.check_output(["git", "remote", "get-url", "origin"],
                                                   cwd=here, stderr=_sp.DEVNULL).decode().strip()
                        _m = _re.match(r".*[:/]([^/:]+/[^/]+?)(?:\.git)?$", _origin)
                        if _m:
                            _repo = _m.group(1)
                    except Exception:
                        pass
                    _req = _u.Request("https://api.github.com/repos/%s/commits?per_page=30" % _repo,
                                      headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "kitchen-panel"})
                    _data = _json.loads(_u.urlopen(_req, timeout=30).read().decode("utf-8", "replace"))
                    for _c in _data:
                        _msg = (_c.get("commit", {}).get("message", "") or "").strip().splitlines()[0]
                        if _msg and not _msg.lower().startswith("auto:"):
                            latest = _msg
                            break
                except Exception:
                    pass
            if latest and sha:
                _VERSION_MEMO[sha] = latest
        # 去掉 feat/fix(范围): 这类技术前缀，显示成"出餐单：xxx"这样的大白话
        m = _re.match(r"^(feat|fix|refactor|chore|perf|docs|style|test)\s*(?:\(([^)]*)\))?\s*[:：]\s*(.+)$", latest)
        if m:
            latest = ((m.group(2) + "：") if m.group(2) else "") + m.group(3)
    except Exception:
        pass
    # 连续运行时长：一直变大 = 这段时间**从没睡过**（休眠会重启进程、时间归零）
    boot_at, uptime_hours = "", 0
    try:
        from keepalive import _state as _kst
        import time as _t2
        import datetime as _dt2
        boot_at = _dt2.datetime.fromtimestamp(_kst["boot_ts"]).strftime("%Y-%m-%d %H:%M")
        uptime_hours = round((_t2.time() - _kst["boot_ts"]) / 3600.0, 1)
    except Exception:
        pass
    return jsonify({"ok": True, "deployed_at": deployed_at, "sha": sha,
                    "latest_change": latest, "boot_at": boot_at,
                    "uptime_hours": uptime_hours})


# ===== 页面 =====
@app.route("/")
def index():
    return render_template("dashboard.html")  # 默认进数据面板


@app.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html")


@app.route("/config")
def config_page():
    return render_template("config.html")


@app.route("/orders")
def orders_page():
    return render_template("orders.html")


@app.route("/orders/<int:oid>")
def order_detail_page(oid):
    return render_template("order_detail.html", oid=oid)


# ===== 出餐单图片：存成"真实网址" =====
# 前端用 canvas 画出小票后，本来只得到一个 data: 开头的内联图片；
# 新版 iOS 长按这种内联图片不再弹「存储到照片」，所以这里把它存下来，
# 换成一个真正的 https 图片地址——长按能存、能转发、能分享。
TICKET_SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "tickets")


@app.route("/api/ticket/<int:oid>/shot", methods=["POST"])
def ticket_shot_save(oid):
    """保存出餐单图片，返回可直接访问的网址。保留策略：每单最多 5 张、超过 7 天自动清理。"""
    import base64
    import time as _time
    import glob as _glob
    data = request.get_json(force=True, silent=True) or {}
    durl = (data.get("data_url") or "").strip()
    if not durl.startswith("data:image/png;base64,"):
        return jsonify({"ok": False, "msg": "图片数据格式不对"}), 400
    try:
        raw = base64.b64decode(durl.split(",", 1)[1])
    except Exception:
        return jsonify({"ok": False, "msg": "图片解码失败"}), 400
    if not raw or len(raw) > 8 * 1024 * 1024:
        return jsonify({"ok": False, "msg": "图片大小异常"}), 400
    try:
        os.makedirs(TICKET_SHOT_DIR, exist_ok=True)
        fn = "t%d_%s.png" % (oid, _time.strftime("%Y%m%d_%H%M%S"))
        with open(os.path.join(TICKET_SHOT_DIR, fn), "wb") as f:
            f.write(raw)
    except Exception as e:
        # 磁盘不可写时不要报错给用户看，前端会自动退回 data: 图片
        return jsonify({"ok": False, "msg": "服务器暂时无法保存图片"}), 500
    # 清理（失败无所谓，不影响返回）
    try:
        same = sorted(_glob.glob(os.path.join(TICKET_SHOT_DIR, "t%d_*.png" % oid)))
        for old in same[:-5]:
            try:
                os.remove(old)
            except Exception:
                pass
        now = _time.time()
        for f2 in _glob.glob(os.path.join(TICKET_SHOT_DIR, "*.png")):
            try:
                if now - os.path.getmtime(f2) > 7 * 86400:
                    os.remove(f2)
            except Exception:
                pass
    except Exception:
        pass
    url = "/static/tickets/%s" % fn
    return jsonify({"ok": True, "url": url,
                    "abs": request.host_url.rstrip("/") + url,
                    "kb": round(len(raw) / 1024.0, 1)})


# ===== 出餐明细单（客户小票：可打印/存PDF/导出图片）=====
@app.route("/orders/<int:oid>/ticket")
def order_ticket(oid):
    """从订单生成出餐明细单（58mm 小票样式，给客户核对菜品用）"""
    import html as _html
    import json as _json
    db = g.db
    cur = db.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (oid,))
    r = cur.fetchone()
    if not r:
        return "订单不存在", 404
    o = dict(r)
    cur.execute("""
        SELECT op.quantity, op.people, op.price as op_price, p.name as pkg_name, p.price
        FROM order_packages op LEFT JOIN packages p ON op.package_id = p.id
        WHERE op.order_id = ?
        ORDER BY op.id
    """, (oid,))
    pkgs = [dict(x) for x in cur.fetchall()]

    def esc(s):
        return _html.escape(str(s if s is not None else ''))

    shop = "刘和牛户外烤肉"
    no = "#%d" % oid
    contact = (o.get("contact_name") or "").strip()
    phone = (o.get("contact_phone") or "").strip()
    customer = (contact + (" " + phone if phone else "")).strip() or "—"
    address = (o.get("address") or "").strip()
    note = (o.get("note") or "").strip()

    calc_total = 0.0
    item_rows = ""
    data_items = []
    for p in pkgs:
        qty = int(p.get("quantity") or 1)
        # 本单单价优先（op.price），没改过就用套餐标准价
        op_price = p.get("op_price")
        price = float(op_price) if op_price is not None else float(p.get("price") or 0)
        line = round(price * qty, 2)
        calc_total += line
        price_str = ("¥%.2f" % line) if price > 0 else "—"
        item_rows += ('<div class="item"><div class="ln"><span class="nm">%s</span>'
                      '<span>x%d</span><span>%s</span></div></div>'
                      % (esc(p.get("pkg_name")), qty, price_str))
        data_items.append({"n": p.get("pkg_name") or "", "q": qty, "ps": price_str})
    if not item_rows:
        item_rows = '<div class="item">（本单未选套餐）</div>'
    if o.get("amount"):
        total_str = "¥%.2f" % float(o["amount"])
    elif calc_total > 0:
        total_str = "¥%.2f" % round(calc_total, 2)
    else:
        total_str = "—"
    deposit = float(o.get("deposit") or 0)

    # 日期单独成行，不再与单号挤在一起；收餐时间那类长句子单独放到下面
    date_str = ("%s %s" % ((o.get("booking_date") or "").strip(),
                           (o.get("booking_time") or "").strip())).strip() or "待定"
    meal_str = (o.get("meal_time") or "").strip()
    if len(meal_str) > 12:
        meal_str = ""
    pickup_str = (o.get("pickup_time") or "").strip()

    # 优惠/加收：总额与套餐标价之和对不上时，用这一行把账做平
    discount = float(o.get("discount") or 0)
    adj_line = ""
    if abs(discount) > 0.001 and calc_total > 0:
        adj_line = ('<div class="item subst"><div class="ln"><span class="nm">套餐小计</span>'
                    '<span></span><span>¥%.2f</span></div></div>' % round(calc_total, 2))
        if discount > 0:
            adj_line += ('<div class="item subst"><div class="ln"><span class="nm">优惠</span>'
                         '<span></span><span>-¥%.2f</span></div></div>' % discount)
        else:
            adj_line += ('<div class="item subst"><div class="ln"><span class="nm">加收</span>'
                         '<span></span><span>+¥%.2f</span></div></div>' % abs(discount))

    data = {
        "shop": shop, "no": no, "date": date_str, "meal": meal_str, "pickup": pickup_str,
        "customer": customer, "address": address,
        "items": data_items, "total": total_str,
        "deposit": ("¥%.2f" % deposit) if deposit > 0 else "",
        "note": note,
        "subtotal": ("¥%.2f" % round(calc_total, 2)) if calc_total > 0 else "",
        "discount_num": discount,
        "discount_str": (("-¥%.2f" % discount) if discount > 0 else ("+¥%.2f" % abs(discount))) if abs(discount) > 0.001 else "",
    }

    html = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>出餐明细单 __NO__</title>
<style>
* { box-sizing: border-box; }
body { margin:0; background:#eef1f5; color:#1f2329; font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif; font-size:14px; }
.toolbar { text-align:center; padding:14px 10px 4px; }
.toolbar button { margin:4px 6px; padding:10px 20px; border:none; border-radius:8px; font-size:15px; font-weight:700; cursor:pointer; }
.b-print { background:#c0392b; color:#fff; }
.b-png { background:#27ae60; color:#fff; }
.hint { text-align:center; font-size:12px; color:#8a97a5; padding:2px 10px 8px; }
.wrap { display:flex; justify-content:center; padding:10px 8px 30px; }
.receipt { width:58mm; background:#fff; color:#000; padding:2mm 3mm; font-family:"Courier New","PingFang SC",monospace; font-size:12px; line-height:1.45; }
.receipt .c { text-align:center; }
.receipt .shop { font-size:15px; font-weight:bold; letter-spacing:1px; }
.receipt .title { font-size:13px; border-top:1px dashed #000; border-bottom:1px dashed #000; padding:3px 0; margin:4px 0; text-align:center; letter-spacing:2px; }
/* 信息行各自独占一行，长内容自动换行 —— 修掉"单号与日期数字挤在一起/重合" */
.receipt .meta { font-size:11px; line-height:1.5; overflow-wrap:anywhere; }
.receipt .sep { border-top:1px dashed #000; margin:4px 0; }
.receipt .item { margin:2px 0; }
.receipt .item .nm { word-break:break-all; }
.receipt .item .ln { display:flex; justify-content:space-between; gap:6px; }
.receipt .item.subst { color:#666; font-size:13px; }
.receipt .grand { font-size:14px; font-weight:bold; display:flex; justify-content:space-between; }
.receipt .ft { text-align:center; font-size:10.5px; margin-top:6px; color:#333; }
.receipt .barcode { text-align:center; font-family:"Courier New",monospace; font-size:10px; letter-spacing:1px; margin-top:2px; }
/* 图片视图：生成后直接显示大图，长按/右键即可另存 */
#imgView { display:none; text-align:center; padding:10px 10px 34px; }
#imgView .ok { color:#27ae60; font-weight:700; font-size:15px; padding:4px 0 2px; }
#imgView .sub { color:#8a97a5; font-size:12px; padding-bottom:10px; }
#imgView img { width:100%; max-width:340px; border:1px solid #d4dae3; border-radius:8px; background:#fff; }
#imgView .acts { margin-top:14px; display:flex; justify-content:center; gap:10px; flex-wrap:wrap; }
#imgView .acts a, #imgView .acts button { display:inline-block; padding:11px 18px; border-radius:8px; font-size:15px; font-weight:700; cursor:pointer; border:none; text-decoration:none; }
#imgView .b-dl { background:#27ae60; color:#fff; }
#imgView .b-open { background:#2471a3; color:#fff; }
#imgView .b-back { background:#f0f2f5; color:#1f2329; }
@media print {
  body { background:#fff; }
  .toolbar, .hint, #imgView { display:none !important; }
  .wrap { padding:0; }
  @page { size: 58mm auto; margin: 2mm; }
}
</style>
</head>
<body>
<div class="toolbar">
  <button class="b-print" onclick="window.print()">🖨️ 打印 / 存为PDF</button>
  <button class="b-png" onclick="exportPNG()">🖼️ 存为图片</button>
  <button class="b-edit" onclick="location.href='/orders/__OID__'" style="background:#f0f2f5;color:#1f2329">✏️ 编辑本单</button>
</div>
<div class="hint" id="listHint">点「🖼️ 存为图片」→ 可「存到相册」，也可长按图片保存</div>
<div class="wrap" id="rcptWrap">
<div class="receipt" id="rcpt">
<div class="c shop">__SHOP__</div>
<div class="c" style="font-size:11px">出 餐 明 细 单</div>
<div class="title">*** 出餐明细单 ***</div>
<div class="meta">单号：__NO__</div>
<div class="meta">日期：__DATE__</div>
__META_EXTRA__
__ITEMS__
<div class="sep"></div>
__ADJ_LINE__
<div class="grand"><span>合计</span><span>__TOTAL__</span></div>
__DEPOSIT_LINE__
__EXTRA_LINE__
__NOTE_LINE__
<div class="ft">谢谢惠顾 · 请按小票核对菜品<br>祝您用餐愉快！</div>
<div class="barcode">__NO__</div>
</div>
</div>
<div id="imgView">
  <div class="ok">✅ 图片已生成</div>
  <div class="sub" id="imgHint">长按图片 → 存储到照片</div>
  <img id="ticketImg" alt="出餐单图片">
  <div class="acts">
    <button id="shareBtn" class="b-dl" onclick="shareImg()" style="display:none">📤 存到相册 / 分享</button>
    <a id="dlLink" class="b-dl" download="ticket.png">⬇️ 下载图片</a>
    <a id="openLink" class="b-open" target="_blank" rel="noopener" style="display:none">↗︎ 新窗口打开</a>
    <button class="b-back" onclick="backToReceipt()">↩︎ 返回小票</button>
  </div>
</div>
<script>
var DATA = __DATA__;
function drawTicket(){
  var S = 2, W = 219;
  var lh = { c:18, title:22, sep:10, meta:15, item:16, grand:20, note:15, ft:26, bar:16 };
  var L = [];
  L.push({t:'c', bold:true, size:15, text:DATA.shop});
  L.push({t:'c', size:11, text:'出 餐 明 细 单'});
  L.push({t:'title', text:'*** 出餐明细单 ***'});
  L.push({t:'meta', l:'单号：'+DATA.no});
  L.push({t:'meta', l:'日期：'+(DATA.date||'待定')});
  if (DATA.meal) L.push({t:'meta', l:'用餐：'+DATA.meal});
  if (DATA.customer && DATA.customer !== '—') L.push({t:'meta', l:'客户：'+DATA.customer});
  if (DATA.address) L.push({t:'meta', l:'地址：'+DATA.address});
  L.push({t:'sep'});
  DATA.items.forEach(function(it){ L.push({t:'item', n:it.n, q:'x'+it.q, p:it.ps}); });
  L.push({t:'sep'});
  if (DATA.discount_num && DATA.subtotal) {
    L.push({t:'item', n:'套餐小计', q:'', p:DATA.subtotal});
    L.push({t:'item', n:(DATA.discount_num > 0 ? '优惠' : '加收'), q:'', p:DATA.discount_str});
  }
  L.push({t:'grand', l:'合计', r:DATA.total});
  if (DATA.deposit) L.push({t:'meta', l:'押金 '+DATA.deposit+'（离场退还）'});
  if (DATA.pickup) L.push({t:'meta', l:'收餐：'+DATA.pickup});
  if (DATA.note) { L.push({t:'sep'}); L.push({t:'note', text:'备注：'+DATA.note}); }
  L.push({t:'ft', text:'谢谢惠顾 · 请按小票核对菜品\\n祝您用餐愉快！'});
  L.push({t:'bar', text:DATA.no});

  // 先画在足够高的画布上，最后按实际高度裁剪（避免文字换行导致高度算不准）
  var BIG = 3000;
  var cv = document.createElement('canvas');
  cv.width = W*S; cv.height = BIG*S;
  var ctx = cv.getContext('2d'); ctx.scale(S,S);
  ctx.fillStyle = '#fff'; ctx.fillRect(0,0,W,BIG); ctx.fillStyle='#000'; ctx.textBaseline='top';
  var FONT = '"PingFang SC","Microsoft YaHei",monospace';
  var y = 8;
  function dash(x1,y1,x2,y2){ ctx.save(); ctx.setLineDash([3,2]); ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke(); ctx.restore(); }
  // 按可用宽度折行，避免长文本（地址/备注/收餐说明）超出票面
  function wrap(text, maxW, font){
    ctx.font = font;
    var out = [], cur = '';
    var chars = String(text).split('');
    for (var i=0;i<chars.length;i++){
      var t = cur + chars[i];
      if (ctx.measureText(t).width > maxW && cur){ out.push(cur); cur = chars[i]; }
      else cur = t;
    }
    if (cur) out.push(cur);
    return out.length ? out : [''];
  }
  L.forEach(function(l){
    if (l.t==='c'){ ctx.textAlign='center'; ctx.font=(l.bold?'bold ':'')+(l.size||12)+'px '+FONT; ctx.fillText(l.text, W/2, y); y += lh.c; }
    else if (l.t==='title'){ ctx.textAlign='center'; ctx.font='bold 13px '+FONT; dash(4,y,W-4,y); y+=5; ctx.fillText(l.text, W/2, y); y += lh.title; }
    else if (l.t==='sep'){ dash(4,y,W-4,y); y += lh.sep; }
    else if (l.t==='meta'){ ctx.textAlign='left'; ctx.font='11px '+FONT; wrap(l.l, W-8, '11px '+FONT).forEach(function(s){ ctx.fillText(s,4,y); y += lh.meta; }); }
    else if (l.t==='item'){
      ctx.font='12px '+FONT;
      var right = (l.q ? (l.q+'   ') : '') + (l.p||'');
      var rw = ctx.measureText(right).width;
      var nm = String(l.n||'');
      if (ctx.measureText(nm).width > (W-8-rw-8)){
        while (nm.length > 1 && ctx.measureText(nm+'…').width > (W-8-rw-8)) nm = nm.slice(0,-1);
        nm += '…';
      }
      ctx.textAlign='left'; ctx.fillText(nm,4,y);
      ctx.textAlign='right'; ctx.fillText(right, W-4, y); ctx.textAlign='left';
      y += lh.item;
    }
    else if (l.t==='grand'){ ctx.textAlign='left'; ctx.font='bold 14px '+FONT; ctx.fillText(l.l,4,y); ctx.textAlign='right'; ctx.fillText(l.r,W-4,y); ctx.textAlign='left'; y += lh.grand; }
    else if (l.t==='note'){ ctx.textAlign='left'; wrap(l.text, W-8, '12px '+FONT).forEach(function(s){ ctx.fillText(s,4,y); y += lh.note; }); }
    else if (l.t==='ft'){ ctx.textAlign='center'; ctx.font='10.5px '+FONT; l.text.split('\\n').forEach(function(s){ ctx.fillText(s, W/2, y); y += 13; }); }
    else if (l.t==='bar'){ ctx.textAlign='center'; ctx.font='10px "Courier New",monospace'; ctx.fillText(l.text, W/2, y); y += lh.bar; }
  });
  var H = Math.max(20, y + 8);
  var out = document.createElement('canvas');
  out.width = W*S; out.height = Math.round(H*S);
  var octx = out.getContext('2d');
  octx.fillStyle = '#fff'; octx.fillRect(0,0,out.width,out.height);
  octx.drawImage(cv, 0, 0, out.width, out.height, 0, 0, out.width, out.height);
  return out;
}
var _cv = null, _blob = null, _fileName = 'ticket.png', _realUrl = '';
function imgFileName(){ return '出餐单_' + String(DATA.no||'').replace('#','') + '.png'; }
function setHint(ready){
  var h = document.getElementById('imgHint');
  if(!h) return;
  h.innerHTML = ready
    ? '点「📤 存到相册」直接保存　·　或长按图片 → 存储到照片'
    : '长按图片 → 存储到照片（若长按没反应，点「↗︎ 新窗口打开」再长按）';
}
function toBlob(cb){
  if(!_cv){ cb(null); return; }
  if(_cv.toBlob){ _cv.toBlob(function(b){ cb(b); }, 'image/png'); return; }
  try{
    var bin = atob(_cv.toDataURL('image/png').split(',')[1]);
    var arr = new Uint8Array(bin.length);
    for (var i=0;i<bin.length;i++) arr[i] = bin.charCodeAt(i);
    cb(new Blob([arr], {type:'image/png'}));
  }catch(e){ cb(null); }
}
function exportPNG(){
  _cv = drawTicket();
  _fileName = imgFileName();
  var dataUrl = _cv.toDataURL('image/png');
  document.getElementById('ticketImg').src = dataUrl;
  var dl = document.getElementById('dlLink');
  dl.href = dataUrl; dl.download = _fileName;
  document.getElementById('rcptWrap').style.display = 'none';
  document.getElementById('listHint').style.display = 'none';
  document.getElementById('imgView').style.display = 'block';
  window.scrollTo(0,0);
  setHint(false);
  // 先把图片数据备好：系统分享必须是"点击瞬间"同步调用，不能等异步
  toBlob(function(b){
    if(!b) return;
    _blob = b;
    var f = null;
    try { f = new File([b], _fileName, {type:'image/png'}); } catch(e){}
    if (f && navigator.canShare && navigator.canShare({files:[f]})) {
      document.getElementById('shareBtn').style.display = 'inline-block';
    }
  });
  // 再存成真实网址（新版 iOS 长按 data: 内联图片不再弹"存储到照片"）
  fetch('/api/ticket/__OID__/shot', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({data_url: dataUrl})
  }).then(function(r){ return r.json(); }).then(function(res){
    if (res && res.ok && res.abs){
      _realUrl = res.abs;
      document.getElementById('ticketImg').src = res.abs;
      document.getElementById('dlLink').href = res.abs;
      var o = document.getElementById('openLink');
      o.href = res.abs; o.style.display = 'inline-block';
      setHint(true);
    }
  }).catch(function(){});
}
function shareImg(){
  var f = null;
  try { f = new File([_blob], _fileName, {type:'image/png'}); } catch(e){}
  if (!f || !navigator.canShare || !navigator.canShare({files:[f]})){
    alert('这个浏览器不支持直接存相册。\\n\\n办法一：长按上面的图片 → 选「存储到照片」\\n办法二：点「↗︎ 新窗口打开」→ 在新页面长按图片保存');
    return;
  }
  navigator.share({files:[f], title:_fileName}).catch(function(){});
}
function backToReceipt(){
  document.getElementById('imgView').style.display = 'none';
  document.getElementById('rcptWrap').style.display = 'flex';
  document.getElementById('listHint').style.display = 'block';
  window.scrollTo(0,0);
}
</script>
</body>
</html>"""

    meta_extra = ""
    if meal_str:
        meta_extra += '<div class="meta">用餐：%s</div>' % esc(meal_str)
    if customer != "—":
        meta_extra += '<div class="meta">客户：%s</div>' % esc(customer)
    if address:
        meta_extra += '<div class="meta">地址：%s</div>' % esc(address)
    deposit_line = ""
    if deposit > 0:
        deposit_line = '<div class="meta" style="margin-top:2px">押金 ¥%.2f（离场退还）</div>' % deposit
    # 收餐时间那栏常被填成整句话（如"22点后加收每小时50元夜间服务费"），
    # 单独占一行自动换行，不再和"单号/日期"挤在一起
    extra_line = ""
    if pickup_str:
        extra_line = '<div class="meta">收餐：%s</div>' % esc(pickup_str)
    note_line = ""
    if note:
        note_line = '<div class="sep"></div><div class="item">备注：%s</div>' % esc(note)

    html = (html
            .replace("__SHOP__", esc(shop))
            .replace("__NO__", esc(no))
            .replace("__OID__", str(oid))
            .replace("__DATE__", esc(date_str))
            .replace("__META_EXTRA__", meta_extra)
            .replace("__ITEMS__", item_rows)
            .replace("__ADJ_LINE__", adj_line)
            .replace("__TOTAL__", esc(total_str))
            .replace("__DEPOSIT_LINE__", deposit_line)
            .replace("__EXTRA_LINE__", extra_line)
            .replace("__NOTE_LINE__", note_line)
            .replace("__DATA__", _json.dumps(data, ensure_ascii=False)))
    return html


@app.route("/api/orders/<int:oid>/detail")
def order_detail(oid):
    """订单详情：基本信息 + 备餐需求 + 工具借还 + 成本明细"""
    db = g.db
    cur = db.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (oid,))
    r = cur.fetchone()
    if not r:
        return jsonify({"ok": False, "msg": "订单不存在"}), 404
    o = dict(r)
    # 套餐
    cur.execute("""
        SELECT op.*, p.name as pkg_name, p.min_people, p.max_people, p.price as std_price
        FROM order_packages op LEFT JOIN packages p ON op.package_id = p.id
        WHERE op.order_id = ?
    """, (oid,))
    o["packages"] = [dict(x) for x in cur.fetchall()]
    # 备餐需求
    from calculator import calc_order_requirements, calc_prep_urgency
    req = calc_order_requirements(oid)
    o["requirements"] = req
    o["urgency"] = calc_prep_urgency(o)
    # 工具借还
    cur.execute("""
        SELECT tl.*, t.name as tool_name, t.cost
        FROM tool_loans tl LEFT JOIN tools t ON tl.tool_id = t.id
        WHERE tl.order_id = ?
    """, (oid,))
    o["tool_loans"] = [dict(x) for x in cur.fetchall()]
    return jsonify({"ok": True, "data": o})


@app.route("/history")
@app.route("/finance")
def finance_page():
    return render_template("finance.html")


@app.route("/prep")
def prep_page():
    return render_template("prep.html")


@app.route("/menu")
def menu_page():
    return render_template("menu.html")


# ===== 订单解析与入库 =====
@app.route("/api/parse", methods=["POST"])
def api_parse():
    """解析订单文本预览（不入库）。支持批量：用 --- 分隔多个订单"""
    data = request.get_json(force=True)
    raw = data.get("raw_text", "")
    if not raw.strip():
        return jsonify({"ok": False, "msg": "文本为空"}), 400
    import re as _re
    chunks = _re.split(r'\n[\-=]{3,}\n', raw.strip())
    chunks = [c.strip() for c in chunks if c.strip()]
    if len(chunks) > 1:
        previews = []
        for i, chunk in enumerate(chunks):
            parsed = preview_parse(chunk)
            parsed["_batch_idx"] = i + 1
            previews.append(parsed)
        return jsonify({"ok": True, "batch": True, "count": len(previews), "items": previews})
    parsed = preview_parse(raw)
    return jsonify({"ok": True, "data": parsed})


def _dup_key_of(parsed):
    """取用于「完全重复」判定的关键字段；关键信息缺失时返回 None（不做重复判断）"""
    bd = (parsed.get("booking_date") or "").strip()
    addr = (parsed.get("address") or "").strip()
    if not bd or not addr:
        return None
    return (bd, (parsed.get("booking_time") or "").strip(), addr,
            (parsed.get("contact_phone") or "").strip())


def _find_duplicate_order(cur, parsed):
    """找一条「完全重复」的现存订单（同日期+时间+地址+电话，且未取消、未删除）"""
    key = _dup_key_of(parsed)
    if not key:
        return None
    return cur.execute("""
        SELECT id, booking_date, booking_time, address, contact_name, contact_phone
        FROM orders
        WHERE deleted_at IS NULL AND status != 'cancelled'
          AND booking_date = ?
          AND COALESCE(booking_time,'') = ?
          AND COALESCE(address,'') = ?
          AND COALESCE(contact_phone,'') = ?
        ORDER BY id DESC LIMIT 1
    """, key).fetchone()


@app.route("/api/orders", methods=["POST"])
def create_order():
    """创建订单：接收 raw_text（单个或批量用 --- 分隔），自动解析入库。
    若发现一模一样的订单，返回 409 + duplicate 标记，由前端弹框让用户确认；
    用户确认后带 force=true 再提交一次即强制入库。"""
    data = request.get_json(force=True)
    raw = data.get("raw_text", "")
    if not raw.strip():
        return jsonify({"ok": False, "msg": "文本为空"}), 400

    # 批量：用 --- 或 === 分隔多个订单
    import re as _re
    chunks = _re.split(r'\n[\-=]{3,}\n', raw.strip())
    chunks = [c.strip() for c in chunks if c.strip()]

    db = g.db
    cur = db.cursor()
    parsed_chunks = [parse_order_text(c) for c in chunks]

    # ---- 重复订单检查（调用方已 force 则跳过）----
    if not data.get("force"):
        for pc in parsed_chunks:
            dup = _find_duplicate_order(cur, pc)
            if dup:
                d = dict(dup)
                where = " ".join(x for x in [d.get("booking_date") or "",
                                             d.get("booking_time") or "",
                                             d.get("address") or "",
                                             d.get("contact_name") or ""] if x)
                return jsonify({
                    "ok": False,
                    "duplicate": True,
                    "existing_id": d["id"],
                    "existing": d,
                    "msg": "已有一模一样的订单 #%s（%s）" % (d["id"], where),
                }), 409

    order_ids = []
    results = []
    for chunk, parsed in zip(chunks, parsed_chunks):
        matched = match_packages_in_db(parsed["packages"])
        cur.execute("""
            INSERT INTO orders
            (raw_text, booking_date, booking_time, address, contact_name,
             contact_phone, amount, deposit, meal_time, pickup_time, note, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending')
        """, (
            chunk, parsed["booking_date"], parsed["booking_time"],
            parsed["address"], parsed["contact_name"], parsed["contact_phone"],
            parsed["amount"], parsed["deposit"], parsed["meal_time"],
            parsed["pickup_time"], parsed["note"],
        ))
        order_id = cur.lastrowid
        order_ids.append(order_id)
        for pk in matched:
            if not pk["package_id"]:
                continue
            cur.execute("""
                INSERT INTO order_packages
                (order_id, package_id, people, quantity)
                VALUES (?,?,?,?)
            """, (order_id, pk["package_id"], pk["people"], pk["quantity"]))
        results.append({"order_id": order_id, "name": parsed.get("contact_name", ""),
                        "date": parsed.get("booking_date", ""), "amount": parsed.get("amount", 0)})

    db.commit()
    if len(results) == 1:
        return jsonify({"ok": True, "order_id": order_ids[0]})
    return jsonify({"ok": True, "batch": True, "count": len(results), "orders": results})


@app.route("/api/orders")
def list_orders():
    db = g.db
    cur = db.cursor()
    purge_expired_deleted(db)          # 超过 7 天的回收站订单自动清掉
    show_deleted = request.args.get("deleted") == "1"
    if show_deleted:
        cur.execute("""
            SELECT id, booking_date, booking_time, address, contact_name,
                   contact_phone, amount, deposit, note, status, created_at, deleted_at
            FROM orders WHERE deleted_at IS NOT NULL ORDER BY id DESC LIMIT 100
        """)
    else:
        cur.execute("""
            SELECT id, booking_date, booking_time, address, contact_name,
                   contact_phone, amount, deposit, note, status, created_at
            FROM orders WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 100
        """)
    orders = [dict(r) for r in cur.fetchall()]
    # 加套餐明细
    for o in orders:
        cur.execute("""
            SELECT op.people, op.quantity, p.name
            FROM order_packages op
            LEFT JOIN packages p ON op.package_id = p.id
            WHERE op.order_id = ?
        """, (o["id"],))
        o["packages"] = [dict(r) for r in cur.fetchall()]
    deleted_count = cur.execute(
        "SELECT COUNT(*) FROM orders WHERE deleted_at IS NOT NULL").fetchone()[0]
    return jsonify({"ok": True, "data": orders, "deleted_count": deleted_count})


PURGE_DAYS = 7


def purge_expired_deleted(db=None):
    """回收站里超过 7 天的订单彻底删除（含级联的套餐/借还/勾选）"""
    db = db or g.db
    try:
        db.execute("""
            DELETE FROM orders
            WHERE deleted_at IS NOT NULL
              AND deleted_at < datetime('now','localtime','-%d days')
        """ % PURGE_DAYS)
        db.commit()
    except Exception:
        pass


@app.route("/api/orders/<int:oid>/restore", methods=["POST"])
def restore_order(oid):
    """从回收站恢复订单"""
    db = g.db
    cur = db.execute("SELECT id FROM orders WHERE id=?", (oid,))
    if not cur.fetchone():
        return jsonify({"ok": False, "msg": "订单不存在"}), 404
    db.execute("UPDATE orders SET deleted_at=NULL WHERE id=?", (oid,))
    db.commit()
    return jsonify({"ok": True, "msg": "已恢复"})


@app.route("/api/orders/<int:oid>/purge", methods=["DELETE"])
def purge_order(oid):
    """彻底删除（不可恢复）"""
    db = g.db
    cur = db.execute("SELECT id FROM orders WHERE id=?", (oid,))
    if not cur.fetchone():
        return jsonify({"ok": False, "msg": "订单不存在"}), 404
    db.execute("DELETE FROM orders WHERE id=?", (oid,))
    db.commit()
    return jsonify({"ok": True, "msg": "已彻底删除"})


@app.route("/api/orders/<int:oid>/edit", methods=["PUT", "POST"])
def edit_order(oid):
    """编辑订单：可改 预约日期/时间、用餐时间、地址、联系人/电话、金额、备注。
    传了 packages 就整单替换套餐明细：[{package_id, people, quantity}, ...]"""
    data = request.get_json(force=True)
    db = g.db
    cur = db.cursor()
    if not cur.execute("SELECT id FROM orders WHERE id=?", (oid,)).fetchone():
        return jsonify({"ok": False, "msg": "订单不存在"}), 404

    def _txt(k):
        return (data.get(k) or "").strip()

    sets, params = [], []
    for k in ("booking_date", "booking_time", "meal_time", "note",
              "address", "contact_name", "contact_phone"):
        if k in data:
            sets.append("%s=?" % k)
            params.append(_txt(k))
    if "amount" in data:
        try:
            amt = float(data.get("amount") or 0)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "msg": "金额格式不对"}), 400
        sets.append("amount=?")
        params.append(amt)
    if "discount" in data:
        try:
            disc = float(data.get("discount") or 0)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "msg": "优惠格式不对"}), 400
        sets.append("discount=?")
        params.append(disc)
    if sets:
        params.append(oid)
        cur.execute("UPDATE orders SET %s WHERE id=?" % ", ".join(sets), params)

    if "packages" in data:
        rows = []
        for p in (data.get("packages") or []):
            try:
                pid = int(p.get("package_id"))
            except (TypeError, ValueError):
                continue
            try:
                qty = int(p.get("quantity") or 1)
            except (TypeError, ValueError):
                qty = 1
            if qty <= 0:
                continue
            pk = cur.execute("SELECT max_people FROM packages WHERE id=?", (pid,)).fetchone()
            if not pk:
                continue
            people = p.get("people")
            if people in (None, ""):
                people = pk["max_people"]
            # 本单该套餐单价（可空 = 用套餐标准价）
            price = p.get("price")
            if price in (None, ""):
                price = None
            else:
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    price = None
            rows.append((pid, int(people or 0), qty, price))
        cur.execute("DELETE FROM order_packages WHERE order_id=?", (oid,))
        for pid, people, qty, price in rows:
            cur.execute("INSERT INTO order_packages (order_id, package_id, people, quantity, price) "
                        "VALUES (?,?,?,?,?)", (oid, pid, people, qty, price))

    db.commit()
    return jsonify({"ok": True, "msg": "已保存"})


@app.route("/api/orders/<int:oid>", methods=["PATCH", "DELETE"])
def update_order_status(oid):
    db = g.db
    # 删除订单 → 进回收站（软删除），7 天内可恢复
    if request.method == "DELETE":
        cur = db.execute("SELECT id FROM orders WHERE id=?", (oid,))
        if not cur.fetchone():
            return jsonify({"ok": False, "msg": "订单不存在"}), 404
        db.execute("UPDATE orders SET deleted_at=datetime('now','localtime') WHERE id=?", (oid,))
        db.commit()
        return jsonify({"ok": True, "msg": "已放进回收站，7 天内可恢复"})
    data = request.get_json(force=True)
    status = data.get("status")
    if status not in ("pending", "preparing", "done", "cancelled"):
        return jsonify({"ok": False, "msg": "状态非法"}), 400
    db.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
    # 注：原先"转备餐中自动借出工具"已停用 —— 搭建/回收外包、粗放阶段不逐笔登记借还，
    # 自动建借出挂账只会累积噪音；装备数量改以定期盘点为准。
    # 若将来要恢复精细借还管理，把下面一行取消注释即可：
    # if status == "preparing":
    #     borrow_tools(db, oid)
    db.commit()
    return jsonify({"ok": True})


def borrow_tools(db, order_id):
    """根据订单需求创建工具借出记录（若已存在则跳过）"""
    req = calc_order_requirements(order_id)
    cur = db.cursor()
    cur.execute("SELECT tool_id FROM tool_loans WHERE order_id=?", (order_id,))
    existing = {r["tool_id"] for r in cur.fetchall()}
    for t in req["tools"]:
        if t["id"] in existing:
            continue
        cur.execute("""
            INSERT INTO tool_loans (order_id, tool_id, quantity, status)
            VALUES (?,?,?,'borrowed')
        """, (order_id, t["id"], t["need"]))


@app.route("/api/orders/<int:oid>/requirements")
def order_requirements(oid):
    req = calc_order_requirements(oid)
    # 补上"采购口径"字段：备餐页要按用户要的「份 / 公斤·斤·袋」显示用量，
    # 而不是只给克数（用户明确：按克算不方便，份是固定克数、公斤是他买货的价格）。
    try:
        meta = {}
        for r in g.db.execute("SELECT id, purchase_unit, purchase_factor, portion_grams FROM ingredients"):
            meta[r["id"]] = {"purchase_unit": r["purchase_unit"],
                             "purchase_factor": r["purchase_factor"],
                             "portion_grams": r["portion_grams"]}
        for it in (req.get("ingredients") or []):
            m = meta.get(it.get("id"))
            if not m:
                continue
            it["purchase_unit"] = m["purchase_unit"]
            it["purchase_factor"] = m["purchase_factor"]
            if not it.get("portion_size"):
                it["portion_size"] = m["portion_grams"]
    except Exception:
        pass
    return jsonify({"ok": True, "data": req})


# ===== 库存面板 =====
@app.route("/api/dashboard")
def dashboard_api():
    return jsonify({"ok": True, "data": calc_dashboard()})


# ===== 库存调整 =====
@app.route("/api/stock/adjust", methods=["POST"])
def adjust_stock():
    """调整库存：item_type(ingredient/tool), item_id, delta, reason"""
    data = request.get_json(force=True)
    itype = data.get("item_type")
    iid = data.get("item_id")
    delta = float(data.get("delta", 0))
    reason = data.get("reason", "")
    if itype not in ("ingredient", "tool") or not iid:
        return jsonify({"ok": False, "msg": "参数错误"}), 400
    db = g.db
    table = "ingredients" if itype == "ingredient" else "tools"
    db.execute(f"UPDATE {table} SET stock = stock + ? WHERE id = ?", (delta, iid))
    db.execute("""
        INSERT INTO stock_logs (item_type, item_id, delta, reason)
        VALUES (?,?,?,?)
    """, (itype, iid, delta, reason))
    db.commit()
    return jsonify({"ok": True})


# ===== 进货单解析 =====
@app.route("/api/stock/parse", methods=["POST"])
def api_stock_parse():
    """解析进货单文本，返回匹配预览"""
    from parser import parse_stock_invoice_text, match_inbound_to_db
    data = request.get_json(force=True)
    raw = data.get("raw_text", "")
    if not raw.strip():
        return jsonify({"ok": False, "msg": "文本为空"}), 400
    items = parse_stock_invoice_text(raw)
    matched = match_inbound_to_db(items)
    ok = sum(1 for x in matched if x["ok"])
    return jsonify({"ok": True, "items": matched, "count": len(matched), "matched": ok, "unmatched": len(matched) - ok})


@app.route("/api/stock/inbound", methods=["POST"])
def api_stock_inbound():
    """确认入库：接收 [{item_type, item_id, qty, unit_price, supplier, note}] 列表
    unit_price 有值时按加权平均更新 cost，并记 purchases 采购记录。
    """
    data = request.get_json(force=True)
    items = data.get("items", [])
    reason = data.get("reason", "进货入库")
    if not items:
        return jsonify({"ok": False, "msg": "无商品"}), 400
    db = g.db
    done = 0
    for it in items:
        iid = it.get("item_id")
        itype = it.get("item_type", "ingredient")
        supplier = it.get("supplier", "")
        note = it.get("note", "") or reason
        # ---- 采购口径换算 ----
        # 界面上用户填的是"买了几公斤 × 一公斤多少钱"，这里换算成基础单位（克/个）再入库。
        # 传法二选一：{purchase_qty, purchase_unit, purchase_factor, unit_price(元/采购单位)}
        #            或直接 {qty, unit_price}（基础单位，老调用方）
        p_qty = it.get("purchase_qty")
        p_unit = (it.get("purchase_unit") or "").strip() or None
        p_factor = None
        try:
            p_factor = float(it.get("purchase_factor")) if it.get("purchase_factor") not in (None, "") else None
        except (TypeError, ValueError):
            p_factor = None
        unit_price_raw = float(it.get("unit_price", 0) or 0)
        if p_qty not in (None, "") and float(p_qty or 0) > 0:
            f = p_factor or 1
            if f <= 0:
                f = 1
            qty = round(float(p_qty) * f, 4)
            unit_price = round(unit_price_raw / f, 6) if unit_price_raw > 0 else 0
        else:
            qty = float(it.get("qty", 0))
            unit_price = unit_price_raw
            p_qty, p_unit = None, None
        if not iid or qty <= 0:
            continue
        table = "ingredients" if itype == "ingredient" else "tools"
        # 成本更新规则（用户定制）：
        #   涨价（新单价 > 旧成本）→ 直接用新单价（保守，配餐不亏本）
        #   跌价或持平（新单价 ≤ 旧成本）→ 加权平均（平滑波动）
        if unit_price > 0:
            row = db.execute(f"SELECT stock, cost FROM {table} WHERE id = ?", (iid,)).fetchone()
            if row:
                old_stock = row["stock"] or 0
                old_cost = row["cost"] or 0
                new_stock = old_stock + qty
                if new_stock > 0:
                    # 加权平均（用于库存价值核算）
                    weighted_cost = round((old_stock * old_cost + qty * unit_price) / new_stock, 4)
                    # 涨价用新价，跌价/持平用加权
                    if unit_price > old_cost and old_cost > 0:
                        new_cost = round(unit_price, 4)  # 涨价→新价
                    else:
                        new_cost = weighted_cost        # 跌价/持平→加权
                    db.execute(f"UPDATE {table} SET stock = stock + ?, cost = ? WHERE id = ?", (qty, new_cost, iid))
                else:
                    db.execute(f"UPDATE {table} SET stock = stock + ? WHERE id = ?", (qty, iid))
            else:
                db.execute(f"UPDATE {table} SET stock = stock + ? WHERE id = ?", (qty, iid))
        else:
            db.execute(f"UPDATE {table} SET stock = stock + ? WHERE id = ?", (qty, iid))
        # 库存日志
        db.execute("""
            INSERT INTO stock_logs (item_type, item_id, delta, reason)
            VALUES (?,?,?,?)
        """, (itype, iid, qty, reason))
        # 采购记录（有单价才记）
        if unit_price > 0:
            db.execute("""
                INSERT INTO purchases (item_type, item_id, quantity, unit_price, total_cost,
                                       supplier, note, purchase_qty, purchase_unit)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (itype, iid, qty, unit_price, round(qty * unit_price, 2), supplier, note, p_qty, p_unit))
            # 顺便记住这个食材的采购口径（下次默认带出来）
            if itype == "ingredient" and p_unit:
                db.execute("UPDATE ingredients SET purchase_unit=?, purchase_factor=? WHERE id=?",
                           (p_unit, p_factor or 1, iid))
        done += 1
    db.commit()
    return jsonify({"ok": True, "done": done})


@app.route("/api/purchases")
def list_purchases():
    """采购记录列表"""
    db = g.db
    rows = db.execute("""
        SELECT p.*, CASE WHEN p.item_type='ingredient' THEN i.name ELSE t.name END as item_name,
               CASE WHEN p.item_type='ingredient' THEN i.unit ELSE '个' END as item_unit
        FROM purchases p
        LEFT JOIN ingredients i ON p.item_id = i.id
        LEFT JOIN tools t ON p.item_id = t.id
        ORDER BY p.purchased_at DESC
        LIMIT 200
    """).fetchall()
    return jsonify({"ok": True, "data": [dict(r) for r in rows]})


# ================= 库存管理（总览 / 流水 / 出库 / 盘点） =================

def _stock_status(stock, thr):
    """库存分档：out=缺货(≤0)，low=低库存(≤阈值)，ok=正常"""
    stock = float(stock or 0)
    thr = float(thr or 0)
    if stock <= 0:
        return "out"
    if thr > 0 and stock <= thr:
        return "low"
    return "ok"


@app.route("/api/stock/overview")
def stock_overview():
    """库存总览：KPI + 缺货/低库存清单 + 待出库订单 + 近7天出入库"""
    db = g.db
    cur = db.cursor()
    ings = [dict(r) for r in cur.execute(
        "SELECT id,name,unit,stock,threshold,cost,category FROM ingredients").fetchall()]
    tools = [dict(r) for r in cur.execute(
        "SELECT id,name,stock,threshold,cost FROM tools").fetchall()]
    # 装备的"剩余可用" = 总库存 − 未完成订单占用（占用由订单自动推导，无需人工登记）
    # 预警按"剩余可用"算，库存价值按总拥有量算
    from calculator import (get_available_tool_stock, get_tool_reserved,
                            list_reserving_orders, calc_order_tool_needs)
    _tavail = get_available_tool_stock()
    _tres = get_tool_reserved()
    for t in tools:
        total = float(t.get("stock") or 0)
        rsv = float(_tres.get(t["id"], 0))
        t["total_stock"] = total
        t["reserved"] = round(rsv, 4)
        t["loaned"] = round(rsv, 4)          # 兼容旧字段名
        t["stock"] = float(_tavail.get(t["id"], total))   # 展示与预警用"剩余可用"

    def summarize(items, val_key="stock"):
        out, low, val = [], [], 0.0
        for x in items:
            x["status"] = _stock_status(x.get("stock"), x.get("threshold"))
            val += float(x.get(val_key) or 0) * float(x.get("cost") or 0)
            if x["status"] == "out":
                out.append(x)
            elif x["status"] == "low":
                low.append(x)
        return {"count": len(items), "out": len(out), "low": len(low),
                "ok": len(items) - len(out) - len(low),
                "value": round(val, 2), "out_items": out[:50], "low_items": low[:50]}

    ing_s = summarize(ings)
    tool_s = summarize(tools, "total_stock")

    pending = [dict(r) for r in cur.execute("""
        SELECT o.id, o.booking_date, o.booking_time, o.address, o.contact_name, o.status
        FROM orders o
        WHERE o.deleted_at IS NULL AND o.status != 'cancelled'
          AND (o.stock_deducted_at IS NULL OR o.stock_deducted_at = '')
        ORDER BY o.booking_date, o.booking_time
        LIMIT 60
    """).fetchall()]
    deducted = cur.execute("""
        SELECT COUNT(*) FROM orders
        WHERE deleted_at IS NULL AND status != 'cancelled'
          AND stock_deducted_at IS NOT NULL AND stock_deducted_at != ''
    """).fetchone()[0]

    r = cur.execute("""SELECT COALESCE(SUM(CASE WHEN delta>0 THEN delta ELSE 0 END),0) as inq,
                              COALESCE(SUM(CASE WHEN delta<0 THEN -delta ELSE 0 END),0) as outq,
                              COUNT(*) as c
                       FROM stock_logs
                       WHERE created_at >= datetime('now','localtime','-7 days')""").fetchone()
    recent = {"in": round(r["inq"], 2), "out": round(r["outq"], 2), "count": r["c"]}

    # 在借未归还装备（户外装备要回收）
    loans = [dict(r) for r in cur.execute("""
        SELECT tl.id, tl.order_id, tl.tool_id, tl.quantity, tl.returned_qty, tl.lost_qty,
               tl.status, tl.created_at, t.name as tool_name,
               o.booking_date, o.booking_time, o.address, o.contact_name
        FROM tool_loans tl
        LEFT JOIN tools t ON tl.tool_id = t.id
        LEFT JOIN orders o ON tl.order_id = o.id
        WHERE tl.status IN ('borrowed','partial')
        ORDER BY tl.order_id DESC, tl.id
    """).fetchall()]
    loaned_total = 0.0
    for l in loans:
        l["outstanding"] = round(float(l["quantity"] or 0) - float(l["returned_qty"] or 0)
                                 - float(l["lost_qty"] or 0), 4)
        loaned_total += l["outstanding"]

    # 上次盘点距今天数（现阶段库存主要靠定期盘点校准）
    import datetime as _dt
    from calculator import today_cst
    _st = cur.execute("SELECT created_at FROM stock_logs WHERE ref='stocktake' "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    last_st = _st["created_at"] if _st else None
    days_st = None
    if last_st:
        try:
            days_st = (today_cst() - _dt.date.fromisoformat(last_st[:10])).days
        except Exception:
            days_st = None

    # 各"未完成订单"分别要用哪些装备（接新单时看这个 + 上面的剩余量）
    _tname = {t["id"]: t["name"] for t in tools}
    tool_plan = []
    for o in list_reserving_orders():
        tl = []
        for tid, info in calc_order_tool_needs(o["id"]).items():
            need = info.get("total") or 0
            if need <= 0:
                continue
            tl.append({"name": _tname.get(tid, "?"), "need": round(need, 2)})
        if tl:
            o["tools"] = sorted(tl, key=lambda x: -x["need"])
            tool_plan.append(o)

    return jsonify({"ok": True, "data": {
        "ingredients": ing_s, "tools": tool_s,
        "total_value": round(ing_s["value"] + tool_s["value"], 2),
        "pending_consume": pending, "deducted_count": deducted,
        "recent7": recent,
        "loans": loans, "loaned_total": round(loaned_total, 2),
        "loan_orders": len({l["order_id"] for l in loans}),
        "tool_plan": tool_plan,
        "last_stocktake": last_st, "days_since_stocktake": days_st,
    }})


@app.route("/api/stock/logs")
def stock_logs_api():
    """库存流水（最近 N 条，可按 item_type 过滤）"""
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 1000))
    itype = request.args.get("item_type") or ""
    sql = """
        SELECT l.*,
               CASE WHEN l.item_type='ingredient' THEN i.name ELSE t.name END as item_name,
               CASE WHEN l.item_type='ingredient' THEN i.unit ELSE '个' END as item_unit
        FROM stock_logs l
        LEFT JOIN ingredients i ON l.item_type='ingredient' AND l.item_id=i.id
        LEFT JOIN tools t ON l.item_type='tool' AND l.item_id=t.id
    """
    params = []
    if itype in ("ingredient", "tool"):
        sql += " WHERE l.item_type = ? "
        params.append(itype)
    sql += " ORDER BY l.id DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in g.db.execute(sql, params).fetchall()]
    return jsonify({"ok": True, "data": rows})


@app.route("/api/stock/order-needs/<int:oid>")
def stock_order_needs(oid):
    """预览某单出库会扣掉什么（不落库）"""
    from calculator import calc_order_requirements
    db = g.db
    o = db.execute("SELECT id,booking_date,booking_time,address,contact_name,status,"
                   "stock_deducted_at FROM orders WHERE id=? AND deleted_at IS NULL", (oid,)).fetchone()
    if not o:
        return jsonify({"ok": False, "msg": "订单不存在"}), 404
    req = calc_order_requirements(oid)
    items = []
    for it in req.get("ingredients", []):
        if float(it.get("need") or 0) <= 0:
            continue
        items.append({"item_type": "ingredient", "item_id": it["id"], "name": it["name"],
                      "need": round(float(it["need"]), 2), "unit": it.get("unit") or "",
                      "stock": it.get("stock", 0)})
    for it in req.get("tools", []):
        if float(it.get("need") or 0) <= 0:
            continue
        items.append({"item_type": "tool", "item_id": it["id"], "name": it["name"],
                      "need": round(float(it["need"]), 2), "unit": "个",
                      "stock": it.get("stock", 0)})
    return jsonify({"ok": True, "data": {"order": dict(o), "items": items}})


@app.route("/api/stock/consume", methods=["POST"])
def stock_consume():
    """按订单出库。
    食材/耗材 → 永久消耗：扣库存并记流水。
    装备/工具 → **不记账**（搭建与回收外包给第三方、粗放阶段不逐笔登记借出归还；
    装备数量以「定期盘点」为准），只在返回里列出该单需要的装备供参考。
    同一单重复调用默认拒绝（幂等保护）；要重出先撤销。"""
    data = request.get_json(force=True)
    oid = data.get("order_id")
    if not oid:
        return jsonify({"ok": False, "msg": "缺少订单号"}), 400
    db = g.db
    cur = db.cursor()
    o = cur.execute("SELECT * FROM orders WHERE id=? AND deleted_at IS NULL", (oid,)).fetchone()
    if not o:
        return jsonify({"ok": False, "msg": "订单不存在"}), 404
    if (o["stock_deducted_at"] or "").strip():
        return jsonify({"ok": False, "already": True,
                        "msg": "订单 #%s 已经出过库了（%s），如需重出请先撤销"
                               % (oid, o["stock_deducted_at"])}), 409

    from calculator import calc_order_requirements, get_available_tool_stock
    req = calc_order_requirements(int(oid))
    short = []

    # ---- 1) 食材/耗材：扣库存 + 记流水 ----
    consumed = 0
    for it in req.get("ingredients", []):
        need = round(float(it.get("need") or 0), 4)
        if need <= 0:
            continue
        row = cur.execute("SELECT stock FROM ingredients WHERE id=?", (it["id"],)).fetchone()
        if not row:
            continue
        avail = float(row["stock"] or 0)
        if avail < need:
            short.append({"kind": "ingredient", "name": it["name"], "need": need,
                          "stock": avail, "unit": it.get("unit") or ""})
        cur.execute("UPDATE ingredients SET stock = stock - ? WHERE id=?", (need, it["id"]))
        cur.execute("INSERT INTO stock_logs (item_type,item_id,delta,reason,ref) "
                    "VALUES (?,?,?,?,?)",
                    ("ingredient", it["id"], -need, "订单#%s 出餐消耗" % oid, "order:%s" % oid))
        consumed += 1

    # ---- 2) 装备/工具：不记账 ----
    # 现状（用户明确说明）：搭建与回收外包给第三方，粗放阶段不逐笔登记借出/归还，
    # 所以出库**不扣装备库存、也不建借出挂账**（挂了也没人去清，还会让可用量假性下降、
    # 触发假预警）。装备数量以「定期盘点」为准；这里只列出该单需要的装备供人工参考。
    avail_map = get_available_tool_stock()
    gears = []
    for it in req.get("tools", []):
        need = round(float(it.get("need") or 0), 4)
        if need <= 0:
            continue
        a = avail_map.get(it["id"], 0)
        gears.append({"name": it["name"], "need": need, "stock": a, "unit": "个"})
        if a < need:
            short.append({"kind": "tool", "name": it["name"], "need": need,
                          "stock": a, "unit": "个"})

    cur.execute("UPDATE orders SET stock_deducted_at=datetime('now','localtime') WHERE id=?", (oid,))
    db.commit()
    return jsonify({"ok": True, "consumed": consumed, "loaned": 0, "gears": gears,
                    "moved": consumed, "short": short})


@app.route("/api/stock/consume/undo", methods=["POST"])
def stock_consume_undo():
    """撤销某单的出库：
    食材 → 逐条反向并把流水删掉；
    工具 → 移除该单**尚未归还**的借出登记（已归还/已丢失的不动，避免把账搞乱）。"""
    data = request.get_json(force=True)
    oid = data.get("order_id")
    if not oid:
        return jsonify({"ok": False, "msg": "缺少订单号"}), 400
    db = g.db
    cur = db.cursor()
    ref = "order:%s" % oid
    rows = cur.execute("SELECT * FROM stock_logs WHERE ref=?", (ref,)).fetchall()
    loans = cur.execute("SELECT * FROM tool_loans WHERE order_id=?", (oid,)).fetchall()
    if not rows and not loans:
        return jsonify({"ok": False, "msg": "该单没有出库/借出记录"}), 404

    for r in rows:
        if r["item_type"] == "ingredient":
            cur.execute("UPDATE ingredients SET stock = stock - ? WHERE id=?",
                        (r["delta"], r["item_id"]))
    cur.execute("DELETE FROM stock_logs WHERE ref=?", (ref,))

    removed, kept = 0, 0
    for l in loans:
        if (l["returned_qty"] or 0) == 0 and (l["lost_qty"] or 0) == 0:
            cur.execute("DELETE FROM tool_loans WHERE id=?", (l["id"],))
            removed += 1
        else:
            kept += 1
    cur.execute("UPDATE orders SET stock_deducted_at=NULL WHERE id=?", (oid,))
    db.commit()
    return jsonify({"ok": True, "reverted": len(rows), "loans_removed": removed,
                    "loans_kept": kept})


@app.route("/api/stock/loans")
def stock_loans_api():
    """在借未归还的装备清单（天幕/桌椅/卡式炉等要回收）"""
    rows = g.db.execute("""
        SELECT tl.*, t.name as tool_name, t.cost,
               o.booking_date, o.booking_time, o.address, o.contact_name
        FROM tool_loans tl
        LEFT JOIN tools t ON tl.tool_id = t.id
        LEFT JOIN orders o ON tl.order_id = o.id
        WHERE tl.status IN ('borrowed','partial')
        ORDER BY tl.order_id DESC, tl.id
    """).fetchall()
    return jsonify({"ok": True, "data": [dict(r) for r in rows]})


@app.route("/api/stock/loans/return-all", methods=["POST"])
def stock_loans_return_all():
    """一键归还某单的全部未还装备。
    注意：借出时**没有扣总库存**，所以归还只需要把借出登记置为已归还，
    可用量会自动恢复（总库存不动）。"""
    data = request.get_json(force=True)
    oid = data.get("order_id")
    if not oid:
        return jsonify({"ok": False, "msg": "缺少订单号"}), 400
    db = g.db
    cur = db.cursor()
    rows = cur.execute("SELECT * FROM tool_loans WHERE order_id=? AND status IN ('borrowed','partial')",
                       (oid,)).fetchall()
    if not rows:
        return jsonify({"ok": False, "msg": "该单没有待归还的装备"}), 404
    n = 0
    for l in rows:
        qty = float(l["quantity"] or 0)
        returned = float(l["returned_qty"] or 0)
        lost = float(l["lost_qty"] or 0)
        rest = qty - returned - lost
        if rest <= 0:
            continue
        cur.execute("UPDATE tool_loans SET returned_qty=?, status='returned', "
                    "returned_at=datetime('now','localtime') WHERE id=?",
                    (returned + rest, l["id"]))
        n += 1
    db.commit()
    return jsonify({"ok": True, "returned": n})


@app.route("/api/stock/stocktake", methods=["POST"])
def stock_stocktake():
    """盘点：传每项实际数量，差额自动调整库存并记流水。
    请求体：{"items":[{"item_type":"ingredient","item_id":1,"actual":1234.5}, ...]}"""
    data = request.get_json(force=True)
    items = data.get("items") or []
    if not items:
        return jsonify({"ok": False, "msg": "没有盘点数据"}), 400
    db = g.db
    cur = db.cursor()
    changed = []
    for it in items:
        itype = it.get("item_type")
        iid = it.get("item_id")
        if itype not in ("ingredient", "tool") or not iid:
            continue
        try:
            actual = float(it.get("actual"))
        except (TypeError, ValueError):
            continue
        table = "ingredients" if itype == "ingredient" else "tools"
        row = cur.execute("SELECT name, stock FROM %s WHERE id=?" % table, (iid,)).fetchone()
        if not row:
            continue
        before = float(row["stock"] or 0)
        delta = round(actual - before, 4)
        if abs(delta) < 0.0001:
            continue
        cur.execute("UPDATE %s SET stock=? WHERE id=?" % table, (actual, iid))
        cur.execute("INSERT INTO stock_logs (item_type,item_id,delta,reason,ref) "
                    "VALUES (?,?,?,?,?)",
                    (itype, iid, delta, "盘点调整", "stocktake"))
        changed.append({"name": row["name"], "before": before, "after": actual, "delta": delta})
    db.commit()
    return jsonify({"ok": True, "changed": changed, "count": len(changed)})


@app.route("/api/ingredients/<int:iid>/cost", methods=["POST"])
def update_ingredient_cost(iid):
    """修改食材成本单价。两种传法都支持：

    · 按**采购单位**（推荐，用户在界面上填的就是这个）：
        {purchase_price: 75, purchase_unit: '公斤', purchase_factor: 1000}
        → 后端换算成基础单位成本（75 ÷ 1000 = 0.075 元/克）
    · 按**基础单位**（老调用方兼容）：{cost: 0.075}
    另可附带 portion_grams（每份克数）。
    """
    data = request.get_json(force=True)
    itype = data.get("item_type", "ingredient")
    db = g.db
    if itype != "ingredient":
        db.execute("UPDATE tools SET cost = ? WHERE id = ?", (float(data.get("cost") or 0), iid))
        db.commit()
        return jsonify({"ok": True})

    row = db.execute("SELECT * FROM ingredients WHERE id=?", (iid,)).fetchone()
    if not row:
        return jsonify({"ok": False, "msg": "食材不存在"}), 404
    cur = dict(row)

    def _num(v, default=None):
        try:
            if v in (None, ""):
                return default
            return float(v)
        except (TypeError, ValueError):
            return default

    factor = _num(data.get("purchase_factor"), _num(cur.get("purchase_factor"), 1)) or 1
    if factor <= 0:
        factor = 1
    punit = (data.get("purchase_unit") or cur.get("purchase_unit") or cur.get("unit") or "").strip()
    pprice = _num(data.get("purchase_price"))
    if pprice is not None:
        cost = pprice / factor
    else:
        cost = _num(data.get("cost"), _num(cur.get("cost"), 0)) or 0
    sets, vals = ["cost=?", "purchase_unit=?", "purchase_factor=?"], [round(cost, 6), punit, factor]
    pg = _num(data.get("portion_grams"))
    if pg is not None and pg > 0:
        sets.append("portion_grams=?")
        vals.append(pg)
    vals.append(iid)
    db.execute("UPDATE ingredients SET %s WHERE id=?" % ",".join(sets), vals)
    db.commit()
    return jsonify({"ok": True, "cost": round(cost, 6), "purchase_price": round(cost * factor, 4),
                    "purchase_unit": punit, "purchase_factor": factor})


# ===== 配置管理 =====
@app.route("/api/ingredients", methods=["GET", "POST"])
def manage_ingredients():
    db = g.db
    if request.method == "GET":
        rows = db.execute("SELECT * FROM ingredients ORDER BY id").fetchall()
        items = [dict(r) for r in rows]
        # meat 细分 + 固定排序（复用 calculator.sort_ingredients）
        from calculator import sort_ingredients
        items = sort_ingredients(items)
        return jsonify({"ok": True, "data": items})
    data = request.get_json(force=True)
    unit = data.get("unit", "")
    from models import purchase_defaults
    dpu, dpf = purchase_defaults(unit)
    pu = (data.get("purchase_unit") or "").strip() or dpu
    try:
        pf = float(data.get("purchase_factor"))
    except (TypeError, ValueError):
        pf = dpf
    if not pf or pf <= 0:
        pf = dpf
    db.execute("""
        INSERT INTO ingredients (name, unit, stock, threshold, cost, category,
                                 purchase_unit, purchase_factor)
        VALUES (?,?,?,?,?,?,?,?)
    """, (data["name"], unit, data.get("stock", 0),
          data.get("threshold", 0), data.get("cost", 0), data.get("category", "other"),
          pu, pf))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/ingredients/<int:iid>", methods=["PUT"])
def update_ingredient(iid):
    data = request.get_json(force=True)
    db = g.db
    # 支持部分更新：只更新传入的字段（避免未传字段被置空）
    cur = db.execute("SELECT * FROM ingredients WHERE id=?", (iid,))
    existing = cur.fetchone()
    if not existing:
        return jsonify({"ok": False, "msg": "not found"}), 404
    existing = dict(existing)
    fields = ["name", "unit", "stock", "threshold", "cost", "category"]
    updates = {}
    for f in fields:
        if f in data:
            updates[f] = data[f]
        else:
            updates[f] = existing[f]
    db.execute("""
        UPDATE ingredients SET name=?, unit=?, stock=?, threshold=?, cost=?, category=?
        WHERE id=?
    """, (updates["name"], updates["unit"], updates["stock"], updates["threshold"],
          updates["cost"], updates["category"], iid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/ingredients/<int:iid>", methods=["DELETE"])
def del_ingredient(iid):
    db = g.db
    db.execute("DELETE FROM ingredients WHERE id=?", (iid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/tools", methods=["GET", "POST"])
def manage_tools():
    db = g.db
    if request.method == "GET":
        rows = db.execute("SELECT * FROM tools").fetchall()
        from calculator import sort_tools, get_available_tool_stock, get_tool_reserved
        avail = get_available_tool_stock()
        reserved = get_tool_reserved()
        items = []
        for r in rows:
            d = dict(r)
            total = d.get("stock") or 0
            rsv = round(float(reserved.get(d["id"], 0)), 4)
            d["total_stock"] = total
            d["avail"] = avail.get(d["id"], total)
            d["reserved"] = rsv      # 被"未完成订单"占用的数量（由订单自动推导，无需登记）
            d["loaned"] = rsv        # 兼容旧字段名
            items.append(d)
        return jsonify({"ok": True, "data": sort_tools(items)})
    data = request.get_json(force=True)
    db.execute("INSERT INTO tools (name, stock, threshold, cost) VALUES (?,?,?,?)",
              (data["name"], data.get("stock", 0), data.get("threshold", 0), data.get("cost", 0)))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/tools/<int:tid>", methods=["PUT", "DELETE"])
def update_tool(tid):
    db = g.db
    if request.method == "DELETE":
        db.execute("DELETE FROM tools WHERE id=?", (tid,))
    else:
        data = request.get_json(force=True)
        db.execute("UPDATE tools SET name=?, stock=?, threshold=? WHERE id=?",
                  (data["name"], data.get("stock", 0), data.get("threshold", 0), tid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/packages", methods=["GET", "POST"])
def manage_packages():
    db = g.db
    if request.method == "GET":
        cur = db.cursor()
        cur.execute("SELECT * FROM packages ORDER BY min_people")
        pkgs = [dict(r) for r in cur.fetchall()]
        for p in pkgs:
            cur.execute("""
                SELECT pi.id, pi.per_package, pi.portion_count,
                       i.name as ing_name, i.id as ing_id, i.unit, i.category
                FROM package_ingredients pi
                JOIN ingredients i ON pi.ingredient_id = i.id
                WHERE pi.package_id = ? AND pi.cost_only = 0
                ORDER BY i.category, i.name
            """, (p["id"],))
            p["ingredients"] = [dict(r) for r in cur.fetchall()]
            cur.execute("""
                SELECT pt.id, pt.per_package, t.name as tool_name, t.id as tool_id
                FROM package_tools pt
                JOIN tools t ON pt.tool_id = t.id
                WHERE pt.package_id = ?
            """, (p["id"],))
            from calculator import sort_tools as _st
            p["tools"] = _st([dict(r) for r in cur.fetchall()], "per_package", "tool_name")
        return jsonify({"ok": True, "data": pkgs})

    data = request.get_json(force=True)
    cur = db.cursor()
    # 新规则：接收 base_price（菜品价），price 由 base_price + delivery_fee 自动算
    base_price = float(data.get("base_price", data.get("price", 0)))
    df_row = db.execute("SELECT CAST(value AS REAL) as df FROM settings WHERE key='delivery_fee'").fetchone()
    df = df_row["df"] if df_row else 0
    total_price = base_price + df
    cur.execute("""
        INSERT INTO packages (name, min_people, max_people, base_price, price, service_type, is_team)
        VALUES (?,?,?,?,?,?,?)
    """, (data["name"], data["min_people"], data["max_people"],
          base_price, total_price, data.get("service_type", "外送"),
          data.get("is_team", 0)))
    pid = cur.lastrowid
    # 关联食材
    for ing in data.get("ingredients", []):
        cur.execute("""
            INSERT INTO package_ingredients (package_id, ingredient_id, per_package, portion_count)
            VALUES (?,?,?,?)
        """, (pid, ing["ingredient_id"], ing["per_package"], ing.get("portion_count", 1)))
    # 关联工具
    for t in data.get("tools", []):
        cur.execute("""
            INSERT INTO package_tools (package_id, tool_id, per_package)
            VALUES (?,?,?)
        """, (pid, t["tool_id"], t.get("per_package", 1)))
    db.commit()
    return jsonify({"ok": True, "id": pid})


@app.route("/api/packages/<int:pid>", methods=["DELETE"])
def del_package(pid):
    db = g.db
    db.execute("DELETE FROM packages WHERE id=?", (pid,))
    db.commit()
    return jsonify({"ok": True})


# ===== 餐标配餐器 =====
@app.route("/api/catering/suggest", methods=["POST"])
def catering_suggest():
    """餐标配餐器：输入人数+餐标+人工成本，返回建议套餐方案"""
    from calculator import catering_suggest as _cs
    data = request.get_json(force=True)
    people = int(data.get("people", 0))
    total = float(data.get("total_budget", 0))
    per_person = data.get("per_person")
    kitchen_labor = float(data.get("kitchen_labor", 0))
    delivery_labor = float(data.get("delivery_labor", 0))
    if per_person is not None:
        per_person = float(per_person)
    result = _cs(people, total, per_person, kitchen_labor, delivery_labor)
    return jsonify(result)


@app.route("/api/catering/save", methods=["POST"])
def catering_save():
    """把配餐方案保存为定制套餐"""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    people = int(data.get("people", 0))
    total = float(data.get("total_budget", 0))
    kitchen_labor = float(data.get("kitchen_labor", 0))
    delivery_labor = float(data.get("delivery_labor", 0))
    ings = data.get("ingredients", [])
    tools = data.get("tools", [])
    if not name or people <= 0:
        return jsonify({"ok": False, "msg": "套餐名和人数必填"}), 400

    db = g.db
    cur = db.cursor()
    # base_price = 餐标 - 配送费
    df_row = db.execute("SELECT CAST(value AS REAL) as df FROM settings WHERE key='delivery_fee'").fetchone()
    df = df_row["df"] if df_row else 0
    base_price = max(0, total - df)
    total_price = base_price + df
    cur.execute("""
        INSERT INTO packages (name, min_people, max_people, base_price, price, service_type, is_team, kitchen_labor_cost, delivery_labor_cost)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (name, people, people, base_price, total_price, "搭建", 0, kitchen_labor, delivery_labor))
    pid = cur.lastrowid
    for ing in ings:
        cur.execute("""
            INSERT INTO package_ingredients (package_id, ingredient_id, per_package, portion_count, cost_only)
            VALUES (?,?,?,?,?)
        """, (pid, ing["ing_id"], ing["per_package"],
              ing.get("portion_count", 1), ing.get("cost_only", 0)))
    for t in tools:
        cur.execute("""
            INSERT INTO package_tools (package_id, tool_id, per_package)
            VALUES (?,?,?)
        """, (pid, t["id"], t.get("per_package", 1)))
    db.commit()
    return jsonify({"ok": True, "id": pid})


# 修改套餐里某食材的配量(每份克数 + 份数)
@app.route("/api/packages/<int:pid>/ingredient/<int:pi_id>", methods=["PUT", "DELETE"])
def update_pkg_ingredient(pid, pi_id):
    db = g.db
    cur = db.cursor()
    if request.method == "DELETE":
        cur.execute("DELETE FROM package_ingredients WHERE id=? AND package_id=?", (pi_id, pid))
        db.commit()
        return jsonify({"ok": True})
    data = request.get_json(force=True)
    size = float(data.get("portion_size", 0))
    count = float(data.get("portion_count", 1))
    total = size * count
    cur.execute("""
        UPDATE package_ingredients SET per_package=?, portion_count=?
        WHERE id=? AND package_id=?
    """, (total, count, pi_id, pid))
    db.commit()
    return jsonify({"ok": True, "per_package": total, "portion_count": count})


# 新增套餐食材
@app.route("/api/packages/<int:pid>/ingredient", methods=["POST"])
def add_pkg_ingredient(pid):
    db = g.db
    cur = db.cursor()
    data = request.get_json(force=True)
    ing_id = data.get("ingredient_id")
    size = float(data.get("portion_size", 0))
    count = float(data.get("portion_count", 1))
    total = size * count
    if not ing_id:
        return jsonify({"ok": False, "msg": "缺少食材ID"}), 400
    cur.execute("SELECT id FROM package_ingredients WHERE package_id=? AND ingredient_id=?", (pid, ing_id))
    if cur.fetchone():
        return jsonify({"ok": False, "msg": "该食材已存在，请直接修改"}), 400
    cur.execute("""
        INSERT INTO package_ingredients (package_id, ingredient_id, per_package, portion_count)
        VALUES (?,?,?,?)
    """, (pid, ing_id, total, count))
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


# 修改套餐工具配量
@app.route("/api/packages/<int:pid>/tool/<int:pt_id>", methods=["PUT", "DELETE"])
def update_pkg_tool(pid, pt_id):
    db = g.db
    cur = db.cursor()
    if request.method == "DELETE":
        cur.execute("DELETE FROM package_tools WHERE id=? AND package_id=?", (pt_id, pid))
        db.commit()
        return jsonify({"ok": True})
    data = request.get_json(force=True)
    per = float(data.get("per_package", 1))
    cur.execute("UPDATE package_tools SET per_package=? WHERE id=? AND package_id=?", (per, pt_id, pid))
    db.commit()
    return jsonify({"ok": True})


# 导出菜单原数据(JSON / CSV / Excel)
@app.route("/api/menu/export")
def export_menu():
    import csv, io, json as _json
    fmt = request.args.get("format", "json")
    pid = request.args.get("pid")  # 单套餐导出
    cur = g.db.cursor()
    sql = "SELECT * FROM packages ORDER BY min_people"
    params = ()
    if pid:
        sql = "SELECT * FROM packages WHERE id=? ORDER BY min_people"
        params = (int(pid),)
    cur.execute(sql, params)
    pkgs = [dict(r) for r in cur.fetchall()]
    for p in pkgs:
        cur.execute("""
            SELECT i.name as ingredient, i.unit, i.category, pi.per_package, pi.portion_count
            FROM package_ingredients pi
            JOIN ingredients i ON pi.ingredient_id = i.id
            WHERE pi.package_id = ?
            ORDER BY i.category, i.name
        """, (p["id"],))
        p["ingredients"] = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT t.name as tool, pt.per_package
            FROM package_tools pt
            JOIN tools t ON pt.tool_id = t.id
            WHERE pt.package_id = ?
        """, (p["id"],))
        from calculator import sort_tools as _st
        p["tools"] = _st([dict(r) for r in cur.fetchall()], "per_package", "tool")

    fname = f"menu_{pkgs[0]['name']}" if len(pkgs)==1 else "menu_all"

    if fmt == "csv":
        buf = io.StringIO()
        buf.write("\ufeff")  # BOM for Excel
        w = csv.writer(buf)
        w.writerow(["套餐", "人数范围", "类别", "食材/工具", "单位", "每份克数", "份数", "总量", "基价", "总价"])
        for p in pkgs:
            people = f"{p['min_people']}-{p['max_people']}人"
            for ing in p["ingredients"]:
                size = ing["per_package"] / max(1, ing["portion_count"])
                w.writerow([p["name"], people, ing["category"], ing["ingredient"],
                           ing["unit"], round(size, 2), ing["portion_count"],
                           ing["per_package"], p["base_price"], p["price"]])
            for t in p["tools"]:
                w.writerow([p["name"], people, "tool", t["tool"], "个", "", "",
                            t["per_package"], p["base_price"], p["price"]])
        resp = app.response_class(buf.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = f"attachment; filename={fname}.csv"
        return resp
    return jsonify({"ok": True, "data": pkgs})


# 导出 Excel(xlsx)
@app.route("/api/menu/export/xlsx")
def export_menu_xlsx():
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return jsonify({"ok": False, "msg": "需要 openpyxl: pip install openpyxl"}), 500
    from calculator import (
        CATEGORY_ORDER, CATEGORY_LABEL, _subcategorize_meat,
        PACKAGING_CATEGORY, UTENSIL_CATEGORY, sort_tools, _get_fixed_sort_index
    )
    pid = request.args.get("pid")
    cur = g.db.cursor()
    if pid:
        cur.execute("SELECT * FROM packages WHERE id=?", (int(pid),))
    else:
        cur.execute("SELECT * FROM packages ORDER BY min_people")
    pkgs = [dict(r) for r in cur.fetchall()]
    for p in pkgs:
        cur.execute("""
            SELECT i.name as ingredient, i.unit, i.category, pi.per_package, pi.portion_count
            FROM package_ingredients pi
            JOIN ingredients i ON pi.ingredient_id = i.id
            WHERE pi.package_id = ? AND pi.cost_only = 0
        """, (p["id"],))
        p["ingredients"] = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT t.name as tool, pt.per_package
            FROM package_tools pt
            JOIN tools t ON pt.tool_id = t.id
            WHERE pt.package_id = ?
        """, (p["id"],))
        p["tools"] = sort_tools([dict(r) for r in cur.fetchall()], "per_package", "tool")

    # 与网页菜单完全一致的分类顺序与标签
    # 食材分类顺序：beef→pork→chicken→vegetable→side→sauce→drink→staple→packaging→utensil→other
    # 工具放最后
    EXPORT_CAT_ORDER = [
        "beef", "pork", "chicken", "vegetable", "side", "sauce", "drink",
        "staple", "packaging", "utensil", "other", "tool"
    ]
    cat_colors = {
        'beef': '8E1E1A', 'pork': 'C75D3E', 'chicken': 'C9962B',
        'vegetable': '3A8A3A', 'side': '5A8A3A', 'sauce': '7A5A3A',
        'drink': '666666', 'staple': '666666',
        'packaging': 'D35400', 'utensil': '8E44AD', 'other': '666666',
        'tool': '4A4A4A'
    }
    cat_labels = {
        'beef': '🥩 牛肉', 'pork': '🥓 猪肉', 'chicken': '🍗 鸡肉',
        'vegetable': '🥬 素菜', 'side': '🥗 小菜', 'sauce': '🧂 小料',
        'drink': '🎁 赠品', 'staple': '🍚 主食',
        'packaging': '📦 食材包装', 'utensil': '🍱 客户餐具',
        'other': '📦 其他', 'tool': '🔧 工具'
    }

    # 食材内部固定排序（与网页 FIXED_ORDER 一致）
    ing_fixed_order = [
        ['beef','肥牛'],['beef','拌牛肉'],['beef','牛肋条'],['beef','牛骰子'],['beef','小肠'],
        ['pork','五花肉'],['pork','小香猪'],['pork','松板肉'],['pork','风味肠'],['pork','梅花肉'],
        ['chicken','鸡尖'],['chicken','郡肝'],['chicken','鸡腿肉'],['chicken','鸡翅根'],
        ['chicken','掌中宝'],['chicken','鸡脚筋'],
        ['vegetable','生菜'],['vegetable','豆腐'],['vegetable','西葫芦'],
        ['vegetable','土豆'],['vegetable','馒头'],['vegetable','韭菜'],
        ['vegetable','杏鲍菇'],['vegetable','洋葱'],
        ['side','海带丝'],['side','辣椒段'],['side','辣白菜'],['side','蒜片'],
        ['sauce','川香'],['sauce','五香'],['sauce','酸辣'],
        ['drink','应季水果'],['drink','可乐'],['drink','雪碧'],
    ]
    def ing_sort_idx(name, cat):
        for i, (c, kw) in enumerate(ing_fixed_order):
            if c == cat and kw in (name or ''):
                return i
        return 999

    wb = Workbook()
    ws = wb.active
    ws.title = "菜单" if len(pkgs) > 1 else pkgs[0]["name"]
    # 与网页一致的表头（名称→份数→每份数量→单位→总量→保存→删除）
    headers = ["名称", "份数", "每份数量", "单位", "总量", "保存", "删除"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF", size=11)
        cell.fill = PatternFill("solid", fgColor="3A2416")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = Border(
            left=Side(border_style="thin", color="FFFFFF"),
            right=Side(border_style="thin", color="FFFFFF"),
            top=Side(border_style="thin", color="FFFFFF"),
            bottom=Side(border_style="thin", color="FFFFFF"),
        )
    thin = Side(border_style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center_align = Alignment(horizontal="center", vertical="center")
    left_align = Alignment(horizontal="left", vertical="center", indent=1)

    for p in pkgs:
        # 套餐标题行（合并 7 列）
        ws.append([f"{p['name']}  ·  {p['min_people']}-{p['max_people']}人  ·  基价¥{p['base_price']}  总价¥{p['price']}", "", "", "", "", "", ""])
        ws.merge_cells(start_row=ws.max_row, start_column=1, end_row=ws.max_row, end_column=7)
        for cell in ws[ws.max_row]:
            cell.fill = PatternFill("solid", fgColor="8E1E1A")
            cell.font = Font(bold=True, color="FFFFFF", size=12)
            cell.alignment = Alignment(horizontal="center", vertical="center")

        # 食材按分类细分 + 固定排序
        by_cat = {}
        for ing in p["ingredients"]:
            raw_cat = ing["category"] or "other"
            # meat 细分为 beef/pork/chicken（与网页 subcategorize 一致）
            cat = _subcategorize_meat(ing["ingredient"], raw_cat)
            by_cat.setdefault(cat, []).append(ing)
        for cat in EXPORT_CAT_ORDER:
            items = by_cat.get(cat, [])
            if not items:
                continue
            # 分类内按固定顺序排序
            items.sort(key=lambda x: (ing_sort_idx(x["ingredient"], cat), x["ingredient"]))
            color = cat_colors.get(cat, "666666")
            label = cat_labels.get(cat, cat)
            # 分类标题行（与网页 cat-row 一致，本身是 7 列）
            ws.append([f"{label} · {len(items)}项", "份数", "每份数量", "单位", "总量", "保存", "删除"])
            for cell in ws[ws.max_row]:
                cell.fill = PatternFill("solid", fgColor=color)
                cell.font = Font(bold=True, color="FFFFFF", size=10)
                cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.cell(ws.max_row, 1).alignment = Alignment(horizontal="left", vertical="center", indent=1)
            # 食材行（列序：名称→份数→每份数量→单位→总量→空→空）
            for ing in items:
                size = ing["per_package"] / max(1, ing["portion_count"])
                ws.append([
                    ing["ingredient"], ing["portion_count"], round(size, 2),
                    ing["unit"], ing["per_package"], "", ""
                ])
                for cell in ws[ws.max_row]:
                    cell.border = border
                    cell.alignment = center_align
                ws.cell(ws.max_row, 1).alignment = left_align
                ws.cell(ws.max_row, 5).font = Font(bold=True, color="8E1E1A")
        # 工具分类
        if p["tools"]:
            ws.append([f"🔧 工具 · {len(p['tools'])}项", "份数", "每份数量", "单位", "总量", "保存", "删除"])
            for cell in ws[ws.max_row]:
                cell.fill = PatternFill("solid", fgColor="4A4A4A")
                cell.font = Font(bold=True, color="FFFFFF", size=10)
                cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.cell(ws.max_row, 1).alignment = Alignment(horizontal="left", vertical="center", indent=1)
            for t in p["tools"]:
                ws.append([
                    t["tool"], 1, t["per_package"], "个", t["per_package"], "", ""
                ])
                for cell in ws[ws.max_row]:
                    cell.border = border
                    cell.alignment = center_align
                ws.cell(ws.max_row, 1).alignment = left_align
                ws.cell(ws.max_row, 5).font = Font(bold=True, color="4A4A4A")
        # 套餐间空行
        ws.append([])
    # 列宽（与网页列对齐）
    widths = [22, 8, 12, 8, 10, 8, 8]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    # 冻结表头
    ws.freeze_panes = "A2"
    import io as _io
    buf = _io.BytesIO()
    wb.save(buf)
    fname = f"menu_{pkgs[0]['name']}" if len(pkgs)==1 else "menu_all"
    resp = app.response_class(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp.headers["Content-Disposition"] = f"attachment; filename={fname}.xlsx"
    return resp


@app.route("/api/dishes", methods=["GET", "POST"])
def manage_dishes():
    db = g.db
    if request.method == "GET":
        rows = db.execute("SELECT * FROM dishes ORDER BY id").fetchall()
        return jsonify({"ok": True, "data": [dict(r) for r in rows]})
    data = request.get_json(force=True)
    db.execute("INSERT INTO dishes (name, price, cost) VALUES (?,?,?)",
              (data["name"], data.get("price", 0), data.get("cost", 0)))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/dishes/<int:did>", methods=["DELETE"])
def del_dish(did):
    db = g.db
    db.execute("DELETE FROM dishes WHERE id=?", (did,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/price", methods=["POST"])
def calc_price():
    """按65%毛利计算售价：售价 = 成本 / (1 - 0.65)，向上取整"""
    data = request.get_json(force=True)
    cost = float(data.get("cost", 0))
    margin = float(data.get("margin", 0.65))
    if margin >= 1:
        return jsonify({"ok": False, "msg": "毛利率不能>=100%"}), 400
    price = cost / (1 - margin)
    # 个位向上取整
    price_int = math.ceil(price)
    return jsonify({"ok": True, "cost": cost, "price": price_int, "raw_price": round(price, 2)})


# ===== 全局设置 =====
def get_setting(key, default=None):
    row = g.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def refresh_package_prices(db):
    """根据 settings.delivery_fee 同步所有 packages.price = base_price + delivery_fee"""
    row = db.execute("SELECT CAST(value AS REAL) as df FROM settings WHERE key='delivery_fee'").fetchone()
    df = row["df"] if row else 0
    db.execute("UPDATE packages SET price = base_price + ?", (df,))


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    rows = g.db.execute("SELECT * FROM settings ORDER BY key").fetchall()
    data = {r["key"]: {"value": r["value"], "note": r["note"]} for r in rows}
    # 顺便把每个套餐的 base_price / price / delivery 拆分也给前端
    pkgs = g.db.execute("SELECT id, name, base_price, price, price - base_price as delivery FROM packages ORDER BY min_people").fetchall()
    return jsonify({"ok": True, "settings": data, "packages": [dict(r) for r in pkgs]})


@app.route("/api/settings", methods=["POST"])
def api_update_settings():
    """设置多个 key=value，例：{"delivery_fee": "80"}"""
    data = request.get_json(force=True) or {}
    db = g.db
    for k, v in data.items():
        if k == "delivery_fee":
            # 强制数值化，脏输入过滤掉
            try:
                v = str(float(v))
            except (TypeError, ValueError):
                continue
        db.execute("INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (k, v))
    refresh_package_prices(db)
    db.commit()
    return jsonify({"ok": True})


# ===== 历史分析 =====
@app.route("/api/history")
def api_history():
    db = g.db
    # 按月聚合
    rows = db.execute("""
        SELECT strftime('%Y-%m', created_at) as month,
               COUNT(*) as cnt,
               COALESCE(SUM(CASE WHEN status!='cancelled' THEN amount ELSE 0 END),0) as revenue
        FROM orders WHERE deleted_at IS NULL GROUP BY strftime('%Y-%m', created_at)
        ORDER BY month DESC LIMIT 12
    """).fetchall()
    monthly = [dict(r) for r in rows]

    # 按周聚合（近 8 周）
    rows = db.execute("""
        SELECT strftime('%Y-W%W', created_at) as week,
               COUNT(*) as cnt,
               COALESCE(SUM(CASE WHEN status!='cancelled' THEN amount ELSE 0 END),0) as revenue
        FROM orders WHERE deleted_at IS NULL GROUP BY strftime('%Y-W%W', created_at)
        ORDER BY week DESC LIMIT 8
    """).fetchall()
    weekly = list(reversed([dict(r) for r in rows]))

    # 按套餐统计（所有订单中各套餐被点了多少次）
    rows = db.execute("""
        SELECT p.name as pkg_name,
               COUNT(*) as times,
               SUM(op.quantity) as total_qty
        FROM order_packages op
        JOIN orders o ON op.order_id = o.id
        JOIN packages p ON op.package_id = p.id
        WHERE o.status != 'cancelled' AND o.deleted_at IS NULL
        GROUP BY p.id
        ORDER BY total_qty DESC
    """).fetchall()
    packages_pop = [dict(r) for r in rows]

    # 状态分布
    rows = db.execute("""
        SELECT status, COUNT(*) as cnt FROM orders WHERE deleted_at IS NULL GROUP BY status
    """).fetchall()
    status_dist = {r["status"]: r["cnt"] for r in rows}

    # 总览
    rows = db.execute("""
        SELECT COUNT(*) as total,
               COALESCE(SUM(CASE WHEN status!='cancelled' THEN amount ELSE 0 END),0) as total_revenue,
               AVG(CASE WHEN status!='cancelled' THEN amount END) as avg_amount
        FROM orders WHERE deleted_at IS NULL
    """).fetchone()
    overview = dict(rows)

    return jsonify({"ok": True, "monthly": monthly, "weekly": weekly,
                    "packages_pop": packages_pop, "status_dist": status_dist, "overview": overview})


# ===== 财务系统 =====
@app.route("/api/finance/summary")
def finance_summary():
    """财务总览：收入、成本、毛利、押金、应收
    可选参数：date_from, date_to（YYYY-MM-DD），按 booking_date 过滤"""
    db = g.db
    cur = db.cursor()
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    # 构建日期过滤条件（用参数化，带 o. 前缀避免 JOIN 歧义）
    date_clause = "o.status != 'cancelled' AND o.deleted_at IS NULL"
    params = []
    if date_from:
        date_clause += " AND o.booking_date >= ?"
        params.append(date_from)
    if date_to:
        date_clause += " AND o.booking_date <= ?"
        params.append(date_to)
    # 收入（非取消订单）
    r = cur.execute(f"""
        SELECT COUNT(*) as order_cnt,
               COALESCE(SUM(amount),0) as total_revenue,
               COALESCE(SUM(deposit),0) as total_deposit,
               COALESCE(SUM(CASE WHEN payment_status='paid' THEN amount ELSE 0 END),0) as paid_amount,
               COALESCE(SUM(CASE WHEN payment_status='unpaid' THEN amount ELSE 0 END),0) as unpaid_amount,
               COALESCE(SUM(CASE WHEN payment_status='partial' THEN amount ELSE 0 END),0) as partial_amount,
               COALESCE(SUM(CASE WHEN deposit_status='pending' THEN deposit ELSE 0 END),0) as deposit_pending,
               COALESCE(SUM(CASE WHEN deposit_status='returned' THEN deposit ELSE 0 END),0) as deposit_returned,
               COALESCE(SUM(CASE WHEN deposit_status='forfeited' THEN deposit ELSE 0 END),0) as deposit_forfeited
        FROM orders o WHERE {date_clause}
    """, params).fetchone()
    summary = dict(r)
    # 成本计算：每单食材成本 = 各食材用量 × 单价
    r = cur.execute(f"""
        SELECT COALESCE(SUM(pi.per_package * op.quantity * i.cost),0) as food_cost
        FROM order_packages op
        JOIN orders o ON op.order_id = o.id
        JOIN package_ingredients pi ON pi.package_id = op.package_id
        JOIN ingredients i ON pi.ingredient_id = i.id
        WHERE {date_clause}
    """, params).fetchone()
    summary["food_cost"] = r["food_cost"] or 0
    # 工具损耗成本（丢失的工具 × 真实成本）
    r = cur.execute(f"""
        SELECT COALESCE(SUM(tl.lost_qty * t.cost),0) as tool_loss_cost
        FROM tool_loans tl
        JOIN orders o ON tl.order_id = o.id
        JOIN tools t ON tl.tool_id = t.id
        WHERE {date_clause}
    """, params).fetchone()
    summary["tool_loss_cost"] = r["tool_loss_cost"] or 0
    # 配送费收入
    r = cur.execute(f"""
        SELECT COALESCE(SUM(CASE WHEN o.status!='cancelled'
            THEN (o.amount - p.base_price * op.quantity)
            ELSE 0 END),0) as delivery_revenue
        FROM orders o
        JOIN order_packages op ON op.order_id = o.id
        JOIN packages p ON op.package_id = p.id
        WHERE {date_clause}
    """, params).fetchone()
    summary["delivery_revenue"] = r["delivery_revenue"] or 0
    summary["total_cost"] = summary["food_cost"] + summary["tool_loss_cost"]
    summary["gross_profit"] = summary["total_revenue"] - summary["total_cost"]
    summary["margin"] = round(summary["gross_profit"] / max(1, summary["total_revenue"]) * 100, 1)
    # 库存价值（食材+工具）
    r = cur.execute("SELECT COALESCE(SUM(stock * cost),0) as stock_value FROM ingredients").fetchone()
    summary["stock_value"] = r["stock_value"] or 0
    r = cur.execute("SELECT COALESCE(SUM(stock * cost),0) as tool_stock_value FROM tools").fetchone()
    summary["tool_stock_value"] = r["tool_stock_value"] or 0
    return jsonify({"ok": True, "data": summary})


@app.route("/api/finance/orders")
def finance_orders():
    """订单财务明细列表
    可选参数：date_from, date_to（YYYY-MM-DD），按 booking_date 过滤"""
    db = g.db
    cur = db.cursor()
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    date_clause = "1=1"
    params = []
    if date_from:
        date_clause += " AND o.booking_date >= ?"
        params.append(date_from)
    if date_to:
        date_clause += " AND o.booking_date <= ?"
        params.append(date_to)
    rows = cur.execute(f"""
        SELECT o.id, o.booking_date, o.contact_name, o.contact_phone, o.address,
               o.amount, o.deposit, o.payment_status, o.deposit_status, o.status,
               o.created_at,
               (SELECT COALESCE(SUM(pi.per_package * op.quantity * i.cost),0)
                FROM order_packages op
                JOIN package_ingredients pi ON pi.package_id = op.package_id
                JOIN ingredients i ON pi.ingredient_id = i.id
                WHERE op.order_id = o.id) as food_cost,
               (SELECT COALESCE(SUM(tl.lost_qty * t.cost),0)
                FROM tool_loans tl JOIN tools t ON tl.tool_id = t.id
                WHERE tl.order_id = o.id) as tool_loss
        FROM orders o WHERE {date_clause}
        ORDER BY o.booking_date DESC, o.created_at DESC LIMIT 500
    """, params).fetchall()
    orders = []
    for r in rows:
        o = dict(r)
        o["total_cost"] = (o["food_cost"] or 0) + (o["tool_loss"] or 0)
        o["profit"] = (o["amount"] or 0) - o["total_cost"]
        o["margin_pct"] = round(o["profit"] / max(1, o["amount"] or 1) * 100, 1)
        orders.append(o)
    return jsonify({"ok": True, "data": orders})


@app.route("/api/finance/order/<int:oid>/cost-detail")
def finance_order_cost_detail(oid):
    """单个订单的食材成本明细（用于财务页成本编辑）"""
    db = g.db
    cur = db.cursor()
    rows = cur.execute("""
        SELECT pi.per_package, pi.portion_count, pi.cost_only,
               op.quantity as order_qty,
               i.id as ingredient_id, i.name, i.unit, i.cost, i.category,
               i.purchase_unit, i.purchase_factor, i.portion_grams
        FROM order_packages op
        JOIN package_ingredients pi ON pi.package_id = op.package_id
        JOIN ingredients i ON pi.ingredient_id = i.id
        WHERE op.order_id = ?
        ORDER BY i.category, i.name
    """, (oid,)).fetchall()
    items = []
    for r in rows:
        total_amount = r["per_package"] * r["order_qty"]
        total_cost = total_amount * (r["cost"] or 0)
        factor = float(r["purchase_factor"] or 1) or 1
        items.append({
            "ingredient_id": r["ingredient_id"],
            "name": r["name"], "unit": r["unit"],
            "per_package": r["per_package"],
            "portion_count": r["portion_count"],
            "order_qty": r["order_qty"],
            "cost_only": r["cost_only"],
            "total_amount": round(total_amount, 2),
            "unit_cost": r["cost"] or 0,
            "purchase_price": round((r["cost"] or 0) * factor, 4),
            "purchase_unit": r["purchase_unit"] or r["unit"],
            "purchase_factor": factor,
            "portion_grams": r["portion_grams"],
            "total_cost": round(total_cost, 2),
            "category": r["category"],
        })
    return jsonify({"ok": True, "data": items})


# ===== 成本与利润（"这一单赚多少"）=====
@app.route("/api/cost/config", methods=["GET", "POST"])
def cost_config():
    """成本与利润配置：场地单价表、每单默认油费/人工/耗材、固定开销、毛利率警戒线"""
    from profit import get_config, save_config
    db = g.db
    if request.method == "GET":
        return jsonify({"ok": True, "data": get_config(db)})
    data = request.get_json(force=True) or {}
    cfg = save_config(db, data)
    return jsonify({"ok": True, "data": cfg})


@app.route("/api/finance/order/<int:oid>/profit")
def order_profit_api(oid):
    """单笔订单利润：收入、成本项明细（食材/搭建回收/油费/人工/耗材/其他）、现金利润、净利、毛利率"""
    from profit import order_profit
    p = order_profit(g.db, oid)
    if not p:
        return jsonify({"ok": False, "msg": "订单不存在"}), 404
    return jsonify({"ok": True, "data": p})


@app.route("/api/finance/order/<int:oid>/cost", methods=["POST"])
def set_order_cost(oid):
    """手工设定某单的成本项（覆盖自动值）；kind=other 时新增一条额外开销。
    传 id 则改已有的其他项。"""
    from profit import order_profit
    data = request.get_json(force=True) or {}
    db = g.db
    cur = db.cursor()
    if not cur.execute("SELECT id FROM orders WHERE id=?", (oid,)).fetchone():
        return jsonify({"ok": False, "msg": "订单不存在"}), 404
    kind = (data.get("kind") or "").strip()
    allowed = ("outsource", "fuel", "labor", "consume", "other")
    if kind not in allowed:
        return jsonify({"ok": False, "msg": "成本项不合法"}), 400
    amount = round(float(data.get("amount") or 0), 2)
    note = (data.get("note") or "").strip() or None
    rid = data.get("id")
    if kind == "other":
        name = (data.get("name") or "").strip() or "其他开销"
        if rid:
            cur.execute("UPDATE order_costs SET name=?,amount=?,note=?,"
                        "updated_at=datetime('now','localtime') WHERE id=? AND order_id=?",
                        (name, amount, note, rid, oid))
        else:
            cur.execute("INSERT INTO order_costs (order_id,kind,name,amount,note) VALUES (?,?,?,?,?)",
                        (oid, kind, name, amount, note))
    else:
        qty = data.get("qty")
        up = data.get("unit_price")
        if kind == "outsource" and (qty is not None or up is not None):
            qty = float(qty) if qty not in (None, "") else None
            up = float(up) if up not in (None, "") else None
        else:
            qty = up = None
        cur.execute("DELETE FROM order_costs WHERE order_id=? AND kind=?", (oid, kind))
        cur.execute("INSERT INTO order_costs (order_id,kind,qty,unit_price,amount,note) VALUES (?,?,?,?,?,?)",
                    (oid, kind, qty, up, amount, note))
    db.commit()
    return jsonify({"ok": True, "data": order_profit(db, oid)})


@app.route("/api/finance/order/<int:oid>/cost/clear", methods=["POST"])
def clear_order_cost(oid):
    """清除手工设定，回到自动算的值（kind=other 需带 id 指定哪一条）"""
    from profit import order_profit
    data = request.get_json(force=True) or {}
    db = g.db
    kind = (data.get("kind") or "").strip()
    if kind == "other" and data.get("id"):
        db.execute("DELETE FROM order_costs WHERE id=? AND order_id=?", (data["id"], oid))
    elif kind:
        db.execute("DELETE FROM order_costs WHERE order_id=? AND kind=?", (oid, kind))
    else:
        return jsonify({"ok": False, "msg": "缺少成本项"}), 400
    db.commit()
    return jsonify({"ok": True, "data": order_profit(db, oid)})


@app.route("/api/finance/profit-overview")
def finance_profit_overview():
    """期间利润汇总：现金利润 + 含固定开销摊销的净利，含每单利润与标红"""
    from profit import period_overview
    ov = period_overview(g.db, request.args.get("date_from"), request.args.get("date_to"))
    return jsonify({"ok": True, "data": ov})


@app.route("/api/finance/payment/<int:oid>", methods=["POST"])
def update_payment(oid):
    """更新订单货款状态"""
    data = request.get_json(force=True)
    status = data.get("payment_status", "unpaid")
    db = g.db
    db.execute("UPDATE orders SET payment_status=? WHERE id=?", (status, oid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/finance/deposit/<int:oid>", methods=["POST"])
def update_deposit(oid):
    """更新押金状态"""
    data = request.get_json(force=True)
    status = data.get("deposit_status", "pending")
    db = g.db
    db.execute("UPDATE orders SET deposit_status=? WHERE id=?", (status, oid))
    db.commit()
    return jsonify({"ok": True})


# ===== 打印备餐单（按食材汇总）=====
@app.route("/api/print/menu")
def print_menu():
    """返回待备/备餐中订单的食材+工具汇总，适合打印"""
    import json as _json
    db = g.db
    cur = db.cursor()
    cur.execute("SELECT id, booking_date, booking_time, address, contact_name, status "
                "FROM orders WHERE status IN ('pending','preparing') AND deleted_at IS NULL ORDER BY id")
    orders = [dict(r) for r in cur.fetchall()]

    order_details = []
    for o in orders:
        cur.execute("""
            SELECT op.quantity, p.name, p.id
            FROM order_packages op
            JOIN packages p ON op.package_id = p.id
            WHERE op.order_id = ?
        """, (o["id"],))
        pkgs = [dict(r) for r in cur.fetchall()]
        order_details.append({**o, "packages": pkgs})

    # 直接调用 calc_merged_prep，与备餐页数据源一致
    from calculator import calc_merged_prep, CATEGORY_ORDER, CATEGORY_LABEL, \
        PACKAGING_CATEGORY, UTENSIL_CATEGORY, SAUCE_LIKE_CATS, \
        _subcategorize_meat, _get_fixed_sort_index, sort_tools
    merged = calc_merged_prep([o["id"] for o in orders])
    # merged["ingredients"] 是按分类分组的 dict: {cat: [item, ...]}
    ing_by_cat = merged["ingredients"]
    tools_sorted = merged["tools"]

    # 渲染成一个可打印的 HTML（独立页面，无 nav，适合 A4 打印）
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    order_lines = ""
    for o in order_details:
        pkg_str = ", ".join(f"{p['name']}×{p['quantity']}" for p in o["packages"])
        order_lines += f"""
        <tr><td>{o['id']}</td><td>{o['booking_date']} {o['booking_time'] or ''}</td>
            <td>{o['address'] or ''}</td><td>{o['contact_name'] or ''}</td>
            <td>{pkg_str}</td></tr>"""

    # 分类配色（与备餐页/菜单页一致）
    cat_colors = {
        "beef": "#8e1e1a", "pork": "#c75d3e", "chicken": "#c9962b",
        "vegetable": "#3a8a3a", "side": "#5a8a3a", "sauce": "#7a5a3a",
        "drink": "#666666", "staple": "#888888", "other": "#6b4f3a",
        "packaging": "#d35400", "utensil": "#8e44ad", "tool": "#4a4a4a",
    }
    # 食材按分类渲染（与备餐/菜单页顺序一致：含packaging/utensil，不再单独处理）
    ing_rows = ""
    for cat in CATEGORY_ORDER:
        items = ing_by_cat.get(cat, [])
        if not items:
            continue
        label = CATEGORY_LABEL.get(cat, cat)
        color = cat_colors.get(cat, "#6b4f3a")
        ing_rows += f'<tr class="cat-row"><td colspan="5" style="background:{color};color:#fff;font-weight:700;padding:7px 10px;font-size:14px;">{label} · {len(items)}项</td></tr>'
        for i in items:
            tot_portions = i.get("total_portions", 0)
            per_size = i.get("portion_size", 0)
            unit = i.get("unit", "") or ""
            # packaging/utensil/tool(无克数列) 与菜单页一致：克数显示"—"
            is_simple = cat in (PACKAGING_CATEGORY, UTENSIL_CATEGORY)
            if is_simple:
                tot_portions = i.get("total_packages", 0)
                per_size = None  # 不显示克数
            total_val = round(i["total"], 2) if i.get("total") else 0
            total_str = f"{total_val}{unit}" if total_val else "-"
            ing_rows += (
                f'<tr><td class="name-cell">{i["name"]}</td>'
                f'<td class="num-cell">{round(tot_portions) if tot_portions else "-"}</td>'
                f'<td class="num-cell">{round(per_size,2) if per_size else "—"}</td>'
                f'<td class="num-cell total-cell">{total_str}</td>'
                f'<td>{unit}</td></tr>'
            )

    # 工具单独渲染（与备餐页一致：🔧 工具分类，克数列显示"—"，单位默认"个"）
    tool_rows = ""
    if tools_sorted:
        tool_rows += f'<tr class="cat-row"><td colspan="5" style="background:#4a4a4a;color:#fff;font-weight:700;padding:7px 10px;font-size:14px;">🔧 工具 · {len(tools_sorted)}项</td></tr>'
        for t in tools_sorted:
            t_total = round(t.get("total", 0))
            tool_rows += (
                f'<tr><td class="name-cell">{t["name"]}</td>'
                f'<td class="num-cell">{t_total}</td>'
                f'<td class="num-cell">—</td>'
                f'<td class="num-cell total-cell">{t_total}个</td>'
                f'<td>个</td></tr>'
            )

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>备餐单 · {now}</title>
<style>
  @media print{{ @page{{ size:A4; margin:12mm }} }}
  body{{ font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif; color:#2b1e14; padding:20px; max-width:800px; margin:0 auto; }}
  h1{{ font-size:22px; margin:0 0 4px; }}
  .meta{{ color:#8a7f75; font-size:13px; margin-bottom:14px; }}
  .sec-title{{ font-size:15px; font-weight:700; margin:18px 0 8px; padding-bottom:4px; border-bottom:2px solid #c0392b; color:#c0392b; }}
  table{{ width:100%; border-collapse:collapse; font-size:13px; }}
  thead th{{ background:#f3efe8; text-align:left; padding:7px 10px; font-weight:700; color:#666; }}
  thead th.num-col{{ text-align:center; }}
  tbody td{{ padding:8px 10px; border-bottom:1px solid #efe8dd; }}
  tbody td.name-cell{{ font-weight:700; }}
  tbody td.num-cell{{ text-align:center; color:#444; font-weight:600; }}
  tbody td.total-cell{{ text-align:right; color:#c0392b; font-weight:800; font-family:"DIN","Helvetica Neue",sans-serif; }}
  tbody tr.cat-row td{{ padding:7px 10px; }}
  .print-btn{{ margin:12px 6px 12px 0; padding:10px 24px; background:#c0392b; color:#fff; border:none; border-radius:6px; font-size:15px; cursor:pointer; font-weight:700; }}
  .export-btn{{ margin:12px 6px 12px 0; padding:10px 24px; background:#27ae60; color:#fff; border:none; border-radius:6px; font-size:15px; cursor:pointer; font-weight:700; }}
  .back-btn{{ margin:12px 0; padding:10px 24px; background:#fff; color:#2b1e14; border:1.5px solid #c0392b; border-radius:6px; font-size:15px; cursor:pointer; text-decoration:none; display:inline-block; }}
  .back-btn:hover{{ background:#fdecea; }}
  @media print{{ .print-btn, .export-btn, .back-btn{{ display:none }} }}
</style>
</head>
<body>
<a class="back-btn" href="javascript:history.length>1?history.back():'/'">← 返回</a>
<button class="print-btn" onclick="window.print()">🖨️ 打印 / 存为PDF</button>
<button class="export-btn" onclick="exportCSV()">📥 导出Excel(CSV)</button>
<h1>🔥 刘和牛户外烤肉 · 备餐单</h1>
<div class="meta">生成时间：{now} · 共 {len(orders)} 单待备 / 备餐中</div>

<div class="sec-title">📋 订单清单</div>
<table><thead><tr><th>#</th><th>时间</th><th>地址</th><th>联系人</th><th>套餐</th></tr></thead>
<tbody>{order_lines or '<tr><td colspan=5 style="text-align:center;color:#aaa;padding:20px">暂无待备订单</td></tr>'}</tbody></table>

<div class="sec-title">🥩 备餐核对清单</div>
<table><thead><tr>
  <th>名字</th><th class="num-col">份数</th><th class="num-col">克数</th><th class="num-col">总量</th><th>单位</th>
</tr></thead>
<tbody>{ing_rows or '<tr><td colspan=5 style="text-align:center;color:#aaa;padding:20px">—</td></tr>'}{tool_rows}</tbody></table>
<script>
function exportCSV(){{
  var rows=[["分类","食材","份数","克数","总量","单位"]];
  document.querySelectorAll("table tbody tr").forEach(function(tr){{
    if(tr.classList.contains("cat-row")) return;
    var cells=tr.querySelectorAll("td");
    if(cells.length<5) return;
    rows.push([tr.parentElement.previousElementSibling? "":"",
      cells[0]?.textContent||"", cells[1]?.textContent||"",
      cells[2]?.textContent||"", cells[3]?.textContent||"",
      cells[4]?.textContent||""]);
  }});
  var csv="\\uFEFF";
  rows.forEach(function(r){{ csv+=r.map(function(c){{ return '"'+(c||"").replace(/"/g,'""')+'"'; }}).join(",")+";"; }});
  var blob=new Blob([csv],{{type:"text/csv;charset=utf-8;"}});
  var a=document.createElement("a");
  a.href=URL.createObjectURL(blob);
  a.download="备餐单_"+new Date().toISOString().slice(0,10)+".csv";
  a.click();
}}
</script>
</body></html>"""
    return html


# ===== 工具借出归还 =====
@app.route("/api/orders/<int:oid>/loans")
def get_order_loans(oid):
    """获取订单的工具借出记录"""
    db = g.db
    cur = db.cursor()
    cur.execute("""
        SELECT tl.*, t.name as tool_name
        FROM tool_loans tl JOIN tools t ON tl.tool_id = t.id
        WHERE tl.order_id = ? ORDER BY tl.id
    """, (oid,))
    loans = [dict(r) for r in cur.fetchall()]
    return jsonify({"ok": True, "data": loans})


@app.route("/api/orders/<int:oid>/tools/return", methods=["POST"])
def return_tools(oid):
    """归还工具。body: {items: [{loan_id, returned_qty, lost_qty, note}]}
    lost_qty > 0 时自动从工具总库存扣除（报损）。
    """
    data = request.get_json(force=True)
    items = data.get("items", [])
    db = g.db
    cur = db.cursor()
    done = 0
    for it in items:
        lid = it.get("loan_id")
        returned = float(it.get("returned_qty", 0))
        lost = float(it.get("lost_qty", 0))
        note = it.get("note", "")
        cur.execute("SELECT * FROM tool_loans WHERE id=? AND order_id=?", (lid, oid))
        loan = cur.fetchone()
        if not loan:
            continue
        total = loan["quantity"]
        new_returned = min(total, returned)
        new_lost = min(total - new_returned, lost)
        if new_returned + new_lost >= total:
            status = "returned" if new_lost == 0 else "lost"
        else:
            status = "partial"
        cur.execute("""
            UPDATE tool_loans SET returned_qty=?, lost_qty=?, status=?, note=?,
            returned_at=datetime('now','localtime') WHERE id=?
        """, (new_returned, new_lost, status, note, lid))
        # 丢失工具从总库存扣除
        if new_lost > 0:
            cur.execute("UPDATE tools SET stock = stock - ? WHERE id=?", (new_lost, loan["tool_id"]))
            cur.execute("""
                INSERT INTO stock_logs (item_type, item_id, delta, reason)
                VALUES ('tool',?,?,?)
            """, (loan["tool_id"], -new_lost, f"订单#{oid} 工具丢失/损坏"))
        done += 1
    db.commit()
    return jsonify({"ok": True, "done": done})


# ===== 备餐合并视图 =====
@app.route("/api/prep/merged")
def prep_merged():
    """合并备餐视图。默认取当天 pending/preparing 订单；可传 ?date=YYYY-MM-DD 或 ?ids=1,2,3"""
    import datetime
    db = g.db
    cur = db.cursor()
    ids_param = request.args.get("ids")
    if ids_param:
        order_ids = [int(x) for x in ids_param.split(",") if x.strip().isdigit()]
    else:
        from calculator import today_cst as _today_cst
        date = request.args.get("date") or _today_cst().isoformat()
        cur.execute("""
            SELECT id FROM orders
            WHERE status IN ('pending','preparing') AND deleted_at IS NULL AND booking_date = ?
            ORDER BY booking_time
        """, (date,))
        order_ids = [r["id"] for r in cur.fetchall()]
    data = calc_merged_prep(order_ids)
    return jsonify({"ok": True, "data": data, "order_ids": order_ids})


@app.route("/api/prep/dates")
def prep_dates():
    """列出所有有 pending/preparing 订单的日期，供前端做日期快捷切换"""
    from calculator import today_cst as _today_cst
    db = g.db
    cur = db.cursor()
    cur.execute("""
        SELECT booking_date, COUNT(*) as cnt
        FROM orders
        WHERE status IN ('pending','preparing') AND deleted_at IS NULL AND booking_date IS NOT NULL AND booking_date != ''
        GROUP BY booking_date
        ORDER BY booking_date
    """)
    today = _today_cst().isoformat()
    dates = []
    for r in cur.fetchall():
        d = dict(r)
        d["is_today"] = (r["booking_date"] == today)
        d["is_past"] = (r["booking_date"] < today)
        dates.append(d)
    return jsonify({"ok": True, "dates": dates, "today": today})


# ===== 备餐勾选 =====
@app.route("/api/prep/check", methods=["POST"])
def prep_check():
    """勾选/取消备餐项。
    body: {order_id or order_ids, item_type, item_id, checked}
    order_ids: 数组，用于合并视图一次勾选多个订单的同一项。
    """
    data = request.get_json(force=True)
    itype = data.get("item_type")
    iid = data.get("item_id")
    checked = 1 if data.get("checked") else 0
    oids = data.get("order_ids")
    if oids is None:
        oids = [data.get("order_id")]
    oids = [int(x) for x in oids if x]
    if not oids or itype not in ("ingredient", "tool") or not iid:
        return jsonify({"ok": False, "msg": "参数错误"}), 400
    db = g.db
    cur = db.cursor()
    import datetime as _dt
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for oid in oids:
        cur.execute("""
            SELECT id, quantity FROM prep_checklist
            WHERE order_id=? AND item_type=? AND item_id=?
        """, (oid, itype, iid))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE prep_checklist SET checked=?, checked_at=? WHERE id=?",
                        (checked, ts if checked else None, row["id"]))
        else:
            qty = 0
            if itype == "ingredient":
                cur.execute("SELECT per_package FROM package_ingredients WHERE ingredient_id=? LIMIT 1", (iid,))
                r = cur.fetchone()
                qty = r["per_package"] if r else 0
            else:
                cur.execute("SELECT per_package FROM package_tools WHERE tool_id=? LIMIT 1", (iid,))
                r = cur.fetchone()
                qty = r["per_package"] if r else 0
            cur.execute("""
                INSERT INTO prep_checklist (order_id, item_type, item_id, quantity, checked, checked_at)
                VALUES (?,?,?,?,?,?)
            """, (oid, itype, iid, qty, checked, ts if checked else None))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/prep/check/batch", methods=["POST"])
def prep_check_batch():
    """批量勾选：给指定订单的所有备餐项设置 checked 状态
    body: {order_ids: [...], checked: 0/1}
    """
    data = request.get_json(force=True)
    order_ids = data.get("order_ids", [])
    checked = 1 if data.get("checked") else 0
    db = g.db
    cur = db.cursor()
    import datetime as _dt
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for oid in order_ids:
        # 确保所有需求项都有记录
        req = calc_order_requirements(oid)
        for ing in req["ingredients"]:
            cur.execute("SELECT id FROM prep_checklist WHERE order_id=? AND item_type='ingredient' AND item_id=?",
                        (oid, ing["id"]))
            if not cur.fetchone():
                cur.execute("""
                    INSERT INTO prep_checklist (order_id, item_type, item_id, quantity, checked)
                    VALUES (?,?,?,?,?)
                """, (oid, "ingredient", ing["id"], ing["need"], 0))
        for tl in req["tools"]:
            cur.execute("SELECT id FROM prep_checklist WHERE order_id=? AND item_type='tool' AND item_id=?",
                        (oid, tl["id"]))
            if not cur.fetchone():
                cur.execute("""
                    INSERT INTO prep_checklist (order_id, item_type, item_id, quantity, checked)
                    VALUES (?,?,?,?,?)
                """, (oid, "tool", tl["id"], tl["need"], 0))
        # 批量更新该订单所有项
        cur.execute("UPDATE prep_checklist SET checked=?, checked_at=? WHERE order_id=?",
                    (checked, ts if checked else None, oid))
    db.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
