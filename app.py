"""烤肉店后厨备餐系统 - Flask 主应用"""
import math
import os


# ===== idna 编码器兜底（2026-09-29 线上全站 500 的根因修复）=====
# 现象：Render 的 Python 3.14 环境缺 encodings.idna（unknown encoding: idna），
# 而 werkzeug 每个请求做路由匹配都要 server_name.encode("idna")，
# 缺了它 = 所有请求一律 500，页面全部打不开。
# 修法：应用启动最前面先检查，缺就补上：
#   1) 尝试 import encodings.idna（导入即自动注册，旧版 Python 自带）；
#   2) 实在没有就用 ascii 编码器顶替 —— 我们的域名全是 ASCII
#      （*.onrender.com），对 ASCII 域名两者编码结果完全一样。
def ensure_idna_codec():
    import codecs
    forced = os.environ.get("_FORCE_IDNA_FALLBACK") == "1"
    if not forced:
        try:
            codecs.lookup("idna")
            return
        except LookupError:
            pass
        try:
            import encodings.idna  # noqa: F401  导入即注册
            return
        except Exception:
            pass
    import encodings.ascii as _ascii
    codecs.register(lambda name: _ascii.getregentry() if name == "idna" else None)

ensure_idna_codec()


# ===== 线上报错自动上报（不再麻烦用户去翻 Render 日志）=====
# 2026-09-28 的教训：线上出 500 时，唯一能定位的 Traceback 只有 Render 日志里有，
# 而让用户去翻日志既慢又容易指错地方。这里把报错直接写回 GitHub 仓库的
# boot_error.txt（线上配了 GITHUB_TOKEN），我这边拉一下就看到了。
def report_error(tag, text):
    try:
        import base64, json, urllib.request, datetime as _dt
        tok = os.environ.get("GITHUB_TOKEN", "")
        if not tok:
            return False
        url = "https://api.github.com/repos/ll136897/kitchen-panel/contents/boot_error.txt"
        body = ("[%s UTC] %s\n%s" % (_dt.datetime.utcnow().isoformat(), tag, text)).encode("utf-8")
        sha = None
        try:                                  # 先查原文件 sha（更新要用）
            rq = urllib.request.Request(url, headers={"Authorization": "token " + tok,
                                                      "User-Agent": "kitchen-panel"})
            with urllib.request.urlopen(rq, timeout=15) as r:
                sha = json.loads(r.read().decode()).get("sha")
        except Exception:
            pass
        data = {"message": "auto: error report", "branch": "main",
                "content": base64.b64encode(body).decode()}
        if sha:
            data["sha"] = sha
        rq = urllib.request.Request(url, data=json.dumps(data).encode(), method="PUT",
                                    headers={"Authorization": "token " + tok,
                                             "Accept": "application/vnd.github.v3+json",
                                             "User-Agent": "kitchen-panel"})
        urllib.request.urlopen(rq, timeout=25).read()
        print("[err-report] 已上报：%s" % tag)
        return True
    except Exception as _e:
        print("[err-report] 上报失败：%r" % (_e,))
        return False


from flask import Flask, request, jsonify, render_template, g, session, redirect
try:
    from models import get_db, init_db
    from addresses import canonical_address
    from parser import parse_order_text, match_packages_in_db
    from calculator import (calc_order_requirements, calc_dashboard, preview_parse,
                            calc_merged_prep, calc_prep_urgency)
except Exception as _imp_err:                  # 导入期就炸 = 应用根本起不来
    import traceback as _tb
    report_error("import", _tb.format_exc())
    raise

app = Flask(__name__, template_folder="templates", static_folder="static")

# 响应体 gzip 压缩：订单/仪表盘等接口返回 60~80KB JSON，Render 免费实例网络慢，
# 开启压缩后传输体积通常降到 1/5~1/8，首屏和 30s 轮询明显变快。Flask-Compress 做这件事。
try:
    from flask_compress import Compress
    Compress(app)
except Exception as _ce:
    print("[init] ⚠️ flask_compress 未安装，响应不压缩：", _ce)

# 会话密钥：登录态靠它签名。务必用环境变量固定一把（Render 里配 SECRET_KEY），
# 否则免费实例重启会随机换密钥 → 全员被踢下线。本地测试没配就给个开发用默认值（仅本地）。
app.secret_key = os.environ.get("SECRET_KEY") or "dev-insecure-secret-key-CHANGE-ME"
if not os.environ.get("SECRET_KEY"):
    print("[init] ⚠️ 未设置 SECRET_KEY，登录态在重启后会失效；上线前请在 Render 环境变量里配一个随机值")

# /api/version 的缓存：{sha: 最新改动标题}，避免每次开页面都去 git fetch（见 api_version）
_VERSION_MEMO = {}

# 请求里抛的异常自动上报（同一种错只报一次，避免刷屏）；
# 同时给前端一句人话，别让用户对着 Internal Server Error 发呆。
_ERROR_REPORTED = set()


@app.errorhandler(Exception)
def _handle_unexpected(e):
    try:
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e                      # 404/405 这类正常状态码，不上报
    except Exception:
        pass
    try:
        import traceback as _tb
        key = "%s|%s" % (type(e).__name__, request.path)
        if key not in _ERROR_REPORTED:
            _ERROR_REPORTED.add(key)
            report_error("request %s" % request.path, _tb.format_exc())
    except Exception:
        pass
    _msg = "服务内部出错了（已自动上报，正在排查）。请稍后刷新重试。"
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "msg": _msg}), 500
    return _msg, 500

# WSGI 最外层兜底：如果错误发生在 Flask 处理链之外（连 errorhandler 都够不到），
# 在这里抓住并上报 —— 2026-09-28 线上"所有请求 500 但 errorhandler 不触发"的坑。
_wsgi_orig = app.wsgi_app


def _wsgi_with_report(environ, start_response):
    try:
        return _wsgi_orig(environ, start_response)
    except Exception:
        import traceback as _tb
        try:
            _path = environ.get("PATH_INFO", "?")
            _key = "wsgi|%s" % _path
            if _key not in _ERROR_REPORTED:
                _ERROR_REPORTED.add(_key)
                report_error("wsgi %s" % _path, _tb.format_exc())
        except Exception:
            pass
        raise


app.wsgi_app = _wsgi_with_report

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
    # /api/ping 承诺"极轻量、不碰数据库"——必须跳过连库。
    # 这样哪怕数据库出问题，探活依然是好的，能从外部判断"应用活着、库挂了"，
    # 而不是全站一起 500、连诊断入口都没有。
    if request.path == "/api/ping":
        return
    try:
        g.db = get_db()
    except Exception:
        # 连不上库（磁盘满/文件损坏/权限等）：完整报错打进日志（Render Logs 可见），
        # 给前端一个可读的 503，而不是满屏 Internal Server Error。
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False,
                        "msg": "系统数据库暂时打不开，正在自动恢复，请 1 分钟后刷新重试"}), 503


@app.teardown_request
def teardown(exc):
    db = getattr(g, "db", None)
    if db is not None:
        db.close()


# ===================== 登录 / 分角色权限 =====================
# 角色（2026-10-02 改为三级）：
#   boss    老板（全权限，含改"原数据"）
#   partner 合伙人（页面/功能全都能看能用；**不能改"原数据"**；订单只能改自己录的）
#   staff   店员/帮手（只看得到 订单/备餐/库存相关；订单只能改自己录的）
# 设计原则：
#   - 所有校验在服务端做（before_request），前端隐藏链接只是体验，不是安全；
#   - 写操作**默认只有老板**；明确列出的才给合伙人/店员，越权在接口里再判一次订单归属。
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime as _dt

# 角色高低：数字越大权限越高；接口声明"需要 partner"= 老板或合伙人
ROLE_LEVEL = {"staff": 1, "partner": 2, "boss": 3}


def _now():
    return _dt.now().strftime("%Y-%m-%d %H:%M:%S")


# 「原数据/配置」——只能看、不能改（连合伙人也不行）：食材/装备/套餐/菜品/价格/设置/成本参数/账号/保活
_BOSSDATA_WRITE = (
    "/api/ingredients", "/api/tools", "/api/packages", "/api/dishes",
    "/api/price", "/api/settings", "/api/cost", "/api/users",
    "/api/keepalive", "/api/catering/save",
)
# 店员/帮手也能做的运营写操作（订单/备餐/库存；具体订单能不能动还看是不是自己录的）
_STAFF_WRITE = (
    "/api/orders", "/api/parse", "/api/prep", "/api/stock", "/api/ticket",
)
# 合伙人可以执行的写操作（= 店员的那些 + 财务/备份等；老板当然也可以）
_PARTNER_WRITE = _STAFF_WRITE + (
    "/api/backup", "/api/catering/suggest",
    # 单笔成本/收款/押金：能不能动由"这单是不是你录的"决定（见 _can_touch_order）
    "/api/finance/order", "/api/finance/payment", "/api/finance/deposit",
    # 门店支出台账：合伙人也能记/改/删门店支出、导入账单对账（运营数据，非原数据）；老板当然可以
    "/api/finance/expense", "/api/finance/expense-statement", "/api/finance/labor",
)
# 只有老板和合伙人能看的页面/接口（店员看不到）：财务/分析/菜单/设置/账号等
_PARTNER_READ = (
    "/config", "/finance", "/api/finance", "/menu", "/api/menu",
    "/history", "/admin", "/api/users", "/dashboard", "/analytics", "/api/analytics",
)


def _hit(path, prefixes):
    return any(path == p or path.startswith(p + "/") for p in prefixes)


# 该请求需要什么角色：None=不用登录(匿名可访问)
def _required_role(path, method):
    if path in ("/login", "/api/login", "/api/ping", "/api/version", "/logout"):
        return None
    if path.startswith("/static/"):
        return None
    write = (method or "GET").upper() in ("POST", "PUT", "DELETE", "PATCH")
    if write:
        # 1) 原数据/配置：只有老板能改（先判，别被后面的规则放过）
        if _hit(path, _BOSSDATA_WRITE):
            return "boss"
        # 2) 店员也能用的运营写操作（订单/备餐/库存）——必须排在合伙人之前，
        #    否则 /api/orders 会先被 _PARTNER_WRITE 命中，店员连加单都被拒
        if _hit(path, _STAFF_WRITE):
            return "staff"
        # 3) 合伙人额外可用的写操作（财务/备份等）
        if _hit(path, _PARTNER_WRITE):
            return "partner"
        # 4) 其余写操作默认只有老板
        return "boss"
    # 读取：这些页面/接口老板和合伙人都能看（店员看不到）
    if _hit(path, _PARTNER_READ):
        return "partner"
    # 其余（订单/备餐/出餐单等）登录即可
    return "staff"


@app.before_request
def auth_guard():
    path = request.path
    need = _required_role(path, request.method)
    if need is None:
        return
    uid = session.get("uid")
    user = None
    if uid:
        try:
            row = get_db().execute(
                "SELECT id,username,name,role,active FROM users WHERE id=?",
                (uid,)).fetchone()
            if row and row["active"]:
                user = dict(row)
        except Exception:
            user = None
    # 未登录
    if not user:
        if path.startswith("/api/"):
            return jsonify({"ok": False, "msg": "请先登录"}), 401
        return redirect("/login")
    g.cur_user = user
    # 角色不够（按等级比：staff 1 < partner 2 < boss 3）
    if ROLE_LEVEL.get(user["role"], 0) < ROLE_LEVEL.get(need, 1):
        if path.startswith("/api/"):
            if need == "boss":
                return jsonify({"ok": False, "msg": "无权限：这是「原数据/配置」，只有老板能改"}), 403
            return jsonify({"ok": False, "msg": "无权限：该操作仅老板/合伙人可用"}), 403
        return "无权限：该页面仅老板/合伙人可访问", 403


@app.context_processor
def _inject_user():
    return {"cur_user": getattr(g, "cur_user", None)}


# ===== 订单归属与权限（2026-10-02）=====
# 订单有两个"人"的概念：
#   created_by / created_by_name  = **归属**（这单算谁的）——决定权限，也是各页面筛选维度
#   entered_by / entered_by_name  = 实际录入人（谁敲进去的），纯审计，只在订单详情里提示"代录"
# 规则：老板全权；其他人只能改/删**自己归属**的订单；录入时可以选择署谁的名（互相代录）。
def _cur_uid():
    u = getattr(g, "cur_user", None) or {}
    return u.get("id")


def _is_boss():
    u = getattr(g, "cur_user", None) or {}
    return u.get("role") == "boss"


def _can_touch_order(db, oid):
    """当前用户能不能改/删这一单（看归属，不看谁录的）"""
    if _is_boss():
        return True
    row = db.execute("SELECT created_by FROM orders WHERE id=?", (oid,)).fetchone()
    if not row:
        return False
    return row["created_by"] is not None and row["created_by"] == _cur_uid()


def _signer_candidates(db=None):
    """可以当"归属"的人：启用中的老板/合伙人 + 自己（每人只能署自己或对方的名）。

    店员不在候选里——除非他自己就是登录人（那他默认署自己）。
    返回 [{id, name, role, is_me}]，自己排第一个。"""
    db = db or g.db
    me = getattr(g, "cur_user", None) or {}
    rows = db.execute("""
        SELECT id, name, username, role FROM users
        WHERE COALESCE(active,1)=1 AND role IN ('boss','partner')
        ORDER BY CASE role WHEN 'boss' THEN 0 ELSE 1 END, id
    """).fetchall()
    out, seen = [], set()
    if me.get("id"):
        out.append({"id": me["id"], "name": me.get("name") or me.get("username") or "",
                    "role": me.get("role") or "staff", "is_me": True})
        seen.add(me["id"])
    for r in rows:
        if r["id"] in seen:
            continue
        out.append({"id": r["id"], "name": r["name"] or r["username"] or "",
                    "role": r["role"], "is_me": False})
        seen.add(r["id"])
    return out


def _resolve_signer(db, raw):
    """把前端传来的归属（用户 id / 'me' / 空）解析成 (uid, name)。

    不在候选清单里的一律忽略 → 退回"署自己"，避免有人把单署给不该署的人。"""
    me = getattr(g, "cur_user", None) or {}
    cand = _signer_candidates(db)
    want = None
    if raw in (None, "", "me"):
        want = me.get("id")
    else:
        try:
            want = int(raw)
        except (TypeError, ValueError):
            want = me.get("id")
    for c in cand:
        if c["id"] == want:
            return c["id"], c["name"]
    return me.get("id"), (me.get("name") or me.get("username") or "")


def _signer_clause(args, alias=""):
    """按归属筛选的 SQL 片段 + 参数：?by=me | 0(=未标注) | <user_id> | 空(=全部)"""
    by = (args.get("by") or "").strip()
    if not by:
        return "", []
    col = (alias + "." if alias else "") + "created_by"
    if by == "me":
        return " AND %s = ? " % col, [_cur_uid()]
    if by == "0":
        return " AND %s IS NULL " % col, []
    try:
        return " AND %s = ? " % col, [int(by)]
    except (TypeError, ValueError):
        return "", []


@app.route("/api/signers")
def api_signers():
    """可归属的人（录入订单时选"这单算谁的"）"""
    return jsonify({"ok": True, "data": _signer_candidates()})


# 别人归属时的统一回复
NO_PERM_MSG = "无权限：这单不是你的归属，你只能修改/删除自己归属的订单"


def _deny_order():
    return jsonify({"ok": False, "msg": NO_PERM_MSG}), 403


@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True) or {}
    u = (data.get("username") or "").strip()
    p = data.get("password") or ""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id,username,name,role,active,pw_hash FROM users WHERE username=?",
            (u,)).fetchone()
        if not row or not row["active"] or not check_password_hash(row["pw_hash"], p):
            return jsonify({"ok": False, "msg": "用户名或密码错误"}), 401
        session["uid"] = row["id"]
        return jsonify({"ok": True, "role": row["role"], "name": row["name"]})


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/api/me")
def api_me():
    if not getattr(g, "cur_user", None):
        return jsonify({"ok": False, "msg": "未登录"}), 401
    return jsonify({"ok": True, "user": g.cur_user})


