"""成本与利润计算模块 —— "这一单到底赚多少、亏不亏本"

户外烤肉的真实成本结构（用户 2026-09-28 确认）：
  收入 = orders.amount（实收货款；押金不算收入）
  成本 = 食材成本（套餐用量×食材单价，自动；含一次性餐具/打包盒）
       + 搭建/回收外包费（"单数" × 场地单价）
       + 配送费
       + 人工（小时工 + 自己人折算）
       + 额外耗材（炭、气罐等；一次性餐具已含在食材里，避免重复计）
       + 其他临时项
  现金利润 = 收入 − 成本合计          → 毛利率 = 现金利润 / 收入
  净利     = 现金利润 − 固定开销摊销   → 摊销 = 月固定开销 ÷ 月单数

搭建/回收外包费的"单数"规则（用户原话）：
  按每 10 人餐算一单，不足 10 人餐也算一单；13 人餐 = 10 人 + 3 人 = 2 单。
  等价算法：单数 = max(套餐总份数, ⌈就餐人数/10⌉)，最低 1 单。
  场地不同单价不同：青龙湖/北湖/玉石公园/江家艺苑 100，锦城湖 130，双流中心公园 150。
"""
import json
import math
import re as _re
from parser import own_raw_block      # 批量录入时从整段原文里切出「本单那一段」

KINDS = ("food", "outsource", "fuel", "labor", "consume", "other")

KIND_LABEL = {
    "food": "食材成本",
    "outsource": "搭建/回收外包费",
    "fuel": "配送费",
    "labor": "打包切配人工",
    "consume": "额外耗材",
    "other": "其他开销",
}
KIND_HINT = {
    "food": "套餐用量 × 食材单价，自动算（已含一次性餐具、打包盒）",
    "outsource": "按人数折算单数 × 场地单价（每 10 人 1 单，不足 10 人也算 1 单）",
    "fuel": "配送/跑腿的费用",
    "labor": "打包切配出餐人工：首单 + 每多一个 10 人单（单越大越省，参数见设置）",
    "consume": "炭、气罐这类；一次性餐具已含在食材里，这里别重复算",
    "other": "临时多出来的开销",
}


# ---------- 套餐情况（录入时口径，不拆分） ----------
# 录入时用户写的是「20人餐」「18人餐」这类整单描述；系统为算成本拆成多个档位套餐
# （如 20人餐 → 两个 9-10人餐）。毛利率表里要展示的是「录入时的套餐名」，不是拆分后的。
def _extract_package_label(raw_text, packages=None):
    """从订单原始文字的「预约项目」取录入时的套餐名（如 '20人餐'），不做拆分。
    取不到时回退到 order_packages 按名称合并（如 '9-10人餐×2'）。"""
    rt = (raw_text or "").strip()
    label = ""
    if rt:
        m = _re.search(
            r"预约项目[:：]?\s*(.*?)(?=预约地址|用餐时间|联系人|联系电话|金额|押金|备注|预约时间|\Z)",
            rt, _re.S)
        seg = (m.group(1) if m else rt).strip()
        # 去掉每行的（搭建）/（不搭建）/（半包）等后缀，只留套餐名
        seg = _re.sub(r"[（(][^）)]*搭建[^）)]*[）)]", " ", seg)
        seg = _re.sub(r"[（(]不搭建[）)]", " ", seg)
        seg = _re.sub(r"[（(]半包[）)]", " ", seg)
        seg = _re.sub(r"\s+", " ", seg).strip(" ·、/｜|")
        label = seg
    if not label and packages:
        agg = {}
        for pk in packages:
            nm = (pk.get("name") or "套餐").strip()
            agg[nm] = agg.get(nm, 0) + int(pk.get("quantity") or 1)
        label = "、".join(("%s×%d" % (nm, q)) if q > 1 else nm
                          for nm, q in agg.items())
    # 套餐名统一：区间档位一律取「人数上限」——2-3人餐→3人餐、3-4人餐→4人餐、
    # 5-6人餐→6人餐、7-8人餐→8人餐、9-10人餐→10人餐。
    # 理由：档位是按最多能坐多少人定的（也是计价依据），下限只是起步人数，
    # 所以"3人餐"就是 2-3 人这一档，两种写法指同一个东西，不该同时出现。
    label = _re.sub(r"(\d+)\s*[-~—]\s*(\d+)\s*人餐", r"\2人餐", label)
    return label

