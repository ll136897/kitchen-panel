"""烤肉店后厨备餐系统 - Flask 主应用"""
import math
from flask import Flask, request, jsonify, render_template, g
from models import get_db, init_db
from parser import parse_order_text, match_packages_in_db
from calculator import calc_order_requirements, calc_dashboard, preview_parse, calc_merged_prep, calc_prep_urgency

app = Flask(__name__, template_folder="templates", static_folder="static")

# 模块加载时初始化数据库（保证 gunicorn 多 worker 也能跑）
init_db()
try:
    from seed import seed_data
    seed_data()
except Exception as _e:
    print(f"[init] seed_data skipped: {_e}")


@app.before_request
def before():
    g.db = get_db()


@app.teardown_request
def teardown(exc):
    db = getattr(g, "db", None)
    if db is not None:
        db.close()


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


@app.route("/history")
def history_page():
    return render_template("history.html")


@app.route("/prep")
def prep_page():
    return render_template("prep.html")


@app.route("/menu")
def menu_page():
    return render_template("menu.html")


# ===== 订单解析与入库 =====
@app.route("/api/parse", methods=["POST"])
def api_parse():
    """解析订单文本预览（不入库）"""
    data = request.get_json(force=True)
    raw = data.get("raw_text", "")
    if not raw.strip():
        return jsonify({"ok": False, "msg": "文本为空"}), 400
    parsed = preview_parse(raw)
    return jsonify({"ok": True, "data": parsed})


@app.route("/api/orders", methods=["POST"])
def create_order():
    """创建订单：接收 raw_text，自动解析入库"""
    data = request.get_json(force=True)
    raw = data.get("raw_text", "")
    if not raw.strip():
        return jsonify({"ok": False, "msg": "文本为空"}), 400

    parsed = parse_order_text(raw)
    matched = match_packages_in_db(parsed["packages"])

    db = g.db
    cur = db.cursor()
    cur.execute("""
        INSERT INTO orders
        (raw_text, booking_date, booking_time, address, contact_name,
         contact_phone, amount, deposit, meal_time, pickup_time, note, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending')
    """, (
        raw, parsed["booking_date"], parsed["booking_time"],
        parsed["address"], parsed["contact_name"], parsed["contact_phone"],
        parsed["amount"], parsed["deposit"], parsed["meal_time"],
        parsed["pickup_time"], parsed["note"],
    ))
    order_id = cur.lastrowid

    for pk in matched:
        if not pk["package_id"]:
            continue
        cur.execute("""
            INSERT INTO order_packages
            (order_id, package_id, people, quantity)
            VALUES (?,?,?,?)
        """, (order_id, pk["package_id"], pk["people"], pk["quantity"]))

    db.commit()
    return jsonify({"ok": True, "order_id": order_id})


