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


def tool_qty_by_people(name, people):
    """装备按人数配的【唯一口径 / single source of truth】。

    所有页面/功能（订单装备表、占用统计、搭建装备表、餐标配餐器 catering_suggest）
    算天幕/蛋卷桌/椅子/炉/盘/罐/夹/剪 时【必须】调本函数，禁止在某个页面或函数里再写一套 if/elif。
    规则由用户 2026-10-24 确认：
      天幕  : ≤10人=1顶，>10人(如13人)=2顶
      蛋卷桌: 3/4人=1，6/8人=2，10人=3  → 即 max(1, ceil(people/4))
      椅子  : =人数（仅搭建套餐计入，见 calc_order_tool_needs）
      卡式炉/烤盘: ceil(people/4)；燃气罐: ceil(people/4)+1（多备1）；夹子/剪刀: ceil(people/8)
    ⚠️ 调用方注意（2026-10-25 修）：天幕/蛋卷桌是**阶梯/取整**规则，一单有多个搭建套餐时
    必须传「整单搭建部分的总人数」调**一次**，不能按套餐个数累加——
    「4人餐×3」=12人按规则是 2顶天幕/3张桌，按包累加会错成 3顶/4张。
    calc_order_tool_needs 已按此实现（setup_people 合计后调一次）。
    改任何装备数量规则 → 只改这里，并同步更新仓库根 LOGIC.md 的对应条目。
    """
    p = people or 0
    if name == "天幕":
        return 1 if p <= 10 else 2
    if name == "蛋卷桌":
        return max(1, (p + 3) // 4)
    if name == "椅子":
        return p
    if name in ("卡式炉", "烤盘"):
        return (p + 3) // 4
    if name == "燃气罐":
        return (p + 3) // 4 + 1
    if name in ("夹子", "剪刀"):
        return (p + 7) // 8
    return 1


def calc_order_tool_needs(order_id, only_setup=False):
    """只算某单需要的装备（套餐配的装备 + 备注追加的），**不依赖可用量**。
    单独抽出来是为了让"占用统计"能调用它而不会与 get_available_tool_stock() 递归。
    返回 {tool_id: {"total": n, "per_package": x, "order_qty": k}}

    only_setup=True 时只累加「需要搭建」的套餐装备（order_packages.setup_flag=1），
    用于搭建装备表——混单（10人餐搭建+3人餐不搭建）只算搭建部分的天幕/桌椅炉，
    避免把半包套餐的天幕也算进去多带。默认 False=全部套餐（备餐/占用统计用）。
    """
    tool_demand = {}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT raw_text, note FROM orders WHERE id = ?", (order_id,))
        row = cur.fetchone()
        if not row:
            return {}
        raw_text = row["raw_text"] or ""
        note_now = (row["note"] or "").strip()
        cur.execute("SELECT package_id, quantity, setup_flag, people FROM order_packages WHERE order_id = ?", (order_id,))
        pkgs = [dict(r) for r in cur.fetchall()]
        # 搭建装备表：只算搭建套餐
        if only_setup:
            pkgs = [p for p in pkgs if p.get("setup_flag") == 1]

        # 天幕/蛋卷桌/椅子：按「人数规则」在搭建套餐上计算（混单只计搭建单，不搭建套餐不带这三项）。
        # 说明：这些规则此前只在单套餐成本页(calc_*_package)生效，订单级装备表/占用统计漏用了，这里补齐。
        # ⚠️ 2026-10-25 修（用户报「8单怎么出9顶天幕」）：天幕(≤10人=1顶/>10人=2顶)和
        # 蛋卷桌(ceil(人/4))是**阶梯/取整**规则，以前按套餐个数累加（每个搭建套餐各算一次）——
        # 「4人餐×3」=12人会被算成 3顶天幕/4张桌，而规则本意是按**整单搭建人数**算一次（12人→2顶/3张）。
        # 椅子=人数是线性规则，两种算法等价；为口径统一也一并按整单人数算。
        # 混单（10人餐搭建+3人餐不搭建）不受影响：只累加搭建部分人数。
        cur.execute("SELECT id, name FROM tools")
        _tid = {r["name"]: r["id"] for r in cur.fetchall()}
        TM, TBL, CHAIR = _tid.get("天幕"), _tid.get("蛋卷桌"), _tid.get("椅子")
        setup_pkgs = [p for p in pkgs if p.get("setup_flag") == 1]
        # 整单「搭建部分」的总人数 = Σ(套餐人数×份数)；阶梯/取整类装备按这个数算一次
        setup_people = sum((pk.get("people") or 0) * (pk.get("quantity") or 1) for pk in setup_pkgs)
        setup_qty = sum(pk.get("quantity") or 1 for pk in setup_pkgs)
        if setup_people > 0:
            for _tname, _tid2 in (("天幕", TM), ("蛋卷桌", TBL), ("椅子", CHAIR)):
                if _tid2 is None:
                    continue
                _n = tool_qty_by_people(_tname, setup_people)
                tool_demand[_tid2] = {"total": _n, "per_package": _n, "order_qty": setup_qty}

        # 其余工具（卡式炉/烤盘/燃气罐/夹子/剪刀等）：按套餐 package_tools 现有配置累加
        for pk in pkgs:
            cur.execute("""
                SELECT pt.per_package, t.id, t.name
                FROM package_tools pt
                JOIN tools t ON pt.tool_id = t.id
                WHERE pt.package_id = ?
            """, (pk["package_id"],))
            for r in cur.fetchall():
                if r["id"] in (TM, TBL, CHAIR):
                    continue  # 这三项已按人数规则算
                amount = r["per_package"] * pk["quantity"]
                if r["id"] not in tool_demand:
                    tool_demand[r["id"]] = {"total": 0, "per_package": r["per_package"], "order_qty": 0}
                tool_demand[r["id"]]["total"] += amount
                tool_demand[r["id"]]["per_package"] = r["per_package"]
                tool_demand[r["id"]]["order_qty"] += pk["quantity"]

        # 备注解析出的追加装备（"多拿2个天幕"等）
        # ⚠️ 2026-10-25 修：用户在订单页改过备注(note)后**以 note 为准**——raw_text 是
        # "原始录入信息"、永不更新，直接用它解析会把用户删掉的装备又算回来。
        # （329 实测：套餐已改成10人餐、备注改成"12份餐具;12把椅子"，天幕却仍按原文的
        # "2天幕"覆盖成 2 顶。录入时 note 由原文备注行同步而来，没改时两者解析结果一致）
        parsed = parse_order_text(raw_text)
        extra = parsed.get("extra_tools", {})
        if note_now and note_now != "无":
            from parser import _parse_extra_tools
            extra = _parse_extra_tools(note_now)
        for tname, info in extra.items():
            qty = info["qty"] if isinstance(info, dict) else info
            mode = info.get("mode", "set") if isinstance(info, dict) else "set"   # 缺省=总数（用户确认的大多数情况）
            cur.execute("SELECT id FROM tools WHERE name = ?", (tname,))
            tr = cur.fetchone()
            if not tr:
                continue
            tid = tr["id"]
            pkg_qty_sum = sum(int(pk.get("quantity") or 1) for pk in pkgs) or 1
            if tid not in tool_demand:
                tool_demand[tid] = {"total": 0, "per_package": qty, "order_qty": pkg_qty_sum}
            if mode == "set":
                tool_demand[tid]["total"] = qty
            else:
                tool_demand[tid]["total"] += qty
            tool_demand[tid]["per_package"] = qty
    return tool_demand


def list_reserving_orders():
    """当前仍然"占着装备"的订单：未完成、未取消、未删除，且预约日期未过期。
    日期已过 = 活动办完了，自动释放占用（不要求用户去点"归还"或"完成"）。"""
    with get_db() as conn:
        cur = conn.cursor()
        return [dict(r) for r in cur.execute("""
            SELECT id, booking_date, booking_time, address, contact_name, status
            FROM orders
            WHERE deleted_at IS NULL AND status IN ('pending','preparing')
              AND (COALESCE(booking_date,'') = '' OR booking_date >= ?)
            ORDER BY booking_date, booking_time
        """, (today_cst().isoformat(),)).fetchall()]


def get_tool_reserved():
    """各装备被"未完成订单"占用的数量。占用完全由订单推导，无需人工登记。"""
    reserved = {}
    for o in list_reserving_orders():
        for tid, info in calc_order_tool_needs(o["id"]).items():
            reserved[tid] = reserved.get(tid, 0) + (info.get("total") or 0)
    return reserved


def get_available_tool_stock():
    """返回 {tool_id: 可用数量} = 总库存 − 未完成订单占用（可用于接新单的量）"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, stock FROM tools")
        stock = {r["id"]: r["stock"] or 0 for r in cur.fetchall()}
    for tid, rsv in get_tool_reserved().items():
        stock[tid] = max(0, stock.get(tid, 0) - rsv)
    return stock


# 菜名连接字（去连接字后做模糊匹配，处理"薄荷牛肉"↔"薄荷拌牛肉"、"松板肉"↔"原切松板肉"等）
CONNECTION_WORDS = ['原切', '精选', '特选', '奶香', '爆汁', '麻辣', '葱香', '蒜香', '南美', '沙葱',
                   '安格斯', '谷饲', '黑椒', '藤椒', '孜然', '蜜汁', '拌', '炒', '煮', '卤', '烤',
                   '熏', '香', '好', '的', '新']


def match_ingredient_id_by_name(name):
    """按菜名模糊匹配 ingredients 表，返回 id 或 None。
    备注写"鸡尖"→库里"麻辣鸡尖"；备注写"麻辣鸡尖"→库里"鸡尖"都能配上。
    备注写"薄荷牛肉"→库里"薄荷拌牛肉"（去连接字）也能配上。
    """
    if not name:
        return None
    key = str(name).strip()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM ingredients WHERE name = ?", (key,))
        r = cur.fetchone()
        if r:
            return r["id"]
        cur.execute("SELECT id FROM ingredients WHERE name LIKE ?", ('%' + key + '%',))
        r = cur.fetchone()
        if r:
            return r["id"]
        cur.execute("SELECT id, name FROM ingredients")
        for x in cur.fetchall():
            nm = x["name"] or ''
            if key in nm or nm in key:
                return x["id"]
        # 4) 去连接字后再试（薄荷牛肉→薄荷拌牛肉、原切松板肉→松板肉 等）。
        #    关键词本身可能无连接字（"薄荷牛肉"里没有"拌"），只要尚未匹配就跑这步，
        #    把库里菜名也去连接字后再比（"薄荷拌牛肉"→"薄荷牛肉"即可对上）。
        stripped_key = key
        for w in CONNECTION_WORDS:
            stripped_key = stripped_key.replace(w, '')
        stripped_key = stripped_key.strip()
        if stripped_key:
            cur.execute("SELECT id, name FROM ingredients")
            for x in cur.fetchall():
                nm = (x["name"] or '')
                nm_stripped = nm
                for w in CONNECTION_WORDS:
                    nm_stripped = nm_stripped.replace(w, '')
                nm_stripped = nm_stripped.strip()
                if stripped_key in nm_stripped or nm_stripped in stripped_key:
                    return x["id"]
    return None


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


# ============ 财务批量预取缓存（period_overview 单次请求内 prime，消除逐单 N+1）============
_PKG_ING_CACHE = {}      # package_id -> [ {per_package, portion_count, cost_only, id(=ingredient_id)} ]
_ING_BY_ID = {}          # ingredient id -> {id, name, cost, unit, category}
_DISH = {}               # dish id -> {id, name, cost}
_DISH_ING = {}           # dish id -> [ (amount, ingredient_id) ]
_CALC_PRIMED = False

def prime_calc_cache(db):
    """一次性把所有"慢速参考表"装进内存字典，供本请求内所有订单复用；未 prime 时走原查询。"""
    global _PKG_ING_CACHE, _ING_BY_ID, _DISH, _DISH_ING, _CALC_PRIMED
    cur = db.cursor()
    _ING_BY_ID = {r["id"]: dict(r) for r in cur.execute("SELECT id, name, cost, unit, category FROM ingredients")}
    _PKG_ING_CACHE = {}
    for r in cur.execute("SELECT package_id, per_package, portion_count, cost_only, ingredient_id FROM package_ingredients"):
        d = dict(r); d["id"] = d["ingredient_id"]
        _PKG_ING_CACHE.setdefault(r["package_id"], []).append(d)
    _DISH = {r["id"]: dict(r) for r in cur.execute("SELECT id, name, cost FROM dishes")}
    _DISH_ING = {}
    for r in cur.execute("SELECT dish_id, amount, ingredient_id FROM dish_ingredients"):
        _DISH_ING.setdefault(r["dish_id"], []).append((r["amount"], r["ingredient_id"]))
    _CALC_PRIMED = True

def clear_calc_cache():
    global _PKG_ING_CACHE, _ING_BY_ID, _DISH, _DISH_ING, _CALC_PRIMED
    _PKG_ING_CACHE = {}; _ING_BY_ID = {}; _DISH = {}; _DISH_ING = {}; _CALC_PRIMED = False


def calc_order_ingredient_net(order_id, _order=None, _pkgs=None):
    """订单『套餐食材』净需求（备餐页与成本页共用的单一真相源）。

    包含：套餐固定配量×份数 + 备注加菜(set覆盖/add增量) + 换菜(份数换算)。
    不含：单点菜品(order_dishes)——备餐端展开成食材量、成本端按菜品报价，口径不同各自处理。
    _order / _pkgs 为批量预取时传入，避免逐单查库（未传则按原逻辑查）。

    返回 {ing_id: {total, per_package, order_qty, portion_count, total_portions,
                   cost_only, swapped_in, swapped_out}}。
    total 为换菜/加菜生效后的净用量（克/个）。
    """
    if _pkgs is None:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT op.package_id, op.quantity FROM order_packages op WHERE op.order_id=?", (order_id,))
            _pkgs = [dict(r) for r in cur.fetchall()]
    pkgs = _pkgs
    if _order is None:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT raw_text, note, contact_name, contact_phone, amount, address FROM orders WHERE id=?", (order_id,))
            row = cur.fetchone()
            _order = dict(row) if row else {}
    raw_text = _order.get("raw_text") or ""
    note_now = (_order.get("note") or "").strip()
    _cname = _order.get("contact_name") or ""
    _cphone = _order.get("contact_phone") or ""
    _amt = _order.get("amount")
    _addr = _order.get("address") or ""

    ing_demand = {}
    for pk in pkgs:
        _rows = _PKG_ING_CACHE.get(pk["package_id"]) if _CALC_PRIMED else None
        if _rows is None:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT pi.per_package, pi.portion_count, pi.cost_only, i.id
                    FROM package_ingredients pi
                    JOIN ingredients i ON pi.ingredient_id = i.id
                    WHERE pi.package_id = ?
                """, (pk["package_id"],))
                _rows = cur.fetchall()
        for r in _rows:
            amount = r["per_package"] * pk["quantity"]
            pc = r["portion_count"] or 1
            iid = r["id"]
            if iid not in ing_demand:
                ing_demand[iid] = {"total": 0, "per_package": r["per_package"],
                                   "order_qty": 0, "portion_count": pc,
                                   "total_portions": 0, "cost_only": bool(r["cost_only"])}
            ing_demand[iid]["total"] += amount
            # per_package/portion_count 取用量最大的套餐作为代表
            if amount > ing_demand[iid]["per_package"] * ing_demand[iid]["order_qty"]:
                ing_demand[iid]["per_package"] = r["per_package"]
                ing_demand[iid]["portion_count"] = pc
            ing_demand[iid]["order_qty"] += pk["quantity"]
            ing_demand[iid]["total_portions"] += pc * pk["quantity"]
            if r["cost_only"]:
                ing_demand[iid]["cost_only"] = True

    # 备注加菜 + 换菜（与下方 calc_order_requirements / preview 同一套算法，避免分叉）
    from parser import parse_order_text, match_extra_ingredients_to_db, own_raw_block
    # 批量录入时整段原文混着好几单，先切出「本单那一段」再解析，
    # 否则会把别人的加菜/换菜算到这单头上（串单坑）。
    own = own_raw_block(raw_text, _cname, _cphone, _amt, _addr)
    parsed = parse_order_text(own or raw_text)
    # ⚠️ 2026-10-25 修：用户在订单页改过备注(note)后，加菜/换菜**以 note 为准**——
    # raw_text 是"原始录入信息"永不更新，直接用它解析会把用户删掉的加菜又算回来。
    # （329 实测：备注改成"12份餐具;12把椅子"后，原文备注行的加菜仍在生效）
    if note_now and note_now != "无":
        from parser import _parse_extra_ingredients, _parse_dish_swaps
        parsed["extra_ingredients"] = _parse_extra_ingredients(note_now)
        parsed["dish_swaps"] = _parse_dish_swaps(note_now)
    for ei in match_extra_ingredients_to_db(parsed.get("extra_ingredients", [])):
        iid = ei.get("matched_id")
        if not iid:
            continue
        amount = ei["total"]
        mode = ei.get("mode", "add")
        if iid not in ing_demand:
            ing_demand[iid] = {"total": 0, "per_package": ei.get("per_package"),
                               "order_qty": 0, "portion_count": 1,
                               "total_portions": 0, "cost_only": False}
        if mode == "set":
            ing_demand[iid]["total"] = amount
        else:
            ing_demand[iid]["total"] += amount
        ing_demand[iid]["per_package"] = ei.get("per_package")
        ing_demand[iid]["order_qty"] += ei.get("qty", 1)

    for sw in (parsed.get("dish_swaps") or []):
        to_id = match_ingredient_id_by_name(sw.get("to"))
        if not to_id:
            continue
        froms = sw.get("froms") or [[sw.get("from"), None]]
        explicit = bool(sw.get("explicit", False))
        multi = len(froms) > 1
        to_qty = sw.get("to_qty", 1) or 1
        use_old = (not explicit) and (not multi)
        removed_total = 0.0
        for fitem in froms:
            fname = fitem[0] if isinstance(fitem, (list, tuple)) else fitem
            fqty = fitem[1] if isinstance(fitem, (list, tuple)) else None
            src_id = match_ingredient_id_by_name(fname)
            if not src_id or src_id not in ing_demand:
                continue
            info = ing_demand[src_id]
            if info.get("total", 0) <= 0:
                continue
            if use_old:
                removed_total += info["total"]
                info["total"] = 0
                info["swapped_out"] = True
            else:
                tp = info.get("total_portions", info.get("portion_count", 0)) or 0
                portion = (info["total"] / tp) if tp > 0 else info["total"]
                rm = portion * (fqty if fqty else 1e9)
                if rm >= info["total"]:
                    removed_total += info["total"]
                    info["total"] = 0
                    info["swapped_out"] = True
                else:
                    removed_total += rm
                    info["total"] -= rm
        if use_old:
            add = removed_total
        else:
            to_info = ing_demand.get(to_id)
            if to_info:
                pc = to_info.get("portion_count", 0) or 1
                dst_portion = (to_info.get("per_package", 0) or 0) / pc if pc > 0 else 100.0
            else:
                dst_portion = 100.0
            add = dst_portion * to_qty
        if to_id not in ing_demand:
            ing_demand[to_id] = {"total": 0, "per_package": 100.0, "order_qty": 0,
                                 "portion_count": 1, "total_portions": 0, "cost_only": False}
        ing_demand[to_id]["total"] += add
        ing_demand[to_id]["swapped_in"] = True

    return ing_demand