# ---- 老板专属：账号管理 ----
@app.route("/admin")
def admin_page():
    return render_template("admin.html")


@app.route("/api/users", methods=["GET"])
def list_users():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,username,name,role,active FROM users ORDER BY id").fetchall()
    return jsonify({"ok": True, "users": [dict(r) for r in rows]})


@app.route("/api/users", methods=["POST"])
def add_user():
    data = request.get_json(force=True) or {}
    un = (data.get("username") or "").strip()
    name = (data.get("name") or "").strip()
    role = data.get("role") if data.get("role") in ("boss", "partner", "staff") else "staff"
    pw = data.get("password") or ""
    if not un or not pw:
        return jsonify({"ok": False, "msg": "用户名和密码必填"}), 400
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO users(username,name,role,pw_hash,active,created_at) VALUES(?,?,?,?,?,?)",
                (un, name, role, generate_password_hash(pw), 1, _now()))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": "创建失败：" + str(e)}), 400


@app.route("/api/users/<int:uid>/toggle", methods=["POST"])
def toggle_user(uid):
    with get_db() as conn:
        conn.execute("UPDATE users SET active=1-active WHERE id=?", (uid,))
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
def del_user(uid):
    with get_db() as conn:
        conn.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>/reset-pw", methods=["POST"])
def reset_pw(uid):
    data = request.get_json(force=True) or {}
    pw = data.get("password") or ""
    if not pw:
        return jsonify({"ok": False, "msg": "请输入新密码"}), 400
    with get_db() as conn:
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?",
                     (generate_password_hash(pw), uid))
    return jsonify({"ok": True})


@app.route("/api/me/change-pw", methods=["POST"])
def change_my_pw():
    if not getattr(g, "cur_user", None):
        return jsonify({"ok": False, "msg": "未登录"}), 401
    data = request.get_json(force=True) or {}
    old = data.get("old") or ""
    new = data.get("new") or ""
    if not new:
        return jsonify({"ok": False, "msg": "请输入新密码"}), 400
    with get_db() as conn:
        row = conn.execute("SELECT pw_hash FROM users WHERE id=?",
                           (session.get("uid"),)).fetchone()
        if not row or not check_password_hash(row["pw_hash"], old):
            return jsonify({"ok": False, "msg": "原密码错误"}), 400
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?",
                     (generate_password_hash(new), session["uid"]))
    return jsonify({"ok": True})


def ensure_admin_user():
    """库里一个账号都没有时，用环境变量 ADMIN_PW（没有就默认 kaorou888）建一个老板号。
    这样第一次部署后你就能用 admin 登录，再去“账号”页改密码、加店员。"""
    try:
        with get_db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if n == 0:
                pw = os.environ.get("ADMIN_PW") or "kaorou888"
                conn.execute(
                    "INSERT INTO users(username,name,role,pw_hash,active,created_at) VALUES(?,?,?,?,?,?)",
                    ("admin", "刘", "boss", generate_password_hash(pw), 1, _now()))
                print("[init] 已创建默认老板账号 admin / %s —— 请尽快在“账号”页修改密码" % pw)
    except Exception as e:
        print("[init] ensure_admin_user 跳过：", e)


# 模块加载时确保至少有一个老板账号（首次部署后即可用 admin 登录），放在函数定义之后
try:
    ensure_admin_user()
except Exception as _e:
    print(f"[init] ensure_admin_user skipped: {_e}")


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
    # 归属显示名取实时账号名（视图层覆盖，绝不改历史快照 created_by_name）
    if o.get("created_by") is not None:
        _cn = cur.execute("SELECT name FROM users WHERE id=?", (o["created_by"],)).fetchone()
        if _cn:
            o["created_by_name"] = _cn["name"]
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
    return render_template("finance.html", cur_user=g.cur_user)


@app.route("/analytics")
def analytics_page():
    """经营分析（趋势/环比/结构/客户）——仅老板可见（/analytics 在鉴权白名单外按角色控制）"""
    return render_template("analytics.html")


@app.route("/prep")
def prep_page():
    return render_template("prep.html", cur_user=g.cur_user)


@app.route("/menu")
def menu_page():
    return render_template("menu.html")


# ===== 订单解析与入库 =====
@app.route("/api/parse", methods=["POST"])
def api_parse():
    """解析订单文本预览（不入库）。

    批量粘贴不需要再手工加分隔号：
      - 自己写了 --- / === → 按它拆（老用法保留）
      - 复制的微信聊天记录 → 按"昵称+时间"的抬头自动拆
      - 多段空行隔开的订单 → 自动拆
      - 拆不出来的就按一单处理，绝不丢内容
    前端还能把人工调整过的 chunks 传回来重解析。
    """
    data = request.get_json(force=True)
    raw = data.get("raw_text", "")
    if not raw.strip() and not data.get("chunks"):
        return jsonify({"ok": False, "msg": "文本为空"}), 400

    db = g.db
    cur = db.cursor()
    from parser import split_orders, split_chunks
    chunks_in = data.get("chunks")
    if isinstance(chunks_in, list) and any((c or "").strip() for c in chunks_in):
        # 前端回传的 chunks 也可能"一段含多单"（人工调整/历史分段）→ 再细拆一层
        parts = [(c, "manual") for c in split_chunks(chunks_in)]
    else:
        parts = split_orders(raw)

    items = []
    for i, (chunk, reason) in enumerate(parts):
        p = preview_parse(chunk)
        p["_raw"] = chunk
        p["_batch_idx"] = i + 1
        p["_split_reason"] = reason
        # 顺带标注这条是否和已有订单重复（不入库、只提示），让用户在预览框就能看到并删除
        dup = _find_duplicate_order(cur, p)
        if dup:
            p["dup"] = {"id": dup["id"], "booking_date": dup["booking_date"],
                        "booking_time": dup["booking_time"], "address": dup["address"],
                        "contact_name": dup["contact_name"], "contact_phone": dup["contact_phone"],
                        "status": dup["status"]}
        items.append(p)
    reasons = sorted({r for _, r in parts})
    return jsonify({"ok": True, "batch": True, "count": len(items),
                    "items": items, "data": (items[0] if items else None),
                    "split_by": (reasons[0] if len(reasons) == 1 else "mixed")})


def _dup_key_of(parsed):
    """取用于「完全重复」判定的关键字段；关键信息缺失时返回 None（不做重复判断）"""
    bd = (parsed.get("booking_date") or "").strip()
    addr = (parsed.get("address") or "").strip()
    if not bd or not addr:
        return None
    return (bd, (parsed.get("booking_time") or "").strip(), addr,
            (parsed.get("contact_phone") or "").strip())


def _find_duplicate_order(cur, parsed):
    """找一条「完全重复」的现存订单（同日期+时间+地址+电话，且未取消、未删除）

    地址按"归一"规则比：青龙湖二期 = 青龙湖，写法不同也算同一单。"""
    key = _dup_key_of(parsed)
    if not key:
        return None
    bd, bt, addr, phone = key
    # 日期 + 时间 + 电话先筛出候选（这几个条件已经很紧了），
    # 地址再按"归一"规则比：青龙湖二期 = 青龙湖，写法不同也算同一单，避免重复建单。
    rows = cur.execute("""
        SELECT id, booking_date, booking_time, address, contact_name, contact_phone, status
        FROM orders
        WHERE deleted_at IS NULL AND status != 'cancelled'
          AND booking_date = ?
          AND COALESCE(booking_time,'') = ?
          AND COALESCE(contact_phone,'') = ?
        ORDER BY id DESC
    """, (bd, bt, phone)).fetchall()
    from addresses import same_address
    for r in rows:
        if same_address(r["address"], addr):
            return r
    return None


@app.route("/api/orders", methods=["POST"])
def create_order():
    """创建订单：粘贴一大段（微信聊天记录）可自动拆成多单，一次全部入库。

    支持两种入参：
      raw_text：整段文本，后端自动拆分（免分隔号，见 parser.split_orders）
      chunks  ：前端人工调整后的数组，每个元素就是一单（优先级更高）
    若发现一模一样的订单，返回 409 + duplicate 标记，由前端弹框让用户确认；
    用户确认后带 force=true 再提交一次即强制入库。"""
    data = request.get_json(force=True)
    raw = data.get("raw_text", "")
    if not raw.strip() and not data.get("chunks"):
        return jsonify({"ok": False, "msg": "文本为空"}), 400

    # 自动拆分多单（不再需要手工加 ---）；前端调整过的 chunks 优先
    from parser import split_orders, split_chunks
    chunks_in = data.get("chunks")
    if isinstance(chunks_in, list) and any((c or "").strip() for c in chunks_in):
        chunks = split_chunks(chunks_in)     # 一段含多单时再细拆，避免只入库最后 1 单
    else:
        chunks = [c for c, _ in split_orders(raw)]
    if not chunks:
        return jsonify({"ok": False, "msg": "没有可入库的内容"}), 400

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
                _st = {None: "未知", "pending": "待备", "preparing": "备餐中",
                       "done": "已完成", "cancelled": "已取消"}.get(d.get("status"), "未知")
                # 该单在哪天、什么状态——让用户一眼知道去哪找、为什么搜不到
                hint = "（%s，%s）" % (_st, d.get("booking_date") or "日期未知")
                return jsonify({
                    "ok": False,
                    "duplicate": True,
                    "existing_id": d["id"],
                    "existing_status": d.get("status"),
                    "existing_status_cn": _st,
                    "existing": d,
                    "msg": "已存在订单 #%s %s" % (d["id"], where),
                    "dup_hint": "这条已经在系统里了%s；可在「订单」页顶部搜索框搜“%s”直接定位，或去「备餐页」切到 %s 那天查看。" % (
                        hint, d.get("contact_name") or d.get("address") or "", d.get("booking_date") or ""),
                }), 409

    order_ids = []
    results = []
    _u = getattr(g, "cur_user", None) or {}
    # 归属：默认自己，可以署对方的名（互相代录）—— 见 _resolve_signer
    _sign_uid, _sign_name = _resolve_signer(db, data.get("signer"))
    _entered_name = _u.get("name") or _u.get("username") or ""
    for chunk, parsed in zip(chunks, parsed_chunks):
        matched = match_packages_in_db(parsed["packages"])
        cur.execute("""
            INSERT INTO orders
            (raw_text, booking_date, booking_time, address, contact_name,
             contact_phone, amount, deposit, meal_time, pickup_time, note, status,
             created_by, created_by_name, entered_by, entered_by_name)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)
        """, (
            chunk, parsed["booking_date"], parsed["booking_time"],
            parsed["address"], parsed["contact_name"], parsed["contact_phone"],
            parsed["amount"], parsed["deposit"], parsed["meal_time"],
            parsed["pickup_time"], parsed["note"],
            _sign_uid, _sign_name, _u.get("id"), _entered_name,
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
    # 单/批量都带上 count 与 order_ids，前端不用区分两种返回
    return jsonify({"ok": True, "batch": len(results) > 1, "count": len(results),
                    "order_ids": order_ids, "orders": results,
                    "signer_id": _sign_uid, "signer_name": _sign_name,
                    "entered_by_name": _entered_name,
                    "order_id": order_ids[0] if results else None})


@app.route("/api/orders")
def list_orders():
    db = g.db
    cur = db.cursor()
    purge_expired_deleted(db)          # 超过 7 天的回收站订单自动清掉
    show_deleted = request.args.get("deleted") == "1"
    # 列表要带上"归属"（created_by）和"实际录入人"（entered_by）：
    # 前端用来区分订单归属、决定给不给改的按钮、显示"谁代录的"
    if show_deleted:
        cur.execute("""
            SELECT o.id, o.booking_date, o.booking_time, o.address, o.contact_name,
                   o.contact_phone, o.amount, o.deposit, o.note, o.status, o.created_at, o.deleted_at,
                   o.created_by, COALESCE(u.name, o.created_by_name) AS created_by_name,
                   o.entered_by, o.entered_by_name
            FROM orders o LEFT JOIN users u ON o.created_by = u.id
            WHERE o.deleted_at IS NOT NULL ORDER BY o.id DESC LIMIT 500
        """)
    else:
        # ⚠️ 以前这里是 LIMIT 100 —— 订单超过 100 单后**订单页会静默丢掉最早的单**
        #    （117 单时 9 月只显示 58 单、实际 75 单）。订单页的筛选/排序/按月分组
        #    全在前端做，必须拿全量，所以放宽到 5000（够用多年，仍留个上限防跑飞）。
        cur.execute("""
            SELECT o.id, o.booking_date, o.booking_time, o.address, o.contact_name,
                   o.contact_phone, o.amount, o.deposit, o.note, o.status, o.created_at,
                   o.created_by, COALESCE(u.name, o.created_by_name) AS created_by_name,
                   o.entered_by, o.entered_by_name
            FROM orders o LEFT JOIN users u ON o.created_by = u.id
            WHERE o.deleted_at IS NULL ORDER BY o.id DESC LIMIT 5000
        """)
    orders = [dict(r) for r in cur.fetchall()]
    # 地址归一：给每单算出"归到哪个地方"（青龙湖二期 → 青龙湖）。
    # 前端订单页按地址分组/筛选就用这个字段，两个写法会被算作同一个地方。
    for o in orders:
        o["address_key"] = canonical_address(o.get("address"))
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
    if not _can_touch_order(db, oid):
        return _deny_order()
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
    if not _can_touch_order(db, oid):
        return _deny_order()
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
    if not _can_touch_order(db, oid):
        return _deny_order()

    def _txt(k):
        return (data.get(k) or "").strip()

    sets, params = [], []
    # 改「归属」：能改这一单的人就能改归属（老板全权；其他人只能改自己归属的单）。
    # 只能在候选清单里选（老板/合伙人/自己），避免把单署给不该署的人。
    _sign_raw = data.get("signer", data.get("created_by"))
    if _sign_raw is not None:
        _sid, _sname = _resolve_signer(db, _sign_raw)
        sets.append("created_by=?")
        params.append(_sid)
        sets.append("created_by_name=?")
        params.append(_sname)
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
    # 记账信息（销售流水表 / 算真实到手用，2026-10-10）
    for k in ("menu_name", "delivery_person", "source"):
        if k in data:
            sets.append("%s=?" % k)
            params.append(_txt(k))
    if "delivery_fee" in data:
        _rf = data.get("delivery_fee")
        if _rf in (None, ""):
            sets.append("delivery_fee=?"); params.append(None)
        else:
            try:
                sets.append("delivery_fee=?"); params.append(float(_rf))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "msg": "配送费格式不对"}), 400
    if "setup_flag" in data:
        _sv = data.get("setup_flag")
        if _sv in (None, ""):
            sets.append("setup_flag=?"); params.append(None)
        else:
            sets.append("setup_flag=?")
            params.append(1 if str(_sv) in ("1", "是", "true", "True") else 0)
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


@app.route("/api/orders/ledger-options")
def order_ledger_options():
    """销售流水/记账用的下拉选项 + 数据驱动的默认值（默认 = 历史最常用那个）。
    目的：记账时尽量少手填——配送人/配送费/套餐名都给下拉，且预选最常用的。"""
    db = g.db
    persons = [r["v"] for r in db.execute(
        "SELECT delivery_person AS v, COUNT(*) AS c FROM orders "
        "WHERE deleted_at IS NULL AND delivery_person IS NOT NULL AND TRIM(delivery_person)<>'' "
        "GROUP BY delivery_person ORDER BY c DESC LIMIT 10").fetchall()]
    fees = [r["v"] for r in db.execute(
        "SELECT delivery_fee AS v, COUNT(*) AS c FROM orders "
        "WHERE deleted_at IS NULL AND delivery_fee IS NOT NULL AND delivery_fee>0 "
        "GROUP BY delivery_fee ORDER BY c DESC LIMIT 10").fetchall()]
    menu_names = [r["menu_name"] for r in db.execute(
        "SELECT DISTINCT menu_name FROM orders "
        "WHERE deleted_at IS NULL AND menu_name IS NOT NULL AND TRIM(menu_name)<>''").fetchall()]
    for r in db.execute("SELECT name FROM packages ORDER BY id"):
        if r["name"] and r["name"] not in menu_names:
            menu_names.append(r["name"])
    return jsonify({"ok": True, "data": {
        "delivery_persons": persons,
        "default_person": persons[0] if persons else "",
        "delivery_fees": fees or [100, 200, 150, 45],
        "default_fee": fees[0] if fees else 100,
        "menu_names": menu_names,
        "sources": ["私人关系", "美团", "抖音", "小红书", "朋友介绍", "其他"],
        "default_source": "私人关系",
    }})


