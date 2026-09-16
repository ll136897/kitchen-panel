"""备餐计算引擎：套餐/单点 → 食材+工具需求；库存缺口计算；可备份数"""
import re
import datetime
from datetime import timezone, timedelta
from models import get_db
from parser import parse_order_text, match_packages_in_db

# 北京时区（UTC+8）——服务器可能是 UTC，所有"今天/现在"判断统一用北京时间
CST = timezone(timedelta(hours=8))


def today_cst():
    """返回北京时间当天的 date"""
    return datetime.datetime.now(CST).date()


def now_cst():
    """返回北京时间的 aware datetime"""
    return datetime.datetime.now(CST)


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
    """把 '12.00' / '12:00' / '12点' / '晚上6点' / '下午3点半' 解析成 (hour, minute)。
    识别中文时段词：凌晨/早上/上午/中午/下午/晚上，自动做 12 小时制转换。
    失败返回 None。
    """
    if not t:
        return None
    s = str(t)
    # 时段词 → 是否 PM（下午/晚上 13~23 点，除 12 点外）
    period = None  # None=未指定 / 'am' / 'pm'
    for kw in ("凌晨", "早上", "早晨", "上午", "清晨"):
        if kw in s:
            period = "am"
            break
    if period is None:
        for kw in ("中午", "正午"):
            if kw in s:
                period = "noon"
                break
    if period is None:
        for kw in ("下午", "傍晚", "晚上", "晚间", "夜里", "夜晚"):
            if kw in s:
                period = "pm"
                break

    # 优先匹配 时:分 / 时.分 / 时点分 / 时分
    m = re.search(r"(\d{1,2})\s*[.:：点时]\s*(\d{1,2})", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        # "12点" / "12点半" / "6点"
        m = re.search(r"(\d{1,2})\s*点(半)?", s)
        if m:
            h = int(m.group(1))
            mi = 30 if m.group(2) == "半" else 0
        else:
            # 纯数字
            m = re.search(r"(\d{1,2})", s)
            if not m:
                return None
            h, mi = int(m.group(1)), 0

    if h < 0 or h > 23 or mi < 0 or mi > 59:
        return None

    # 12 小时制转换
    if period == "pm" and h != 12:
        h += 12
    elif period == "noon":
        h = 12
    elif period == "am" and h == 12:
        h = 0

    return h, mi


def calc_prep_urgency(order):
    """根据订单日期 + 用餐时间计算备餐紧迫性。
    返回 {level: 'normal'|'soon'|'overdue'|'past'|'future', minutes_to_prep: int|None, label: str}
    level:
      - past:   订单日期在今天之前 → "已逾期 X 天"
      - normal: 今天订单，充裕
      - soon:   今天订单，1 小时内该备餐
      - overdue:今天订单，已过应备餐时间
      - future:订单日期在今天之后 → "X 天后备餐"
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

    now = now_cst()
    today_d = now.date()

    booking_date = (order.get("booking_date") or "").strip()
    meal_t = order.get("meal_time") or order.get("booking_time")
    hhmm = parse_time_str(meal_t)

    # 订单日期明确在过去
    if booking_date and booking_date < today_d.isoformat():
        try:
            past_d = datetime.date.fromisoformat(booking_date)
            days = (today_d - past_d).days
            if days > 0:
                label = f"⚠️ 已逾期 {days} 天"
            else:
                label = "⚠️ 已逾期"
            return {"level": "past", "minutes_to_prep": None, "label": label,
                    "days_overdue": days}
        except Exception:
            pass

    # 订单日期明确在未来
    if booking_date and booking_date > today_d.isoformat():
        try:
            fut_d = datetime.date.fromisoformat(booking_date)
            days = (fut_d - today_d).days
            if hhmm:
                label = f"{days} 天后 · {hhmm[0]:02d}:{hhmm[1]:02d} 备餐"
            else:
                label = f"{days} 天后备餐"
            return {"level": "future", "minutes_to_prep": None, "label": label,
                    "days_until": days}
        except Exception:
            pass

    # 今天订单（或无日期）：用时间算紧迫度
    if not hhmm:
        return {"level": "normal", "minutes_to_prep": None, "label": "时间待定"}

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
                WHERE pi.package_id = ? AND pi.cost_only = 0
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
        for tname, info in extra.items():
            qty = info["qty"] if isinstance(info, dict) else info
            mode = info.get("mode", "add") if isinstance(info, dict) else "add"
            cur.execute("SELECT id FROM tools WHERE name = ?", (tname,))
            tr = cur.fetchone()
            if tr:
                tid = tr["id"]
                if tid not in tool_demand:
                    tool_demand[tid] = {"total": 0, "per_package": qty, "order_qty": 0}
                if mode == "set":
                    tool_demand[tid]["total"] = qty       # 覆盖：总数=qty
                else:
                    tool_demand[tid]["total"] += qty      # 增量：套餐基础上 +qty
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
            mode = ei.get("mode", "add")
            if iid not in ing_demand:
                ing_demand[iid] = {"total": 0, "per_package": ei["per_package"], "order_qty": 0}
            if mode == "set":
                ing_demand[iid]["total"] = amount        # 覆盖
            else:
                ing_demand[iid]["total"] += amount       # 增量
            ing_demand[iid]["per_package"] = ei["per_package"]
            ing_demand[iid]["order_qty"] += ei["qty"]

    # 拉取详情
    ingredients_result = []
    with get_db() as conn:
        cur = conn.cursor()
        for ing_id, info in ing_demand.items():
            cur.execute("SELECT name, unit, stock, threshold, category FROM ingredients WHERE id = ?", (ing_id,))
            r = cur.fetchone()
            if r:
                stock = r["stock"] or 0
                need = info["total"]
                # 细分 meat 类 + 合并小料
                cat = r["category"] or "other"
                cat = _subcategorize_meat(r["name"], cat)
                if cat in SAUCE_LIKE_CATS:
                    cat = "sauce"
                ingredients_result.append({
                    "id": ing_id, "name": r["name"], "unit": r["unit"],
                    "need": round(need, 2), "stock": stock,
                    "shortage": round(max(0, need - stock), 2),
                    "threshold": r["threshold"] or 0,
                    "category": cat,
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

    return {"ingredients": ingredients_result, "tools": sort_tools(tools_result, "need")}


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
        "tools": sort_tools(tool_panel, "need"),
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
                WHERE pi.package_id = ? AND pi.cost_only = 0
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
        for tname, info in parsed["extra_tools"].items():
            qty = info["qty"] if isinstance(info, dict) else info
            mode = info.get("mode", "add") if isinstance(info, dict) else "add"
            cur.execute("SELECT id, stock, threshold FROM tools WHERE name = ?", (tname,))
            r = cur.fetchone()
            if r:
                if r["id"] not in preview_tool:
                    preview_tool[r["id"]] = {
                        "name": tname, "stock": r["stock"],
                        "threshold": r["threshold"], "need": 0,
                        "per_package": qty,
                    }
                if mode == "set":
                    preview_tool[r["id"]]["need"] = qty      # 覆盖
                else:
                    preview_tool[r["id"]]["need"] += qty     # 增量

        # 备注额外加菜：合并到 preview_ing（按食材 id），set 覆盖 / add 增量
        from parser import match_extra_ingredients_to_db
        extra_ings_matched = match_extra_ingredients_to_db(parsed.get("extra_ingredients", []))
        for ei in extra_ings_matched:
            iid = ei.get("matched_id")
            if not iid:
                continue
            cat = _subcategorize_meat(ei.get("matched_name", ""), ei.get("category", "other"))
            if cat in SAUCE_LIKE_CATS:
                cat = "sauce"
            mode = ei.get("mode", "add")
            amount = ei["total"]
            if iid in preview_ing:
                if mode == "set":
                    preview_ing[iid]["need"] = amount
                else:
                    preview_ing[iid]["need"] += amount
                preview_ing[iid]["per_package"] = ei["per_package"]
                preview_ing[iid]["total_packages"] = preview_ing[iid].get("total_packages", 0) + ei["qty"]
            else:
                preview_ing[iid] = {
                    "id": iid, "name": ei["matched_name"], "unit": ei["unit"],
                    "stock": ei["stock"], "threshold": ei["threshold"],
                    "need": amount, "category": cat,
                    "per_package": ei["per_package"],
                    "portion_count": 1,
                    "portion_size": ei["per_package"],
                    "total_packages": ei["qty"],
                    "total_portions": ei["qty"],
                }

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

    # 食材按分类分组（主表顺序），包装/餐具单独拎出
    ing_by_cat = {}
    packaging_list = []
    utensil_list = []
    for cat in CATEGORY_ORDER:
        items = [v for v in preview_ing.values() if v["category"] == cat]
        if items:
            ing_by_cat[cat] = sorted(items, key=lambda x: (_get_fixed_sort_index(x["name"], cat), -x["need"]))
    packaging_list = [v for v in preview_ing.values() if v["category"] == PACKAGING_CATEGORY]
    packaging_list = sorted(packaging_list, key=lambda x: -x["need"])
    utensil_list = [v for v in preview_ing.values() if v["category"] == UTENSIL_CATEGORY]
    utensil_list = sorted(utensil_list, key=lambda x: -x["need"])

    parsed["matched_packages"] = matched
    parsed["preview_ingredients"] = ing_by_cat  # dict: {category: [items]}
    parsed["preview_packaging"] = packaging_list
    parsed["preview_tableware"] = utensil_list
    parsed["preview_tools"] = sort_tools(tool_list, "need")

    # 备注额外加菜已在上方合并进 preview_ing（set 覆盖 / add 增量）
    parsed["preview_extra_ingredients"] = extra_ings_matched
    return parsed


# 食材分类展示顺序与中文名（用户要求：牛肉、猪肉、鸡肉、蔬菜、小料）
# 注：packaging/utensil 不在主表展示，单独拎出到包装核对区和餐具核对区
CATEGORY_ORDER = ["beef", "pork", "chicken", "vegetable", "side", "sauce", "drink", "packaging", "utensil", "staple", "other", "tool"]
CATEGORY_LABEL = {
    "beef": "🥩 牛肉",
    "pork": "🥓 猪肉",
    "chicken": "🍗 鸡肉",
    "vegetable": "🥬 素菜",
    "side": "🥗 小菜",
    "sauce": "🧂 小料",
    "drink": "🎁 赠品",
    "packaging": "📦 食材包装",
    "utensil": "🍱 客户餐具",
    "staple": "🍚 主食",
    "other": "📦 其他",
    "tool": "🔧 工具",
}

# 食材包装（独立分类，与菜单页一致）
PACKAGING_CATEGORY = "packaging"
# 客户餐具/工具（独立分类，与菜单页一致）
UTENSIL_CATEGORY = "utensil"
# 工具分类
TOOL_CATEGORY = "tool"

# 小料类：不再合并，side/sauce/staple/drink 各自独立分类显示
# （与菜单页 CAT_ORDER 一致：素菜→小菜→小料→赠品→食材包装→客户餐具→主食→其他→工具）
SAUCE_LIKE_CATS = set()


def _subcategorize_meat(name, current_cat):
    """把 meat 类按食材名细分成 beef / pork / chicken。
    韩式风味肠归 pork（市售多为猪肉肠）。
    爆汁烤小肠归 beef（实际是牛肠）。
    奶香小馒头归 vegetable（用户指定素菜类）。
    """
    n = name or ""
    # 馒头归素菜（用户指定）
    if "馒头" in n:
        return "vegetable"
    if current_cat != "meat":
        return current_cat
    # 小肠优先归牛肉（爆汁烤小肠 = 牛肠）
    if "小肠" in n:
        return "beef"
    if "牛" in n:
        return "beef"
    if "鸡" in n or "掌中宝" in n or "脚筋" in n or "郡肝" in n:
        return "chicken"
    if "猪" in n or "五花肉" in n or "松板肉" in n or "梅花肉" in n or "肠" in n:
        return "pork"
    return "other"


# 食材固定排序顺序（用户指定，所有菜单/备餐/订单详情统一）
# 每个条目：(分类, 关键词)，按列表顺序赋索引 0,1,2,...
# 不在列表里的食材排在最后（索引 999），按用量降序
INGREDIENT_FIXED_ORDER = [
    # 牛肉：肥牛、拌牛肉、牛肋条、牛骰子、小肠
    ("beef", "肥牛"),
    ("beef", "拌牛肉"),
    ("beef", "牛肋条"),
    ("beef", "牛骰子"),
    ("beef", "小肠"),
    # 猪肉：五花肉、沙葱小香猪、松板肉、风味肠、梅花肉
    ("pork", "五花肉"),
    ("pork", "小香猪"),
    ("pork", "松板肉"),
    ("pork", "风味肠"),
    ("pork", "梅花肉"),
    # 鸡肉：鸡尖、郡肝、鸡腿肉、鸡翅根、掌中宝、鸡脚筋
    ("chicken", "鸡尖"),
    ("chicken", "郡肝"),
    ("chicken", "鸡腿肉"),
    ("chicken", "鸡翅根"),
    ("chicken", "掌中宝"),
    ("chicken", "鸡脚筋"),
    # 素菜：生菜、豆腐、西葫芦、土豆、奶香馒头、韭菜、杏鲍菇、洋葱
    ("vegetable", "生菜"),
    ("vegetable", "豆腐"),
    ("vegetable", "西葫芦"),
    ("vegetable", "土豆"),
    ("vegetable", "馒头"),
    ("vegetable", "韭菜"),
    ("vegetable", "杏鲍菇"),
    ("vegetable", "洋葱"),
    # 小菜：海带丝、辣椒段、辣白菜、蒜片
    ("side", "海带丝"),
    ("side", "辣椒段"),
    ("side", "辣白菜"),
    ("side", "蒜片"),
    # 蘸料：川香料、五香料、酸辣汁
    ("sauce", "川香"),
    ("sauce", "五香"),
    ("sauce", "酸辣"),
]


def _get_fixed_sort_index(name, category):
    """返回固定排序索引（0-based）。不在列表里的返回 999（排最后）。"""
    n = name or ""
    for idx, (cat, kw) in enumerate(INGREDIENT_FIXED_ORDER):
        if cat == category and kw in n:
            return idx
    return 999


# 工具固定排序顺序（用户指定，所有页面统一）：
# 天幕、桌子(蛋卷桌)、椅子、卡式炉、烤盘、气罐(燃气罐)、夹子、剪刀
TOOL_FIXED_ORDER = [
    "天幕", "蛋卷桌", "椅子", "卡式炉", "烤盘", "燃气罐", "夹子", "剪刀",
]


def _tool_sort_index(name):
    """返回工具固定排序索引。不在列表里的返回 999（排最后）。"""
    n = name or ""
    for idx, kw in enumerate(TOOL_FIXED_ORDER):
        if kw in n or n in kw:
            return idx
    return 999


def sort_tools(items, key="need", name_field="name"):
    """按固定顺序排序工具列表，相同顺序按用量降序。"""
    return sorted(items, key=lambda x: (_tool_sort_index(x.get(name_field, "")), -x.get(key, 0)))


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
    # 小肠在牛肉类里排最末
    ingredients_by_cat = {}
    for cat in CATEGORY_ORDER:
        items = [v for v in ing_merge.values() if v["category"] == cat]
        if items:
            ingredients_by_cat[cat] = sorted(items, key=lambda x: (_get_fixed_sort_index(x["name"], cat), -x["total"]))

    # 食材包装单独拎出（不在主表显示，给包装核对区）
    packaging = [v for v in ing_merge.values() if v["category"] == PACKAGING_CATEGORY]
    packaging = sorted(packaging, key=lambda x: -x["total"])
    # 客户餐具/工具单独拎出（给餐具分拣打包区）
    utensil = [v for v in ing_merge.values() if v["category"] == UTENSIL_CATEGORY]
    utensil = sorted(utensil, key=lambda x: -x["total"])

    tools_list = sort_tools(list(tool_merge.values()), "total")

    return {
        "orders": orders,
        "ingredients": ingredients_by_cat,
        "tools": tools_list,
        "packaging": packaging,
        "tableware": utensil,
        "checked_map": checked_map,
    }