DEFAULT_CONFIG = {
    # 场地单价表：outsource=搭建回收每单单价，fuel=配送费默认值
    "places": [
        {"name": "青龙湖", "outsource": 100, "fuel": 0},
        {"name": "北湖", "outsource": 100, "fuel": 0},
        {"name": "玉石公园", "outsource": 100, "fuel": 0},
        {"name": "江家艺苑", "outsource": 100, "fuel": 0},
        {"name": "锦城湖", "outsource": 130, "fuel": 0},
        {"name": "双流中心公园", "outsource": 150, "fuel": 0},
    ],
    "default_outsource": 100,      # 地址没匹配到场地时用它
    "people_per_unit": 10,         # 多少人一单
    "defaults": {"fuel": 0, "consume": 0},   # 每单默认金额
    "labor_first": 40,             # 打包切配出餐人工：首单金额（元）
    "labor_per_unit": 25,          # 每多一个"单"（10人）增加的人工——单越大越省
    "fixed_monthly": 0,            # 每月固定开销（装备折旧、工具添置等）
    "fixed_orders_per_month": 0,   # 预计每月单数（用于摊销；0=按当期实际单数）
    "margin_alert": 40,            # 毛利率低于它标红
}

CONFIG_KEY = "cost_config"


# ---------- 配置读写 ----------
def get_config(db):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    row = db.execute("SELECT value FROM settings WHERE key=?", (CONFIG_KEY,)).fetchone()
    if row and row["value"]:
        try:
            saved = json.loads(row["value"])
        except Exception:
            saved = {}
        for k, v in (saved or {}).items():
            if k == "defaults" and isinstance(v, dict):
                cfg["defaults"].update(v)
            elif k == "places" and isinstance(v, list) and v:
                cfg["places"] = v
            else:
                cfg[k] = v
    return cfg


def save_config(db, patch):
    cfg = get_config(db)
    for k, v in (patch or {}).items():
        if k == "defaults" and isinstance(v, dict):
            cfg["defaults"].update(v)
        else:
            cfg[k] = v
    db.execute("INSERT OR REPLACE INTO settings (key,value,note) VALUES (?,?,?)",
               (CONFIG_KEY, json.dumps(cfg, ensure_ascii=False), "成本与利润配置"))
    db.commit()
    return cfg


def match_place(address, cfg):
    """按地址文字匹配场地（'青龙湖二期' → 青龙湖）；匹配不到返回 None

    先走『地址归一』（addresses.canonical_address），保证和订单页筛选、
    备餐页用的是同一套规则：同一个地方的不同写法算一个地方。
    """
    from addresses import canonical_address
    addr = canonical_address((address or "").strip())
    if not addr:
        return None
    for p in cfg.get("places", []):
        nm = (p.get("name") or "").strip()
        if nm and (nm in addr or addr in nm):
            return p
    return None


def outsource_units(people, pkg_qty, per_unit=10):
    """外包单数：每 per_unit 人一单、不足也算一单；再与套餐份数取大（13人=2单）"""
    people = float(people or 0)
    pkg_qty = int(pkg_qty or 0)
    by_people = int(math.ceil(people / float(per_unit or 10))) if people > 0 else 0
    return max(1, by_people, pkg_qty)


# 半包（不搭建）判定里的租赁关键词
_RENT_KW = ("桌椅", "卡式炉", "气罐", "烤盘", "租赁", "出租", "租用")


def _setup_from_text(text):
    """从订单原始文字(配合备注)提取搭建标记：含'不搭建'→0（不搭建），含'搭建'→1（搭建），都没有→None。
    用户每单都在「预约项目」里明确写了（搭建）/（不搭建），应优先于价格推导使用。"""
    t = text or ""
    if "不搭建" in t:
        return 0
    if "搭建" in t:
        return 1
    return None


def _order_setup(db, o, cfg):
    """搭建判定（0/1）：手工 setup_flag 优先；否则按价格推导。
    规则与 app._guess_setup_and_fee 一致——半包(金额≈正价−搭建费)即不搭建。
    无正价信息时保守返回 0（不搭建），避免误收搭建费。"""
    sf = o.get("setup_flag")
    if sf is not None:
        return 1 if sf == 1 else 0
    # 优先读原始文字（用户每单都标注了 搭建/不搭建）；取不到就从库里补查
    rt = o.get("raw_text")
    nt = o.get("note")
    if rt is None and o.get("id") is not None:
        _rr = db.execute("SELECT raw_text, note FROM orders WHERE id=?", (o.get("id"),)).fetchone()
        if _rr:
            rt = _rr["raw_text"]; nt = _rr["note"]
    # 批量录入时 raw_text 里混着好几单（别人的"不搭建"会串进来），先切出本单那一段再判
    _own = own_raw_block(rt, o.get("contact_name"), o.get("contact_phone"),
                         o.get("amount"), o.get("address"))
    _t = _setup_from_text((_own or "") + " " + (nt or ""))
    if _t is not None:
        return _t
    address = (o.get("address") or "")
    amount = float(o.get("amount") or 0)
    note = (o.get("note") or "")
    row = db.execute(
        """SELECT COALESCE(SUM(COALESCE(op.price, p.price) * op.quantity), 0) AS pt,
                  COALESCE(SUM(op.quantity), 0) AS qty
           FROM order_packages op LEFT JOIN packages p ON p.id = op.package_id
           WHERE op.order_id = ?""", (o.get("id"),)).fetchone()
    total = float(row["pt"] or 0)
    qty = int(row["qty"] or 1) or 1
    if total <= 0:
        return 0
    place = match_place(address, cfg)
    unit = float((place or {}).get("outsource") if place else cfg.get("default_outsource", 100) or 0)
    half = total - unit * qty
    if amount >= total - 0.5:
        return 1
    if amount <= half + 0.5:
        return 0
    for kw in _RENT_KW:
        if kw in note:
            return 0
    return 1 if abs(amount - total) <= abs(amount - half) else 0