@app.route("/api/finance/labor", methods=["POST"])
def add_daily_labor():
    """按天记一笔「兼职制作人员工资」——写进门店支出台账（归类=人工/labor）。
    这样月度可汇总，导出销售流水表时再按当天单数摊到每单，得到"真实到手"。"""
    data = request.get_json(force=True) or {}
    u = getattr(g, "cur_user", None) or {}
    db = g.db
    d = _exp_str(data.get("date"))
    if not d:
        import datetime as _dt
        d = _dt.date.today().isoformat()
    amt = _exp_float(data.get("amount"))
    if amt is None:
        return jsonify({"ok": False, "msg": "请填工资金额"}), 400
    cols = {
        'use_date': d, 'channel': '现金', 'merchant': '兼职制作', 'item_name': '兼职工资',
        'cat1': '人工', 'cat2': '人工', 'cost_kind': 'labor', 'is_cost': 1,
        'amount': amt, 'purpose': '当日兼职工资', 'note': _exp_str(data.get("note")),
        'created_by': u.get('id'), 'created_by_name': u.get('name') or u.get('username'),
    }
    keys = list(cols.keys())
    db.execute("INSERT INTO expenses (" + ",".join(keys) + ") VALUES (" + ",".join("?" * len(keys)) + ")",
               [cols[k] for k in keys])
    db.commit()
    return jsonify({"ok": True, "id": db.execute("SELECT last_insert_rowid()").fetchone()[0]})


@app.route("/api/finance/labor")
def list_daily_labor():
    """按月看兼职工资（按天汇总），供财务页显示。"""
    month = request.args.get("month")
    where = ["deleted_at IS NULL", "cost_kind='labor'"]
    params = []
    if month:
        where.append("use_date LIKE ?"); params.append(month + "%")
    rows = g.db.execute(
        "SELECT use_date, COALESCE(SUM(amount),0) AS amount, COUNT(*) AS n FROM expenses "
        "WHERE " + " AND ".join(where) + " GROUP BY use_date ORDER BY use_date DESC", params).fetchall()
    total = sum(float(r["amount"] or 0) for r in rows)
    return jsonify({"ok": True, "data": [dict(r) for r in rows], "total": round(total, 2)})


