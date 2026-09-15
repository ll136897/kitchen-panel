"""备餐计算引擎：套餐/单点 → 食材+工具需求；库存缺口计算；可备份数"""
import re
import datetime
from models import get_db
from parser import parse_order_text, match_packages_in_db


def get_available_tool_stock():
    """返回 {tool_id: 可用数量} = 总库存 - 已借出未归还数量"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, stock FROM tools")
        stock = {r["id"]: r["stock"] or 0 for r in cur.fetchall()}
        cur.execute("""
            SELECT tool_id, SUM(quantity - returned_qty - lost_qty) as out
            FROM tool_loans WHERE status IN ('borrowed','partial')
            GROUP BY tool_id
        """)
        for r in cur.fetchall():
            out = r["out"] or 0
            stock[r["tool_id"]] = max(0, stock.get(r["tool_id"], 0) - out)
        return stock


def parse_time_str(t):
    """把 '12.00' / '12:00' / '12点' 解析成 (hour, minute)，失败返回 None"""
    if not t:
        return None
    m = re.search(r"(\d{1,2})[.:：点时](\d{1,2})", str(t))
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{1,2})", str(t))
    if m:
        return int(m.group(1)), 0
    return None


def calc_prep_urgency(order):
    """根据用餐时间计算备餐紧迫性。
    返回 {level: 'normal'|'soon'|'overdue', minutes_to_prep: int, label: str}
    level: normal=充裕, soon=1小时内该备餐, overdue=已过应备餐时间
    """
    prep_lead = 2
    try:
        from models import get_db as _g
        with _g() as c:
            r = c.execute("SELECT value FROM settings WHERE key='prep_lead_hours'").fetchone()
            if r:
                prep_lead = float(r["value"])
    except Exception:
        pass

    meal_t = order.get("meal_time") or order.get("booking_time")
    hhmm = parse_time_str(meal_t)
    if not hhmm:
        return {"level": "normal", "minutes_to_prep": None, "label": "时间待定"}

    now = datetime.datetime.now()
    meal_dt = now.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)
    prep_start = meal_dt - datetime.timedelta(hours=prep_lead)
    diff_min = int((prep_start - now).total_seconds() / 60)

    if diff_min < 0:
        level = "overdue"
        label = f"⚠️ 应在 {-diff_min} 分钟前开始备餐"
    elif diff_min <= 60:
        level = "soon"
        label = f"⏰ 还剩 {diff_min} 分钟该备餐"
    else:
        level = "normal"
        label = f"还有 {diff_min//60}小时{diff_min%60}分"
    return {"level": level, "minutes_to_prep": diff_min, "label": label}


def calc_order_requirements(order_id):
    """
    根据订单ID计算该订单的备餐需求（食材+工具）。
    核心逻辑：每套餐固定配量 × 订单里的套餐份数
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT op.*, p.name as pkg_name, p.min_people, p.max_people
            FROM order_packages op
            LEFT JOIN packages p ON op.package_id = p.id
            WHERE op.order_id = ?
        """, (order_id,))
        pkgs = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT od.*, d.name as dish_name, d.price
            FROM order_dishes od
            LEFT JOIN dishes d ON od.dish_id = d.id
            WHERE od.order_id = ?
        """, (order_id,))
        dishes = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT raw_text FROM orders WHERE id = ?", (order_id,))
        row = cur.fetchone()
        raw_text = row["raw_text"] if row else ""

    # 食材需求 = 套餐(每套餐固定配量 × 订单份数) + 单点
    # ing_demand: id -> {total, per_package(代表), order_qty(套餐份数和)}
    ing_demand = {}

    with get_db() as conn:
        cur = conn.cursor()
        for pk in pkgs:
            cur.execute("""
                SELECT pi.per_package, pi.portion_count, i.id, i.name, i.unit
                FROM package_ingredients pi
                JOIN ingredients i ON pi.ingredient_id = i.id
                WHERE pi.package_id = ?
            """, (pk["package_id"],))
            for r in cur.fetchall():
                amount = r["per_package"] * pk["quantity"]
                pc = r["portion_count"] or 1
                if r["id"] not in ing_demand:
                    ing_demand[r["id"]] = {"total": 0, "per_package": r["per_package"], "order_qty": 0, "portion_count": pc}
                ing_demand[r["id"]]["total"] += amount
                ing_demand[r["id"]]["per_package"] = r["per_package"]
                ing_demand[r["id"]]["portion_count"] = pc
                ing_demand[r["id"]]["order_qty"] += pk["quantity"]

        for d in dishes:
            cur.execute("""
                SELECT di.amount, i.id
                FROM dish_ingredients di
                JOIN ingredients i ON di.ingredient_id = i.id
                WHERE di.dish_id = ?
            """, (d["dish_id"],))
            for r in cur.fetchall():
                amount = r["amount"] * d["quantity"]
                if r["id"] not in ing_demand:
                    ing_demand[r["id"]] = {"total": 0, "per_package": r["amount"], "order_qty": 0}
                ing_demand[r["id"]]["total"] += amount
                ing_demand[r["id"]]["per_package"] = r["amount"]
                ing_demand[r["id"]]["order_qty"] += d["quantity"]

    # 工具需求 = 套餐工具 + 备注解析的额外工具
    tool_demand = {}
    with get_db() as conn:
        cur = conn.cursor()
        for pk in pkgs:
            cur.execute("""
                SELECT pt.per_package, t.id, t.name
                FROM package_tools pt
                JOIN tools t ON pt.tool_id = t.id
                WHERE pt.package_id = ?
            """, (pk["package_id"],))
            for r in cur.fetchall():
                amount = r["per_package"] * pk["quantity"]
                if r["id"] not in tool_demand:
                    tool_demand[r["id"]] = {"total": 0, "per_package": r["per_package"], "order_qty": 0}
                tool_demand[r["id"]]["total"] += amount
                tool_demand[r["id"]]["per_package"] = r["per_package"]
                tool_demand[r["id"]]["order_qty"] += pk["quantity"]

        # 备注解析额外工具
        parsed = parse_order_text(raw_text)
        extra = parsed.get("extra_tools", {})
        for tname, qty in extra.items():
            cur.execute("SELECT id FROM tools WHERE name = ?", (tname,))
            tr = cur.fetchone()
            if tr:
                tid = tr["id"]
                if tid not in tool_demand:
                    tool_demand[tid] = {"total": 0, "per_package": qty, "order_qty": 0}
                tool_demand[tid]["total"] += qty
                tool_demand[tid]["per_package"] = qty
                tool_demand[tid]["order_qty"] += 1

        # 备注解析额外加菜（单点食材）
        from parser import match_extra_ingredients_to_db
        extra_ings = match_extra_ingredients_to_db(parsed.get("extra_ingredients", []))
        for ei in extra_ings:
            iid = ei.get("matched_id")
            if not iid:
                continue
            amount = ei["total"]  # 已经算好的总克数
            if iid not in ing_demand:
                ing_demand[iid] = {"total": 0, "per_package": ei["per_package"], "order_qty": 0}
            ing_demand[iid]["total"] += amount
            ing_demand[iid]["per_package"] = ei["per_package"]
            ing_demand[iid]["order_qty"] += ei["qty"]

    # 拉取详情
    ingredients_result = []
    with get_db() as conn:
        cur = conn.cursor()
        for ing_id, info in ing_demand.items():
            cur.execute("SELECT name, unit, stock, threshold FROM ingredients WHERE id = ?", (ing_id,))
            r = cur.fetchone()
            if r:
                stock = r["stock"] or 0
                need = info["total"]
                ingredients_result.append({
                    "id": ing_id, "name": r["name"], "unit": r["unit"],
                    "need": round(need, 2), "stock": stock,
                    "shortage": round(max(0, need - stock), 2),
                    "threshold": r["threshold"] or 0,
                    "warning": stock <= (r["threshold"] or 0),
                    "per_package": info["per_package"],
                    "order_qty": info["order_qty"],
                    "portion_count": info.get("portion_count", 1),
                    "portion_size": round(info["per_package"] / max(1, info.get("portion_count", 1)), 2),
                    "total_portions": info.get("portion_count", 1) * info["order_qty"],
                })

        cur.execute("SELECT id, name, unit, stock, threshold FROM ingredients")
        all_ings = [dict(r) for r in cur.fetchall()]
        existing_ids = {x["id"] for x in ingredients_result}
        for ing in all_ings:
            if ing["id"] not in existing_ids and ing["stock"] <= ing["threshold"]:
                ingredients_result.append({
                    "id": ing["id"], "name": ing["name"], "unit": ing["unit"],
                    "need": 0, "stock": ing["stock"], "shortage": 0,
                    "threshold": ing["threshold"], "warning": True,
                    "per_package": 0, "order_qty": 0,
                })

        tools_result = []
        avail_tools = get_available_tool_stock()
        for t_id, info in tool_demand.items():
            cur.execute("SELECT name, stock, threshold FROM tools WHERE id = ?", (t_id,))
            r = cur.fetchone()
            if r:
                total_stock = r["stock"] or 0
                avail = avail_tools.get(t_id, total_stock)
                need = info["total"]
                tools_result.append({
                    "id": t_id, "name": r["name"],
                    "need": need, "stock": avail, "total_stock": total_stock,
                    "shortage": max(0, need - avail),
                    "threshold": r["threshold"] or 0,
                    "warning": avail <= (r["threshold"] or 0),
                    "per_package": info["per_package"],
                    "order_qty": info["order_qty"],
                })

        cur.execute("SELECT id, name, stock, threshold FROM tools")
        all_tools = [dict(r) for r in cur.fetchall()]
        existing_tids = {x["id"] for x in tools_result}
        for t in all_tools:
            avail = avail_tools.get(t["id"], t["stock"])
            if t["id"] not in existing_tids and avail <= t["threshold"]:
                tools_result.append({
                    "id": t["id"], "name": t["name"],
                    "need": 0, "stock": avail, "total_stock": t["stock"],
                    "shortage": 0,
                    "threshold": t["threshold"], "warning": True,
                    "per_package": 0, "order_qty": 0,
                })

    return {"ingredients": ingredients_result, "tools": tools_result}


