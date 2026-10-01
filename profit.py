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
        r = cur.execute("SELECT raw_text FROM orders WHERE id=?", (oid,)).fetchone()
        raw = (r["raw_text"] if r else "") or ""
        if raw:
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


def _auto_items(db, oid, cfg):
    """自动算出的成本项（未手工覆盖前的默认值）"""
    cur = db.cursor()
    o = cur.execute("SELECT address FROM orders WHERE id=?", (oid,)).fetchone()
    address = (o["address"] if o else "") or ""
    r = cur.execute("""
        SELECT COALESCE(SUM(op.quantity),0) AS qty, COALESCE(SUM(op.people),0) AS people
        FROM order_packages op WHERE op.order_id=?
    """, (oid,)).fetchone()
    place = match_place(address, cfg)
    out_price = float((place or {}).get("outsource") if place else cfg.get("default_outsource", 100) or 0)
    units = outsource_units(r["people"], r["qty"], cfg.get("people_per_unit", 10))
    d = cfg.get("defaults", {})
    # 打包切配人工：首单 + 每多一个"单"（单价更低，单越大越省）
    l_first = float(cfg.get("labor_first", 40) or 0)
    l_per = float(cfg.get("labor_per_unit", 25) or 0)
    extra = max(0, units - 1)
    labor_auto = round(l_first + l_per * extra, 2)
    labor_detail = ("首单 ¥%g" % l_first) if extra == 0 else ("首单 ¥%g + %d 个多单 × ¥%g" % (l_first, extra, l_per))
    return {
        "food": {"amount": _food_cost(db, oid)},
        "outsource": {"amount": round(units * out_price, 2), "qty": units, "unit_price": out_price,
                      "detail": "%d单 × ¥%g（%s）" % (units, out_price, (place or {}).get("name") or "默认价")},
        "fuel": {"amount": float((place or {}).get("fuel") if place and (place or {}).get("fuel") else d.get("fuel", 0) or 0)},
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
    auto = _auto_items(db, oid, cfg)
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

    return {
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
    }


# ---------- 期间汇总 ----------
def period_overview(db, date_from=None, date_to=None, cfg=None):
    """期间利润汇总：现金口径 + 含固定开销摊销口径"""
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
    rows = cur.execute("SELECT o.id FROM orders o WHERE %s ORDER BY o.booking_date DESC, o.id DESC" % clause,
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
            "id": p["order_id"], "booking_date": p["booking_date"], "contact_name": p["contact_name"],
            "address": p["address"], "revenue": p["revenue"], "food": food, "other": other,
            "cost_total": p["cost_total"], "profit": p["cash_profit"], "margin": p["margin"],
            "net_profit": p["net_profit"], "alert": p["alert"],
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