@app.route("/api/orders")
def list_orders():
    db = g.db
    cur = db.cursor()
    cur.execute("""
        SELECT id, booking_date, booking_time, address, contact_name,
               contact_phone, amount, deposit, note, status, created_at
        FROM orders ORDER BY id DESC LIMIT 100
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
    return jsonify({"ok": True, "data": orders})


@app.route("/api/orders/<int:oid>", methods=["PATCH"])
def update_order_status(oid):
    data = request.get_json(force=True)
    status = data.get("status")
    if status not in ("pending", "preparing", "done", "cancelled"):
        return jsonify({"ok": False, "msg": "状态非法"}), 400
    db = g.db
    db.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
    # 转备餐中 → 自动借出该订单所需工具
    if status == "preparing":
        borrow_tools(db, oid)
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
    """确认入库：接收 [{item_type, item_id, qty, reason}] 列表"""
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
        qty = float(it.get("qty", 0))
        if not iid or qty <= 0:
            continue
        table = "ingredients" if itype == "ingredient" else "tools"
        db.execute(f"UPDATE {table} SET stock = stock + ? WHERE id = ?", (qty, iid))
        db.execute("""
            INSERT INTO stock_logs (item_type, item_id, delta, reason)
            VALUES (?,?,?,?)
        """, (itype, iid, qty, reason))
        done += 1
    db.commit()
    return jsonify({"ok": True, "done": done})


# ===== 配置管理 =====
@app.route("/api/ingredients", methods=["GET", "POST"])
def manage_ingredients():
    db = g.db
    if request.method == "GET":
        rows = db.execute("SELECT * FROM ingredients ORDER BY id").fetchall()
        return jsonify({"ok": True, "data": [dict(r) for r in rows]})
    data = request.get_json(force=True)
    db.execute("""
        INSERT INTO ingredients (name, unit, stock, threshold, cost)
        VALUES (?,?,?,?,?)
    """, (data["name"], data.get("unit", ""), data.get("stock", 0),
          data.get("threshold", 0), data.get("cost", 0)))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/ingredients/<int:iid>", methods=["PUT"])
def update_ingredient(iid):
    data = request.get_json(force=True)
    db = g.db
    db.execute("""
        UPDATE ingredients SET name=?, unit=?, stock=?, threshold=?, cost=?
        WHERE id=?
    """, (data["name"], data.get("unit", ""), data.get("stock", 0),
          data.get("threshold", 0), data.get("cost", 0), iid))
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
        rows = db.execute("SELECT * FROM tools ORDER BY id").fetchall()
        return jsonify({"ok": True, "data": [dict(r) for r in rows]})
    data = request.get_json(force=True)
    db.execute("INSERT INTO tools (name, stock, threshold) VALUES (?,?,?)",
              (data["name"], data.get("stock", 0), data.get("threshold", 0)))
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
                WHERE pi.package_id = ?
                ORDER BY i.category, i.name
            """, (p["id"],))
            p["ingredients"] = [dict(r) for r in cur.fetchall()]
            cur.execute("""
                SELECT pt.id, pt.per_package, t.name as tool_name, t.id as tool_id
                FROM package_tools pt
                JOIN tools t ON pt.tool_id = t.id
                WHERE pt.package_id = ?
            """, (p["id"],))
            p["tools"] = [dict(r) for r in cur.fetchall()]
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