def calc_dashboard():
    """
    全局库存面板数据。
    套餐可备份数：按该套餐每套餐所需食材，用剩余库存（减已汇总需求）反算。
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM orders WHERE status IN ('pending','preparing')")
        order_ids = [r["id"] for r in cur.fetchall()]

        # ---- 今日统计 ----
        cur.execute("SELECT COUNT(*) as c, COALESCE(SUM(amount),0) as amt "
                    "FROM orders WHERE date(created_at) = date('now','localtime')")
        today = cur.fetchone()
        cur.execute("SELECT status, COUNT(*) as c FROM orders GROUP BY status")
        status_map = {r["status"]: r["c"] for r in cur.fetchall()}
        today_count = today["c"]
        today_amt = round(today["amt"] or 0, 2)
        pending_count = len(order_ids)
        preparing_count = status_map.get("preparing", 0)
        done_count = status_map.get("done", 0)

    total_ing = {}
    total_tool = {}
    for oid in order_ids:
        req = calc_order_requirements(oid)
        for x in req["ingredients"]:
            total_ing[x["id"]] = total_ing.get(x["id"], 0) + x["need"]
        for x in req["tools"]:
            total_tool[x["id"]] = total_tool.get(x["id"], 0) + x["need"]

    ing_panel = []
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM ingredients")
        for r in cur.fetchall():
            need = total_ing.get(r["id"], 0)
            stock = r["stock"] or 0
            shortage = max(0, need - stock)
            total_ref = max(stock, need, r["threshold"] or 0, 1)
            # 库存百分比（已用 = 需求/总可用）—— 用 stock/(stock+need) 或 need/stock
            pct = round(min(100, (need / (stock + need) * 100))) if (stock + need) > 0 else 0
            ing_panel.append({
                "id": r["id"], "name": r["name"], "unit": r["unit"],
                "stock": stock, "need": round(need, 2),
                "shortage": round(shortage, 2),
                "threshold": r["threshold"] or 0,
                "warning": stock <= (r["threshold"] or 0),
                "low_stock": shortage > 0,
                "pct": pct,
            })

        tool_panel = []
        avail_tools = get_available_tool_stock()
        cur.execute("SELECT * FROM tools")
        for r in cur.fetchall():
            need = total_tool.get(r["id"], 0)
            total_stock = r["stock"] or 0
            stock = avail_tools.get(r["id"], total_stock)
            shortage = max(0, need - stock)
            pct = round(min(100, (need / (stock + need) * 100))) if (stock + need) > 0 else 0
            tool_panel.append({
                "id": r["id"], "name": r["name"],
                "stock": stock, "total_stock": total_stock, "need": need,
                "shortage": shortage,
                "threshold": r["threshold"] or 0,
                "warning": stock <= (r["threshold"] or 0),
                "low_stock": shortage > 0,
                "pct": pct,
            })

        # 套餐可备份数：用 per_package 反算
        cur.execute("SELECT * FROM packages ORDER BY min_people")
        pkgs = [dict(r) for r in cur.fetchall()]
        pkg_capacity = []
        max_cap_ref = 1
        for pk in pkgs:
            cur.execute("""
                SELECT pi.per_package, i.id, i.stock
                FROM package_ingredients pi
                JOIN ingredients i ON pi.ingredient_id = i.id
                WHERE pi.package_id = ?
            """, (pk["id"],))
            rows = cur.fetchall()
            if not rows:
                capacity = 0
            else:
                cap_per_pkg = []
                for r in rows:
                    need_per_pkg = r["per_package"]
                    available = (r["stock"] or 0) - total_ing.get(r["id"], 0)
                    if need_per_pkg <= 0:
                        continue
                    cap_per_pkg.append(int(available // need_per_pkg) if available > 0 else 0)
                capacity = min(cap_per_pkg) if cap_per_pkg else 0
            pkg_capacity.append({
                "id": pk["id"], "name": pk["name"],
                "max_people": pk["max_people"],
                "available_packages": max(0, capacity),
            })
            max_cap_ref = max(max_cap_ref, capacity)
        # 算个百分比（相对最高可备数）用于前端进度条
        for p in pkg_capacity:
            p["pct"] = round(p["available_packages"] / max_cap_ref * 100) if max_cap_ref else 0

    # 告警汇总
    shortage_ings = [x for x in ing_panel if x["shortage"] > 0]
    shortage_tools = [x for x in tool_panel if x["shortage"] > 0]
    low_ings = [x for x in ing_panel if x["warning"] and x["shortage"] == 0]
    low_tools = [x for x in tool_panel if x["warning"] and x["shortage"] == 0]

    # ---- 近期预约订单（未来订单，按日期分组）----
    upcoming = []
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, booking_date, booking_time, meal_time, address, contact_name,
                   contact_phone, amount, status, note
            FROM orders
            WHERE status IN ('pending','preparing')
              AND booking_date IS NOT NULL AND booking_date != ''
            ORDER BY booking_date, booking_time
        """)
        for r in cur.fetchall():
            o = dict(r)
            cur.execute("""
                SELECT op.quantity, p.name
                FROM order_packages op
                LEFT JOIN packages p ON op.package_id = p.id
                WHERE op.order_id = ?
            """, (o["id"],))
            o["packages"] = [dict(x) for x in cur.fetchall()]
            o["urgency"] = calc_prep_urgency(o)
            upcoming.append(o)

    return {
        "ingredients": ing_panel,
        "tools": tool_panel,
        "package_capacity": pkg_capacity,
        "pending_order_count": len(order_ids),
        "upcoming_orders": upcoming,
        "today": {
            "count": today_count,
            "revenue": today_amt,
            "pending": pending_count,
            "preparing": preparing_count,
            "done": done_count,
        },
        "alerts": {
            "shortage_ing": len(shortage_ings),
            "shortage_tool": len(shortage_tools),
            "low_ing": len(low_ings),
            "low_tool": len(low_tools),
            "shortage_ing_names": [x["name"] for x in shortage_ings],
            "shortage_tool_names": [x["name"] for x in shortage_tools],
        },
    }