def _delivery_cost(o, setup):
    """不搭建单的配送费（搭建单的配送已含在搭建费里，返回0）：
      · 自提/老板送/顺带/自己送 → 0
      · 平台配送(滴滴/闪送/货拉拉…) → 用记录的 delivery_fee；没记录则暂计0
      · 空（未标注配送方式）→ 暂计0，提示补录
    注：青龙湖起步的滴滴/闪送费若未逐笔录入，这里会低估，属已知缺口。"""
    if setup == 1:
        return 0.0, "搭建单：配送含在搭建费内"
    dp = (o.get("delivery_person") or "").strip()
    if any(k in dp for k in ("自提", "自送", "老板", "顺带", "自己", "门店", "自取")):
        return 0.0, "自提/老板送：配送费0"
    df = o.get("delivery_fee")
    if df is not None:
        return float(df), "配送费 ¥%g（%s）" % (float(df), dp or "未标配送人")
    if dp:
        return 0.0, "平台配送（%s）：未记录配送费，暂计0" % dp
    return 0.0, "不搭建：未标注配送方式，暂计0（平台配送请补录配送费）"


# ---------- 成本明细 ----------
def _food_cost(db, oid):
    """食材成本 = 套餐配量×份数×单价 + 单点加菜 + 备注加菜（含 set 覆盖 / add 增量）"""
    cur = db.cursor()
    pk = cur.execute("""
        SELECT COALESCE(SUM(pi.per_package * op.quantity * COALESCE(i.cost,0)),0) AS c
        FROM order_packages op
        JOIN package_ingredients pi ON pi.package_id = op.package_id
        JOIN ingredients i ON pi.ingredient_id = i.id
        WHERE op.order_id = ?
    """, (oid,)).fetchone()
    cost = float(pk["c"] or 0)
    # 单点加菜：优先按菜品单价，没有则由菜品食材反推
    dishes = cur.execute("""
        SELECT od.quantity, d.id AS dish_id, COALESCE(d.cost,0) AS dish_cost
        FROM order_dishes od LEFT JOIN dishes d ON od.dish_id = d.id
        WHERE od.order_id = ?
    """, (oid,)).fetchall()
    for d in dishes:
        unit = float(d["dish_cost"] or 0)
        if unit <= 0 and d["dish_id"]:
            r = cur.execute("""
                SELECT COALESCE(SUM(di.amount * COALESCE(i.cost,0)),0) AS c
                FROM dish_ingredients di JOIN ingredients i ON di.ingredient_id = i.id
                WHERE di.dish_id = ?
            """, (d["dish_id"],)).fetchone()
            unit = float(r["c"] or 0)
        cost += unit * float(d["quantity"] or 0)
    # 备注加菜（备注解析出来的额外食材）：按 set 覆盖 / add 增量 并进成本
    try:
        from parser import parse_order_text, match_extra_ingredients_to_db
        r = cur.execute(
            "SELECT raw_text, contact_name, contact_phone, amount, address FROM orders WHERE id=?",
            (oid,)).fetchone()
        raw = (r["raw_text"] if r else "") or ""
        if raw:
            # 批量录入时整段原文混着好几单，先切出本单那段——否则会把别人的加菜算到这单头上
            raw = own_raw_block(raw, (r["contact_name"] if r else ""),
                                (r["contact_phone"] if r else ""),
                                (r["amount"] if r else None),
                                (r["address"] if r else "")) or raw
            parsed = parse_order_text(raw)
            for ei in (match_extra_ingredients_to_db(parsed.get("extra_ingredients", []) or []) or []):
                iid = ei.get("matched_id")
                if not iid:
                    continue
                c = cur.execute("SELECT COALESCE(cost,0) AS c FROM ingredients WHERE id=?", (iid,)).fetchone()
                unit_cost = float((c["c"] if c else 0) or 0)
                amt = float(ei.get("total") or 0)
                if ei.get("mode", "add") == "set":
                    # 覆盖：先扣掉这个食材在套餐里的那份，再加设定的量
                    pk2 = cur.execute("""
                        SELECT COALESCE(SUM(pi.per_package * op.quantity * COALESCE(i.cost,0)),0) AS c
                        FROM order_packages op
                        JOIN package_ingredients pi ON pi.package_id = op.package_id
                        JOIN ingredients i ON pi.ingredient_id = i.id
                        WHERE op.order_id = ? AND pi.ingredient_id = ?
                    """, (oid, iid)).fetchone()
                    cost -= float(pk2["c"] or 0)
                cost += amt * unit_cost
    except Exception:
        pass
    return round(cost, 2)


