"""订单文本解析器：从微信确认文本提取结构化字段"""
import re
from models import get_db


def _clean(s):
    if not s:
        return ""
    return s.strip().strip(":：").strip()


def _parse_amount(text):
    """提取金额数字，去掉 元/￥"""
    if not text:
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)", str(text))
    return float(m.group(1)) if m else 0.0


def _normalize_date(s):
    """把各种日期格式归一化成 YYYY-MM-DD。
    支持：9.14 / 9-14 / 09.14 / 9月14日 / 2026-09-14
    缺少年份时用当前年份。
    """
    import datetime
    s = str(s).strip()
    if not s:
        return ""
    # 已是 YYYY-MM-DD
    m = re.match(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # M.D 或 M-D
    m = re.match(r"^(\d{1,2})[.\-/月](\d{1,2})日?$", s)
    if m:
        year = datetime.date.today().year
        return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return s


def parse_order_text(raw_text):
    """
    解析微信确认文本。
    输入示例：
        预约时间:9.11 12.00
        预约项目:
        3-4人餐 （搭建）
        预约地址:青龙湖
        联系人:刘和牛
        联系电话:15108302848
        金额:458元（餐到后付全款）
        押金:200元（餐具回收后退还）
        用餐时间:12.00
        收餐时间:22点后加收每小时50元夜间服务费
        备注:多拿1张桌子，1个椅子，10点钟搭好天幕桌椅，餐食12点左右送到。
    """
    result = {
        "raw_text": raw_text,
        "booking_date": "",
        "booking_time": "",
        "address": "",
        "contact_name": "",
        "contact_phone": "",
        "amount": 0.0,
        "deposit": 0.0,
        "meal_time": "",
        "pickup_time": "",
        "note": "",
        "package_lines": [],   # 套餐明细行（原始文本）
        "extra_tools": {},     # 从备注解析出的额外工具需求
    }

    text = raw_text.replace("\r\n", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    current_key = None
    buffer = []
    sections = []  # [(key, value_lines)]

    for ln in lines:
        # 匹配 "字段:值" 或 "字段：值"
        m = re.match(r"^(预约时间|预约项目|预约地址|联系人|联系电话|金额|押金|用餐时间|收餐时间|备注)\s*[:：]\s*(.*)$", ln)
        if m:
            # 保存上一个字段
            if current_key is not None:
                sections.append((current_key, buffer))
            current_key = m.group(1)
            buffer = [m.group(2)] if m.group(2) else []
        else:
            if current_key is not None:
                buffer.append(ln)
    if current_key is not None:
        sections.append((current_key, buffer))

    for key, value_lines in sections:
        value = " ".join(v for v in value_lines if v).strip()
        if key == "预约时间":
            # 9.11 12.00 / 9月11日 12点 / 09-11 12:00
            parts = re.split(r"[\s]+", value)
            if parts:
                result["booking_date"] = _normalize_date(parts[0])
            if len(parts) > 1:
                result["booking_time"] = parts[1]
        elif key == "预约项目":
            result["package_lines"] = value_lines
        elif key == "预约地址":
            result["address"] = value
        elif key == "联系人":
            result["contact_name"] = value
        elif key == "联系电话":
            result["contact_phone"] = re.sub(r"[^\d\-\+\s]", "", value)
        elif key == "金额":
            result["amount"] = _parse_amount(value)
        elif key == "押金":
            result["deposit"] = _parse_amount(value)
        elif key == "用餐时间":
            result["meal_time"] = value
        elif key == "收餐时间":
            result["pickup_time"] = value
        elif key == "备注":
            result["note"] = value

    # 匹配套餐行
    result["packages"] = []
    for line in result["package_lines"]:
        info = _match_package_line(line)
        if info:
            result["packages"].append(info)

    # 从备注解析额外工具需求
    result["extra_tools"] = _parse_extra_tools(result["note"])

    return result


def _match_package_line(line):
    """
    解析套餐行，如 "3-4人餐 （搭建）" 或 "9-10人餐 x2"
    返回 {raw, min, max, service_type, quantity}
    """
    line = line.strip()
    # 人数范围
    m = re.search(r"(\d+)\s*[-~]\s*(\d+)\s*人", line)
    if not m:
        return None
    min_p, max_p = int(m.group(1)), int(m.group(2))

    # 份数
    q = 1
    qm = re.search(r"[x×*]\s*(\d+)", line)
    if qm:
        q = int(qm.group(1))

    # 服务类型
    service = "外送"
    for kw in ["搭建", "自提", "外送"]:
        if kw in line:
            service = kw
            break

    return {
        "raw": line,
        "min_people": min_p,
        "max_people": max_p,
        "service_type": service,
        "quantity": q,
    }


# 工具简称别名（备注里常用简称 → 数据库标准名）
# 注意：只用多字别名，单字别名容易误匹配（如"桌"会匹配"天幕桌椅"）
TOOL_ALIASES = {
    "桌子": "蛋卷桌", "餐桌": "蛋卷桌", "蛋卷桌": "蛋卷桌",
    "椅子": "椅子",
    "天幕": "天幕", "棚": "天幕",
    "烤炉": "卡式炉/烤盘", "炉子": "卡式炉/烤盘", "烤盘": "卡式炉/烤盘",
    "卡式炉": "卡式炉/烤盘",
    "夹子": "夹子",
    "刷子": "刷子",
    "剪刀": "剪刀",
    "燃气罐": "燃气罐", "气罐": "燃气罐",
}


def _parse_extra_tools(note):
    """
    从备注解析额外工具需求，如 "多拿1张桌子，1个椅子"
    返回 {工具名: 数量}（用数据库标准名）
    """
    if not note:
        return {}
    tools = {}

    # 优先匹配别名（更宽松，能识别"桌子""椅子"等简称）
    for alias, std_name in TOOL_ALIASES.items():
        # 匹配 "1张桌子" "1个椅子" "桌子1张" "多拿1张桌子" 等
        patterns = [
            rf"(\d+)\s*[张个把条只]?\s*{re.escape(alias)}",
            rf"{re.escape(alias)}\s*(\d+)\s*[张个把条只]?",
        ]
        for p in patterns:
            for m in re.finditer(p, note):
                n = int(m.group(1))
                tools[std_name] = tools.get(std_name, 0) + n

    # 再匹配数据库完整工具名（兜底）
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM tools")
        known_tools = [r["name"] for r in cur.fetchall()]
    for tname in known_tools:
        if tname in tools:
            continue
        patterns = [
            rf"(\d+)\s*[张个把条只]?\s*{re.escape(tname)}",
            rf"{re.escape(tname)}\s*(\d+)\s*[张个把条只]?",
        ]
        for p in patterns:
            for m in re.finditer(p, note):
                n = int(m.group(1))
                tools[tname] = tools.get(tname, 0) + n

    return tools


def match_packages_in_db(parsed_packages):
    """
    将解析出的套餐行与数据库 packages 表匹配。
    返回 [{package_id, name, people, quantity, service_type}, ...]
    """
    matched = []
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM packages")
        all_pkgs = [dict(r) for r in cur.fetchall()]

    for pk in parsed_packages:
        # 在数据库找人数范围匹配的套餐
        found = None
        for p in all_pkgs:
            if pk["min_people"] == p["min_people"] and pk["max_people"] == p["max_people"]:
                found = p
                break
        if not found:
            # 放宽：落入某套餐范围
            for p in all_pkgs:
                if p["min_people"] <= pk["max_people"] and pk["max_people"] <= p["max_people"]:
                    found = p
                    break
        matched.append({
            "package_id": found["id"] if found else None,
            "name": found["name"] if found else f"{pk['min_people']}-{pk['max_people']}人餐(未配置)",
            "people": pk["max_people"],   # 取最大人数
            "quantity": pk["quantity"],
            "service_type": pk["service_type"],
            "raw": pk["raw"],
        })
    return matched


if __name__ == "__main__":
    sample = """预约时间:9.11 12.00
预约项目:
3-4人餐 （搭建）
预约地址:青龙湖
联系人:刘和牛
联系电话:15108302848
金额:458元（餐到后付全款）
押金:200元（餐具回收后退还）
用餐时间:12.00
收餐时间:22点后加收每小时50元夜间服务费
备注:多拿1张桌子，1个椅子，10点钟搭好天幕桌椅，餐食12点左右送到。"""
    import json
    print(json.dumps(parse_order_text(sample), ensure_ascii=False, indent=2))


# ============================================================
# 进货单解析：从供应商销售单文字里提取 商品名→数量
# ============================================================
# 常见单位换算到数据库标准单位
UNIT_NORMALIZE = {
    "克": "g", "g": "g", "G": "g",
    "千克": "g", "公斤": "g", "kg": "g", "KG": "g", "1kg": "g",
    "毫升": "ml", "ml": "ml", "ML": "ml",
    "瓶": "瓶",
    "个": "个", "件": "个", "只": "个",
    "份": "份",
    "袋": "袋", "包": "包",
    "张": "张",
    "双": "双",
}
# kg → g 换算倍数
UNIT_TO_GRAM = {
    "g": 1, "ml": 1,  # 近似
}


def _extract_quantity(text):
    """从 '1kg*10袋' 或 '280.00' 或 '2件' 里提取数量 + 单位"""
    text = text.strip()
    # 先看规格里有没有带数量和单位的组合，如 1kg*10袋 → 10*1000 = 10000g
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|KG|千克|公斤)\s*\*\s*(\d+)", text)
    if m:
        val = float(m.group(1)) * 1000 * float(m.group(3))
        return val, "g", True  # 规格乘起来了

    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|KG|千克|公斤)(?!\s*\*\s*\d+)", text)
    if m:
        return float(m.group(1)) * 1000, "g", True

    m = re.search(r"(\d+(?:\.\d+)?)\s*(ml|ML|毫升)", text)
    if m:
        return float(m.group(1)), "ml", True

    # 裸数字（数量列）
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1)), "", False
    return 0, "", False