def calc_order_requirements(order_id):
    """
    根据订单ID计算该订单的备餐需求（食材+工具）。
    套餐食材净需求走 calc_order_ingredient_net（与成本页共用，换菜/加菜只此一处实现）。
    """
    # 套餐食材净需求（含备注加菜 + 换菜），与成本页同一函数，保证口径一致
    ing_demand = calc_order_ingredient_net(order_id)

    # 单点菜品：展开成食材量进入备餐清单（成本页按菜品报价另算，口径不同，此处不共用）
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT od.*, d.name as dish_name FROM order_dishes od
            LEFT JOIN dishes d ON od.dish_id = d.id WHERE od.order_id = ?
        """, (order_id,))
        dishes = [dict(r) for r in cur.fetchall()]
        for d in dishes:
            cur.execute("""
                SELECT di.amount, i.id FROM dish_ingredients di
                JOIN ingredients i ON di.ingredient_id = i.id WHERE di.dish_id = ?
            """, (d["dish_id"],))
            for r in cur.fetchall():
                amount = r["amount"] * d["quantity"]
                if r["id"] not in ing_demand:
                    ing_demand[r["id"]] = {"total": 0, "per_package": r["amount"], "order_qty": 0,
                                           "portion_count": 1, "total_portions": 0, "cost_only": False}
                ing_demand[r["id"]]["total"] += amount
                ing_demand[r["id"]]["per_package"] = r["amount"]
                ing_demand[r["id"]]["order_qty"] += d["quantity"]

    # 工具需求：统一走 calc_order_tool_needs()（占用统计也用它，保证口径一致、不会递归）
    tool_demand = calc_order_tool_needs(order_id)

    # 拉取详情
    ingredients_result = []
    with get_db() as conn:
        cur = conn.cursor()
        for ing_id, info in ing_demand.items():
            if info.get("cost_only"):
                continue  # 成本专用项（如生菜）不进备餐采购清单，但成本页照算
            cur.execute("SELECT name, unit, stock, threshold, category FROM ingredients WHERE id = ?", (ing_id,))
            r = cur.fetchone()
            if r:
                stock = r["stock"] or 0
                need = info["total"]
                # 被换掉的菜用量为 0，不再出现在备餐表里
                if need <= 0:
                    continue
                # 细分 meat 类 + 合并小料
                cat = r["category"] or "other"
                cat = _subcategorize_meat(r["name"], cat)
                if cat in SAUCE_LIKE_CATS:
                    cat = "sauce"
                ingredients_result.append({
                    "id": ing_id, "name": r["name"], "unit": r["unit"],
                    "swapped_in": bool(info.get("swapped_in")),
                    "need": round(need, 2), "stock": stock,
                    "shortage": round(max(0, need - stock), 2),
                    "threshold": r["threshold"] or 0,
                    "category": cat,
                    "warning": stock <= (r["threshold"] or 0),
                    "per_package": info["per_package"],
                    "order_qty": info["order_qty"],
                    "portion_count": info.get("portion_count", 1),
                    "portion_size": round(info["per_package"] / max(1, info.get("portion_count", 1)), 2),
                    "total_portions": info.get("total_portions", info.get("portion_count", 1) * info["order_qty"]),
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



def calc_dashboard(signer_clause="", signer_params=()):
    """
    全局库存面板数据。
    套餐可备份数：按该套餐每套餐所需食材，用剩余库存（减已汇总需求）反算。

    signer_clause / signer_params：按"归属"筛选（orders.created_by），只作用于
    **今日订单/今日营收/待备预约列表**；库存面板与告警仍是全店口径（库存是共用的）。
    """
    _sp = tuple(signer_params or ())
    with get_db() as conn:
        cur = conn.cursor()
        # 库存占用只算"未来/当天的、且还没扣过库存的"预约——历史单与已扣库存的单
        # 不该继续占库存（否则历史遗留的 pending 单会无限膨胀库存占用计算，越积越慢）。
        _today = today_cst().isoformat()
        cur.execute(
            "SELECT id FROM orders WHERE status IN ('pending','preparing') AND deleted_at IS NULL "
            "AND stock_deducted_at IS NULL AND booking_date >= ?",
            (_today,),
        )
        order_ids = [r["id"] for r in cur.fetchall()]

        # ---- 今日统计 ----
        # 口径统一为"预约日期=今天(北京时间)"且未取消，与下方"待备预约"列表一致；
        # 已取消的单单独计数，供前端提示，避免"数字比列表多"造成"丢单"误解。
        cur.execute("SELECT COUNT(*) as c, COALESCE(SUM(amount),0) as amt "
                    "FROM orders WHERE booking_date = ? AND status != 'cancelled' AND deleted_at IS NULL"
                    + signer_clause,
                    (_today,) + _sp)
        today = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM orders WHERE booking_date = ? AND status = 'cancelled' "
                    "AND deleted_at IS NULL" + signer_clause, (_today,) + _sp)
        today_cancelled_count = cur.fetchone()[0]
        cur.execute("SELECT status, COUNT(*) as c FROM orders WHERE deleted_at IS NULL GROUP BY status")
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
                   contact_phone, amount, status, note, created_by, created_by_name
            FROM orders
            WHERE status IN ('pending','preparing')
              AND deleted_at IS NULL
              AND booking_date IS NOT NULL AND booking_date != ''
        """ + signer_clause + """
            ORDER BY booking_date, booking_time
        """, _sp)
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
            "cancelled": today_cancelled_count,
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
                        "base_need": 0,          # 套餐本来用量（换菜前），用于弹窗拆分展示「原套餐 + 换入」
                        "base_portions": 0,
                    }
                preview_ing[r["id"]]["need"] += amount
                preview_ing[r["id"]]["base_need"] += amount
                preview_ing[r["id"]]["total_packages"] += pk["quantity"]
                preview_ing[r["id"]]["portion_count"] = pc
                preview_ing[r["id"]]["total_portions"] = pc * preview_ing[r["id"]]["total_packages"]
                preview_ing[r["id"]]["base_portions"] = preview_ing[r["id"]]["total_portions"]
                preview_ing[r["id"]]["portion_size"] = round(preview_ing[r["id"]]["need"] / max(1, preview_ing[r["id"]]["total_portions"]), 2) if preview_ing[r["id"]]["total_portions"] else 0

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
            mode = info.get("mode", "set") if isinstance(info, dict) else "set"   # 缺省=总数（用户确认的大多数情况）
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

        # 备注换菜：把换菜生效到预览食材（与 calc_order_requirements 完全一致的算法），
        # 这样"食材需求预览"就是备餐页最终会显示的那张表，粘贴时就能逐项核对识别对没。
        # need 对应 calc 里的 total；per_package/portion_count/total_portions 字段含义一致。
        for sw in (parsed.get("dish_swaps") or []):
            to_id = match_ingredient_id_by_name(sw.get("to"))
            if not to_id:
                continue
            froms = sw.get("froms") or [[sw.get("from"), None]]
            explicit = bool(sw.get("explicit", False))
            multi = len(froms) > 1
            to_qty = sw.get("to_qty", 1) or 1
            use_old = (not explicit) and (not multi)

            removed_total = 0.0
            for fitem in froms:
                fname = fitem[0] if isinstance(fitem, (list, tuple)) else fitem
                fqty = fitem[1] if isinstance(fitem, (list, tuple)) else None
                src_id = match_ingredient_id_by_name(fname)
                if not src_id or src_id not in preview_ing:
                    continue
                info = preview_ing[src_id]
                if info.get("need", 0) <= 0:
                    continue
                if use_old:
                    removed_total += info["need"]
                    info["need"] = 0
                    info["swapped_out"] = True
                else:
                    tp = info.get("total_portions", info.get("portion_count", 0)) or 0
                    portion = (info["need"] / tp) if tp > 0 else info["need"]
                    rm = portion * (fqty if fqty else 1e9)
                    if rm >= info["need"]:
                        removed_total += info["need"]
                        info["need"] = 0
                        info["swapped_out"] = True
                    else:
                        removed_total += rm
                        info["need"] -= rm

            if use_old:
                add = removed_total
            else:
                to_info = preview_ing.get(to_id)
                if to_info:
                    pc = to_info.get("portion_count", 0) or 1
                    dst_portion = (to_info.get("per_package", 0) or 0) / pc if pc > 0 else 100.0
                else:
                    dst_portion = 100.0
                add = dst_portion * to_qty
            if to_id not in preview_ing:
                cur.execute("SELECT name, unit, stock, threshold, category FROM ingredients WHERE id = ?", (to_id,))
                r = cur.fetchone()
                if r:
                    preview_ing[to_id] = {
                        "id": to_id, "name": r["name"], "unit": r["unit"],
                        "stock": r["stock"], "threshold": r["threshold"],
                        "need": 0, "category": r["category"] or "other",
                        "per_package": 100.0, "portion_count": 1,
                        "portion_size": 100.0, "total_packages": 0, "total_portions": 0,
                        "base_need": 0, "base_portions": 0,
                    }
            if to_id in preview_ing:
                preview_ing[to_id]["need"] = round(preview_ing[to_id].get("need", 0) + add, 2)
                preview_ing[to_id]["swapped_in"] = True
                preview_ing[to_id]["total_portions"] = (preview_ing[to_id].get("total_portions", 0) or 0) + to_qty
                tp2 = preview_ing[to_id].get("total_portions", 0) or 0
                preview_ing[to_id]["portion_size"] = round(preview_ing[to_id]["need"] / tp2, 2) if tp2 else 0

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
    """按固定顺序排序工具列表，相同顺序按 key 降序。"""
    return sorted(items, key=lambda x: (_tool_sort_index(x.get(name_field, "")), -x.get(key, 0)))