def _overrides(db, oid):
    """该单手工设过的成本项：{kind: row} + 其他项列表"""
    rows = db.execute("SELECT * FROM order_costs WHERE order_id=? ORDER BY id", (oid,)).fetchall()
    fixed, extras = {}, []
    for r in rows:
        d = dict(r)
        if d["kind"] == "other":
            extras.append(d)
        else:
            fixed[d["kind"]] = d
    return fixed, extras


def _auto_items(db, oid, cfg, setup=None):
    """自动算出的成本项（未手工覆盖前的默认值）。
    搭建费：仅搭建(全包)单征收；不搭建(半包/自送)一律不计。
    配送费：仅不搭建单、且为平台配送时计（自提/老板送=0）。"""
    cur = db.cursor()
    o = cur.execute("SELECT address, setup_flag, delivery_person, delivery_fee FROM orders WHERE id=?", (oid,)).fetchone()
    o = dict(o) if o else {}
    address = (o.get("address") or "") or ""
    r = cur.execute("""
        SELECT COALESCE(SUM(op.quantity),0) AS qty, COALESCE(SUM(op.people),0) AS people
        FROM order_packages op WHERE op.order_id=?
    """, (oid,)).fetchone()
    place = match_place(address, cfg)
    out_price = float((place or {}).get("outsource") if place else cfg.get("default_outsource", 100) or 0)
    units = outsource_units(r["people"], r["qty"], cfg.get("people_per_unit", 10))
    if setup is None:
        setup = _order_setup(db, o, cfg)
    d = cfg.get("defaults", {})
    # 打包切配人工：首单 + 每多一个"单"（单价更低，单越大越省）
    l_first = float(cfg.get("labor_first", 40) or 0)
    l_per = float(cfg.get("labor_per_unit", 25) or 0)
    extra = max(0, units - 1)
    labor_auto = round(l_first + l_per * extra, 2)
    labor_detail = ("首单 ¥%g" % l_first) if extra == 0 else ("首单 ¥%g + %d 个多单 × ¥%g" % (l_first, extra, l_per))
    # —— 搭建费：仅搭建(全包)单；不搭建(半包/自送)不计 ——
    if setup == 1:
        outsource_amount = round(units * out_price, 2)
        outsource_detail = "%d单 × ¥%g（%s）" % (units, out_price, (place or {}).get("name") or "默认价")
    else:
        outsource_amount = 0.0
        outsource_detail = "不搭建（半包/自送）：无搭建费"
    # —— 配送费：仅不搭建单、且为平台配送；自提/老板送=0 ——
    fuel_amount, fuel_detail = _delivery_cost(o, setup)
    return {
        "food": {"amount": _food_cost(db, oid)},
        "outsource": {"amount": outsource_amount, "qty": units, "unit_price": out_price,
                      "detail": outsource_detail},
        "fuel": {"amount": fuel_amount, "detail": fuel_detail},
        "labor": {"amount": labor_auto, "detail": labor_detail},
        "consume": {"amount": float(d.get("consume", 0) or 0)},
    }