def parse_stock_invoice_text(raw_text):
    """
    从进货单文本里提取 [(商品名, 数量, 单位, 规格说明), ...]
    支持格式：
      1. 表格形式（含 | 分隔或多空格）
      2. 逐行文本：商品名 + 数量
      3. 规格列带数量：如 1kg*10袋
    """
    text = raw_text.replace("\r\n", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    items = []
    in_table = any("|" in ln for ln in lines)

    for ln in lines:
        # 跳过表头/合计/空行
        if re.match(r"^(合计|制单人|客户|单据|联系人|应收|温馨|序号|$)", ln):
            continue
        if "金额" in ln and "备注" in ln:
            continue
        if ln.startswith("单据") or ln.startswith("客户"):
            continue

        if in_table:
            cols = [c.strip() for c in ln.split("|") if c.strip()]
        else:
            # 用多空格或制表符分
            cols = [c.strip() for c in re.split(r"\s{2,}|\t", ln) if c.strip()]

        if len(cols) < 2:
            continue

        # 找出商品名列（通常第2列或第1列，如果第1列是数字序号就跳）
        name_idx = 0
        if cols[0].isdigit():
            name_idx = 1

        # 找出数量列（优先找标了数量的列，其次找最后几个数字列）
        qty = None
        unit_spec = ""
        name = cols[name_idx]

        # 规格列（商品名后通常是规格）
        spec_col = cols[name_idx + 1] if (name_idx + 1 < len(cols)) else ""

        # 数量列（找 "数量" 标签或者取第 4-5 列附近的数字）
        for j in range(name_idx + 1, len(cols)):
            c = cols[j]
            # 看当前列里有没有纯数字（数量）
            m = re.fullmatch(r"(\d+(?:\.\d+)?)", c)
            if m and qty is None and j >= name_idx + 2:
                qty = float(m.group(1))

        # 如果没找到，尝试直接从规格列里提
        if qty is None and spec_col:
            val, unit, from_spec = _extract_quantity(spec_col)
            if from_spec and val > 0:
                qty = val

        if qty is None or qty <= 0:
            # 最后兜底：从整行找商品名+数量的模式
            m = re.search(r"(.+?)[（(].*?[）)][\s|]*(\d+(?:\.\d+)?)", ln)
            if m:
                name = m.group(1).strip()
                qty = float(m.group(2))
            else:
                # 真不行就跳过
                continue

        # 清理商品名：去掉括号里的标注
        clean_name = re.sub(r"[（(].*?[）)]", "", name).strip()

        items.append({
            "raw_name": name,
            "clean_name": clean_name,
            "qty": qty,
            "spec": spec_col,
        })

    return items


def match_inbound_to_db(items):
    """
    将解析出的商品与数据库 ingredients 表匹配。
    返回 [{name, qty, matched_id, matched_name, unit, stock, unit_mult, ok}]
    unit_mult：数量要乘以多少变成数据库单位（比如商品是件，每件=10袋，库单位是g）
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, unit, stock FROM ingredients")
        all_ings = {r["name"]: dict(r) for r in cur.fetchall()}

    results = []
    for it in items:
        qty = it["qty"]
        name = it["clean_name"]
        spec = it["spec"]
        matched = None
        unit_mult = 1.0  # 默认数量直接加

        # 1. 精确匹配
        if name in all_ings:
            matched = all_ings[name]
        else:
            # 2. 模糊包含
            for dname, drow in all_ings.items():
                if dname in name or name in dname:
                    matched = drow
                    break

        if matched:
            db_unit = matched["unit"]
            # 规格里带 kg 的话要 ×1000
            if db_unit == "g":
                val, unit, from_spec = _extract_quantity(spec)
                if from_spec and unit == "g":
                    # 规格已经算成 g 了，数量就是 qty × 每件的重量
                    # 比如规格 1kg*10袋，数量 1 件 → 10000g
                    # 但我们的 qty 是"件数"，规格已经包含了每件的总量
                    unit_mult = val  # 每件 = val g
                else:
                    # 没有规格里的 kg 信息，就按数量直接加（假设用户单位一致）
                    unit_mult = 1.0
            elif db_unit == "ml":
                val, unit, from_spec = _extract_quantity(spec)
                if from_spec and unit == "ml":
                    unit_mult = val
                else:
                    unit_mult = 1.0
            else:
                # 瓶/个/份 直接加
                unit_mult = 1.0

            final_qty = qty * unit_mult
            results.append({
                "raw_name": it["raw_name"],
                "clean_name": name,
                "spec": spec,
                "qty_input": qty,
                "unit_mult": unit_mult,
                "matched_id": matched["id"],
                "matched_name": matched["name"],
                "matched_unit": matched["unit"],
                "current_stock": matched["stock"],
                "final_qty": round(final_qty, 2),
                "after_stock": round(matched["stock"] + final_qty, 2),
                "ok": True,
            })
        else:
            results.append({
                "raw_name": it["raw_name"],
                "clean_name": name,
                "spec": spec,
                "qty_input": qty,
                "matched_id": None,
                "matched_name": None,
                "matched_unit": None,
                "ok": False,
                "final_qty": qty,
                "current_stock": 0,
                "after_stock": qty,
            })

    return results
