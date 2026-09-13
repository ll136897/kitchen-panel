"""备餐计算引擎：套餐/单点 → 食材+工具需求；库存缺口计算；可备份数"""
from models import get_db
from parser import parse_order_text, match_packages_in_db


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
    ing_demand = {}  # ingredient_id -> total

    with get_db() as conn:
        cur = conn.cursor()
        for pk in pkgs:
            cur.execute("""
                SELECT pi.per_package, i.id, i.name, i.unit
                FROM package_ingredients pi
                JOIN ingredients i ON pi.ingredient_id = i.id
                WHERE pi.package_id = ?
            """, (pk["package_id"],))
            for r in cur.fetchall():
                amount = r["per_package"] * pk["quantity"]
                ing_demand[r["id"]] = ing_demand.get(r["id"], 0) + amount

        for d in dishes:
            cur.execute("""
                SELECT di.amount, i.id
                FROM dish_ingredients di
                JOIN ingredients i ON di.ingredient_id = i.id
                WHERE di.dish_id = ?
            """, (d["dish_id"],))
            for r in cur.fetchall():
                amount = r["amount"] * d["quantity"]
                ing_demand[r["id"]] = ing_demand.get(r["id"], 0) + amount

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
                tool_demand[r["id"]] = tool_demand.get(r["id"], 0) + amount

        # 备注解析额外工具
        parsed = parse_order_text(raw_text)
        extra = parsed.get("extra_tools", {})
        for tname, qty in extra.items():
            cur.execute("SELECT id FROM tools WHERE name = ?", (tname,))
            tr = cur.fetchone()
            if tr:
                tool_demand[tr["id"]] = tool_demand.get(tr["id"], 0) + qty

    # 拉取详情
    ingredients_result = []
    with get_db() as conn:
        cur = conn.cursor()
        for ing_id, need in ing_demand.items():
            cur.execute("SELECT name, unit, stock, threshold FROM ingredients WHERE id = ?", (ing_id,))
            r = cur.fetchone()
            if r:
                stock = r["stock"] or 0
                ingredients_result.append({
                    "id": ing_id, "name": r["name"], "unit": r["unit"],
                    "need": round(need, 2), "stock": stock,
                    "shortage": round(max(0, need - stock), 2),
                    "threshold": r["threshold"] or 0,
                    "warning": stock <= (r["threshold"] or 0),
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
                })

        tools_result = []
        for t_id, need in tool_demand.items():
            cur.execute("SELECT name, stock, threshold FROM tools WHERE id = ?", (t_id,))
            r = cur.fetchone()
            if r:
                stock = r["stock"] or 0
                tools_result.append({
                    "id": t_id, "name": r["name"],
                    "need": need, "stock": stock,
                    "shortage": max(0, need - stock),
                    "threshold": r["threshold"] or 0,
                    "warning": stock <= (r["threshold"] or 0),
                })

        cur.execute("SELECT id, name, stock, threshold FROM tools")
        all_tools = [dict(r) for r in cur.fetchall()]
        existing_tids = {x["id"] for x in tools_result}
        for t in all_tools:
            if t["id"] not in existing_tids and t["stock"] <= t["threshold"]:
                tools_result.append({
                    "id": t["id"], "name": t["name"],
                    "need": 0, "stock": t["stock"], "shortage": 0,
                    "threshold": t["threshold"], "warning": True,
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
        cur.execute("SELECT * FROM tools")
        for r in cur.fetchall():
            need = total_tool.get(r["id"], 0)
            stock = r["stock"] or 0
            shortage = max(0, need - stock)
            pct = round(min(100, (need / (stock + need) * 100))) if (stock + need) > 0 else 0
            tool_panel.append({
                "id": r["id"], "name": r["name"],
                "stock": stock, "need": need,
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
            SELECT id, booking_date, booking_time, address, contact_name,
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
    """预览解析结果（不入库）"""
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
                SELECT pi.per_package, i.id, i.name, i.unit, i.stock, i.threshold
                FROM package_ingredients pi
                JOIN ingredients i ON pi.ingredient_id = i.id
                WHERE pi.package_id = ?
            """, (pk["package_id"],))
            for r in cur.fetchall():
                amount = r["per_package"] * pk["quantity"]
                if r["id"] not in preview_ing:
                    preview_ing[r["id"]] = {
                        "name": r["name"], "unit": r["unit"],
                        "stock": r["stock"], "threshold": r["threshold"],
                        "need": 0,
                    }
                preview_ing[r["id"]]["need"] += amount

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
                    }
                preview_tool[r["id"]]["need"] += qty

    ing_list = []
    for v in preview_ing.values():
        v["need"] = round(v["need"], 2)
        v["shortage"] = round(max(0, v["need"] - v["stock"]), 2)
        v["warning"] = v["stock"] <= v["threshold"]
        ing_list.append(v)
    tool_list = []
    for v in preview_tool.values():
        v["shortage"] = max(0, v["need"] - v["stock"])
        v["warning"] = v["stock"] <= v["threshold"]
        tool_list.append(v)

    parsed["matched_packages"] = matched
    parsed["preview_ingredients"] = sorted(ing_list, key=lambda x: -x["need"])
    parsed["preview_tools"] = sorted(tool_list, key=lambda x: -x["need"])
    return parsed