def order_profit(db, oid, cfg=None):
    """单笔订单的完整利润账"""
    cfg = cfg or get_config(db)
    cur = db.cursor()
    o = cur.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return None
    o = dict(o)
    revenue = float(o.get("amount") or 0)
    setup = _order_setup(db, o, cfg)
    auto = _auto_items(db, oid, cfg, setup=setup)
    fixed, extras = _overrides(db, oid)

    items, cost_total = [], 0.0
    for kind in ("food", "outsource", "fuel", "labor", "consume"):
        a = auto.get(kind, {})
        ov = fixed.get(kind)
        if ov:
            if kind == "outsource":
                qty = float(ov["qty"]) if ov.get("qty") is not None else a.get("qty")
                up = float(ov["unit_price"]) if ov.get("unit_price") is not None else a.get("unit_price")
                amount = round(qty * up, 2) if qty is not None and up is not None else float(ov["amount"] or 0)
            else:
                amount = float(ov["amount"] or 0)
            detail = "手工改过" + (("（%s）" % ov["note"]) if ov.get("note") else "")
        else:
            amount = float(a.get("amount") or 0)
            detail = a.get("detail", "")
        amount = round(amount, 2)
        cost_total += amount
        items.append({
            "kind": kind, "name": KIND_LABEL[kind], "hint": KIND_HINT[kind],
            "amount": amount, "auto": round(float(a.get("amount") or 0), 2),
            "overridden": bool(ov), "detail": detail,
            "qty": (float(ov["qty"]) if ov and ov.get("qty") is not None else a.get("qty")),
            "unit_price": (float(ov["unit_price"]) if ov and ov.get("unit_price") is not None else a.get("unit_price")),
            "locked": (kind == "food"),      # 食材不允许直接改总额（改食材单价即可）
            "note": (ov["note"] if ov else None),
        })
    for e in extras:
        amt = round(float(e["amount"] or 0), 2)
        cost_total += amt
        items.append({"kind": "other", "id": e["id"], "name": e["name"] or "其他开销",
                      "hint": KIND_HINT["other"], "amount": amt, "auto": None,
                      "overridden": True, "detail": e["note"] or "", "locked": False})

    cost_total = round(cost_total, 2)
    cash_profit = round(revenue - cost_total, 2)
    # 配送费是代收代付（收多少基本付给搭建师傅多少）→ 算真实毛利率时从收入里扣掉，作分母
    _dv = cur.execute("""
        SELECT COALESCE(SUM((p.price - p.base_price) * op.quantity),0) AS dv
        FROM order_packages op JOIN packages p ON op.package_id = p.id
        WHERE op.order_id = ?
    """, (oid,)).fetchone()
    delivery_passthrough = round(float(_dv["dv"] or 0), 2)
    margin_base = round(revenue - delivery_passthrough, 2)
    margin = round(cash_profit / margin_base * 100, 1) if margin_base > 0 else 0.0
    # 固定开销摊销
    fixed_monthly = float(cfg.get("fixed_monthly", 0) or 0)
    per_month = float(cfg.get("fixed_orders_per_month", 0) or 0)
    if fixed_monthly > 0:
        if per_month <= 0:
            row = cur.execute("""
                SELECT COUNT(*) AS c FROM orders
                WHERE deleted_at IS NULL AND status!='cancelled'
                  AND substr(booking_date,1,7) = substr(COALESCE(?, datetime('now','localtime')),1,7)
            """, (o.get("booking_date"),)).fetchone()
            per_month = float(row["c"] or 1) or 1.0
        fixed_alloc = round(fixed_monthly / per_month, 2)
    else:
        fixed_alloc = 0.0
    net_profit = round(cash_profit - fixed_alloc, 2)
    alert_pct = float(cfg.get("margin_alert", 40) or 0)

    if cash_profit < 0:
        level = "loss"
    elif margin < alert_pct:
        level = "warn"
    else:
        level = "ok"

    # 这单**原始录入信息**：财务页改成本时要能对着看，免得改错单（2026-10-14）
    _pkgs = cur.execute("""
        SELECT op.quantity, op.people, op.price, op.setup_flag, p.name AS pkg_name
        FROM order_packages op LEFT JOIN packages p ON op.package_id = p.id
        WHERE op.order_id = ? ORDER BY op.id
    """, (oid,)).fetchall()
    order_info = {
        "order_id": oid,
        "contact_name": o.get("contact_name") or "",
        "contact_phone": o.get("contact_phone") or "",
        "address": o.get("address") or "",
        "booking_date": o.get("booking_date") or "",
        "booking_time": o.get("booking_time") or "",
        "meal_time": o.get("meal_time") or "",
        "note": o.get("note") or "",
        "amount": round(revenue, 2),
        "deposit": round(float(o.get("deposit") or 0), 2),
        "discount": round(float(o.get("discount") or 0), 2),
        "menu_name": o.get("menu_name") or "",
        "setup_flag": o.get("setup_flag"),
        "setup": setup,                 # 判定后的搭建（1=搭建 / 0=不搭建），供明细弹窗"录入信息"显示
        "delivery_fee": o.get("delivery_fee"),
        "delivery_person": o.get("delivery_person") or "",
        "source": o.get("source") or "",
        "packages": [{"name": (dict(_p).get("pkg_name") or ""),
                      "people": dict(_p).get("people"),
                      "quantity": dict(_p).get("quantity"),
                      "price": dict(_p).get("price"),
                      "setup_flag": dict(_p).get("setup_flag")} for _p in _pkgs],
    }

    return {
        "order": order_info,
        "order_id": oid,
        "revenue": round(revenue, 2),
        "deposit": round(float(o.get("deposit") or 0), 2),
        "discount": round(float(o.get("discount") or 0), 2),
        "address": o.get("address") or "",
        "booking_date": o.get("booking_date") or "",
        "contact_name": o.get("contact_name") or "",
        "payment_status": o.get("payment_status"),
        "deposit_status": o.get("deposit_status"),
        "status": o.get("status"),
        "items": items,
        "cost_total": cost_total,
        "cash_profit": cash_profit,
        "margin": margin,
        "delivery_passthrough": delivery_passthrough,
        "margin_base": margin_base,
        "fixed_alloc": fixed_alloc,
        "fixed_monthly": fixed_monthly,
        "fixed_orders_per_month": per_month,
        "net_profit": net_profit,
        "alert": level,
        "margin_alert": alert_pct,
        # 录入时的套餐名（如「20人餐」），不拆分；成本仍按拆后的档位套餐算
        # 批量录入时 raw_text 混着好几单，先切出本单那段，否则套餐名会显示成别人的
        "package_label": _extract_package_label(
            own_raw_block(o.get("raw_text"), o.get("contact_name"), o.get("contact_phone"),
                          o.get("amount"), o.get("address")),
            order_info.get("packages", [])),
        "setup": setup,          # 1=搭建 / 0=不搭建（供财务页逐单表显示搭建图标）
    }


