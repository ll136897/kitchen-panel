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


@app.route("/orders/<int:oid>")
def order_detail_page(oid):
    return render_template("order_detail.html", oid=oid)


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
        SELECT op.*, p.name as pkg_name, p.min_people, p.max_people
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


@app.route("/api/orders", methods=["POST"])
def create_order():
    """创建订单：接收 raw_text（单个或批量用 --- 分隔），自动解析入库"""
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
    order_ids = []
    results = []
    for chunk in chunks:
        parsed = parse_order_text(chunk)
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
        qty = float(it.get("qty", 0))
        unit_price = float(it.get("unit_price", 0) or 0)
        supplier = it.get("supplier", "")
        note = it.get("note", "") or reason
        if not iid or qty <= 0:
            continue
        table = "ingredients" if itype == "ingredient" else "tools"
        # 加权平均更新成本：new_cost = (旧库存*旧成本 + 新采购量*采购单价) / (旧库存+新采购量)
        if unit_price > 0:
            row = db.execute(f"SELECT stock, cost FROM {table} WHERE id = ?", (iid,)).fetchone()
            if row:
                old_stock = row["stock"] or 0
                old_cost = row["cost"] or 0
                new_stock = old_stock + qty
                if new_stock > 0:
                    new_cost = round((old_stock * old_cost + qty * unit_price) / new_stock, 4)
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
                INSERT INTO purchases (item_type, item_id, quantity, unit_price, total_cost, supplier, note)
                VALUES (?,?,?,?,?,?,?)
            """, (itype, iid, qty, unit_price, round(qty * unit_price, 2), supplier, note))
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


@app.route("/api/ingredients/<int:iid>/cost", methods=["POST"])
def update_ingredient_cost(iid):
    """手动修改食材/工具成本单价"""
    data = request.get_json(force=True)
    cost = float(data.get("cost", 0))
    itype = data.get("item_type", "ingredient")
    table = "ingredients" if itype == "ingredient" else "tools"
    db = g.db
    db.execute(f"UPDATE {table} SET cost = ? WHERE id = ?", (cost, iid))
    db.commit()
    return jsonify({"ok": True})


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
        rows = db.execute("SELECT * FROM tools").fetchall()
        from calculator import sort_tools
        return jsonify({"ok": True, "data": sort_tools([dict(r) for r in rows], "stock")})
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
    except ImportError:
        return jsonify({"ok": False, "msg": "需要 openpyxl: pip install openpyxl"}), 500
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

    cat_colors = {
        'beef': '8E1E1A', 'pork': 'C75D3E', 'chicken': 'C9962B',
        'vegetable': '3A8A3A', 'sauce': '7A5A3A',
        'packaging': 'D35400', 'utensil': '8E44AD', 'tableware': '8E44AD',
        'staple': '666666', 'side': '666666', 'drink': '666666',
        'other': '666666', 'tool': '4A4A4A'
    }
    cat_labels = {
        'beef': '牛肉', 'pork': '猪肉', 'chicken': '鸡肉',
        'vegetable': '蔬菜', 'sauce': '小料',
        'packaging': '食材包装', 'utensil': '客户餐具/工具', 'tableware': '餐具配套',
        'staple': '主食', 'side': '小菜', 'drink': '饮料',
        'other': '其他', 'tool': '工具'
    }
    wb = Workbook()
    ws = wb.active
    ws.title = "菜单" if len(pkgs) > 1 else pkgs[0]["name"]
    headers = ["套餐", "人数", "分类", "食材/工具", "单位", "每份克数", "份数", "总量", "基价", "总价"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF", size=11)
        cell.fill = PatternFill("solid", fgColor="3A2416")
        cell.alignment = Alignment(horizontal="center")
    thin = Side(border_style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for p in pkgs:
        people = f"{p['min_people']}-{p['max_people']}人"
        # 按分类分组
        by_cat = {}
        for ing in p["ingredients"]:
            c = ing["category"] or "other"
            by_cat.setdefault(c, []).append(ing)
        for cat, items in by_cat.items():
            color = cat_colors.get(cat, "666666")
            label = cat_labels.get(cat, cat)
            # 分类标题行
            ws.append([p["name"], people, label, f"{len(items)}项", "", "", "", "", "", ""])
            for cell in ws[ws.max_row]:
                cell.fill = PatternFill("solid", fgColor=color)
                cell.font = Font(bold=True, color="FFFFFF", size=10)
            for ing in items:
                size = ing["per_package"] / max(1, ing["portion_count"])
                ws.append([p["name"], people, label, ing["ingredient"], ing["unit"],
                          round(size, 2), ing["portion_count"], ing["per_package"],
                          p["base_price"], p["price"]])
                for cell in ws[ws.max_row]:
                    cell.border = border
                    cell.alignment = Alignment(horizontal="center")
                ws.cell(ws.max_row, 4).alignment = Alignment(horizontal="left")
        # 工具
        if p["tools"]:
            ws.append([p["name"], people, "工具", f"{len(p['tools'])}项", "", "", "", "", "", ""])
            for cell in ws[ws.max_row]:
                cell.fill = PatternFill("solid", fgColor="4A4A4A")
                cell.font = Font(bold=True, color="FFFFFF", size=10)
            for t in p["tools"]:
                ws.append([p["name"], people, "工具", t["tool"], "个", "", "",
                          t["per_package"], p["base_price"], p["price"]])
                for cell in ws[ws.max_row]:
                    cell.border = border
                    cell.alignment = Alignment(horizontal="center")
                ws.cell(ws.max_row, 4).alignment = Alignment(horizontal="left")
    # 列宽
    widths = [12, 10, 10, 18, 6, 10, 6, 10, 8, 8]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64+i)].width = w
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


# ===== 财务系统 =====
@app.route("/api/finance/summary")
def finance_summary():
    """财务总览：收入、成本、毛利、押金、应收"""
    db = g.db
    cur = db.cursor()
    # 收入（非取消订单）
    r = cur.execute("""
        SELECT COUNT(*) as order_cnt,
               COALESCE(SUM(amount),0) as total_revenue,
               COALESCE(SUM(deposit),0) as total_deposit,
               COALESCE(SUM(CASE WHEN payment_status='paid' THEN amount ELSE 0 END),0) as paid_amount,
               COALESCE(SUM(CASE WHEN payment_status='unpaid' THEN amount ELSE 0 END),0) as unpaid_amount,
               COALESCE(SUM(CASE WHEN payment_status='partial' THEN amount ELSE 0 END),0) as partial_amount,
               COALESCE(SUM(CASE WHEN deposit_status='pending' THEN deposit ELSE 0 END),0) as deposit_pending,
               COALESCE(SUM(CASE WHEN deposit_status='returned' THEN deposit ELSE 0 END),0) as deposit_returned,
               COALESCE(SUM(CASE WHEN deposit_status='forfeited' THEN deposit ELSE 0 END),0) as deposit_forfeited
        FROM orders WHERE status != 'cancelled'
    """).fetchone()
    summary = dict(r)
    # 成本计算：每单食材成本 = 各食材用量 × 单价
    r = cur.execute("""
        SELECT COALESCE(SUM(pi.per_package * op.quantity * i.cost),0) as food_cost
        FROM order_packages op
        JOIN orders o ON op.order_id = o.id
        JOIN package_ingredients pi ON pi.package_id = op.package_id
        JOIN ingredients i ON pi.ingredient_id = i.id
        WHERE o.status != 'cancelled'
    """).fetchone()
    summary["food_cost"] = r["food_cost"] or 0
    # 工具损耗成本（丢失的工具 × 真实成本）
    r = cur.execute("""
        SELECT COALESCE(SUM(tl.lost_qty * t.cost),0) as tool_loss_cost
        FROM tool_loans tl
        JOIN orders o ON tl.order_id = o.id
        JOIN tools t ON tl.tool_id = t.id
        WHERE o.status != 'cancelled'
    """).fetchone()
    summary["tool_loss_cost"] = r["tool_loss_cost"] or 0
    # 配送费收入
    r = cur.execute("""
        SELECT COALESCE(SUM(CASE WHEN o.status!='cancelled'
            THEN (o.amount - p.base_price * op.quantity)
            ELSE 0 END),0) as delivery_revenue
        FROM orders o
        JOIN order_packages op ON op.order_id = o.id
        JOIN packages p ON op.package_id = p.id
    """).fetchone()
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
    """订单财务明细列表"""
    db = g.db
    cur = db.cursor()
    rows = cur.execute("""
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
        FROM orders o ORDER BY o.created_at DESC LIMIT 200
    """).fetchall()
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
               i.id as ingredient_id, i.name, i.unit, i.cost, i.category
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
        items.append({
            "ingredient_id": r["ingredient_id"],
            "name": r["name"], "unit": r["unit"],
            "per_package": r["per_package"],
            "portion_count": r["portion_count"],
            "order_qty": r["order_qty"],
            "cost_only": r["cost_only"],
            "total_amount": round(total_amount, 2),
            "unit_cost": r["cost"] or 0,
            "total_cost": round(total_cost, 2),
            "category": r["category"],
        })
    return jsonify({"ok": True, "data": items})


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
                "FROM orders WHERE status IN ('pending','preparing') ORDER BY id")
    orders = [dict(r) for r in cur.fetchall()]

    ing_sum = {}  # id -> {name, unit, total, per_package, total_portions, total_packages}
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
                SELECT pi.per_package, pi.portion_count, i.name, i.unit, i.id
                FROM package_ingredients pi JOIN ingredients i ON pi.ingredient_id = i.id
                WHERE pi.package_id = ? AND pi.cost_only = 0
            """, (pk["id"],))
            for r in cur.fetchall():
                key = r["id"]
                ing_sum.setdefault(key, {
                    "name": r["name"], "unit": r["unit"], "total": 0,
                    "per_package": r["per_package"],
                    "portion_count": r["portion_count"] or 1,
                    "total_packages": 0,
                })
                ing_sum[key]["total"] += r["per_package"] * pk["quantity"]
                ing_sum[key]["total_packages"] += pk["quantity"]
                ing_sum[key]["per_package"] = r["per_package"]
                ing_sum[key]["portion_count"] = r["portion_count"] or 1
            cur.execute("""
                SELECT pt.per_package, t.name, t.id
                FROM package_tools pt JOIN tools t ON pt.tool_id = t.id
                WHERE pt.package_id = ?
            """, (pk["id"],))
            for r in cur.fetchall():
                key = r["id"]
                tool_sum.setdefault(key, {"name": r["name"], "total": 0})
                tool_sum[key]["total"] += r["per_package"] * pk["quantity"]

    from calculator import CATEGORY_ORDER, CATEGORY_LABEL, PACKAGING_CATEGORY, \
        UTENSIL_CATEGORY, SAUCE_LIKE_CATS, _subcategorize_meat, _get_fixed_sort_index, sort_tools
    tools_sorted = sort_tools(list(tool_sum.values()), "total")
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
        # 计算份数列
        pc = v.get("portion_count", 1) or 1
        v["portion_size"] = round(v["per_package"] / max(1, pc), 2)
        v["total_portions"] = pc * v.get("total_packages", 1)

    ing_by_cat = {}
    packaging_list = []
    tableware_list = []
    for cat in CATEGORY_ORDER:
        items = [v for v in ing_sum.values() if v.get("category") == cat]
        if items:
            ing_by_cat[cat] = sorted(items, key=lambda x: (_get_fixed_sort_index(x["name"], cat), -x["total"]))
    packaging_list = [v for v in ing_sum.values() if v.get("category") == PACKAGING_CATEGORY]
    packaging_list = sorted(packaging_list, key=lambda x: -x["total"])
    tableware_list = [v for v in ing_sum.values() if v.get("category") == UTENSIL_CATEGORY]
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
        ing_rows += f'<tr><td colspan="5" style="background:{color};color:#fff;font-weight:700;padding:6px 8px;">{label}</td></tr>'
        for i in items:
            ing_rows += f'<tr><td>{i["name"]}</td><td style="text-align:right">{i.get("total_portions","-")}份</td><td style="text-align:right">{i.get("portion_size","-")}{i["unit"]}/份</td><td style="text-align:right;font-weight:700">{round(i["total"],2)}{i["unit"]}</td><td></td></tr>'
    if packaging_list:
        ing_rows += '<tr><td colspan="5" style="background:#d35400;color:#fff;font-weight:700;padding:6px 8px;">📦 食材包装</td></tr>'
        for i in packaging_list:
            ing_rows += f'<tr><td>{i["name"]}</td><td></td><td></td><td style="text-align:right;font-weight:700">{round(i["total"],2)}{i["unit"]}</td><td></td></tr>'
    if tableware_list:
        ing_rows += '<tr><td colspan="5" style="background:#8e44ad;color:#fff;font-weight:700;padding:6px 8px;">🍱 客户餐具/工具</td></tr>'
        for i in tableware_list:
            ing_rows += f'<tr><td>{i["name"]}</td><td></td><td></td><td style="text-align:right;font-weight:700">{round(i["total"],2)}{i["unit"]}</td><td></td></tr>'

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
<table><thead><tr><th>食材</th><th>份数</th><th>克数/份</th><th>总克数</th><th></th></tr></thead>
<tbody>{ing_rows or '<tr><td colspan=5 style="text-align:center;color:#aaa;padding:20px">—</td></tr>'}</tbody></table>

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