@app.route("/api/orders/export/sales")
def export_sales_ledger():
    """导出「销售流水表」（复刻原表格式）：每单一行 + 月份合并单元格 + 右侧月度汇总块。
    单笔口径：价格 − 配送费 = 实收；周期口径：实收 − 当日兼职工资分摊 = 净到手。"""
    from flask import Response
    from urllib.parse import quote as _q
    import io, datetime as _dt
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except Exception:
        return jsonify({"ok": False, "msg": "需要 openpyxl: pip install openpyxl"}), 500
    month = request.args.get("month")
    db = g.db
    where = ["o.deleted_at IS NULL"]
    params = []
    if month:
        where.append("o.booking_date LIKE ?"); params.append(month + "%")
    rows = db.execute("""
        SELECT o.id, o.booking_date, o.menu_name, o.setup_flag, o.delivery_fee, o.amount,
               o.delivery_person, o.address, o.source, o.note,
               (SELECT GROUP_CONCAT(p.name, '、') FROM order_packages op JOIN packages p ON p.id=op.package_id
                 WHERE op.order_id=o.id) AS pkgnames,
               (SELECT SUM(COALESCE(op.price, p.price)*op.quantity) FROM order_packages op JOIN packages p ON p.id=op.package_id
                 WHERE op.order_id=o.id) AS price_total
        FROM orders o WHERE """ + " AND ".join(where) + " ORDER BY o.booking_date ASC, o.id ASC", params).fetchall()

    labor_by_date = {}
    labor_sql = ("SELECT use_date AS d, COALESCE(SUM(amount),0) AS a FROM expenses "
                 "WHERE deleted_at IS NULL AND cost_kind='labor'")
    if month:
        labor_sql += " AND use_date LIKE ?"
        for r in db.execute(labor_sql + " GROUP BY use_date", (month + "%",)):
            labor_by_date[r["d"]] = float(r["a"] or 0)
    else:
        for r in db.execute(labor_sql + " GROUP BY use_date"):
            labor_by_date[r["d"]] = float(r["a"] or 0)
    cnt_by_date = {}
    for r in rows:
        d = r["booking_date"] or ""
        cnt_by_date[d] = cnt_by_date.get(d, 0) + 1

    wb = Workbook(); ws = wb.active; ws.title = "销售流水"
    thin = Side(style="thin", color="999999")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="EFE6DA")
    head_font = Font(bold=True, size=11)
    center = Alignment(horizontal="center", vertical="center")
    headers = ["月份", "时间", "套餐", "搭建", "价格", "配送费", "实收", "配送", "地址",
               "客户来源", "备注", "兼职工资分摊", "净到手"]
    for c, h in enumerate(headers, 1):
        cell = ws.cell(1, c, h)
        cell.font = head_font; cell.fill = head_fill; cell.alignment = center; cell.border = border

    months, month_amt = [], {}
    r_idx = 2; month_start = 2; prev_month = None
    for r in rows:
        d = r["booking_date"] or ""
        mon = ("%d月" % int(d[5:7])) if (len(d) >= 7 and d[5:7].isdigit()) else ""
        price = r["price_total"]
        if price in (None, 0):
            price = (r["amount"] or 0) + (r["delivery_fee"] or 0)
        fee = r["delivery_fee"] if r["delivery_fee"] is not None else None
        amt = r["amount"] or 0
        labor = labor_by_date.get(d, 0) / max(cnt_by_date.get(d, 1), 1)
        net = amt - labor
        pkg = r["menu_name"] or r["pkgnames"] or ""
        setup = "是" if r["setup_flag"] == 1 else ("否" if r["setup_flag"] == 0 else "")
        vals = [mon, d, pkg, setup, price, fee, amt, r["delivery_person"] or "", r["address"] or "",
                r["source"] or "", r["note"] or "", round(labor, 2) if labor else None, round(net, 2)]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(r_idx, c, v); cell.border = border
            if c in (1, 4):
                cell.alignment = center
        if mon:
            if mon != prev_month:
                if prev_month is not None and month_start <= r_idx - 1:
                    ws.merge_cells(start_row=month_start, start_column=1, end_row=r_idx - 1, end_column=1)
                months.append(mon); month_start = r_idx; prev_month = mon
            month_amt[mon] = month_amt.get(mon, 0.0) + amt
        r_idx += 1
    if prev_month is not None and month_start <= r_idx - 1:
        ws.merge_cells(start_row=month_start, start_column=1, end_row=r_idx - 1, end_column=1)

    sc = 15  # 右侧汇总块从第 O 列开始
    ws.cell(1, sc, "月度汇总").font = head_font
    for j, h in enumerate(["月份", "实收合计", "兼职工资", "净到手", "环比"]):
        cell = ws.cell(2, sc + j, h)
        cell.font = head_font; cell.fill = head_fill; cell.alignment = center; cell.border = border
    month_labor = {}
    for _dd, _aa in labor_by_date.items():
        if len(_dd) >= 7 and _dd[5:7].isdigit():
            _mm = "%d月" % int(_dd[5:7])
            month_labor[_mm] = month_labor.get(_mm, 0.0) + _aa
    prev_net = None
    for i, mon in enumerate(months):
        amt_s = month_amt.get(mon, 0.0)
        labor_s = month_labor.get(mon, 0.0)
        net_s = amt_s - labor_s
        chg = "" if prev_net in (None, 0) else ("%.1f%%" % ((net_s - prev_net) / abs(prev_net) * 100))
        for j, v in enumerate([mon, round(amt_s, 2), round(labor_s, 2), round(net_s, 2), chg]):
            cell = ws.cell(3 + i, sc + j, v); cell.border = border; cell.alignment = center
        prev_net = net_s

    for i, w in enumerate([7, 12, 12, 6, 9, 9, 9, 8, 18, 10, 16, 11, 10], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for j in range(5):
        ws.column_dimensions[get_column_letter(sc + j)].width = 11
    ws.freeze_panes = "A2"

    bio = io.BytesIO(); wb.save(bio); bio.seek(0)
    _now = _dt.datetime.now()
    fname = "销售流水表%s-%d-%d.xlsx" % (("（%s）" % month) if month else "", _now.month, _now.day)
    resp = Response(bio.getvalue(),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp.headers["Content-Disposition"] = "attachment; filename=\"sales.xlsx\"; filename*=UTF-8''" + _q(fname)
    return resp


@app.route("/api/orders/batch", methods=["POST"])
def batch_orders():
    """批量操作订单：delete=软删进回收站 / purge=彻底删除 / restore=恢复"""
    data = request.get_json(force=True)
    action = data.get("action")
    ids = data.get("ids") or []
    if action not in ("delete", "purge", "restore"):
        return jsonify({"ok": False, "msg": "未知操作"}), 400
    if not isinstance(ids, list):
        return jsonify({"ok": False, "msg": "ids 格式错误"}), 400
    ids = [int(x) for x in ids if str(x).isdigit()]
    if not ids:
        return jsonify({"ok": False, "msg": "未选择任何订单"}), 400
    # 店员只能批量处理自己录的单：有别人的单就直接拒绝（不做"部分执行"，避免误删）
    if not _is_boss():
        placeholders0 = ",".join("?" * len(ids))
        bad = g.db.execute(
            f"SELECT COUNT(*) c FROM orders WHERE id IN ({placeholders0}) "
            f"AND (created_by IS NULL OR created_by<>?)", (ids + [_cur_uid()])).fetchone()["c"]
        if bad:
            return jsonify({"ok": False, "msg": f"无权限：选中的 {bad} 个订单不是你录的，你只能处理自己录的订单"}), 403
    placeholders = ",".join("?" * len(ids))
    db = g.db
    if action == "delete":
        cur = db.execute(
            f"UPDATE orders SET deleted_at=datetime('now','localtime') WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            ids)
    elif action == "restore":
        cur = db.execute(
            f"UPDATE orders SET deleted_at=NULL WHERE id IN ({placeholders})", ids)
    else:  # purge
        cur = db.execute(
            f"DELETE FROM orders WHERE id IN ({placeholders})", ids)
    affected = cur.rowcount
    db.commit()
    return jsonify({"ok": True, "affected": affected, "msg": f"已处理 {affected} 个订单"})


@app.route("/api/orders/<int:oid>", methods=["PATCH", "DELETE"])
def update_order_status(oid):
    db = g.db
    # 删除订单 → 进回收站（软删除），7 天内可恢复
    if request.method == "DELETE":
        cur = db.execute("SELECT id FROM orders WHERE id=?", (oid,))
        if not cur.fetchone():
            return jsonify({"ok": False, "msg": "订单不存在"}), 404
        if not _can_touch_order(db, oid):
            return _deny_order()
        db.execute("UPDATE orders SET deleted_at=datetime('now','localtime') WHERE id=?", (oid,))
        db.commit()
        return jsonify({"ok": True, "msg": "已放进回收站，7 天内可恢复"})
    data = request.get_json(force=True)
    status = data.get("status")
    if status not in ("pending", "preparing", "done", "cancelled"):
        return jsonify({"ok": False, "msg": "状态非法"}), 400
    if not _can_touch_order(db, oid):
        return _deny_order()
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
    """录入页数据。可选 by=me|0|<user_id>：只影响今日订单/今日营收/待备预约列表"""
    _sc, _sp = _signer_clause(request.args)
    return jsonify({"ok": True, "data": calc_dashboard(_sc, _sp)})


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
        w.writerow(["套餐", "人数范围", "类别", "食材/工具", "单位", "每份克数", "份数", "总量", "总价"])
        for p in pkgs:
            people = f"{p['min_people']}-{p['max_people']}人"
            for ing in p["ingredients"]:
                size = ing["per_package"] / max(1, ing["portion_count"])
                w.writerow([p["name"], people, ing["category"], ing["ingredient"],
                           ing["unit"], round(size, 2), ing["portion_count"],
                           ing["per_package"], p["price"]])
            for t in p["tools"]:
                w.writerow([p["name"], people, "tool", t["tool"], "个", "", "",
                            t["per_package"], p["price"]])
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

    # 与网页菜单完全一致的分类顺序与标签（以菜单原数据页 CAT_ORDER 为准）
    # beef→pork→chicken→vegetable→side→sauce→drink→packaging→utensil→staple→other→tool
    EXPORT_CAT_ORDER = [
        "beef", "pork", "chicken", "vegetable", "side", "sauce", "drink",
        "packaging", "utensil", "staple", "other", "tool"
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
        # 客户餐具固定顺序（与菜单页一致）：一次性油壶属包装，小料中的油属餐具
        ['utensil','三格底料盒'],['utensil','筷子'],['utensil','勺子'],['utensil','纸杯'],
        ['utensil','纸巾'],['utensil','围裙'],['utensil','油'],['utensil','垃圾袋'],['utensil','一次性桌布'],
        # 食材包装固定顺序（与菜单页一致）：含一次性油壶
        ['packaging','金色打包盒'],['packaging','圆形透明打包盒'],['packaging','生菜水果打包盒'],['packaging','烤肉盒子'],['packaging','绑带'],
        ['packaging','杂物保温袋'],['packaging','餐具打包袋'],['packaging','一次性油壶'],
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
        ws.append([f"{p['name']}  ·  {p['min_people']}-{p['max_people']}人  ·  总价¥{p['price']}", "", "", "", "", "", ""])
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
                if cat in ("packaging", "utensil"):
                    # 食材包装/客户餐具：每份数量无意义，直接划掉；份数=该套餐总数量
                    ws.append([ing["ingredient"], ing["per_package"], "—", ing["unit"], ing["per_package"], "", ""])
                else:
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
                # 工具：每份数量无意义，直接划掉；份数=该套餐需要的总数量
                ws.append([t["tool"], t["per_package"], "—", "个", t["per_package"], "", ""])
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
    from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
    from urllib.parse import quote as _quote2
    _now2 = _dt2.now(_tz2(_td2(hours=8)))
    # 用"点"代替冒号：Windows 文件名不允许 ':'（会被系统换成 _）
    _mname = f"菜单表{_now2.year}-{_now2.month}-{_now2.day}-{_now2.hour}点{_now2.minute:02d}.xlsx"
    resp = app.response_class(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    # 中文文件名要用 RFC 5987 filename* 编码，否则 response header latin-1 会报错
    resp.headers["Content-Disposition"] = "attachment; filename=\"menu.xlsx\"; filename*=UTF-8''" + _quote2(_mname)
    return resp


# ===== 备餐矩阵（按日期，修掉原 print/menu 不分日期的坑）=====
# 行=食材(按分类)，列=当天各套餐类型(含套数)，末尾总份数/总备料量/备餐勾选。
@app.route("/api/prep/export/matrix")
def export_prep_matrix():
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        import io as _io
    except ImportError:
        return jsonify({"ok": False, "msg": "需要 openpyxl: pip install openpyxl"}), 500
    from calculator import (_subcategorize_meat, PACKAGING_CATEGORY, UTENSIL_CATEGORY)
    date = request.args.get("date")
    cur = g.db.cursor()
    if not date:
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=8))
        date = datetime.now(tz).strftime("%Y-%m-%d")
    cur.execute("""
        SELECT id, booking_date, booking_time, address, contact_name, status
        FROM orders WHERE booking_date=? AND status IN ('pending','preparing') AND deleted_at IS NULL
    """ + _signer_clause(request.args)[0] + """
        ORDER BY id
    """, (date,) + tuple(_signer_clause(request.args)[1]))
    orders = [dict(r) for r in cur.fetchall()]
    if not orders:
        return jsonify({"ok": False, "msg": f"{date} 无待备订单"}), 400
    oids = [o["id"] for o in orders]
    pkg_sets = {}
    order_pkgs = {}
    for o in orders:
        cur.execute("SELECT package_id, quantity FROM order_packages WHERE order_id=?", (o["id"],))
        lst = [(r["package_id"], r["quantity"]) for r in cur.fetchall()]
        order_pkgs[o["id"]] = lst
        for pid, q in lst:
            pkg_sets[pid] = pkg_sets.get(pid, 0) + q
    pids = list(pkg_sets.keys())
    if pids:
        cur.execute("SELECT id, name, min_people, max_people FROM packages WHERE id IN (%s)" % ",".join("?"*len(pids)), tuple(pids))
        pkg_info = {r["id"]: dict(r) for r in cur.fetchall()}
    else:
        pkg_info = {}
    pkg_cols = sorted(pids, key=lambda x: pkg_info[x]["min_people"])
    # 各套餐配方（cost_only=0，与备餐页一致）
    recipe = {}
    tool_recipe = {}
    for pid in pids:
        cur.execute("""
            SELECT i.id, i.name, i.unit, i.category, i.cost, pi.per_package, pi.portion_count
            FROM package_ingredients pi JOIN ingredients i ON pi.ingredient_id = i.id
            WHERE pi.package_id = ? AND pi.cost_only = 0
        """, (pid,))
        recipe[pid] = {r["id"]: dict(r) for r in cur.fetchall()}
        cur.execute("""
            SELECT t.id, t.name, t.cost, pt.per_package
            FROM package_tools pt JOIN tools t ON pt.tool_id = t.id
            WHERE pt.package_id = ?
        """, (pid,))
        tool_recipe[pid] = {r["id"]: dict(r) for r in cur.fetchall()}
    # 聚合：by_pkg[pid] = [份数, 量]
    ing = {}
    tool = {}
    for oid, lst in order_pkgs.items():
        for pid, q in lst:
            for iid, r in recipe.get(pid, {}).items():
                pc = r["portion_count"]; pp = r["per_package"]
                spec_v = pp / pc if pc else 0
                d = ing.setdefault(iid, {"id": iid, "name": r["name"], "unit": r["unit"] or "",
                                         "category": r["category"] or "other",
                                         "cost": r["cost"] or 0, "spec": spec_v,
                                         "by_pkg": {}, "order_ids": set()})
                cur2 = d["by_pkg"].get(pid, [0, 0])
                d["by_pkg"][pid] = [cur2[0] + pc * q, cur2[1] + pp * q]
                d["order_ids"].add(oid)
            for tid, r in tool_recipe.get(pid, {}).items():
                d = tool.setdefault(tid, {"id": tid, "name": r["name"], "cost": r["cost"] or 0,
                                         "by_pkg": {}, "order_ids": set()})
                cur2 = d["by_pkg"].get(pid, [0, 0])
                d["by_pkg"][pid] = [cur2[0] + r["per_package"] * q, cur2[1] + r["per_package"] * q]
                d["order_ids"].add(oid)
    # 勾选状态（全部相关单都勾了才算勾）
    checked_detail = {}
    if oids:
        cur.execute("SELECT order_id, item_type, item_id, checked FROM prep_checklist WHERE order_id IN (%s)" % ",".join("?"*len(oids)), tuple(oids))
        for r in cur.fetchall():
            checked_detail[(r["order_id"], r["item_type"], r["item_id"])] = bool(r["checked"])

    def is_checked(iid, order_ids, itype):
        vals = [checked_detail.get((oid, itype, iid), False) for oid in order_ids]
        return bool(vals) and all(vals)

    cat_colors = {'beef':'8E1E1A','pork':'C75D3E','chicken':'C9962B','vegetable':'3A8A3A',
                  'side':'5A8A3A','sauce':'7A5A3A','drink':'666666','staple':'666666',
                  'packaging':'D35400','utensil':'8E44AD','other':'666666','tool':'4A4A4A'}
    cat_labels = {'beef':'🥩 牛肉','pork':'🥓 猪肉','chicken':'🍗 鸡肉','vegetable':'🥬 素菜',
                  'side':'🥗 小菜','sauce':'🧂 小料','drink':'🎁 赠品','staple':'🍚 主食',
                  'packaging':'📦 食材包装','utensil':'🍱 客户餐具','other':'📦 其他','tool':'🔧 工具'}
    EXPORT_CAT_ORDER = ['beef','pork','chicken','vegetable','side','sauce','drink','staple','packaging','utensil','other','tool']
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

    def eff_cat(v, is_tool=False):
        if is_tool:
            return 'tool'
        raw = v["category"]
        if raw in ('packaging','utensil','drink','staple','side','sauce','other','vegetable'):
            return raw
        return _subcategorize_meat(v["name"], raw)

    def ing_sort_idx(name, cat):
        for i, (c, kw) in enumerate(ing_fixed_order):
            if c == cat and kw in (name or ''):
                return i
        return 999

    cats = {c: [] for c in EXPORT_CAT_ORDER}
    for d in ing.values():
        cats[eff_cat(d)].append(d)
    for d in tool.values():
        cats['tool'].append(d)

    thin = Side(border_style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")
    left = Alignment(horizontal="left", vertical="center", indent=1)
    ncol = 3 + len(pkg_cols) + 4  # 类别,品名,单份规格 | 套餐列 | 总份数,总备料量,成本,备餐勾选

    wb = Workbook(); ws = wb.active; ws.title = "备餐表"
    ws.append([f"刘和牛户外烤肉 · {date} 备餐清单"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF", size=13)
        cell.fill = PatternFill("solid", fgColor="3A2416")
        cell.alignment = center
    _combo = " ｜ ".join(f"{pkg_info[pid]['name']}×{pkg_sets[pid]}套" for pid in pkg_cols)
    ws.append([f"配套方案：{_combo}（共 {len(orders)} 单，合计 {sum(pkg_sets.values())} 套）"])
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    for cell in ws[2]:
        cell.font = Font(italic=True, color="8B7355", size=10)
        cell.alignment = left
    headers = ["类别", "品名", "单份规格"] + [f"{pkg_info[pid]['name']}({pkg_sets[pid]}套)" for pid in pkg_cols] + ["总份数", "总备料量", "成本(元)", "备餐勾选"]
    ws.append(headers)
    for cell in ws[3]:
        cell.font = Font(bold=True, color="FFFFFF", size=11)
        cell.fill = PatternFill("solid", fgColor="3A2416")
        cell.alignment = center
        cell.border = border

    def _num(x):
        """整数就不显示 .0（900.0 → 900），与参考表一致"""
        try:
            x = round(float(x), 2)
        except Exception:
            return x
        return int(x) if abs(x - round(x)) < 1e-9 else x
    # 类别列用的简称（与参考备餐清单一致：荤菜/素菜/主食/小菜/蘸料/饮料/餐具配套/装备）
    cat_short = {'beef': '荤菜', 'pork': '荤菜', 'chicken': '荤菜', 'vegetable': '素菜',
                 'staple': '主食', 'side': '小菜', 'sauce': '蘸料', 'drink': '饮料',
                 'packaging': '餐具配套', 'utensil': '餐具配套', 'other': '其他', 'tool': '装备'}
    for cat in EXPORT_CAT_ORDER:
        items = cats[cat]
        if not items:
            continue
        is_tool = (cat == 'tool')
        items.sort(key=lambda x: (ing_sort_idx(x["name"], cat), x["name"]))
        ws.append([f"{cat_labels.get(cat, cat)} · {len(items)}项"])
        ws.merge_cells(start_row=ws.max_row, start_column=1, end_row=ws.max_row, end_column=ncol)
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True, color="FFFFFF", size=10)
            cell.fill = PatternFill("solid", fgColor=cat_colors.get(cat, "666666"))
            cell.alignment = left
        for it in items:
            by_pkg = it["by_pkg"]
            if is_tool:
                spec = ""; unit = "个"
            else:
                unit = it["unit"] or ""
                spec = (_num(it["spec"]) if it.get("spec") else "")
            pkg_cells = []
            tot_cnt = 0.0; tot_amt = 0.0
            for pid in pkg_cols:
                if pid in by_pkg:
                    cnt, amt = by_pkg[pid]
                    pkg_cells.append(round(cnt) if not is_tool else round(amt))
                    tot_cnt += cnt
                    tot_amt += amt
                else:
                    pkg_cells.append("")
            if is_tool:
                total_str = f"{round(tot_amt)}个"
                cnt_str = round(tot_amt)
            else:
                total_str = (f"{_num(tot_amt)}{unit}" if (tot_amt and unit)
                             else (f"{_num(tot_amt)}" if tot_amt else "-"))
                cnt_str = round(tot_cnt)
            # 成本 = 总备料量(基础单位) × 食材单价（与系统成本口径一致）
            cost_val = round(tot_amt * (it.get("cost") or 0), 2)
            checked = is_checked(it.get("id"), it["order_ids"], "tool" if is_tool else "ingredient")
            chk = "✓" if checked else "☐"
            row = [cat_short.get(cat, ""), it["name"], (f"{spec}{unit}" if spec != "" else "—")] + pkg_cells + [cnt_str, total_str, cost_val, chk]
            ws.append(row)
            for cell in ws[ws.max_row]:
                cell.border = border
                cell.alignment = center
            ws.cell(ws.max_row, 1).alignment = left                 # 类别
            ws.cell(ws.max_row, 2).alignment = left                 # 品名
            ws.cell(ws.max_row, 2).font = Font(bold=True)
            ws.cell(ws.max_row, ncol - 2).font = Font(bold=True, color="8E1E1A")   # 总备料量
            ws.cell(ws.max_row, ncol - 1).font = Font(bold=True, color="8E1E1A")   # 成本
            ws.cell(ws.max_row, ncol - 1).number_format = "0.##"
            ws.cell(ws.max_row, ncol).font = Font(bold=True, color=("27AE60" if checked else "999999"), size=14)

    widths = [10, 22, 12] + [13] * len(pkg_cols) + [9, 12, 11, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A4"
    buf = _io.BytesIO()
    wb.save(buf)
    resp = app.response_class(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    from datetime import datetime as _dt3, timezone as _tz3, timedelta as _td3
    from urllib.parse import quote as _quote3
    _now3 = _dt3.now(_tz3(_td3(hours=8)))
    # 用"点"代替冒号：Windows 文件名不允许 ':'（会被系统换成 _）
    _pname = f"备餐表{_now3.year}-{_now3.month}-{_now3.day}-{_now3.hour}点{_now3.minute:02d}.xlsx"
    # 中文文件名要用 RFC 5987 filename* 编码，否则 response header latin-1 会报错
    resp.headers["Content-Disposition"] = "attachment; filename=\"prep.xlsx\"; filename*=UTF-8''" + _quote3(_pname)
    return resp


# 导出"搭建装备表"：按单逐行（搭建时间/用餐时间/位置/客户/装备数量/特殊需求），
# 给搭建师傅按单拿装备、门店备工具用。
# 只含需要搭建的单（自动排除"不搭建"单），不显示与搭建无关的「是否搭建/付款情况」列。
# 主要供备餐页按钮调用：前端传当前所选日期 ?date=YYYY-MM-DD（即 datePick 选中的日期，导出"所选日期"的搭建装备表）。
# 也可用 ?date= 指定任意日期；无参数时兜底为"明天"（兼容旧直链）。
@app.route("/api/prep/export/setup")
def export_setup_sheet():
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        import io as _io
    except ImportError:
        return jsonify({"ok": False, "msg": "需要 openpyxl: pip install openpyxl"}), 500
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import re as _re
    _tz8 = _tz(_td(hours=8))
    date = request.args.get("date")
    if not date:
        date = (_dt.now(_tz8) + _td(days=1)).strftime("%Y-%m-%d")
    cur = g.db.cursor()
    cur.execute("""
        SELECT o.id, o.booking_date, o.booking_time, o.meal_time, o.address,
               o.contact_name, o.contact_phone, o.note,
               COALESCE(u.name, o.created_by_name) AS signer
        FROM orders o LEFT JOIN users u ON o.created_by = u.id
        WHERE o.booking_date=? AND o.status IN ('pending','preparing') AND o.deleted_at IS NULL
          AND (o.raw_text IS NULL OR o.raw_text NOT LIKE '%不搭建%')
        ORDER BY o.booking_time, o.id
    """, (date,))
    orders = [dict(r) for r in cur.fetchall()]
    if not orders:
        return jsonify({"ok": False, "msg": f"{date} 没有需要搭建的订单（已自动排除不搭建单）"}), 400

    from calculator import calc_order_tool_needs

    # 本表只列"需要搭建"的单，不搭建单已在 SQL 中排除；付款情况等与搭建无关的信息不展示。
    def time_txt(v):
        v = (v or "").strip()
        # 解析器把"13点"存成 13.00 这类点号格式，导出时还原成 13:00
        if _re.match(r"^\d{1,2}\.\d{2}$", v):
            v = v.replace(".", ":", 1)
        return v

    def _intish(v):
        """装备数量是浮点(2.0)时显示成整数(2)"""
        try:
            f = float(v)
            return int(f) if abs(f - round(f)) < 1e-9 else f
        except Exception:
            return v

    headers = ["序号", "归属", "日期", "搭建时间", "用餐时间", "用餐位置",
               "客户姓名", "客户联系方式", "套餐", "人数",
               "天幕", "桌子", "椅子", "卡式炉", "其他装备", "特殊需求"]
    ncol = len(headers)
    rows = []
    totals = {"天幕": 0, "桌子": 0, "椅子": 0, "卡式炉": 0}
    for i, o in enumerate(orders, 1):
        cur.execute("""
            SELECT p.name, p.max_people, op.quantity
            FROM order_packages op JOIN packages p ON op.package_id = p.id
            WHERE op.order_id=?
        """, (o["id"],))
        pkgs = cur.fetchall()
        pkg_txt = "、".join(f"{r['name']}×{r['quantity']}" for r in pkgs)
        people = sum((r["max_people"] or 0) * (r["quantity"] or 1) for r in pkgs)

        needs = calc_order_tool_needs(o["id"]) or {}
        names = {}
        for tid, info in needs.items():
            if not info.get("total"):
                continue
            tr = cur.execute("SELECT name FROM tools WHERE id=?", (tid,)).fetchone()
            if tr:
                names[tr["name"]] = info["total"]

        def tmatch(*kws):
            for nm, tot in names.items():
                if any(k in nm for k in kws):
                    return tot
            return ""
        tian, zhuo, yi, ka = (_intish(tmatch("天幕")), _intish(tmatch("桌子", "蛋卷桌")),
                              _intish(tmatch("椅子")), _intish(tmatch("卡式炉")))
        fixed_keys = ("天幕", "桌子", "蛋卷桌", "椅子", "卡式炉")
        other = "、".join(f"{nm}×{_intish(tot)}" for nm, tot in names.items()
                          if not any(k in nm for k in fixed_keys))
        for k, v in (("天幕", tian), ("桌子", zhuo), ("椅子", yi), ("卡式炉", ka)):
            if isinstance(v, (int, float)):
                totals[k] += v

        d_txt = o["booking_date"] or ""
        try:
            _d = _dt.strptime(d_txt, "%Y-%m-%d")
            d_txt = f"{_d.month}月{_d.day}日"
        except Exception:
            pass
        note = (o["note"] or "").strip()
        rows.append([
            i, o["signer"] or "", d_txt, time_txt(o["booking_time"]), time_txt(o["meal_time"]),
            o["address"] or "", o["contact_name"] or "",
            o["contact_phone"] or "", pkg_txt, people or "",
            tian, zhuo, yi, ka, other, note if note and note != "无" else "无",
        ])

    thin = Side(border_style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    leftw = Alignment(horizontal="left", vertical="center", wrap_text=True, indent=1)

    wb = Workbook()
    ws = wb.active
    ws.title = "搭建装备表"
    ws.append([f"刘和牛户外烤肉 · {date} 搭建装备表（共 {len(orders)} 单）"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF", size=13)
        cell.fill = PatternFill("solid", fgColor="3A2416")
        cell.alignment = center
    ws.append(headers)
    for cell in ws[2]:
        cell.font = Font(bold=True, color="FFFFFF", size=10)
        cell.fill = PatternFill("solid", fgColor="3A2416")
        cell.alignment = center
        cell.border = border
    left_cols = {6, 9, 15, 16}   # 用餐位置/套餐/其他装备/特殊需求 左对齐
    for r in rows:
        ws.append(r)
        rr = ws.max_row
        for ci in range(1, ncol + 1):
            cell = ws.cell(rr, ci)
            cell.border = border
            cell.alignment = leftw if ci in left_cols else center
            if ci in (11, 12, 13, 14) and isinstance(cell.value, (int, float)) and cell.value > 0:
                cell.font = Font(bold=True)
        # 特殊需求有实际内容时标红（对齐用户参考表的习惯）
        if r[15] != "无":
            ws.cell(rr, 16).font = Font(bold=True, color="C0392B")
    # 合计行：搭建师傅拿装备的总量
    ws.append(["合计", "", "", "", "", "", "", "", "", "",
               _intish(totals["天幕"]), _intish(totals["桌子"]),
               _intish(totals["椅子"]), _intish(totals["卡式炉"]), "", ""])
    rr = ws.max_row
    for ci in range(1, ncol + 1):
        cell = ws.cell(rr, ci)
        cell.border = border
        cell.alignment = center
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="C75D3E")
    widths = [5, 6, 9, 9, 9, 15, 11, 13, 17, 6, 6, 6, 6, 7, 13, 22]
    for i2, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i2)].width = w
    ws.freeze_panes = "C3"   # 冻结「序号/归属」两列，横向滚动时仍能对上是谁的单
    buf = _io.BytesIO()
    wb.save(buf)
    resp = app.response_class(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    from urllib.parse import quote as _quote4
    _pname = f"搭建装备表{date}.xlsx"
    resp.headers["Content-Disposition"] = "attachment; filename=\"setup.xlsx\"; filename*=UTF-8''" + _quote4(_pname)
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
    可选参数：date_from, date_to（YYYY-MM-DD），按 booking_date 过滤
             by=me|0|<user_id>，按"归属"（orders.created_by）过滤"""
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
    # 归属筛选（同一份条件用于下面每条查询，保证 KPI 和明细口径一致）
    _sc, _sp = _signer_clause(request.args, "o")
    date_clause += _sc
    params.extend(_sp)
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
    可选参数：date_from, date_to（YYYY-MM-DD），按 booking_date 过滤
             by=me|0|<user_id>，按"归属"过滤"""
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
    _sc, _sp = _signer_clause(request.args, "o")
    date_clause += _sc
    params.extend(_sp)
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
    """成本与利润配置：场地单价表、每单默认配送费/人工/耗材、固定开销、毛利率警戒线"""
    from profit import get_config, save_config
    db = g.db
    if request.method == "GET":
        return jsonify({"ok": True, "data": get_config(db)})
    data = request.get_json(force=True) or {}
    cfg = save_config(db, data)
    return jsonify({"ok": True, "data": cfg})


@app.route("/api/finance/order/<int:oid>/profit")
def order_profit_api(oid):
    """单笔订单利润：收入、成本项明细（食材/搭建回收/配送费/人工/耗材/其他）、现金利润、净利、毛利率"""
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
    if not _can_touch_order(db, oid):
        return _deny_order()
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
    if not _can_touch_order(db, oid):
        return _deny_order()
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
    """期间利润汇总：现金利润 + 含固定开销摊销的净利，含每单利润与标红
    可选 by=me|0|<user_id> 按"归属"筛选（固定开销仍按每单摊销，不会全压到一个人头上）"""
    from profit import period_overview
    _sc, _sp = _signer_clause(request.args, "o")
    ov = period_overview(g.db, request.args.get("date_from"), request.args.get("date_to"),
                         extra_clause=_sc, extra_params=_sp)
    return jsonify({"ok": True, "data": ov})


# ===== 门店支出台账（2026-10-08）=====
# 散落各渠道的支出统一登记，支持手动录入 / 粘贴账单文字识别 / 渠道对账（不丢项、知道上次做到哪），
# 「是否计入成本」标记后联动财务页利润。
def _exp_str(v):
    return (v or "").strip() if v is not None else ""
def _exp_int(v):
    try: return int(v) if v not in (None, "") else None
    except (TypeError, ValueError): return None
def _exp_float(v):
    try: return round(float(v), 2) if v not in (None, "") else None
    except (TypeError, ValueError): return None

import re as _re
_KW_RE = _re.compile(r'(支出|收入|扣款|支付|实付|花费|转账|退款|到账|交易|消费|¥|元|块|￥)')
def _exp_parse(text):
    """把渠道账单原文拆成候选支出行（文字识别，非 OCR）。返回 [{line_no,raw,date,merchant,amount}]。

    解析策略（针对微信/支付宝/淘宝/1688 账单常见格式）：
      · 日期优先用 YYYY-MM-DD / X月X日，缺年份补当前年；
      · 金额优先取带 ¥/￥ 前缀的（避开年份、时间这类数字），没有货币符号时取"非时间数字"里最大那个；
      · 商户名 = 去掉日期/时间/金额/交易类型词（支出/支付/元…）后剩下的文本。
    """
    if not text:
        return []
    date_re = _re.compile(r'(\d{4})[-/年月.\s](\d{1,2})[-/月日.\s](\d{1,2})日?|(\d{1,2})[-/月日.\s](\d{1,2})日?')
    cur_re = _re.compile(r'[¥￥]\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)')
    num_re = _re.compile(r'\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?')
    time_re = _re.compile(r'\d{1,2}\s*:\s*\d{2}')
    out = []
    for i, raw in enumerate(text.replace('\r', '\n').split('\n'), 1):
        s = raw.strip()
        if not s:
            continue
        m = date_re.search(s)
        pdate = None
        if m:
            if m.group(1):
                y, mo, d = m.group(1), m.group(2), m.group(3)
            else:
                y, mo, d = _dt_year(), m.group(4), m.group(5)
            try:
                yi, moi, di = int(y), int(mo), int(d)
                pdate = '%04d-%02d-%02d' % (yi, moi, di) if (1 <= moi <= 12 and 1 <= di <= 31) else None
            except Exception:
                pdate = None
        merchant = s
        if pdate:
            merchant = merchant.replace(m.group(0), ' ')
        # 金额：优先带 ¥/￥ 的；没有则取非时间数字里最大那个
        cur = []
        for mm in cur_re.finditer(merchant):
            try: cur.append(float(mm.group(1).replace(',', '')))
            except Exception: pass
        if cur:
            amount = round(max(cur), 2)
        else:
            nums = []
            for mm in num_re.finditer(merchant):
                around = merchant[max(0, mm.start() - 1):mm.end() + 1]
                if ':' in around:
                    continue
                try: nums.append(float(mm.group(0).replace(',', '')))
                except Exception: pass
            amount = round(max(nums), 2) if nums else None
        # 商户名清洗：去时间、去交易类型词、去残留数字
        merchant = time_re.sub(' ', merchant)
        merchant = _re.sub(r'[-–—]', ' ', merchant)
        merchant = _KW_RE.sub(' ', merchant)
        for mm in num_re.finditer(merchant):
            merchant = merchant.replace(mm.group(0), ' ')
        merchant = _re.sub(r'\s+', ' ', merchant).strip(' -·•*，。、')
        if not merchant:
            merchant = s[:40]
        out.append({'line_no': i, 'raw': raw, 'date': pdate,
                    'merchant': merchant, 'amount': amount})
    return out

def _dt_year():
    import datetime as _dt
    return _dt.date.today().year

def _exp_parse_csv(text):
    """解析「账单导出 CSV」（微信/支付宝「用于个人对账」账单明细）为候选支出行。

    与 _exp_parse（一行一段的乱文本）不同：这里先用表头定位列，再只取「支出」方向的行，
    所以是"全量、结构化、零识别误差"的付款台账来源。
      日期=交易时间、商户=交易对方(缺则取商品)、金额=金额(元) 去掉 ¥￥, 空格。
    找不到可识别表头时，回退到 _exp_parse（按行文本解析兜底）。
    """
    if not text:
        return []
    import csv as _csv, io as _io
    text = text.lstrip('\ufeff')
    try:
        rows = list(_csv.reader(_io.StringIO(text)))
    except Exception:
        return _exp_parse(text)
    hi, idx = -1, {}
    for i, r in enumerate(rows):
        cells = [(c or '').strip() for c in r]
        joined = '|'.join(cells)
        if ('时间' in joined) and ('金额' in joined) and ('对方' in joined or '收' in joined or '商品' in joined):
            idx = {}
            for j, c in enumerate(cells):
                if not c:
                    continue
                if ('交易时间' in c or c == '时间') and 'date' not in idx:
                    idx['date'] = j
                elif ('交易对方' in c or '对方' in c or '商户' in c) and 'merchant' not in idx:
                    idx['merchant'] = j
                elif ('金额' in c) and 'amount' not in idx:
                    idx['amount'] = j
                elif ('收/支' in c or '收支' in c) and 'dir' not in idx:
                    idx['dir'] = j
                elif ('商品' in c) and 'item' not in idx:
                    idx['item'] = j
                elif ('交易类型' in c) and 'type' not in idx:
                    idx['type'] = j
                elif ('状态' in c) and 'status' not in idx:
                    idx['status'] = j
            if 'date' in idx and 'amount' in idx:
                hi = i
                break
    if hi < 0:
        return _exp_parse(text)  # 不是可识别账单 CSV → 按文本兜底

    def _cell(r, k):
        j = idx.get(k)
        return (r[j] or '').strip() if (j is not None and j < len(r)) else ''

    out = []
    for r in rows[hi + 1:]:
        if not r or all((c or '').strip() == '' for c in r):
            continue
        d = _cell(r, 'dir')
        if '收入' in d:
            continue
        if '退款' in _cell(r, 'status'):
            continue
        tp = _cell(r, 'type')
        if not d and not any(k in tp for k in ('转账', '红包', '付款', '消费', '支出')):
            continue
        raw_date = _cell(r, 'date')
        pdate = None
        m = _re.search(r'(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})', raw_date)
        if m:
            try:
                pdate = '%04d-%02d-%02d' % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                pdate = None
        amt_clean = _re.sub(r'[^0-9.]', '', _cell(r, 'amount'))
        amt = None
        try:
            if amt_clean not in ('', '.'):
                amt = round(float(amt_clean), 2)
        except Exception:
            amt = None
        merchant = _cell(r, 'merchant') or _cell(r, 'item')
        if amt is None and not merchant:
            continue
        out.append({'line_no': len(out) + 1, 'raw': ','.join([x or '' for x in r]),
                    'date': pdate, 'merchant': merchant.strip(), 'amount': amt})
    return out

# ===== 门店支出自动分类 ====================================================
# 按「用途 / 物料名」关键词推断 分类一/分类二/成本归类。
# 仅当使用者在「更多」里没手填分类时才自动补；识别不到则留空（不强塞）。
_AUTO_CAT_RULES = [
    # (关键词元组, 分类一, 分类二, cost_kind)
    (("兼职","工资","人工","劳务","佣金","提成","小时工","服务费","帮工","小工","人员","工人","师傅","临工"), "其他", "人工", "labor"),
    (("牛肉","猪肉","羊肉","鸡肉","鸭","鹅","鱼","虾","海鲜","蟹","蔬菜","青菜","生菜","白菜","土豆","洋葱","番茄","西红柿","黄瓜","调料","调味料","香料","食材","大米","米","面粉","面条","食用油","盐","酱","酱油","醋","葱","姜","蒜","辣椒","火锅","蘸料","腌料","芝麻","花生","豆腐","豆","鸡蛋","芝士","奶酪","年糕","粉条","海带","木耳","蘑菇","菌","五花","里脊","排骨","牛排","鸡翅","虾仁","鱿鱼"), "食材采购", "", "food"),
    (("炭","炭火","碳","烤炉","烤盘","烧烤架","烤网","设备","锅","平底锅","灶","刀具","刀","冰柜","冰箱","货架","推车","卡式炉","气罐","煤气","炉具","夹子","铲子","签子","竹签","烤叉"), "厨房设备", "", "other"),
    (("桌布","一次性","餐具","杯子","纸巾","湿巾","手套","垃圾袋","打包盒","餐盒","饭盒","牙签","保鲜膜","锡纸","铝箔","包装","盘子","碗","筷子","勺子","吸管"), "包装耗材", "", "consume"),
    (("场地","租金","租","帐篷","天幕","桌椅","遮阳","场地费","电费","水费","物业","管理费","停车费"), "场地相关", "", "outsource"),
    (("油费","打车","滴滴","配送","运费","快递","物流","货拉拉","汽油","柴油","路费","过路","外卖","跑腿","开车","车费"), "交通配送", "", "fuel"),
    (("推广","广告","抖音","美团","大众点评","投放","营销","宣传","海报","拍摄","视频","小红书","网红","探店","流量","推广费"), "营销", "", "other"),
]

def _auto_classify(text):
    if not text:
        return None
    t = str(text)
    for kws, c1, c2, ck in _AUTO_CAT_RULES:
        for kw in kws:
            if kw in t:
                return {"cat1": c1, "cat2": c2, "cost_kind": ck}
    return None

def _exp_cols(data, user):
    qty = _exp_float(data.get('qty'))
    up = _exp_float(data.get('unit_price'))
    amt = _exp_float(data.get('amount'))
    # 模块5：总金额 = 数量 × 单价 自动算（避免手填出错）。仅当未显式给金额、且数量单价齐全时自动推导
    if not amt and qty and up:
        amt = round(qty * up, 2)
    return {
        'use_date': _exp_str(data.get('use_date')),
        'channel': _exp_str(data.get('channel')) or '其他',
        'merchant': _exp_str(data.get('merchant')),
        'item_name': _exp_str(data.get('item_name')),
        'cat1': _exp_str(data.get('cat1')),
        'cat2': _exp_str(data.get('cat2')),
        'menu_item': _exp_str(data.get('menu_item')),
        'ingredient_id': _exp_int(data.get('ingredient_id')) or None,
        'spec': _exp_str(data.get('spec')),
        'qty': qty,
        'unit_price': up,
        'amount': amt or 0,
        'is_cost': 1 if data.get('is_cost', True) else 0,
        'cost_kind': _exp_str(data.get('cost_kind')) or 'other',
        'batch': _exp_str(data.get('batch')),
        'purpose': _exp_str(data.get('purpose')),
        'buyer': _exp_str(data.get('buyer')),
        'note': _exp_str(data.get('note')),
        'statement_id': _exp_int(data.get('statement_id')),
        'statement_line_no': _exp_int(data.get('statement_line_no')),
        'raw_text': _exp_str(data.get('raw_text')),
        'created_by': (user or {}).get('id'),
        'created_by_name': (user or {}).get('name') or (user or {}).get('username'),
    }

@app.route("/api/finance/expense")
def list_expenses():
    db = g.db
    month = request.args.get("month")
    channel = request.args.get("channel")
    is_cost = request.args.get("is_cost")
    q = request.args.get("q")
    where = ["deleted_at IS NULL"]
    params = []
    if month:
        where.append("use_date LIKE ?"); params.append(month + "%")
    if channel:
        where.append("channel=?"); params.append(channel)
    if is_cost in ("1", "0"):
        where.append("is_cost=?"); params.append(int(is_cost))
    if q:
        where.append("(item_name LIKE ? OR merchant LIKE ? OR menu_item LIKE ? OR note LIKE ?)")
        params += ["%" + q + "%"] * 4
    # 归属筛选（?by=me|0|<user_id>）：支出台账同样受顶部"归属"筛选条控制，
    # 不再只筛订单/利润而漏掉台账——做到"哪里显示归属，哪里就能按归属筛"。
    by_clause, by_params = _signer_clause(request.args)
    if by_clause:
        where.append(by_clause.lstrip(" AND "))
        params += by_params
    rows = db.execute(
        "SELECT * FROM expenses WHERE " + " AND ".join(where) +
        " ORDER BY use_date DESC, id DESC", params).fetchall()
    return jsonify({"ok": True, "data": [dict(r) for r in rows]})

@app.route("/api/finance/expense", methods=["POST"])
def add_expense():
    data = request.get_json(force=True) or {}
    db = g.db
    cols = _exp_cols(data, getattr(g, "cur_user", None))
    # 自动分类：用途/物料能识别时补全 分类一/二/成本归类（用户没手填分类才补）
    if not cols.get('cat1'):
        ac = _auto_classify((data.get('item_name') or '') + ' ' + (data.get('purpose') or ''))
        if ac:
            cols['cat1'] = ac['cat1']
            if ac['cat2']:
                cols['cat2'] = ac['cat2']
            cols['cost_kind'] = ac['cost_kind']
    keys = list(cols.keys())
    db.execute("INSERT INTO expenses (" + ",".join(keys) + ") VALUES (" +
               ",".join("?" * len(keys)) + ")", [cols[k] for k in keys])
    eid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    # 「补记」对账：这笔来自某条待对账账单行时回写关联 → 该行标记为已入账（不再出现在未入账清单）
    if cols.get("statement_id") and cols.get("statement_line_no"):
        db.execute("UPDATE expense_statement_lines SET matched_expense_id=? "
                   "WHERE statement_id=? AND line_no=? AND matched_expense_id IS NULL",
                   (eid, cols["statement_id"], cols["statement_line_no"]))
    db.commit()
    return jsonify({"ok": True, "id": eid})

@app.route("/api/finance/expense/<int:eid>", methods=["PUT"])
def update_expense(eid):
    data = request.get_json(force=True) or {}
    db = g.db
    if not db.execute("SELECT id FROM expenses WHERE id=? AND deleted_at IS NULL", (eid,)).fetchone():
        return jsonify({"ok": False, "msg": "支出不存在"}), 404
    cols = _exp_cols(data, getattr(g, "cur_user", None))
    sets = [k + "=?" for k in cols if k not in ("created_by", "created_by_name")]
    db.execute("UPDATE expenses SET " + ",".join(sets) + " WHERE id=?",
               [cols[k] for k in cols if k not in ("created_by", "created_by_name")] + [eid])
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/finance/expense/<int:eid>", methods=["DELETE"])
def delete_expense(eid):
    db = g.db
    db.execute("UPDATE expenses SET deleted_at=datetime('now','localtime') WHERE id=?", (eid,))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/finance/expense/parse", methods=["POST"])
def parse_expense_text():
    data = request.get_json(force=True) or {}
    return jsonify({"ok": True, "lines": _exp_parse(data.get("text", ""))})

@app.route("/api/finance/expense/auto-classify")
def auto_classify_expense():
    """实时预览：根据用途/物料名返回建议分类（供录入时给提示，不强制）。"""
    text = request.args.get("text", "")
    return jsonify({"ok": True, "result": _auto_classify(text)})

@app.route("/api/finance/expense-statement", methods=["POST"])
def create_statement():
    data = request.get_json(force=True) or {}
    text = data.get("text", "")
    channel = _exp_str(data.get("channel")) or "其他"
    u = getattr(g, "cur_user", None) or {}
    db = g.db
    lines = _exp_parse(text)
    db.execute("INSERT INTO expense_statements (channel, raw_text, line_count, created_by, created_by_name) "
               "VALUES (?,?,?,?,?)",
               (channel, text, len(lines), u.get("id"), u.get("name") or u.get("username")))
    sid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    out = []
    for ln in lines:
        db.execute("INSERT INTO expense_statement_lines "
                   "(statement_id, line_no, raw_line, parsed_date, parsed_merchant, parsed_amount) "
                   "VALUES (?,?,?,?,?,?)",
                   (sid, ln["line_no"], ln["raw"], ln["date"], ln["merchant"], ln["amount"]))
        out.append({"id": db.execute("SELECT last_insert_rowid()").fetchone()[0],
                    "statement_id": sid, **ln, "matched_expense_id": None})
    db.commit()
    return jsonify({"ok": True, "statement_id": sid, "lines": out})

@app.route("/api/finance/expense-statement")
def list_statements():
    rows = g.db.execute("""
        SELECT s.id, s.channel, s.created_at, s.line_count,
               (SELECT COUNT(*) FROM expense_statement_lines l
                WHERE l.statement_id=s.id AND l.matched_expense_id IS NULL) AS unmatched
        FROM expense_statements s ORDER BY s.id DESC LIMIT 50
    """).fetchall()
    return jsonify({"ok": True, "data": [dict(r) for r in rows]})

@app.route("/api/finance/expense-statement/<int:sid>")
def statement_unmatched(sid):
    rows = g.db.execute(
        "SELECT * FROM expense_statement_lines WHERE statement_id=? AND matched_expense_id IS NULL "
        "ORDER BY line_no", (sid,)).fetchall()
    return jsonify({"ok": True, "data": [dict(r) for r in rows]})

@app.route("/api/finance/expense-statement/csv", methods=["POST"])
def create_statement_csv():
    """导入「微信/支付宝账单 CSV」：一次把整月付款变成待对账行（付款台账 = 系统真账）。
    结构化解析、零识别误差，用于根治"月底翻所有渠道逐笔对、怕漏"的问题。"""
    data = request.get_json(force=True) or {}
    text = data.get("text", "")
    channel = _exp_str(data.get("channel")) or "微信"
    u = getattr(g, "cur_user", None) or {}
    db = g.db
    lines = _exp_parse_csv(text)
    db.execute("INSERT INTO expense_statements (channel, raw_text, line_count, created_by, created_by_name) "
               "VALUES (?,?,?,?,?)",
               (channel, (text or "")[:20000], len(lines), u.get("id"), u.get("name") or u.get("username")))
    sid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    out = []
    for ln in lines:
        db.execute("INSERT INTO expense_statement_lines "
                   "(statement_id, line_no, raw_line, parsed_date, parsed_merchant, parsed_amount) "
                   "VALUES (?,?,?,?,?,?)",
                   (sid, ln["line_no"], ln["raw"], ln["date"], ln["merchant"], ln["amount"]))
        out.append({"id": db.execute("SELECT last_insert_rowid()").fetchone()[0],
                    "statement_id": sid, **ln, "matched_expense_id": None})
    db.commit()
    return jsonify({"ok": True, "statement_id": sid, "lines": out, "count": len(out)})

@app.route("/api/finance/expense/unmatched")
def expense_unmatched_all():
    """跨批次的「全局未入账清单」：所有付了但还没记成支出的行，一条不漏地列出来。
    这是"对账看板"的数据源——你只需看这一栏，不用再翻聊天记录逐笔找。"""
    month = request.args.get("month")
    channel = request.args.get("channel")
    where = ["l.matched_expense_id IS NULL"]
    params = []
    if month:
        where.append("l.parsed_date LIKE ?"); params.append(month + "%")
    if channel:
        where.append("s.channel=?"); params.append(channel)
    rows = g.db.execute(
        "SELECT l.id, l.statement_id, l.line_no, l.parsed_date AS date, l.parsed_merchant AS merchant, "
        "l.parsed_amount AS amount, s.channel AS channel, s.created_at AS stmt_at "
        "FROM expense_statement_lines l JOIN expense_statements s ON s.id=l.statement_id "
        "WHERE " + " AND ".join(where) + " ORDER BY l.parsed_date ASC, l.id ASC", params).fetchall()
    return jsonify({"ok": True, "data": [dict(r) for r in rows]})

@app.route("/api/finance/expense/batch", methods=["POST"])
def batch_save_expenses():
    data = request.get_json(force=True) or {}
    items = data.get("items") or []
    link = data.get("link_statement", True)
    u = getattr(g, "cur_user", None) or {}
    db = g.db
    saved = 0
    for it in items:
        cols = _exp_cols(it, u)
        # 批量入库同样自动分类（粘贴账单里的行通常没填分类）
        if not cols.get('cat1'):
            ac = _auto_classify((it.get('item_name') or '') + ' ' + (it.get('purpose') or ''))
            if ac:
                cols['cat1'] = ac['cat1']
                if ac['cat2']:
                    cols['cat2'] = ac['cat2']
                cols['cost_kind'] = ac['cost_kind']
        keys = list(cols.keys())
        db.execute("INSERT INTO expenses (" + ",".join(keys) + ") VALUES (" +
                   ",".join("?" * len(keys)) + ")", [cols[k] for k in keys])
        eid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        saved += 1
        if link and cols["statement_id"] and cols["statement_line_no"]:
            db.execute("UPDATE expense_statement_lines SET matched_expense_id=? "
                       "WHERE statement_id=? AND line_no=? AND matched_expense_id IS NULL",
                       (eid, cols["statement_id"], cols["statement_line_no"]))
    db.commit()
    return jsonify({"ok": True, "saved": saved})

@app.route("/api/finance/expense/<int:eid>/map-ingredient", methods=["POST"])
def map_expense_ingredient(eid):
    """把支出关联到某个食材（仅记录关联关系，方便「菜单对应品名」核对；不改全局单价）。
    要改某食材采购价请用食材单价管理或订单利润弹窗。"""
    data = request.get_json(force=True) or {}
    db = g.db
    # 0 / 空 / None 一律视为"不关联"（避免外键指向不存在的食材 id）
    iid = _exp_int(data.get("ingredient_id")) or None
    db.execute("UPDATE expenses SET ingredient_id=?, menu_item=? WHERE id=?",
               (iid, _exp_str(data.get("menu_item")), eid))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/finance/expense-summary")
def expense_summary():
    month = request.args.get("month")
    db = g.db
    where = ["deleted_at IS NULL"]
    params = []
    if month:
        where.append("use_date LIKE ?"); params.append(month + "%")
    row = db.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS total, "
        "COALESCE(SUM(CASE WHEN is_cost=1 THEN amount ELSE 0 END),0) AS cost_total "
        "FROM expenses WHERE " + " AND ".join(where), params).fetchone()
    by_kind = {}
    for r in db.execute(
        "SELECT cost_kind, COALESCE(SUM(amount),0) AS s FROM expenses WHERE " +
        " AND ".join(where) + " AND is_cost=1 GROUP BY cost_kind", params).fetchall():
        by_kind[r["cost_kind"]] = round(float(r["s"] or 0), 2)
    un = db.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(parsed_amount),0) AS a "
        "FROM expense_statement_lines WHERE matched_expense_id IS NULL").fetchone()
    return jsonify({"ok": True, "data": {
        "count": row["cnt"] or 0,
        "total": round(float(row["total"] or 0), 2),
        "cost_total": round(float(row["cost_total"] or 0), 2),
        "by_kind": by_kind,
        "unmatched_count": un["c"] or 0,
        "unmatched_amount": round(float(un["a"] or 0), 2),
    }})

@app.route("/api/finance/expense/export")
def export_expenses():
    from flask import Response
    import csv as _csv, io
    month = request.args.get("month")
    db = g.db
    where = ["deleted_at IS NULL"]
    params = []
    if month:
        where.append("use_date LIKE ?"); params.append(month + "%")
    rows = db.execute(
        "SELECT id, use_date, channel, merchant, item_name, cat1, cat2, menu_item, spec, "
        "qty, unit_price, amount, is_cost, cost_kind, batch, purpose, buyer, note, created_by_name "
        "FROM expenses WHERE " + " AND ".join(where) + " ORDER BY use_date ASC, id ASC",
        params).fetchall()

    def _month_of(d):
        if not d:
            return ""
        try:
            return "%d月" % int(str(d)[:7].split("-")[1])
        except Exception:
            return ""

    def _num(v):
        # 原版整数显示为整：2.0→"2"、170.0→"170"；空/0→""
        if v is None:
            return ""
        try:
            f = float(v)
        except Exception:
            return v
        if f == 0:
            return ""
        if f == int(f):
            return str(int(f))
        return ("%.2f" % f).rstrip("0").rstrip(".")

    # 原版「附件3 资金用途明细表」24 列（含右侧分类汇总块 C22-C24）
    headers = ["序号", "批次", "分类一", "分类二", "使用日期", "月份", "物料名称", "菜单对应品名",
               "规格", "数量", "单价", "总金额", "付款总额", "供应商", "用途", "购物人",
               "付费截图", "发票(普票)", "发票(专票)", "备注", "", "分类一(汇总)", "分类二(汇总)", "费用(汇总)"]
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(headers)
    total_sum = 0.0
    cat_sum = {}  # (cat1, cat2) -> sum of 付款总额
    for i, r in enumerate(rows, 1):
        qty = r["qty"] or 0
        up = r["unit_price"] or 0
        amount = r["amount"] or 0
        total = round(qty * up, 2) if (qty and up) else amount  # C12 总金额 = 数量×单价
        buyer = r["buyer"] or r["created_by_name"] or ""
        # 只填3样（用途/金额，无单独用途备注）时，用途列回退到物料名，保证整行不空
        purpose = r["purpose"] or r["item_name"] or ""
        w.writerow([
            i, r["batch"] or "", r["cat1"] or "", r["cat2"] or "", r["use_date"] or "",
            _month_of(r["use_date"]), r["item_name"] or "", r["menu_item"] or "", r["spec"] or "",
            _num(qty), _num(up), _num(total), _num(amount), r["merchant"] or "", purpose, buyer,
            "", "", "", r["note"] or "", "", "", "", ""  # C17-C19 图片位留空；C21 空白；C22-C24 汇总块
        ])
        total_sum += amount
        key = (r["cat1"] or "", r["cat2"] or "")
        cat_sum[key] = cat_sum.get(key, 0) + amount
    # 右侧分类汇总块（C22-C24）
    if rows:
        w.writerow(["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "合计", "", _num(total_sum)])
        for (c1, c2), s in cat_sum.items():
            w.writerow(["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", c1, c2, _num(s)])
    b = buf.getvalue().encode("utf-8-sig")
    return Response(b, mimetype="text/csv; charset=utf-8",
                   headers={"Content-Disposition": "attachment; filename=附件3资金用途明细表_%s.csv" % (month or "全部")})


@app.route("/api/finance/dates")
def finance_dates():
    """有账的日期（供财务页日期条一键切换）：每天的单数、收入合计。

    只统计未取消、未删除的订单；收入按实收货款（amount）汇总。
    可选 by=me|0|<user_id> 按"归属"筛选。
    """
    _sc, _sp = _signer_clause(request.args)
    rows = g.db.execute("""
        SELECT booking_date AS d, COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS amt
        FROM orders
        WHERE status!='cancelled' AND deleted_at IS NULL AND booking_date IS NOT NULL AND booking_date!=''
    """ + _sc + """
        GROUP BY booking_date
        ORDER BY booking_date DESC
    """, _sp).fetchall()
    return jsonify({"ok": True, "data": [
        {"date": r["d"], "cnt": r["cnt"], "amount": round(float(r["amt"] or 0), 2)} for r in rows
    ]})


@app.route("/api/finance/signer-counts")
def finance_signer_counts():
    """归属筛选条角标：各 created_by 的订单数 + 金额（按当前日期范围、不受 by= 影响）。

    与 profit-overview 的 by= 过滤解耦——无论当前选中刘/孙/全部，角标都显示
    各自在“当前日期范围”内的真实单数，点谁就是谁，不会因为筛选而错位成 0。
    """
    db = g.db
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    clause = "o.status!='cancelled' AND o.deleted_at IS NULL"
    params = []
    if date_from:
        clause += " AND o.booking_date >= ?"
        params.append(date_from)
    if date_to:
        clause += " AND o.booking_date <= ?"
        params.append(date_to)
    rows = db.execute(f"""
        SELECT COALESCE(o.created_by,0) AS cb, COUNT(*) AS cnt,
               COALESCE(SUM(o.amount),0) AS amt
        FROM orders o WHERE {clause}
        GROUP BY o.created_by
    """, params).fetchall()
    total = sum(r["cnt"] for r in rows)
    data = {str(r["cb"]): {"cnt": r["cnt"], "amount": round(float(r["amt"] or 0), 2)} for r in rows}
    return jsonify({"ok": True, "data": data, "total": total})


@app.route("/api/analytics/overview")
def analytics_overview():
    """经营分析：趋势（含环比）+ 结构分布（套餐/场地/时段/星期/性别）+ 回头客

    口径（只在这里算一次，前端不重算）：
      · 只统计未取消、未删除订单；收入＝实收货款 amount（押金不算收入）
      · 客单价 = 收入 ÷ 单数
      · 期（period）按 granularity 分为 日 / 周(周一起) / 月；环比＝当前期 vs 上一期
      · 结构分布用"最近 4 期"合并统计（当期样本太小会失真）
    """
    import datetime as _dt
    import re as _re
    from calculator import today_cst as _today_cst

    gran = (request.args.get("granularity") or "week").lower()
    if gran not in ("day", "week", "month"):
        gran = "week"
    try:
        n_periods = int(request.args.get("periods") or 8)
    except Exception:
        n_periods = 8
    n_periods = max(2, min(24, n_periods))

    db = g.db
    today = _today_cst()

    fem = _re.compile(r"(女士|小姐|太太|夫人|姐|妹|阿姨|婆婆)")
    mal = _re.compile(r"(先生|男士|哥|叔|大爷|总)")

    def gen_of(nm):
        nm = nm or ""
        if fem.search(nm):
            return "f"
        if mal.search(nm):
            return "m"
        return "u"

    def pstart(d):
        if gran == "day":
            return d
        if gran == "month":
            return d.replace(day=1)
        return d - _dt.timedelta(days=d.weekday())          # 周一为一周起点

    def prev_start(s):
        if gran == "day":
            return s - _dt.timedelta(days=1)
        if gran == "week":
            return s - _dt.timedelta(days=7)
        return (s - _dt.timedelta(days=1)).replace(day=1)

    def plabel(s):
        if gran == "day":
            return "%d/%d" % (s.month, s.day)
        if gran == "month":
            return "%d年%d月" % (s.year, s.month)
        e = s + _dt.timedelta(days=6)
        return "%d/%d~%d/%d" % (s.month, s.day, e.month, e.day)

    starts, s = [], pstart(today)
    for _ in range(n_periods):
        starts.append(s)
        s = prev_start(s)
    starts.reverse()

    # 全部历史模式：取“从最早一笔订单”到“今天”的全量期（不受 24 期上限限制，
    # 这样趋势图可以一路拖到最开始的日期）。极端数据用 400 期兜底防撑爆。
    mode = (request.args.get("mode") or "").strip()
    if mode == "all":
        _emin = db.execute(
            "SELECT MIN(booking_date) FROM orders WHERE status!='cancelled' AND deleted_at IS NULL "
            "AND booking_date IS NOT NULL AND booking_date!=''").fetchone()[0]
        _all = []
        if _emin:
            try:
                _ey, _em, _ed = [int(x) for x in _emin.split("-")[:3]]
                s = pstart(_dt.date(_ey, _em, _ed))
                _end = pstart(today)
                while s <= _end:
                    _all.append(s)
                    if gran == "day":
                        s = s + _dt.timedelta(days=1)
                    elif gran == "week":
                        s = s + _dt.timedelta(days=7)
                    else:  # month：跳到下个月 1 号
                        s = s.replace(month=1, year=s.year + 1) if s.month == 12 else s.replace(month=s.month + 1)
                _all = _all[-400:]
            except Exception:
                _all = []
        if _all:
            starts = _all

    # 录入人筛选（按 orders.created_by）：'' / 'me' / 用户 id / '0'=未标注
    # 「能独立统计」——两人的单都计入系统，但可以单独看各自的经营数据
    by = (request.args.get("by") or "").strip()
    by_clause, by_params = "", []
    if by == "me":
        by_clause, by_params = " AND created_by = ? ", [_cur_uid()]
    elif by == "0":
        by_clause = " AND created_by IS NULL "
    elif by.isdigit():
        by_clause, by_params = " AND created_by = ? ", [int(by)]

    rows = db.execute("""
        SELECT o.id, o.booking_date, o.booking_time, o.contact_name, o.contact_phone, o.address,
               o.amount, o.status, o.payment_status, o.created_by,
               COALESCE(u.name, o.created_by_name) AS created_by_name
        FROM orders o LEFT JOIN users u ON o.created_by = u.id
        WHERE o.status!='cancelled' AND o.deleted_at IS NULL
              AND o.booking_date IS NOT NULL AND o.booking_date!=''
    """ + by_clause + """
        ORDER BY o.booking_date DESC, o.id DESC
    """, by_params).fetchall()

    def to_date(v):
        try:
            return _dt.date(*[int(x) for x in v.split("-")[:3]])
        except Exception:
            return None

    pk_rows = db.execute("""
        SELECT p.name, p.min_people, p.max_people, op.quantity, op.order_id
        FROM order_packages op
        JOIN packages p ON p.id = op.package_id
        JOIN orders o ON o.id = op.order_id
        WHERE o.status!='cancelled' AND o.deleted_at IS NULL
    """).fetchall()
    order_pkgs = {}
    for r in pk_rows:
        order_pkgs.setdefault(r["order_id"], []).append(r["name"])

    agg = {k: {"revenue": 0.0, "orders": 0} for k in starts}
    recs = []
    for r in rows:
        d = to_date(r["booking_date"])
        if not d:
            continue
        amt = float(r["amount"] or 0)
        st = pstart(d)
        if st in agg:
            agg[st]["revenue"] += amt
            agg[st]["orders"] += 1
        recs.append({
            "id": r["id"], "date": r["booking_date"], "hour": (r["booking_time"] or "")[:2],
            "name": r["contact_name"] or "", "phone": r["contact_phone"] or "",
            "address": r["address"] or "", "amount": amt, "period": st.isoformat(),
            "payment_status": r["payment_status"] or "unpaid",
            "pkgs": order_pkgs.get(r["id"], []), "gender": gen_of(r["contact_name"]),
            "by": r["created_by"], "by_name": r["created_by_name"] or "",
        })

    # 录入人清单（不受当前筛选影响，用来渲染筛选按钮）
    _cr = db.execute("""
        SELECT o.created_by AS uid, COALESCE(u.name, '') AS nm, COUNT(*) c
        FROM orders o LEFT JOIN users u ON o.created_by = u.id
        WHERE o.status!='cancelled' AND o.deleted_at IS NULL
        GROUP BY o.created_by ORDER BY c DESC
    """).fetchall()
    creators = [{"id": ("" if r["uid"] is None else str(r["uid"])),
                 "name": (r["nm"] or ""), "orders": r["c"]} for r in _cr]

    def pack(k):
        a = agg[k]
        n = a["orders"]
        return {"start": k.isoformat(), "label": plabel(k), "revenue": round(a["revenue"], 2),
                "orders": n, "avg_price": round(a["revenue"] / n, 2) if n else 0.0}

    periods = [pack(k) for k in starts]
    cur = periods[-1] if periods else {}
    pre = periods[-2] if len(periods) > 1 else {}

    def pct(a, b):
        return None if not b else round((a - b) / b * 100, 1)

    # 当前期可能还没过完（比如今天是周三，本期只走了 3/7 天）→ 直接比会天然偏低，
    # 所以额外算"日均"，并在前端标注"进行中"。
    if gran == "day":
        days_total, days_passed = 1, 1
    elif gran == "week":
        days_total = 7
        days_passed = (today - starts[-1]).days + 1 if starts else 1
    else:
        import calendar as _cal
        days_total = _cal.monthrange(today.year, today.month)[1]
        days_passed = today.day
    days_passed = max(1, min(days_passed, days_total))
    pre_days = max(1, (starts[-1] - starts[-2]).days) if len(starts) > 1 else 1
    cur_pd = (cur.get("revenue", 0) or 0) / days_passed
    pre_pd = (pre.get("revenue", 0) or 0) / pre_days

    delta = {
        "revenue_pct": pct(cur.get("revenue", 0), pre.get("revenue", 0)),
        "orders_pct": pct(cur.get("orders", 0), pre.get("orders", 0)),
        "avg_pct": pct(cur.get("avg_price", 0), pre.get("avg_price", 0)),
        "cur_label": cur.get("label", ""), "prev_label": pre.get("label", ""),
        "days_passed": days_passed, "days_total": days_total,
        "in_progress": days_passed < days_total,
        "cur_per_day": round(cur_pd, 1), "pre_per_day": round(pre_pd, 1),
        "per_day_pct": pct(cur_pd, pre_pd),
    }

    # ---- 结构分布：最近 4 期 ----
    recent_from = starts[-4] if len(starts) >= 4 else starts[0]
    recent = [x for x in recs if x["date"] >= recent_from.isoformat()]
    recent_ids = set(x["id"] for x in recent)
    rev_sum = sum(x["amount"] for x in recent) or 1.0

    pk_map = {}
    for r in pk_rows:
        if r["order_id"] not in recent_ids:
            continue
        k = (r["name"], r["min_people"], r["max_people"])
        pk_map[k] = pk_map.get(k, 0) + int(r["quantity"] or 0)
    total_portions = sum(pk_map.values()) or 1
    packages = sorted(
        [{"name": k[0], "people": ("%s-%s人" % (k[1], k[2])) if k[1] != k[2] else ("%s人" % k[1]),
          "portions": v, "share": round(v / total_portions * 100, 1)} for k, v in pk_map.items()],
        key=lambda x: -x["portions"])

    def dist(keyfn, limit=8):
        m = {}
        for x in recent:
            k = keyfn(x) or "（未填）"
            m.setdefault(k, {"orders": 0, "revenue": 0.0})
            m[k]["orders"] += 1
            m[k]["revenue"] += x["amount"]
        out = [{"name": k, "orders": v["orders"], "revenue": round(v["revenue"], 2),
                "share": round(v["revenue"] / rev_sum * 100, 1)} for k, v in m.items()]
        out.sort(key=lambda x: -x["orders"])
        return out[:limit]

    places = dist(lambda x: x["address"])
    hours = dist(lambda x: (x["hour"] + "点") if x["hour"] else "", 8)
    hours.sort(key=lambda x: int(x["name"].replace("点", "") or 0))
    WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekdays = dist(lambda x: (WD[to_date(x["date"]).weekday()] if to_date(x["date"]) else ""), 7)
    weekdays.sort(key=lambda x: WD.index(x["name"]) if x["name"] in WD else 9)

    # 性别（从联系人称谓推断："廖女士"/"黄先生"）
    # ⚠️ 变量别叫 g——会盖住 Flask 的 g（上下文对象），导致 g.db 报 UnboundLocalError
    gc = {"f": 0, "m": 0, "u": 0}
    for x in recent:
        gc[x.get("gender") or "u"] = gc.get(x.get("gender") or "u", 0) + 1
    known = gc["f"] + gc["m"]
    gender = {"female": gc["f"], "male": gc["m"], "unknown": gc["u"],
              "known_ratio": round(known / max(1, len(recent)) * 100, 1),
              "female_share": round(gc["f"] / known * 100, 1) if known else 0.0}

    # 回头客（按电话聚合；名字会重名，不能用来识别客户）
    # ⚠️「刘和牛」是门店前期代客下单时用的**自己店名**——那些是**真实订单**，
    #    金额已计入全部财务/结构统计，绝不能当内部测试单剔除；
    #    但一个店名电话代表不了"同一位回头客"，所以不参与回头客排名，单独汇总说明。
    # ⚠️ 识别只看联系人姓名：**不要匹配地址**——"自取/自提"是地址值，
    #    真实客户选自取会被误判成内部单（原来那版就埋了这个雷）。
    proxy_re = _re.compile(r"(刘和牛|测试|test|内部单)")
    rp = {}
    proxy = {"orders": 0, "revenue": 0.0, "names": [], "phones": [], "first": "", "last": ""}
    for r in rows:
        nm_raw = r["contact_name"] or ""
        ph = (r["contact_phone"] or "").strip()
        if proxy_re.search(nm_raw):
            proxy["orders"] += 1
            proxy["revenue"] += float(r["amount"] or 0)
            if nm_raw and nm_raw not in proxy["names"]:
                proxy["names"].append(nm_raw)
            if ph and ph not in proxy["phones"]:
                proxy["phones"].append(ph)
            bd = r["booking_date"] or ""
            if bd and (not proxy["first"] or bd < proxy["first"]):
                proxy["first"] = bd
            if bd > proxy["last"]:
                proxy["last"] = bd
            continue
        if len(ph) < 7:
            continue
        e = rp.setdefault(ph, {"phone": ph, "name": nm_raw, "orders": 0,
                               "revenue": 0.0, "last_date": ""})
        e["orders"] += 1
        e["revenue"] += float(r["amount"] or 0)
        if (r["booking_date"] or "") > e["last_date"]:
            e["last_date"] = r["booking_date"]
            e["name"] = nm_raw or e["name"]
    proxy["revenue"] = round(proxy["revenue"], 2)
    repeat = sorted([v for v in rp.values() if v["orders"] >= 2],
                    key=lambda x: -x["orders"])
    for v in repeat:
        v["revenue"] = round(v["revenue"], 2)
        v["avg_price"] = round(v["revenue"] / max(1, v["orders"]), 2)

    return jsonify({"ok": True, "data": {
        "granularity": gran, "today": today.isoformat(),
        "periods": periods, "current": cur, "previous": pre, "delta": delta,
        "recent_from": recent_from.isoformat(), "recent_orders": len(recent),
        "packages": packages, "places": places, "hours": hours, "weekdays": weekdays,
        "gender": gender, "repeat": repeat[:30], "proxy": proxy,
        "creators": creators, "by": by,
        "orders": recs,          # 明细：供前端点条目下钻
    }})


@app.route("/api/finance/payment/<int:oid>", methods=["POST"])
def update_payment(oid):
    """更新订单货款状态"""
    data = request.get_json(force=True)
    status = data.get("payment_status", "unpaid")
    db = g.db
    if not _can_touch_order(db, oid):
        return _deny_order()
    db.execute("UPDATE orders SET payment_status=? WHERE id=?", (status, oid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/finance/deposit/<int:oid>", methods=["POST"])
def update_deposit(oid):
    """更新押金状态"""
    data = request.get_json(force=True)
    status = data.get("deposit_status", "pending")
    db = g.db
    if not _can_touch_order(db, oid):
        return _deny_order()
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
<a class="back-btn" href="/" onclick="var r=document.referrer;if(r&amp;&amp;r.indexOf(location.origin)===0){{location.href=r;return false;}}">← 返回</a>
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


# ===== 菜单打印页（顾客版 / 内部成本版）=====
# 独立可打印页面，带固定工具条（← 返回 / 🖨️ 打印），A4 友好。
# 挂在 /menu/ 前缀下 → 仅老板可见（成本属敏感信息）。
_MENU_PRINT_TPL = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{--red:#c0392b;--ink:#2c1810;--brown:#6b4f3a}
  *{box-sizing:border-box}
  body{margin:0;background:#efe9e1;color:var(--ink);
       font-family:-apple-system,"PingFang SC","Microsoft YaHei","Helvetica Neue",sans-serif;
       -webkit-print-color-adjust:exact;print-color-adjust:exact}
  .ptoolbar{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:10px;
            padding:10px 14px;background:rgba(255,255,255,.97);border-bottom:1px solid #e6ded1}
  .pback{padding:8px 16px;border:1.5px solid var(--red);border-radius:8px;color:var(--red);
         text-decoration:none;font-weight:700;font-size:14px;background:#fff}
  .pback:hover{background:#fdecea}
  .spacer{flex:1}
  .pprint{padding:9px 20px;border:none;border-radius:8px;background:var(--red);color:#fff;
          font-weight:700;font-size:14px;cursor:pointer}
  .wrap{max-width:840px;margin:18px auto 44px;padding:0 12px}
  .page{background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 8px 34px rgba(0,0,0,.13)}
  .hero{background:linear-gradient(135deg,#8e1e1a 0%,#c0392b 55%,#e74c3c 100%);color:#fff;
        padding:34px 28px;text-align:center;position:relative;overflow:hidden}
  .hero::before{content:"";position:absolute;top:-46px;right:-40px;width:170px;height:170px;
        background:rgba(255,255,255,.09);border-radius:50%}
  .hero::after{content:"";position:absolute;bottom:-64px;left:-34px;width:130px;height:130px;
        background:rgba(255,255,255,.07);border-radius:50%}
  .hero h1{margin:0 0 8px;font-size:27px;letter-spacing:3px;position:relative;z-index:1}
  .hero p{margin:0;opacity:.92;font-size:13px;letter-spacing:1.5px;position:relative;z-index:1}
  .hero-cost{background:linear-gradient(135deg,#2c1810 0%,#4a2c1a 60%,#6b4f3a 100%)}
  .pkg{border-bottom:1px dashed #e8e0d5}
  .pkg:last-of-type{border-bottom:none}
  .pkg-head{display:flex;justify-content:space-between;align-items:center;gap:10px;
            padding:16px 24px;background:#faf5ed;border-bottom:1px solid #eee5d8;flex-wrap:wrap}
  .pkg-name{font-size:18px;font-weight:800;color:#2c1810;display:flex;align-items:center;gap:10px}
  .people{font-size:12px;color:#8b7355;background:#e8dcc8;padding:3px 10px;border-radius:12px;font-weight:400}
  .pkg-price{white-space:nowrap}
  .pkg-price .sym{font-size:15px;font-weight:700;color:var(--red)}
  .pkg-price .num{font-size:26px;font-weight:800;color:var(--red)}
  .pkg-price .unit{font-size:11px;color:#999}
  .pkg-body{padding:16px 24px 20px}
  .cat{margin-bottom:14px}
  .cat:last-child{margin-bottom:0}
  .cat-title{display:flex;align-items:center;gap:8px;font-size:13.5px;font-weight:800;color:var(--brown);
             margin-bottom:9px;padding-bottom:6px;border-bottom:2px solid #f0e6d8}
  .cat-icon{width:21px;height:21px;border-radius:6px;display:inline-flex;align-items:center;
            justify-content:center;font-size:12px;color:#fff;flex:0 0 auto}
  .dishes{display:flex;flex-wrap:wrap;gap:7px 8px}
  .dish{font-size:14px;color:#3d2817;background:#faf5ed;border:1px solid #f0e6d8;
        border-radius:8px;padding:4px 11px}
  .dish:first-child{color:var(--red);border-color:#f2d5d0;background:#fdf3f1;font-weight:700}
  .footer{background:#2c1810;color:#fff;padding:18px 24px;text-align:center;font-size:12.5px}
  .footer p{margin:4px 0}
  .foot-note{padding:14px 24px;font-size:12px;color:#8a7f75;background:#faf5ed;
             border-top:1px solid #eee5d8;line-height:1.7}
  table.cost-tbl{width:100%;border-collapse:collapse;font-size:13.5px}
  table.cost-tbl th{background:#f3efe8;text-align:left;padding:7px 10px;font-weight:700;color:#7a6a58}
  table.cost-tbl th:last-child{text-align:right}
  table.cost-tbl td{padding:7px 10px;border-bottom:1px solid #f0ebe2}
  table.cost-tbl td.num{text-align:right;font-variant-numeric:tabular-nums;font-weight:600;color:#444}
  table.cost-tbl tr.cat-row td{color:#fff;font-weight:800;font-size:12.5px;padding:6px 10px;letter-spacing:.5px}
  .loss{font-size:10.5px;color:#b06a00;background:#fff3d9;border-radius:6px;padding:1px 6px;margin-left:4px}
  .summary{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
  .sum-item{flex:1;min-width:120px;background:#faf5ed;border:1px solid #efe6d8;border-radius:10px;
            padding:10px 14px;display:flex;flex-direction:column;gap:3px}
  .sum-item span{font-size:11.5px;color:#8b7355}
  .sum-item b{font-size:18px;color:#2c1810;font-variant-numeric:tabular-nums}
  .sum-item b.pos{color:#1e8449}
  .sum-item b.neg{color:var(--red)}
  @media print{
    body{background:#fff}
    .ptoolbar{display:none!important}
    .wrap{margin:0;max-width:none;padding:0}
    .page{box-shadow:none;border-radius:0}
    .pkg{break-inside:avoid}
    @page{size:A4;margin:10mm}
  }
</style></head>
<body>
<div class="ptoolbar">
  <a class="pback" href="__BACK__" onclick="try{var r=document.referrer;if(r&&r.indexOf(location.origin)===0){location.href=r;return false;}}catch(e){}">← 返回</a>
  <div class="spacer"></div>
  <button class="pprint" onclick="window.print()">🖨️ 打印 / 存PDF</button>
</div>
<div class="wrap"><div class="page">
__BODY__
</div></div>
</body></html>"""


def _menu_print_build(show_cost):
    """构建菜单打印页 HTML：show_cost=False 顾客版；True 内部成本版。"""
    import html as _html

    def fmt_num(x):
        try:
            x = round(float(x), 2)
        except Exception:
            return x
        return int(x) if abs(x - round(x)) < 1e-9 else x

    try:
        from calculator import _subcategorize_meat
    except Exception:
        def _subcategorize_meat(name, cat):
            return cat or "other"
    # 打包切配人工：套餐页按"1 个套餐 = 1 单"算首单价（与财务口径一致）
    try:
        from profit import get_config as _get_cfg
        _labor_first = float((_get_cfg(g.db) or {}).get("labor_first", 40) or 0)
    except Exception:
        _labor_first = 40.0

    CATS = [
        ("beef", "牛肉", "🥩", "#8e1e1a"),
        ("pork", "猪肉", "🥓", "#c75d3e"),
        ("chicken", "鸡肉", "🍗", "#c9962b"),
        ("vegetable", "素菜", "🥬", "#3a8a3a"),
        ("side", "小菜", "🥗", "#5a8a3a"),
        ("sauce", "蘸料", "🧂", "#7a5a3a"),
        ("drink", "赠饮", "🎁", "#666666"),
        ("packaging", "食材包装", "📦", "#d35400"),
        ("utensil", "客户餐具", "🍱", "#8e44ad"),
        ("staple", "主食", "🍚", "#b8860b"),
        ("other", "其他", "📦", "#6b4f3a"),
    ]
    # 固定显示顺序（与菜单原数据页完全一致）：避免各套餐因数量排序而乱跳，破坏观看惯性
    # 注：一次性油壶属食材包装；小料中的油属客户餐具
    UTENSIL_ORDER = ['三格底料盒', '筷子', '勺子', '纸杯', '纸巾', '围裙', '油', '垃圾袋', '一次性桌布']
    PACKAGING_ORDER = ['金色打包盒', '圆形透明打包盒', '生菜水果打包盒', '烤肉盒子', '绑带', '杂物保温袋', '餐具打包袋', '一次性油壶']
    def _fx_idx(name, order):
        for i, n in enumerate(order):
            if n in (name or ''):
                return i
        return 999
    CUSTOMER_KEYS = {"beef", "pork", "chicken", "vegetable", "side", "sauce", "drink"}

    def eff_cat(name, raw):
        raw = raw or "other"
        if raw in ("packaging", "utensil", "drink", "staple", "side", "sauce", "other", "vegetable"):
            return raw
        return _subcategorize_meat(name, raw)

    def build_customer(p, groups):
        parts = []
        for key, label, icon, color in CATS:
            if key not in CUSTOMER_KEYS:
                continue
            items = groups.get(key) or []
            if not items:
                continue
            seen = set(); names = []
            for it in items:
                if it["name"] not in seen:
                    seen.add(it["name"]); names.append(it["name"])
            dishes = "".join('<span class="dish">%s</span>' % _html.escape(n) for n in names)
            parts.append(
                '<div class="cat"><div class="cat-title">'
                '<span class="cat-icon" style="background:%s">%s</span>%s · %d款</div>'
                '<div class="dishes">%s</div></div>'
                % (color, icon, label, len(names), dishes)
            )
        price = p.get("price") or p.get("base_price") or 0   # 对外价用总价（含配送搭建费）
        return (
            '<div class="pkg"><div class="pkg-head">'
            '<div class="pkg-name">%s <span class="people">%s-%s人餐</span></div>'
            '<div class="pkg-price"><span class="sym">¥</span><span class="num">%s</span>'
            '<span class="unit"> / 套</span></div></div>'
            '<div class="pkg-body">%s</div></div>'
            % (_html.escape(p["name"] or ""), p["min_people"], p["max_people"],
               fmt_num(price), "".join(parts))
        )

    def build_cost(p, groups):
        rows = []
        total_cost = 0.0
        for key, label, icon, color in CATS:
            items = groups.get(key) or []
            if key in ("utensil", "packaging"):
                items = sorted(items, key=lambda it: _fx_idx(it.get("name"), UTENSIL_ORDER if key == "utensil" else PACKAGING_ORDER))
            if not items:
                continue
            rows.append('<tr class="cat-row"><td colspan="3" style="background:%s">%s %s</td></tr>'
                        % (color, icon, label))
            for it in items:
                pp = it.get("per_package") or 0
                c = float(pp) * float(it.get("cost") or 0)
                total_cost += c
                pc = it.get("portion_count") or 0
                unit = it.get("unit") or ""
                # 食材包装/客户餐具无“每份数量”概念（仅有该套餐总数量），直接划掉单份规格列
                spec = "—" if key in ("utensil", "packaging") else (("%s%s" % (fmt_num(pp / pc), unit)) if (pp and pc) else (unit or ""))
                loss = ' <span class="loss">损耗</span>' if it.get("cost_only") else ""
                rows.append('<tr><td>%s%s</td><td>%s</td><td class="num">%.2f</td></tr>'
                            % (_html.escape(it["name"] or ""), loss, spec or "—", c))
        # 对外总价 = 菜品价 + 配送搭建费；算真实毛利率要先把这笔配送/搭建费扣掉
        total_price = float(p.get("price") or 0)
        base = float(p.get("base_price") or 0)
        fee = max(0.0, total_price - base)          # 配送搭建费（代收代付，与搭建成本基本抵消）
        revenue = total_price - fee                 # 真实收益基数（≈菜品价）
        labor = float(_labor_first or 0)            # 打包切配人工：1 个套餐 = 1 单 = 首单价
        profit = revenue - total_cost - labor
        rate = (profit / revenue * 100) if revenue else 0
        cls = "pos" if profit >= 0 else "neg"
        summary = (
            '<div class="summary">'
            '<div class="sum-item"><span>对外总价</span><b>¥%s</b></div>'
            '<div class="sum-item"><span>－ 配送搭建费</span><b>¥%s</b></div>'
            '<div class="sum-item"><span>食材成本</span><b>¥%.2f</b></div>'
            '<div class="sum-item"><span>打包切配人工</span><b>¥%s</b></div>'
            '<div class="sum-item"><span>真实毛利</span><b class="%s">¥%.2f</b></div>'
            '<div class="sum-item"><span>真实毛利率</span><b class="%s">%.1f%%</b></div>'
            '</div>'
            % (fmt_num(total_price), fmt_num(fee), total_cost, fmt_num(labor), cls, profit, cls, rate)
        )
        return (
            '<div class="pkg"><div class="pkg-head">'
            '<div class="pkg-name">%s <span class="people">%s-%s人餐</span></div>'
            '<div class="pkg-price"><span class="sym">¥</span><span class="num">%s</span>'
            '<span class="unit"> / 套 (对外总价)</span></div></div>'
            '<table class="cost-tbl"><thead><tr><th>菜品</th><th>单份规格</th><th>成本(元)</th></tr></thead>'
            '<tbody>%s</tbody></table>%s</div>'
            % (_html.escape(p["name"] or ""), p["min_people"], p["max_people"],
               fmt_num(total_price), "".join(rows), summary)
        )

    cur = g.db.cursor()
    cur.execute("SELECT id, name, min_people, max_people, base_price, price FROM packages ORDER BY min_people")
    pkgs = [dict(r) for r in cur.fetchall()]

    body = ['<div class="hero%s"><h1>%s</h1><p>%s</p></div>' % (
        " hero-cost" if show_cost else "",
        "刘和牛 · 内部成本表" if show_cost else "刘和牛 · 精品烤肉套餐",
        "仅供内部参考 · 食材成本 / 毛利核算" if show_cost else "现切现送 · 鲜料直达 · 青龙湖户外烤肉",
    )]
    for p in pkgs:
        cur.execute(
            """SELECT i.name, i.unit, i.category, i.cost, pi.per_package, pi.portion_count, pi.cost_only
               FROM package_ingredients pi JOIN ingredients i ON pi.ingredient_id = i.id
               WHERE pi.package_id = ?""", (p["id"],))
        groups = {}
        for it in [dict(r) for r in cur.fetchall()]:
            groups.setdefault(eff_cat(it["name"], it["category"]), []).append(it)
        body.append(build_cost(p, groups) if show_cost else build_customer(p, groups))

    if show_cost:
        body.append('<div class="foot-note">注：对外总价 = 菜品价 + 配送搭建费（配送费属代收代付、与搭建成本基本抵消，故从毛利率基数里扣掉）；'
                    '真实毛利 = 对外总价 － 配送搭建费 － 食材成本 － 打包切配人工；'
                    '真实毛利率 = 真实毛利 ÷（对外总价 － 配送搭建费）。食材含损耗项；人工按首单价（1 个套餐 = 1 单）。</div>')
    else:
        body.append('<div class="footer"><p>🔥 下单即送精美餐具套装</p>'
                    '<p>🚗 支持全城配送 · 提前 1 天预订</p></div>')

    return (_MENU_PRINT_TPL
            .replace("__TITLE__", "刘和牛 · 内部成本表" if show_cost else "刘和牛 · 精品菜单")
            .replace("__BACK__", "/menu")
            .replace("__BODY__", "".join(body)))


@app.route("/menu/print/customer")
def menu_print_customer():
    return _menu_print_build(False)


@app.route("/menu/print/cost")
def menu_print_cost():
    return _menu_print_build(True)


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
        # 归属筛选（?by=...）：只看某个人归属的单
        _sc, _sp = _signer_clause(request.args)
        cur.execute("""
            SELECT id FROM orders
            WHERE status IN ('pending','preparing') AND deleted_at IS NULL AND booking_date = ?
        """ + _sc + """
            ORDER BY booking_time
        """, (date,) + tuple(_sp))
        order_ids = [r["id"] for r in cur.fetchall()]
    data = calc_merged_prep(order_ids)
    return jsonify({"ok": True, "data": data, "order_ids": order_ids})


@app.route("/api/prep/dates")
def prep_dates():
    """列出所有有 pending/preparing 订单的日期，供前端做日期快捷切换。
    可选 by=me|0|<user_id> 按"归属"筛选（日期条上的单数跟着变）"""
    from calculator import today_cst as _today_cst
    db = g.db
    cur = db.cursor()
    _sc, _sp = _signer_clause(request.args)
    cur.execute("""
        SELECT booking_date, COUNT(*) as cnt
        FROM orders
        WHERE status IN ('pending','preparing') AND deleted_at IS NULL AND booking_date IS NOT NULL AND booking_date != ''
    """ + _sc + """
        GROUP BY booking_date
        ORDER BY booking_date
    """, _sp)
    today = _today_cst().isoformat()
    dates = []
    for r in cur.fetchall():
        d = dict(r)
        d["is_today"] = (r["booking_date"] == today)
        d["is_past"] = (r["booking_date"] < today)
        dates.append(d)
    return jsonify({"ok": True, "dates": dates, "today": today})


# ===== 备餐勾选 =====
@app.route("/api/prep/board-flag", methods=["POST"])
def prep_board_flag():
    """简略备餐表（白板）的「送达/出餐」勾选。
    body: {order_id, flag: 'delivered'|'served', value: 0/1}
    flag 走白名单再拼 SQL，防注入。"""
    data = request.get_json(force=True)
    oid = data.get("order_id")
    flag = data.get("flag")
    if flag not in ("delivered", "served") or not oid:
        return jsonify({"ok": False, "msg": "参数错误"}), 400
    value = 1 if data.get("value") else 0
    db = g.db
    cur = db.execute("SELECT id FROM orders WHERE id=? AND deleted_at IS NULL", (oid,))
    if not cur.fetchone():
        return jsonify({"ok": False, "msg": "订单不存在"}), 404
    db.execute("UPDATE orders SET %s=? WHERE id=?" % flag, (value, int(oid)))
    db.commit()
    return jsonify({"ok": True})


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