# ---------- 期间汇总 ----------
def period_overview(db, date_from=None, date_to=None, cfg=None, extra_clause="", extra_params=()):
    """期间利润汇总：现金口径 + 含固定开销摊销口径
    extra_clause/extra_params：额外过滤（如按"归属" by= 筛选），直接拼进 WHERE。
    """
    cfg = cfg or get_config(db)
    cur = db.cursor()
    clause = "o.status!='cancelled' AND o.deleted_at IS NULL"
    params = []
    if date_from:
        clause += " AND o.booking_date >= ?"
        params.append(date_from)
    if date_to:
        clause += " AND o.booking_date <= ?"
        params.append(date_to)
    if extra_clause:
        clause += " " + extra_clause
        params.extend(extra_params)
    rows = cur.execute("SELECT o.id, o.created_by, u.name AS created_by_name FROM orders o LEFT JOIN users u ON u.id=o.created_by WHERE %s ORDER BY o.booking_date DESC, o.id DESC" % clause,
                       params).fetchall()
    orders, tot = [], {"revenue": 0.0, "food": 0.0, "other": 0.0, "cost": 0.0,
                       "profit": 0.0, "net": 0.0, "loss": 0, "warn": 0, "delivery": 0.0,
                       "food_cost": 0.0, "labor_cost": 0.0, "outsource_cost": 0.0,
                       "fuel_cost": 0.0, "consume_cost": 0.0, "other_cost": 0.0,
                       "fixed_alloc": 0.0}
    by_kind = {}
    for r in rows:
        p = order_profit(db, r["id"], cfg)
        if not p:
            continue
        food = next((i["amount"] for i in p["items"] if i["kind"] == "food"), 0.0)
        other = round(p["cost_total"] - food, 2)
        # 按成本类型汇总（供财务页"成本明细"逐项展开）
        bk = {}
        for it in p["items"]:
            k = it.get("kind") or "other"
            bk[k] = round(bk.get(k, 0.0) + float(it.get("amount") or 0), 2)
        for k, v in bk.items():
            by_kind[k] = round(by_kind.get(k, 0.0) + v, 2)
        tot["revenue"] += p["revenue"]
        tot["food"] += food
        tot["other"] += other
        tot["cost"] += p["cost_total"]
        tot["profit"] += p["cash_profit"]
        tot["net"] += p["net_profit"]
        tot["delivery"] += p.get("delivery_passthrough", 0.0)
        tot["food_cost"] += bk.get("food", 0.0)
        tot["labor_cost"] += bk.get("labor", 0.0)
        tot["outsource_cost"] += bk.get("outsource", 0.0)
        tot["fuel_cost"] += bk.get("fuel", 0.0)
        tot["consume_cost"] += bk.get("consume", 0.0)
        tot["other_cost"] += bk.get("other", 0.0)
        tot["fixed_alloc"] += p.get("fixed_alloc", 0.0)
        if p["alert"] == "loss":
            tot["loss"] += 1
        elif p["alert"] == "warn":
            tot["warn"] += 1
        orders.append({
            "id": p["order_id"], "created_by": r["created_by"],
            "created_by_name": r["created_by_name"] or "",
            "booking_date": p["booking_date"], "contact_name": p["contact_name"],
            "address": p["address"], "revenue": p["revenue"], "food": food, "other": other,
            "cost_total": p["cost_total"], "profit": p["cash_profit"], "margin": p["margin"],
            "net_profit": p["net_profit"], "alert": p["alert"],
            "package_label": p.get("package_label", ""),
            "setup": p.get("setup"),
            "payment_status": p["payment_status"],
            "deposit_status": p.get("deposit_status"),
            "deposit": p["deposit"],
            "fixed_alloc": p.get("fixed_alloc", 0.0),
            "by_kind": bk,                      # 这一单的成本分项（供"成本明细"逐单展开）
        })
    tot = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in tot.items()}
    tot["order_cnt"] = len(orders)
    tot["by_kind"] = {k: round(v, 2) for k, v in by_kind.items()}
    _mbase = tot["revenue"] - tot["delivery"]
    tot["margin_base"] = round(_mbase, 2)
    tot["margin"] = round(tot["profit"] / _mbase * 100, 1) if _mbase > 0 else 0.0
    tot["avg_margin"] = round(sum(o["margin"] for o in orders) / len(orders), 1) if orders else 0.0
    fixed_monthly = float(cfg.get("fixed_monthly", 0) or 0)
    tot["fixed_monthly"] = fixed_monthly
    if fixed_monthly > 0:
        base = float(cfg.get("fixed_orders_per_month") or 0) or float(max(1, len(orders)))
        tot["fixed_alloc_per_order"] = round(fixed_monthly / base, 2)
    else:
        tot["fixed_alloc_per_order"] = 0.0
    tot["margin_alert"] = cfg.get("margin_alert", 40)
    return {"totals": tot, "orders": orders,
            "config_used": {"places": cfg.get("places"), "defaults": cfg.get("defaults"),
                            "people_per_unit": cfg.get("people_per_unit"),
                            "fixed_monthly": fixed_monthly,
                            "fixed_orders_per_month": cfg.get("fixed_orders_per_month"),
                            "margin_alert": cfg.get("margin_alert")}}