def preview_parse(raw_text):
    """预览解析结果（不入库）。
    食材按分类分组返回（与备餐表顺序一致：牛肉→猪肉→鸡肉→蔬菜→小料），
    餐具单独拎出到 preview_tableware。
    """
    parsed = parse_order_text(raw_text)
    matched = match_packages_in_db(parsed["packages"])

    preview_ing = {}
    preview_tool = {}
    with get_db() as conn:
        cur = conn.cursor()
        for pk in matched:
            if not pk["package_id"]:
                continue
            cur.execute("""
                SELECT pi.per_package, pi.portion_count, i.id, i.name, i.unit, i.stock, i.threshold, i.category
                FROM package_ingredients pi
                JOIN ingredients i ON pi.ingredient_id = i.id
                WHERE pi.package_id = ?
            """, (pk["package_id"],))
            for r in cur.fetchall():
                amount = r["per_package"] * pk["quantity"]
                pc = r["portion_count"] or 1
                if r["id"] not in preview_ing:
                    preview_ing[r["id"]] = {
                        "id": r["id"], "name": r["name"], "unit": r["unit"],
                        "stock": r["stock"], "threshold": r["threshold"],
                        "need": 0, "category": r["category"] or "other",
                        "per_package": r["per_package"],
                        "portion_count": pc,
                        "portion_size": round(r["per_package"] / max(1, pc), 2),
                        "total_packages": 0,
                        "total_portions": 0,
                    }
                preview_ing[r["id"]]["need"] += amount
                preview_ing[r["id"]]["total_packages"] += pk["quantity"]
                preview_ing[r["id"]]["portion_count"] = pc
                preview_ing[r["id"]]["portion_size"] = round(r["per_package"] / max(1, pc), 2)
                preview_ing[r["id"]]["total_portions"] = pc * preview_ing[r["id"]]["total_packages"]

            cur.execute("""
                SELECT pt.per_package, t.id, t.name, t.stock, t.threshold
                FROM package_tools pt
                JOIN tools t ON pt.tool_id = t.id
                WHERE pt.package_id = ?
            """, (pk["package_id"],))
            for r in cur.fetchall():
                amount = r["per_package"] * pk["quantity"]
                if r["id"] not in preview_tool:
                    preview_tool[r["id"]] = {
                        "name": r["name"], "stock": r["stock"],
                        "threshold": r["threshold"], "need": 0,
                        "per_package": r["per_package"],
                    }
                preview_tool[r["id"]]["need"] += amount

        # 备注额外工具
        for tname, qty in parsed["extra_tools"].items():
            cur.execute("SELECT id, stock, threshold FROM tools WHERE name = ?", (tname,))
            r = cur.fetchone()
            if r:
                if r["id"] not in preview_tool:
                    preview_tool[r["id"]] = {
                        "name": tname, "stock": r["stock"],
                        "threshold": r["threshold"], "need": 0,
                        "per_package": qty,
                    }
                preview_tool[r["id"]]["need"] += qty

    # 细分 meat → beef/pork/chicken，合并小料类，分离餐具
    for v in preview_ing.values():
        v["category"] = _subcategorize_meat(v["name"], v["category"])
        if v["category"] in SAUCE_LIKE_CATS:
            v["category"] = "sauce"
        v["need"] = round(v["need"], 2)
        v["shortage"] = round(max(0, v["need"] - v["stock"]), 2)
        v["warning"] = v["stock"] <= v["threshold"]

    tool_list = []
    for v in preview_tool.values():
        v["shortage"] = max(0, v["need"] - v["stock"])
        v["warning"] = v["stock"] <= v["threshold"]
        tool_list.append(v)

    # 食材按分类分组（主表顺序），餐具单独拎出
    ing_by_cat = {}
    tableware_list = []
    for cat in CATEGORY_ORDER:
        items = [v for v in preview_ing.values() if v["category"] == cat]
        if items:
            ing_by_cat[cat] = sorted(items, key=lambda x: -x["need"])
    tableware_list = [v for v in preview_ing.values() if v["category"] == TABLEWARE_CATEGORY]
    tableware_list = sorted(tableware_list, key=lambda x: -x["need"])

    parsed["matched_packages"] = matched
    parsed["preview_ingredients"] = ing_by_cat  # dict: {category: [items]}
    parsed["preview_tableware"] = tableware_list
    parsed["preview_tools"] = sorted(tool_list, key=lambda x: -x["need"])

    # 备注里的额外加菜（单点食材）
    from parser import match_extra_ingredients_to_db
    extra_ings_matched = match_extra_ingredients_to_db(parsed.get("extra_ingredients", []))
    # 把加菜合并到对应的分类里
    for ei in extra_ings_matched:
        if not ei.get("matched_id"):
            continue
        cat = _subcategorize_meat(ei.get("matched_name", ""), ei.get("category", "other"))
        if cat in SAUCE_LIKE_CATS:
            cat = "sauce"
        if cat == TABLEWARE_CATEGORY:
            # 餐具类加到 tableware_list
            tableware_list.append({
                "id": ei["matched_id"], "name": ei["matched_name"], "unit": ei["unit"],
                "stock": ei["stock"], "threshold": ei["threshold"],
                "need": ei["total"], "shortage": round(max(0, ei["total"] - ei["stock"]), 2),
                "warning": ei["stock"] <= ei["threshold"],
                "per_package": ei["per_package"],
                "total_packages": ei["qty"],
                "category": cat,
            })
            tableware_list.sort(key=lambda x: -x["need"])
        else:
            ing_by_cat.setdefault(cat, [])
            ing_by_cat[cat].append({
                "id": ei["matched_id"], "name": ei["matched_name"], "unit": ei["unit"],
                "stock": ei["stock"], "threshold": ei["threshold"],
                "need": ei["total"], "shortage": round(max(0, ei["total"] - ei["stock"]), 2),
                "warning": ei["stock"] <= ei["threshold"],
                "per_package": ei["per_package"],
                "total_packages": ei["qty"],
                "category": cat,
            })
            ing_by_cat[cat].sort(key=lambda x: -x["need"])
    parsed["preview_extra_ingredients"] = extra_ings_matched
    return parsed