def sort_ingredients(items, name_field="name", cat_field="category"):
    """食材列表排序：先按 CATEGORY_ORDER 分类，组内按 INGREDIENT_FIXED_ORDER 固定顺序，
    不在固定列表的按库存升序（低库存排前面）。
    同时自动细分 meat 类 → beef/pork/chicken。
    输入可以是 dict 列表，返回新的排好序的 dict 列表，且每个 dict 的 category 已被细分。
    """
    # 先细分 category
    for x in items:
        x[cat_field] = _subcategorize_meat(x.get(name_field, ""), x.get(cat_field, "other"))

    # 按 CATEGORY_ORDER + 固定索引排序
    cat_rank = {cat: i for i, cat in enumerate(CATEGORY_ORDER)}

    def _sort_key(x):
        cat = x.get(cat_field, "other")
        cat_idx = cat_rank.get(cat, 999)
        fixed_idx = _get_fixed_sort_index(x.get(name_field, ""), cat)
        # 不在固定列表的（fixed_idx=999），按 stock 升序（低库存优先）
        if fixed_idx >= 999:
            stock = x.get("stock", 0) or 0
            return (cat_idx, 999, stock)
        return (cat_idx, fixed_idx, 0)

    return sorted(items, key=_sort_key)


def calc_merged_prep(order_ids):
    """合并多订单的备餐需求。
    返回：
      orders: [{id, booking_time, meal_time, address, contact_name, packages, urgency, delivered, served}]
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
                   contact_phone, amount, status, note, delivered, served,
                   created_by, created_by_name, raw_text
            FROM orders WHERE id = ?
            """, (oid,))
            r = cur.fetchone()
            if not r:
                continue
            o = dict(r)
            cur.execute("""
                SELECT op.quantity, op.people, p.name, op.setup_flag
                FROM order_packages op
                LEFT JOIN packages p ON op.package_id = p.id
                WHERE op.order_id = ?
            """, (oid,))
            o["packages"] = [dict(x) for x in cur.fetchall()]
            # 备餐「按订单」视图要显示"原录入订单"原文：用 own_raw_block 切出本单片段，
            # 避免批量粘贴多单时把别人的原文也带出来。
            try:
                from parser import own_raw_block as _orb
                o["own_raw_text"] = _orb(o.get("raw_text"), o.get("contact_name"),
                                         o.get("contact_phone"), o.get("amount"), o.get("address"))
            except Exception:
                o["own_raw_text"] = o.get("raw_text") or ""
            # 备注要点：换菜/加菜写在备注里，简略表要能一眼看到（否则等于"备注被省略"）
            # ⚠️ 2026-10-25 修：用户改过备注(note)后以 note 为准——raw_text 是原始录入信息，
            # 不更新；直接用原文解析会显示用户已删掉的换菜/加菜徽标。
            try:
                from parser import parse_order_text as _pot
                _pp = _pot(o.get("own_raw_text") or "") or {}
                _note = (o.get("note") or "").strip()
                if _note and _note != "无":
                    from parser import _parse_extra_ingredients as _pei, _parse_dish_swaps as _pds
                    _pp = {"extra_ingredients": _pei(_note), "dish_swaps": _pds(_note)}
                o["has_swap"] = bool(_pp.get("dish_swaps"))
                o["has_extra"] = bool(_pp.get("extra_ingredients"))
            except Exception:
                o["has_swap"] = False
                o["has_extra"] = False
            # 地点分两级（2026-10-25 用户定）：
            #   addr_group  = 一级地点（青龙湖 / 江家艺苑…归一后的名字），占主要位置、也用于分组
            #   addr_detail = 小地区（原地址去掉一级地名后的剩余，如"二期"、"东入口儿童乐园区"），
            #                 在最下一排小字显示，不挤占主要版面
            try:
                from addresses import canonical_address as _ca
                _addr = (o.get("address") or "").strip()
                _g = _ca(_addr) or _addr          # 归一值：分组用（青龙湖 / 江家艺苑）
                o["addr_group"] = _g or "未填地点"
                _d = ""
                if _g and _g in _addr:
                    _d = _addr.replace(_g, "", 1).strip(" 　·、,，-—~(")
                # 细节很短（"二期" / "一期" / "A区"…）说明它只是地名后缀、不是独立点位：
                # 不单独占一行浪费版面 —— 一级地点直接显示完整原地址（"青龙湖二期"）。
                if _d and len(_d) <= 3:
                    o["addr_main"] = _addr or "未填地点"
                    o["addr_detail"] = ""
                else:
                    o["addr_main"] = _g or _addr or "未填地点"
                    o["addr_detail"] = _d
            except Exception:
                o["addr_group"] = o.get("address") or "未填地点"
                o["addr_main"] = o.get("address") or "未填地点"
                o["addr_detail"] = ""
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
                    "total_portions": 0,
                }
            ing_merge[key]["total"] += ing["need"]
            if ing.get("swapped_in"):
                ing_merge[key]["swapped_in"] = True
            ing_merge[key]["order_ids"].append(o["id"])
            ing_merge[key]["per_package_samples"].append((ing.get("per_package"), ing.get("order_qty", 1)))
            ing_merge[key]["portion_count_samples"].append((ing.get("portion_count", 1), ing.get("order_qty", 1)))
            ing_merge[key]["total_packages"] += ing.get("order_qty", 1)
            ing_merge[key]["total_portions"] += ing.get("total_portions", 0)
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
        v["total_portions"] = v.get("total_portions", v["portion_count"] * v["total_packages"])
        v["portion_size"] = round(v["per_package"] / max(1, v["portion_count"]), 2)
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