# ---------- 实际人工（按天日结，均摊到当天每一单） ----------
_LABOR_KW = ("兼职", "工资", "人工", "帮工", "小时工", "临工", "小工", "劳务")


def real_labor_by_date(db, date_from=None, date_to=None):
    """按天汇总"实际兼职/人工"支出：cost_kind='labor' 或 关键词命中（item_name/merchant/note/purpose/cat1）。
    返回 {use_date: 当天人工总额}。用于把"日结兼职工资"均摊到当天每一单，得到真实到手利润。"""
    kw_or, kw_p = "", []
    for k in _LABOR_KW:
        kw_or += " OR item_name LIKE ? OR merchant LIKE ? OR note LIKE ? OR purpose LIKE ? OR cat1 LIKE ?"
        kw_p += ["%" + k + "%"] * 5
    sql = ("SELECT use_date AS d, COALESCE(SUM(amount),0) AS a FROM expenses "
           "WHERE deleted_at IS NULL AND (cost_kind='labor'" + kw_or + ")")
    params = list(kw_p)
    if date_from:
        sql += " AND use_date >= ?"; params.append(date_from)
    if date_to:
        sql += " AND use_date <= ?"; params.append(date_to)
    sql += " GROUP BY use_date"
    return {r["d"]: float(r["a"] or 0) for r in db.execute(sql, params).fetchall()}