# 导出菜单原数据(JSON / CSV)
@app.route("/api/menu/export")
def export_menu():
    import csv, io, json as _json
    fmt = request.args.get("format", "json")
    cur = g.db.cursor()
    cur.execute("SELECT * FROM packages ORDER BY min_people")
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
        p["tools"] = [dict(r) for r in cur.fetchall()]

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
        resp.headers["Content-Disposition"] = "attachment; filename=menu.csv"
        return resp
    return jsonify({"ok": True, "data": pkgs})


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
        FROM orders GROUP BY strftime('%Y-%m', created_at)
        ORDER BY month DESC LIMIT 12
    """).fetchall()
    monthly = [dict(r) for r in rows]

    # 按周聚合（近 8 周）
    rows = db.execute("""
        SELECT strftime('%Y-W%W', created_at) as week,
               COUNT(*) as cnt,
               COALESCE(SUM(CASE WHEN status!='cancelled' THEN amount ELSE 0 END),0) as revenue
        FROM orders GROUP BY strftime('%Y-W%W', created_at)
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
        WHERE o.status != 'cancelled'
        GROUP BY p.id
        ORDER BY total_qty DESC
    """).fetchall()
    packages_pop = [dict(r) for r in rows]

    # 状态分布
    rows = db.execute("""
        SELECT status, COUNT(*) as cnt FROM orders GROUP BY status
    """).fetchall()
    status_dist = {r["status"]: r["cnt"] for r in rows}

    # 总览
    rows = db.execute("""
        SELECT COUNT(*) as total,
               COALESCE(SUM(CASE WHEN status!='cancelled' THEN amount ELSE 0 END),0) as total_revenue,
               AVG(CASE WHEN status!='cancelled' THEN amount END) as avg_amount
        FROM orders
    """).fetchone()
    overview = dict(rows)

    return jsonify({"ok": True, "monthly": monthly, "weekly": weekly,
                    "packages_pop": packages_pop, "status_dist": status_dist, "overview": overview})


# ===== 打印备餐单（按食材汇总）=====
@app.route("/api/print/menu")
def print_menu():
    """返回待备/备餐中订单的食材+工具汇总，适合打印"""
    import json as _json
    db = g.db
    cur = db.cursor()
    cur.execute("SELECT id, booking_date, booking_time, address, contact_name, status "
                "FROM orders WHERE status IN ('pending','preparing') ORDER BY id")
    orders = [dict(r) for r in cur.fetchall()]

    ing_sum = {}  # id -> {name, unit, total}
    tool_sum = {}
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

        for pk in pkgs:
            cur.execute("""
                SELECT pi.per_package, i.name, i.unit, i.id
                FROM package_ingredients pi JOIN ingredients i ON pi.ingredient_id = i.id
                WHERE pi.package_id = ?
            """, (pk["id"],))
            for r in cur.fetchall():
                key = r["id"]
                ing_sum.setdefault(key, {"name": r["name"], "unit": r["unit"], "total": 0})
                ing_sum[key]["total"] += r["per_package"] * pk["quantity"]
            cur.execute("""
                SELECT pt.per_package, t.name, t.id
                FROM package_tools pt JOIN tools t ON pt.tool_id = t.id
                WHERE pt.package_id = ?
            """, (pk["id"],))
            for r in cur.fetchall():
                key = r["id"]
                tool_sum.setdefault(key, {"name": r["name"], "total": 0})
                tool_sum[key]["total"] += r["per_package"] * pk["quantity"]

    tools_sorted = sorted(tool_sum.values(), key=lambda x: -x["total"])

    # 食材按分类分组，与备餐表顺序一致
    from calculator import CATEGORY_ORDER, CATEGORY_LABEL, TABLEWARE_CATEGORY, \
        SAUCE_LIKE_CATS, _subcategorize_meat
    db2 = get_db()
    cur2 = db2.cursor()
    cur2.execute("SELECT id, category, name FROM ingredients")
    cat_map = {r["id"]: (r["category"], r["name"]) for r in cur2.fetchall()}
    db2.close()

    for k, v in ing_sum.items():
        raw_cat, name = cat_map.get(k, ("other", v["name"]))
        sub_cat = _subcategorize_meat(name, raw_cat)
        if sub_cat in SAUCE_LIKE_CATS:
            sub_cat = "sauce"
        v["category"] = sub_cat

    ing_by_cat = {}
    tableware_list = []
    for cat in CATEGORY_ORDER:
        items = [v for v in ing_sum.values() if v.get("category") == cat]
        if items:
            ing_by_cat[cat] = sorted(items, key=lambda x: -x["total"])
    tableware_list = [v for v in ing_sum.values() if v.get("category") == TABLEWARE_CATEGORY]
    tableware_list = sorted(tableware_list, key=lambda x: -x["total"])

    # 渲染成一个可打印的 HTML（独立页面，无 nav，适合 A4 打印）
    today = _json
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    order_lines = ""
    for o in order_details:
        pkg_str = ", ".join(f"{p['name']}×{p['quantity']}" for p in o["packages"])
        order_lines += f"""
        <tr><td>{o['id']}</td><td>{o['booking_date']} {o['booking_time'] or ''}</td>
            <td>{o['address'] or ''}</td><td>{o['contact_name'] or ''}</td>
            <td>{pkg_str}</td></tr>"""

    # 食材按分类渲染（与备餐表顺序一致）
    cat_colors = {
        "beef": "#8e1e1a", "pork": "#c75d3e", "chicken": "#c9962b",
        "vegetable": "#3a8a3a", "sauce": "#7a5a3a", "other": "#6b4f3a",
    }
    ing_rows = ""
    for cat in CATEGORY_ORDER:
        items = ing_by_cat.get(cat, [])
        if not items:
            continue
        label = CATEGORY_LABEL.get(cat, cat)
        color = cat_colors.get(cat, "#6b4f3a")
        ing_rows += f'<tr><td colspan="2" style="background:{color};color:#fff;font-weight:700;padding:6px 8px;">{label}</td></tr>'
        for i in items:
            ing_rows += f'<tr><td>{i["name"]}</td><td>{round(i["total"],2)}{i["unit"]}</td></tr>'
    if tableware_list:
        ing_rows += '<tr><td colspan="2" style="background:#8e44ad;color:#fff;font-weight:700;padding:6px 8px;">🍱 餐具配套</td></tr>'
        for i in tableware_list:
            ing_rows += f'<tr><td>{i["name"]}</td><td>{round(i["total"],2)}{i["unit"]}</td></tr>'

    tool_rows = "".join(
        f'<tr><td>{t["name"]}</td><td>{t["total"]}</td></tr>'
        for t in tools_sorted)

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>备餐单 · {now}</title>
<style>
  @media print{{ @page{{ size:A4; margin:12mm }} }}
  body{{ font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif; color:#2b1e14; padding:20px; }}
  h1{{ font-size:22px; margin:0 0 4px; }}
  .meta{{ color:#8a7f75; font-size:13px; margin-bottom:14px; }}
  .sec-title{{ font-size:15px; font-weight:700; margin:18px 0 8px; padding-bottom:4px; border-bottom:2px solid #c0392b; color:#c0392b; }}
  table{{ width:100%; border-collapse:collapse; font-size:13px; }}
  th{{ background:#f3efe8; text-align:left; padding:6px 8px; }}
  td{{ padding:6px 8px; border-bottom:1px solid #efe8dd; }}
  .print-btn{{ margin:12px 6px 12px 0; padding:8px 16px; background:#c0392b; color:#fff; border:none; border-radius:6px; font-size:14px; cursor:pointer; }}
  .back-btn{{ margin:12px 0; padding:8px 16px; background:#fff; color:#2b1e14; border:1.5px solid #c0392b; border-radius:6px; font-size:14px; cursor:pointer; text-decoration:none; display:inline-block; }}
  .back-btn:hover{{ background:#fdecea; }}
  @media print{{ .print-btn, .back-btn{{ display:none }} }}
</style></head><body>
<a class="back-btn" href="javascript:history.length>1?history.back():'/'">← 返回</a>
<button class="print-btn" onclick="window.print()">🖨️ 打印 / 存为PDF</button>
<h1>🔥 刘和牛户外烤肉 · 备餐单</h1>
<div class="meta">生成时间：{now} · 共 {len(orders)} 单待备 / 备餐中</div>

<div class="sec-title">📋 订单清单</div>
<table><thead><tr><th>#</th><th>时间</th><th>地址</th><th>联系人</th><th>套餐</th></tr></thead>
<tbody>{order_lines or '<tr><td colspan=5 style="text-align:center;color:#aaa;padding:20px">暂无待备订单</td></tr>'}</tbody></table>

<div class="sec-title">🥩 食材汇总</div>
<table><thead><tr><th>食材</th><th>合计用量</th></tr></thead>
<tbody>{ing_rows or '<tr><td colspan=2 style="text-align:center;color:#aaa;padding:20px">—</td></tr>'}</tbody></table>

<div class="sec-title">🔧 工具汇总</div>
<table><thead><tr><th>工具</th><th>合计数量</th></tr></thead>
<tbody>{tool_rows or '<tr><td colspan=2 style="text-align:center;color:#aaa;padding:20px">—</td></tr>'}</tbody></table>
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
        date = request.args.get("date") or datetime.date.today().isoformat()
        cur.execute("""
            SELECT id FROM orders
            WHERE status IN ('pending','preparing') AND booking_date = ?
            ORDER BY booking_time
        """, (date,))
        order_ids = [r["id"] for r in cur.fetchall()]
    data = calc_merged_prep(order_ids)
    return jsonify({"ok": True, "data": data, "order_ids": order_ids})


@app.route("/api/prep/dates")
def prep_dates():
    """列出所有有 pending/preparing 订单的日期，供前端做日期快捷切换"""
    import datetime
    db = g.db
    cur = db.cursor()
    cur.execute("""
        SELECT booking_date, COUNT(*) as cnt
        FROM orders
        WHERE status IN ('pending','preparing') AND booking_date IS NOT NULL AND booking_date != ''
        GROUP BY booking_date
        ORDER BY booking_date
    """)
    today = datetime.date.today().isoformat()
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