# 食材分类展示顺序与中文名（用户要求：牛肉、猪肉、鸡肉、蔬菜、小料）
# 注：tableware 不在主表展示，单独拎出到餐具核对区
CATEGORY_ORDER = ["beef", "pork", "chicken", "vegetable", "sauce", "other"]
CATEGORY_LABEL = {
    "beef": "🥩 牛肉",
    "pork": "🥓 猪肉",
    "chicken": "🍗 鸡肉",
    "vegetable": "🥬 蔬菜",
    "sauce": "🧂 小料",
    "other": "📦 其他",
}

# 餐具单独拎出
TABLEWARE_CATEGORY = "tableware"

# 小料类：合并 staple主食 / side小菜 / sauce蘸料 / drink饮料水果
SAUCE_LIKE_CATS = {"staple", "side", "sauce", "drink"}


def _subcategorize_meat(name, current_cat):
    """把 meat 类按食材名细分成 beef / pork / chicken。
    韩式风味肠归 pork（市售多为猪肉肠）。
    """
    if current_cat != "meat":
        return current_cat
    n = name or ""
    if "牛" in n:
        return "beef"
    if "鸡" in n or "掌中宝" in n or "脚筋" in n or "郡肝" in n:
        return "chicken"
    if "猪" in n or "五花肉" in n or "松板肉" in n or "梅花肉" in n or "小肠" in n or "肠" in n:
        return "pork"
    return "other"