def period_real_profit(db, date_from=None, date_to=None, cfg=None, extra_clause="", extra_params=()):
    """周期真实利润：与 period_overview 同口径、同返回结构，
    唯一差别——人工用「支出台账里实际记的日结工资 ÷ 当天单数」替换掉配置估算值。
    食材=实际消耗（order_profit 已含）；配送/搭建=地址规则（特殊情况你手填的优先）；人工=实际。

    返回结构里 totals 额外带：
      labor_recorded   实际人工总额（你真的转出去的钱）
      labor_days       有录人工的天数
      order_days       有订单的天数
      labor_missing    有单但没录人工的天数（数据缺口提示）
    """
    cfg = cfg or get_config(db)
    cur = db.cursor()
    clause = "o.status!='cancelled' AND o.deleted_at IS NULL"
    params = []
    if date_from:
        clause += " AND o.booking_date >= ?"; params.append(date_from)
    if date_to:
        clause += " AND o.booking_date <= ?"; params.append(date_to)
    if extra_clause:
        clause += " " + extra_clause
        params.extend(extra_params)
    rows = cur.execute(
        "SELECT o.id, o.created_by, o.booking_date, u.name AS created_by_name FROM orders o LEFT JOIN users u ON u.id=o.created_by WHERE %s ORDER BY o.booking_date ASC, o.id ASC" % clause,
        params).fetchall()

    labor_by_date = real_labor_by_date(db, date_from, date_to)
    cnt_by_date = {}
    for r in rows:
        d = r["booking_date"] or ""
        cnt_by_date[d] = cnt_by_date.get(d, 0) + 1

    orders, tot = [], {"revenue": 0.0, "food": 0.0, "other": 0.0, "cost": 0.0,
                       "profit": 0.0, "net": 0.0, "loss": 0, "warn": 0, "delivery": 0.0,
                       "food_cost": 0.0, "labor_cost": 0.0, "outsource_cost": 0.0,
                       "fuel_cost": 0.0, "consume_cost": 0.0, "other_cost": 0.0,
                       "fixed_alloc": 0.0}
    by_kind = {}
    labor_days = set()
    order_days = set()
    for r in rows:
        p = order_profit(db, r["id"], cfg)
        if not p:
            continue
        date = p["booking_date"]
        order_days.add(date)
        food = next((i["amount"] for i in p["items"] if i["kind"] == "food"), 0.0)
        # —— 核心：把估算人工换成「实际日结 ÷ 当天单数」 ——
        day_labor = labor_by_date.get(date, 0.0)
        day_cnt = cnt_by_date.get(date, 1) or 1
        real_labor = round(day_labor / day_cnt, 2)
        if date in labor_by_date:
            labor_days.add(date)
        new_items, cost_total, bk = [], 0.0, {}
        for it in p["items"]:
            if it["kind"] == "labor":
                it = dict(it)
                it["amount"] = real_labor
                it["auto"] = real_labor
                it["overridden"] = True
                it["detail"] = "实际日结 ¥%g ÷ %d 单" % (day_labor, day_cnt)
            cost_total += float(it["amount"] or 0)
            bk[it["kind"]] = round(bk.get(it["kind"], 0.0) + float(it["amount"] or 0), 2)
            new_items.append(it)
        cost_total = round(cost_total, 2)
        cash_profit = round(p["revenue"] - cost_total, 2)
        margin_base = p["margin_base"]
        margin = round(cash_profit / margin_base * 100, 1) if margin_base > 0 else 0.0
        alert_pct = cfg.get("margin_alert", 40) or 0
        if cash_profit < 0:
            level = "loss"
        elif margin < alert_pct:
            level = "warn"
        else:
            level = "ok"
        fixed_alloc = p.get("fixed_alloc", 0.0)
        net_profit = round(cash_profit - fixed_alloc, 2)
        # 汇总
        tot["revenue"] += p["revenue"]
        tot["food"] += food
        tot["other"] += round(cost_total - food, 2)
        tot["cost"] += cost_total
        tot["profit"] += cash_profit
        tot["net"] += net_profit
        tot["delivery"] += p.get("delivery_passthrough", 0.0)
        tot["food_cost"] += bk.get("food", 0.0)
        tot["labor_cost"] += bk.get("labor", 0.0)
        tot["outsource_cost"] += bk.get("outsource", 0.0)
        tot["fuel_cost"] += bk.get("fuel", 0.0)
        tot["consume_cost"] += bk.get("consume", 0.0)
        tot["other_cost"] += bk.get("other", 0.0)
        tot["fixed_alloc"] += fixed_alloc
        if level == "loss":
            tot["loss"] += 1
        elif level == "warn":
            tot["warn"] += 1
        for k, v in bk.items():
            by_kind[k] = round(by_kind.get(k, 0.0) + v, 2)
        orders.append({
            "id": p["order_id"], "created_by": r["created_by"],
            "created_by_name": r["created_by_name"] or "",
            "booking_date": date, "contact_name": p["contact_name"],
            "address": p["address"], "revenue": p["revenue"], "food": food, "other": round(cost_total - food, 2),
            "cost_total": cost_total, "profit": cash_profit, "margin": margin,
            "net_profit": net_profit, "alert": level,
            "package_label": p.get("package_label", ""),
            "setup": p.get("setup"),
            "payment_status": p["payment_status"], "deposit_status": p.get("deposit_status"),
            "deposit": p["deposit"], "fixed_alloc": fixed_alloc, "by_kind": bk,
        })
    tot = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in tot.items()}
    tot["order_cnt"] = len(orders)
    tot["by_kind"] = {k: round(v, 2) for k, v in by_kind.items()}
    _mbase = tot["revenue"] - tot["delivery"]
    tot["margin_base"] = round(_mbase, 2)
    tot["margin"] = round(tot["profit"] / _mbase * 100, 1) if _mbase > 0 else 0.0
    tot["avg_margin"] = round(sum(o["margin"] for o in orders) / len(orders), 1) if orders else 0.0
    fixed_monthly = float(cfg.get("fixed_monthly", 0) or 0)
    tot["fixed_monthly"] = fixed_monthly
    if fixed_monthly > 0:
        base = float(cfg.get("fixed_orders_per_month") or 0) or float(max(1, len(orders)))
        tot["fixed_alloc_per_order"] = round(fixed_monthly / base, 2)
    else:
        tot["fixed_alloc_per_order"] = 0.0
    tot["margin_alert"] = cfg.get("margin_alert", 40)
    tot["labor_recorded"] = round(sum(labor_by_date.values()), 2)
    tot["labor_days"] = len(labor_days)
    tot["order_days"] = len(order_days)
    tot["labor_missing"] = len(order_days - labor_days)
    return {"totals": tot, "orders": orders,
            "config_used": {"places": cfg.get("places"), "defaults": cfg.get("defaults"),
                            "people_per_unit": cfg.get("people_per_unit"),
                            "fixed_monthly": fixed_monthly,
                            "fixed_orders_per_month": cfg.get("fixed_orders_per_month"),
                            "margin_alert": cfg.get("margin_alert")}}