# ===== 餐标配餐器 =====
# 思路：基于现有套餐的"人均配比"做线性扩展
# 1. 按 per_person_budget 找最接近的现有套餐做基准
# 2. 按人数比例缩放 package_ingredients
# 3. 成本校验（目标 38-45%）：超了减高价牛肉，不够加饱腹菜
# 4. 工具按人数配：椅子=人数，炉子=ceil(人数/4)

def catering_suggest(people, total_budget, per_person=None, kitchen_labor=0, delivery_labor=0):
    """餐标配餐器：返回建议方案（食材列表+工具+成本摘要）
    people: 人数
    total_budget: 总餐标（元）
    per_person: 可选人均餐标，优先于 total_budget/people
    kitchen_labor: 后厨兼职人工成本（每单固定）
    delivery_labor: 配送兼职人工成本（每单固定）
    """
    import math
    if per_person is not None:
        total_budget = per_person * people
    if people <= 0 or total_budget <= 0:
        return {"ok": False, "msg": "人数和餐标必须大于0"}

    db = get_db()
    cur = db.cursor()

    # 1. 取所有套餐含食材+成本
    cur.execute("""
        SELECT p.id, p.name, p.min_people, p.max_people, p.base_price,
               pi.per_package, pi.portion_count, pi.cost_only,
               i.id as ing_id, i.name as ing_name, i.unit, i.cost, i.category
        FROM packages p
        LEFT JOIN package_ingredients pi ON pi.package_id = p.id
        LEFT JOIN ingredients i ON i.id = pi.ingredient_id
        ORDER BY p.min_people
    """)
    rows = cur.fetchall()

    # 按套餐分组
    packages = {}
    for r in rows:
        pid = r["id"]
        if pid not in packages:
            packages[pid] = {
                "id": pid, "name": r["name"],
                "min_people": r["min_people"], "max_people": r["max_people"],
                "base_price": r["base_price"],
                "ingredients": []
            }
        if r["ing_id"]:
            packages[pid]["ingredients"].append({
                "ing_id": r["ing_id"], "name": r["ing_name"],
                "per_package": r["per_package"], "portion_count": r["portion_count"],
                "cost_only": r["cost_only"], "unit": r["unit"],
                "cost": r["cost"], "category": r["category"]
            })

    if not packages:
        return {"ok": False, "msg": "无套餐数据"}

    # 2. 找基准套餐：人均餐标最接近的
    target_per = total_budget / people
    # 排除含配送费的干扰，用 base_price / max_people 做比较
    best_pkg = None
    best_diff = float('inf')
    max_pkg = None       # 人均最贵的套餐（高档方案 fallback）
    max_ref_per = 0
    for pid, pkg in packages.items():
        if not pkg["ingredients"]:
            continue
        ref_per = pkg["base_price"] / pkg["max_people"]
        diff = abs(ref_per - target_per)
        if diff < best_diff:
            best_diff = diff
            best_pkg = pkg
        if ref_per > max_ref_per:
            max_ref_per = ref_per
            max_pkg = pkg

    # 如果所有套餐人均都远低于目标（比如人均差 > 100%），选人均最贵的那个
    if max_pkg and target_per > max_ref_per * 2:
        best_pkg = max_pkg

    if not best_pkg:
        return {"ok": False, "msg": "无有效套餐可参考"}

    # 3. 按人数比例缩放（按基准套餐的 max_people 缩放）
    base_people = best_pkg["max_people"]
    ratio = people / base_people

    # 聚合同名食材（可能多次出现，如生菜有 cost_only=0 和 =1）
    ing_map = {}
    for ing in best_pkg["ingredients"]:
        key = (ing["ing_id"], ing["cost_only"] or 0)
        if key not in ing_map:
            ing_map[key] = {
                "ing_id": ing["ing_id"], "name": ing["name"],
                "unit": ing["unit"], "cost": ing["cost"] or 0,
                "category": ing["category"], "cost_only": ing["cost_only"] or 0,
                "per_package": 0, "portion_count": 0
            }
        ing_map[key]["per_package"] += ing["per_package"] * ratio
        ing_map[key]["portion_count"] += ing["portion_count"] * ratio

    # 4. 数值取整：重量类保留 10g 精度，份数类向上取整
    for key, it in ing_map.items():
        if it["unit"] == "g":
            it["per_package"] = round(it["per_package"] / 10) * 10
        elif it["unit"] in ("份", "个", "瓶", "双", "张", "包"):
            it["per_package"] = math.ceil(it["per_package"])
        else:
            it["per_package"] = round(it["per_package"], 1)
        # 份数取整（不能出现 3.2 份）
        it["portion_count"] = math.ceil(it["portion_count"]) if it["portion_count"] > 0 else 0
        # 克数 = 总量 ÷ 份数
        it["portion_size"] = round(it["per_package"] / max(1, it["portion_count"]), 2) if it["portion_count"] else 0
        it["sub_total"] = round(it["per_package"] * it["cost"], 2)

    # 5. 成本校验：食材成本目标 28-35%（扣除人工后的合理区间）
    # 人工成本（后厨+配送）固定，先扣出来再校验食材
    labor_cost = round(float(kitchen_labor or 0) + float(delivery_labor or 0), 2)
    total_cost = round(sum(it["sub_total"] for it in ing_map.values()), 2)
    # 食材目标区间 = 餐标 × [28%, 35%]（留出人工空间）
    target_low = total_budget * 0.28
    target_high = total_budget * 0.35
    warnings = []

    # 超预算：按高价到低价顺序减牛肉（每 50g 一档）
    def _adjust_beef(delta_g):
        beef_items = sorted([it for it in ing_map.values()
                              if it["category"] in ("meat", "beef")
                              and "牛" in it["name"]
                              and it["unit"] == "g"],
                             key=lambda x: -x["cost"])
        for it in beef_items:
            if delta_g == 0:
                break
            change = min(delta_g, max(0, it["per_package"] - 50))
            it["per_package"] -= change
            it["sub_total"] = round(it["per_package"] * it["cost"], 2)
            delta_g -= change
        return delta_g

    # 不够预算：直接给所有肉类按 cost 比例增加克数，快速拉高成本
    def _add_filler(delta_budget):
        if delta_budget <= 0:
            return 0
        # 所有按g计价的食材，按 cost 从高到低
        all_g_items = sorted([it for it in ing_map.values()
                              if it["unit"] == "g" and it["cost"] > 0],
                             key=lambda x: -x["cost"])
        # 贵的先来（牛肉>猪肉>鸡肉>蘸料>蔬菜）
        for it in all_g_items:
            if delta_budget <= 0:
                break
            # 计算这一元素最多能加多少，直到补完缺口或加 1000g 上限
            max_g = min(int(delta_budget / it["cost"] / 50) * 50, 1000)
            if max_g >= 50:
                it["per_package"] += max_g
                it["sub_total"] = round(it["per_package"] * it["cost"], 2)
                delta_budget -= max_g * it["cost"]
        # 最后补主食（馒头/土豆）
        if delta_budget > 0:
            for it in ing_map.values():
                if delta_budget <= 0: break
                if ("馒头" in it["name"] or "土豆" in it["name"]) and it["cost"] > 0:
                    if it["unit"] == "个":
                        add = min(math.ceil(delta_budget / it["cost"]), 20)
                        it["per_package"] += add
                        it["sub_total"] = round(it["per_package"] * it["cost"], 2)
                        delta_budget -= add * it["cost"]
                    elif it["unit"] == "g":
                        add = min(int(delta_budget / it["cost"] / 50) * 50, 2000)
                        if add >= 50:
                            it["per_package"] += add
                            it["sub_total"] = round(it["per_package"] * it["cost"], 2)
                            delta_budget -= add * it["cost"]
        return delta_budget

    if total_cost > target_high:
        excess = total_cost - target_high
        # 按牛肉 cost 降序，减 50g 一档
        delta_g = int(excess / 0.1)  # 粗算每 100g 牛肉约 10 元
        remaining = _adjust_beef(delta_g)
        total_cost = round(sum(it["sub_total"] for it in ing_map.values()), 2)
        if total_cost > target_high:
            warnings.append(f"成本 ¥{total_cost} 仍超上限 ¥{target_high:.0f}，建议手动减项")
    elif total_cost < target_low:
        shortage = target_low - total_cost
        _add_filler(shortage)
        total_cost = round(sum(it["sub_total"] for it in ing_map.values()), 2)
        if total_cost < target_low:
            warnings.append(f"成本 ¥{total_cost} 低于目标 ¥{target_low:.0f}，可加牛肉提档")

    # 6. 工具按人数配
    cur.execute("SELECT id, name, stock, threshold, cost FROM tools")
    all_tools = [dict(r) for r in cur.fetchall()]
    tool_list = []
    for t in all_tools:
        n = t["name"]
        qty = tool_qty_by_people(n, people)  # 统一走单一口径，避免和订单装备表各算各的
        tool_list.append({
            "id": t["id"], "name": n, "per_package": qty,
            "stock": t["stock"], "shortage": max(0, qty - t["stock"])
        })

    # 7. 摘要
    meat_weight = sum(it["per_package"] for it in ing_map.values()
                      if it["category"] in ("meat", "beef", "pork", "chicken")
                      and it["unit"] == "g" and not it["cost_only"])
    # 最终成本 = 食材 + 人工
    final_cost = round(total_cost + labor_cost, 2)
    final_margin = round(total_budget - final_cost, 2)
    return {
        "ok": True,
        "data": {
            "base_package": best_pkg["name"],
            "base_people": base_people,
            "ratio": round(ratio, 2),
            "ingredients": list(ing_map.values()),
            "tools": tool_list,
            "summary": {
                "people": people,
                "budget": total_budget,
                "per_person": round(total_budget / people, 2),
                "food_cost": total_cost,              # 食材成本
                "kitchen_labor": float(kitchen_labor or 0),  # 后厨人工
                "delivery_labor": float(delivery_labor or 0),# 配送人工
                "labor_cost": labor_cost,            # 人工合计
                "total_cost": final_cost,            # 总成本=食材+人工
                "margin": final_margin,              # 毛利=餐标-总成本
                "margin_pct": round(final_margin / total_budget * 100, 1),
                "meat_weight_per_person": round(meat_weight / people),
                "target_cost_range": [round(target_low), round(target_high)],
                "is_healthy": target_low <= total_cost <= target_high,
            },
            "warnings": warnings,
        }
    }