def calc_merged_prep(order_ids):
    """合并多订单的备餐需求。
    返回：
      orders: [{id, booking_time, meal_time, address, contact_name, packages, urgency}]
      ingredients: {category: [{id, name, unit, total, shortage, stock, ...}]}  按分类
      tools: [{id, name, total, shortage, stock, ...}]
      tableware: [{id, name, unit, total, ...}]  餐具单独拎出给打包核对
      checked_map: {"ing_<id>": bool, "tool_<id>": bool}  合并勾选状态（全部子项勾了才算勾）
    """
    if not order_ids:
        order_ids = []

    # 拉取订单基础信息
    orders = []
    with get_db() as conn:
        cur = conn.cursor()
        for oid in order_ids:
            cur.execute("""
                SELECT id, booking_date, booking_time, meal_time, address, contact_name,
                       contact_phone, amount, status, note
                FROM orders WHERE id = ?
            """, (oid,))
            r = cur.fetchone()
            if not r:
                continue
            o = dict(r)
            cur.execute("""
                SELECT op.quantity, p.name
                FROM order_packages op
                LEFT JOIN packages p ON op.package_id = p.id
                WHERE op.order_id = ?
            """, (oid,))
            o["packages"] = [dict(x) for x in cur.fetchall()]
            o["urgency"] = calc_prep_urgency(o)
            orders.append(o)

    # 逐单算需求并合并
    ing_merge = {}  # ing_id -> {name, unit, category, total, stock, threshold, order_ids, per_package_samples, total_packages}
    tool_merge = {}  # tool_id -> {name, total, stock, threshold}
    # 记录每个 (order_id, item_type, item_id) 的明细，用于勾选
    detail_keys = []  # [(order_id, item_type, item_id)]

    for o in orders:
        req = calc_order_requirements(o["id"])
        for ing in req["ingredients"]:
            key = ing["id"]
            if key not in ing_merge:
                ing_merge[key] = {
                    "id": ing["id"], "name": ing["name"], "unit": ing["unit"],
                    "category": "other", "total": 0, "stock": ing["stock"],
                    "threshold": ing["threshold"], "order_ids": [],
                    "per_package_samples": [],  # [(per_package, order_quantity), ...]
                    "portion_count_samples": [],  # [(portion_count, order_quantity), ...]
                    "total_packages": 0,
                }
            ing_merge[key]["total"] += ing["need"]
            ing_merge[key]["order_ids"].append(o["id"])
            ing_merge[key]["per_package_samples"].append((ing.get("per_package"), ing.get("order_qty", 1)))
            ing_merge[key]["portion_count_samples"].append((ing.get("portion_count", 1), ing.get("order_qty", 1)))
            ing_merge[key]["total_packages"] += ing.get("order_qty", 1)
            detail_keys.append((o["id"], "ingredient", ing["id"]))
        for tl in req["tools"]:
            key = tl["id"]
            if key not in tool_merge:
                tool_merge[key] = {
                    "id": tl["id"], "name": tl["name"], "total": 0,
                    "stock": tl["stock"], "threshold": tl["threshold"], "order_ids": [],
                    "per_package_samples": [], "total_packages": 0,
                }
            tool_merge[key]["total"] += tl["need"]
            tool_merge[key]["order_ids"].append(o["id"])
            tool_merge[key]["per_package_samples"].append((tl.get("per_package"), tl.get("order_qty", 1)))
            tool_merge[key]["total_packages"] += tl.get("order_qty", 1)
            detail_keys.append((o["id"], "tool", tl["id"]))

    # 补全食材 category + 细分 meat + 合并小料类
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, category, name FROM ingredients")
        cat_map = {r["id"]: (r["category"], r["name"]) for r in cur.fetchall()}
    for k, v in ing_merge.items():
        raw_cat, name = cat_map.get(k, ("other", v["name"]))
        # 细分 meat → beef/pork/chicken
        sub_cat = _subcategorize_meat(name, raw_cat)
        # 合并小料类
        if sub_cat in SAUCE_LIKE_CATS:
            sub_cat = "sauce"
        v["category"] = sub_cat
        # 取代表单份量（per_package）：取所有样本里的最大值（最常见规格）
        samples = [p for p, _ in v["per_package_samples"] if p]
        v["per_package"] = max(samples) if samples else v["total"]
        # 取代表 portion_count（份数）：取所有样本里的最大值
        pc_samples = [p for p, _ in v.get("portion_count_samples", []) if p]
        v["portion_count"] = max(pc_samples) if pc_samples else 1
        v["portion_size"] = round(v["per_package"] / max(1, v["portion_count"]), 2)
        v["total_portions"] = v["portion_count"] * v["total_packages"]
        v["order_count"] = len(v["order_ids"])
        v["shortage"] = round(max(0, v["total"] - v["stock"]), 2)

    for v in tool_merge.values():
        samples = [p for p, _ in v["per_package_samples"] if p]
        v["per_package"] = max(samples) if samples else v["total"]
        v["order_count"] = len(v["order_ids"])
        v["shortage"] = max(0, v["total"] - v["stock"])

    # 勾选状态：从 prep_checklist 查，某合并项只有当所有相关 order 的该 item 都勾了才算勾
    checked_map = {}
    with get_db() as conn:
        cur = conn.cursor()
        # 收集每个 (order_id, item_type, item_id) 的 checked
        cur.execute("""
            SELECT order_id, item_type, item_id, checked FROM prep_checklist
            WHERE order_id IN (%s)
        """ % ",".join("?" * len(order_ids)) if order_ids else "SELECT 1 WHERE 0",
                    tuple(order_ids) if order_ids else ())
        if order_ids:
            rows = cur.fetchall()
            checked_detail = {(r["order_id"], r["item_type"], r["item_id"]): bool(r["checked"]) for r in rows}
        else:
            checked_detail = {}

    # 按 item_id 聚合：同类型同item的所有detail都checked才算
    ing_checked = {}
    tool_checked = {}
    for oid, itype, iid in detail_keys:
        c = checked_detail.get((oid, itype, iid), False)
        if itype == "ingredient":
            ing_checked.setdefault(iid, []).append(c)
        else:
            tool_checked.setdefault(iid, []).append(c)
    for iid, vals in ing_checked.items():
        checked_map[f"ing_{iid}"] = all(vals) and len(vals) > 0
    for iid, vals in tool_checked.items():
        checked_map[f"tool_{iid}"] = all(vals) and len(vals) > 0

    # 食材按分类分组（主表顺序：牛肉、猪肉、鸡肉、蔬菜、小料、其他）
    ingredients_by_cat = {}
    for cat in CATEGORY_ORDER:
        items = [v for v in ing_merge.values() if v["category"] == cat]
        if items:
            ingredients_by_cat[cat] = sorted(items, key=lambda x: -x["total"])

    # 餐具单独拎出（不在主表显示，给打包核对区）
    tableware = [v for v in ing_merge.values() if v["category"] == TABLEWARE_CATEGORY]
    tableware = sorted(tableware, key=lambda x: -x["total"])

    tools_list = sorted(tool_merge.values(), key=lambda x: -x["total"])

    return {
        "orders": orders,
        "ingredients": ingredients_by_cat,
        "tools": tools_list,
        "tableware": tableware,
        "checked_map": checked_map,
    }
