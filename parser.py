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
    """从字符串中提取日期并归一化成 YYYY-MM-DD。
    支持（可带后缀时间/文字）：
      9.14 / 9-14 / 09.14 / 9月14日 / 9月14号 / 9.14号 / 2026-09-14
    缺少年份时用当前年份。用 search 提取首个日期模式，容忍后面跟着的"12点/下午"等。
    """
    import datetime
    s = str(s).strip()
    if not s:
        return ""
    # 完整 YYYY-MM-DD（可带后缀）
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # M.D / M-D / M月D日 / M月D号 / M.D号（容忍"号/日"及后缀文字）
    m = re.search(r"(\d{1,2})[.\-/月](\d{1,2})[号日]?", s)
    if m:
        year = datetime.date.today().year
        return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


def _extract_time(s):
    """从字符串中提取时间，归一化成 HH:MM。
    支持：12.00 / 12:00 / 12点 / 12点30分 / 下午3点
    """
    s = str(s).strip()
    if not s:
        return ""
    # HH:MM 或 HH.MM
    m = re.search(r"(\d{1,2})[:.点](\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    # 仅小时：12点 / 12点整
    m = re.search(r"(\d{1,2})\s*点", s)
    if m:
        return f"{int(m.group(1)):02d}:00"
    return ""


def _build_time(hhmm_h, hhmm_m, h_only, h_only_m, pm_h):
    """根据组合正则捕获组构造 HH:MM 时间字符串。
    分别对应：HH:MM/HH.MM 的时分 / HH点 / HH点MM分 / 下午HH点
    """
    if hhmm_h:
        return f"{int(hhmm_h):02d}:{int(hhmm_m or 0):02d}"
    if h_only:
        return f"{int(h_only):02d}:{int(h_only_m or 0):02d}"
    if pm_h:
        # 下午 HH 点 → 12+HH（13~23）
        h = int(pm_h) + 12
        if h >= 24:
            h = int(pm_h)
        return f"{h:02d}:00"
    return ""


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
            # 容忍多种格式：9.11 12.00 / 9月11日12点 / 9.11号 12:00 / 9月11号下午
            # 用组合正则一次性提取日期+时间，避免日期剥离误吃时间（"9.11"与"12.00"模式相同）
            # 日期：YYYY-M-D / M.D / M-D / M月D[日号] / M.D号
            # 时间（可选，跟在日期后）：HH:MM / HH.MM / HH点[MM分] / 下午HH点
            import datetime as _dt
            today = _dt.date.today()
            # 1) 完整年月日 + 可选时间
            m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?:[号日]?)?\s*(?:(\d{1,2})[:.点](\d{1,2})|(\d{1,2})\s*点(?:\s*(\d{1,2})\s*分)?|下午(\d{1,2})\s*点)?", value)
            if m and m.group(1) and len(m.group(1)) == 4:
                result["booking_date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                result["booking_time"] = _build_time(m.group(4), m.group(5), m.group(6), m.group(7), m.group(8))
            else:
                # 2) 月日 + 可选时间
                m = re.search(r"(\d{1,2})[.\-/月](\d{1,2})[号日]?\s*(?:(\d{1,2})[:.点](\d{1,2})|(\d{1,2})\s*点(?:\s*(\d{1,2})\s*分)?|下午(\d{1,2})\s*点)?", value)
                if m:
                    result["booking_date"] = f"{today.year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
                    result["booking_time"] = _build_time(m.group(3), m.group(4), m.group(5), m.group(6), m.group(7))
                else:
                    # 3) 仅"11号"（省略月份）+ 可选时间
                    m = re.search(r"(\d{1,2})\s*号\s*(?:(\d{1,2})[:.点](\d{1,2})|(\d{1,2})\s*点)?", value)
                    if m:
                        result["booking_date"] = f"{today.year}-{today.month:02d}-{int(m.group(1)):02d}"
                        result["booking_time"] = _build_time(m.group(2), m.group(3), m.group(4), None, None)
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

    # 匹配套餐行（超过10人餐自动拆分：20→10+10, 18→10+8, 16→10+6 等）
    result["packages"] = []
    for line in result["package_lines"]:
        info = _match_package_line(line)
        if info:
            for split_info in _split_large_package(info):
                result["packages"].append(split_info)

    # 从备注解析额外工具需求
    result["extra_tools"] = _parse_extra_tools(result["note"])

    # 从备注解析额外加菜需求（单点食材）
    result["extra_ingredients"] = _parse_extra_ingredients(result["note"])

    return result


def _match_package_line(line):
    """
    解析套餐行，如 "3-4人餐 （搭建）" / "9-10人餐 x2" / "4人餐 （不搭建）"
    返回 {raw, min, max, service_type, quantity}
    """
    line = line.strip()
    # 人数范围：优先 3-4人餐，其次单数 4人餐
    m = re.search(r"(\d+)\s*[-~至到]\s*(\d+)\s*人", line)
    if m:
        min_p, max_p = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(\d+)\s*人", line)
        if not m:
            return None
        min_p = max_p = int(m.group(1))

    # 份数
    q = 1
    qm = re.search(r"[x×*]\s*(\d+)", line)
    if qm:
        q = int(qm.group(1))

    # 服务类型：先判"不搭建"避免误命中"搭建"
    service = "外送"
    if "不搭建" in line or "不自理" in line:
        service = "外送"
    else:
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


def _split_large_package(info):
    """
    人数超过10的套餐自动拆分。
    规则：拆成 10人餐 + (n-10)人餐
      20人餐 → 10人餐 + 10人餐  （两个9-10人餐）
      18人餐 → 10人餐 + 8人餐   （9-10人餐 + 7-8人餐）
      16人餐 → 10人餐 + 6人餐   （9-10人餐 + 5-6人餐）
      14人餐 → 10人餐 + 4人餐   （9-10人餐 + 3-4人餐）
      13人餐 → 10人餐 + 3人餐   （9-10人餐 + 2-3人餐）
    10人餐部分对应数据库 9-10人餐套餐(min=9,max=10)。
    返回拆分后的套餐列表，不超10则原样返回单元素列表。
    """
    max_p = info["max_people"]
    qty = info["quantity"]
    if max_p <= 10:
        return [info]

    service = info["service_type"]
    raw = info["raw"]
    remainder = max_p - 10

    # 10人餐部分 → 9-10人餐
    pkg_10 = {
        "raw": f"10人餐(拆自「{raw}」)",
        "min_people": 9,
        "max_people": 10,
        "service_type": service,
        "quantity": qty,
    }

    # 余下部分：remainder=10 → 也是9-10人餐；否则按 remainder 匹配
    if remainder >= 10:
        # 20人餐 → 10+10，余下也是10人餐
        pkg_rem = {
            "raw": f"10人餐(拆自「{raw}」)",
            "min_people": 9,
            "max_people": 10,
            "service_type": service,
            "quantity": qty,
        }
    else:
        pkg_rem = {
            "raw": f"{remainder}人餐(拆自「{raw}」)",
            "min_people": remainder,
            "max_people": remainder,
            "service_type": service,
            "quantity": qty,
        }

    return [pkg_10, pkg_rem]


# 工具简称别名（备注里常用简称 → 数据库标准名）
# 注意：只用多字别名，单字别名容易误匹配（如"桌"会匹配"天幕桌椅"）
TOOL_ALIASES = {
    "桌子": "蛋卷桌", "餐桌": "蛋卷桌", "蛋卷桌": "蛋卷桌",
    "椅子": "椅子",
    "天幕": "天幕", "棚": "天幕",
    "烤炉": "卡式炉", "炉子": "卡式炉", "卡式炉": "卡式炉",
    "烤盘": "烤盘",
    "夹子": "夹子",
    "剪刀": "剪刀",
    "燃气罐": "燃气罐", "气罐": "燃气罐",
}


# 中文数字 → 阿拉伯数字（备注里常写"一套""两把"）
CHINESE_NUM_MAP = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _num_from_str(s):
    """把 '1' 或 '一' 转成数字，失败返回 None"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    return CHINESE_NUM_MAP.get(s)


def _detect_mode(seg):
    """判断备注片段是'覆盖'(set)还是'增量'(add)。

    语义规则：
      - '拿4把椅子' / '要4把椅子' / '配4把椅子' → 总数=4（覆盖套餐默认）
      - '加1把椅子' / '多加1把' / '再加1把'      → 在套餐基础上 +1（增量）
      - '多拿1张' / '多要1个'                     → 增量
    增量关键词命中即为 add，否则为 set。
    """
    if re.search(r"(多加|再加|加|多拿|多要|多配|多带|多备)", seg):
        return "add"
    return "set"


def _parse_extra_tools(note):
    """
    从备注解析额外工具需求，支持：
      - 阿拉伯数字："多拿1张桌子，1个椅子"
      - 中文数字："配一套卡式炉"
      - 加号分隔："配一套新卡式炉+烤盘"
    返回 {工具名: {"qty": 数量, "mode": "set"|"add"}}
      mode="set" → 覆盖（总数=qty）；mode="add" → 增量（套餐基础上 +qty）
    """
    if not note:
        return {}
    # 收集每个工具的所有 (mode, qty) 条目，最后统一解析
    tool_entries = {}  # std_name -> [(mode, qty), ...]

    def _add_entry(std_name, mode, qty):
        tool_entries.setdefault(std_name, []).append((mode, qty))

    # 拆分成段：按 + ，,。；;、 空格 切分
    segments = re.split(r"[+，,。；;、\s]+", note)
    # 过滤时间相关段：含"X点""X点钟""X点左右"等时间表达的都是时间安排，
    # 不是工具数量需求（如"15点钟搭好天幕桌椅"不应被解析成15个天幕）
    segments = [s for s in segments if not re.search(r"\d+\s*点", s)]

    # 量词集合
    quantifier = "[张个把条只套份台]?"

    # 1. 先用别名匹配（能识别"桌子""椅子""烤盘"等简称）
    for alias, std_name in TOOL_ALIASES.items():
        for seg in segments:
            if alias not in seg:
                continue
            mode = _detect_mode(seg)
            n = None
            # 数字在前：1张桌子 / 一套卡式炉 / 一套新卡式炉
            m = re.search(rf"(\d+|一|两|二|三|四|五|六|七|八|九|十)\s*{quantifier}\S{{0,4}}?{re.escape(alias)}", seg)
            if m:
                n = _num_from_str(m.group(1))
            else:
                # 别名在前：桌子1张 / 卡式炉一套
                m = re.search(rf"{re.escape(alias)}\S{{0,4}}?(\d+|一|两|二|三|四|五|六|七|八|九|十)\s*{quantifier}", seg)
                if m:
                    n = _num_from_str(m.group(1))
            if n is None:
                # 没数字但有"配/带/拿"+别名 → 默认1
                if re.search(rf"(配|带|拿|加|备|要)\S{{0,3}}?{re.escape(alias)}", seg):
                    n = 1
            if n:
                _add_entry(std_name, mode, n)

    # 2. 再用数据库完整工具名兜底匹配
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM tools")
        known_tools = [r["name"] for r in cur.fetchall()]
    for tname in known_tools:
        if tname in tool_entries:
            continue
        for seg in segments:
            if tname not in seg:
                continue
            mode = _detect_mode(seg)
            n = None
            m = re.search(rf"(\d+|一|两|二|三|四|五|六|七|八|九|十)\s*{quantifier}\S{{0,4}}?{re.escape(tname)}", seg)
            if m:
                n = _num_from_str(m.group(1))
            else:
                m = re.search(rf"{re.escape(tname)}\S{{0,4}}?(\d+|一|两|二|三|四|五|六|七|八|九|十)\s*{quantifier}", seg)
                if m:
                    n = _num_from_str(m.group(1))
            if n is None and re.search(rf"(配|带|拿|加|备|要)\S{{0,3}}?{re.escape(tname)}", seg):
                n = 1
            if n:
                _add_entry(tname, mode, n)

    # 解析每个工具的最终 mode 和 qty
    result = {}
    for std_name, entries in tool_entries.items():
        has_add = any(m == "add" for m, _ in entries)
        if has_add:
            # 有任意增量 → 全部累加
            total = sum(q for _, q in entries)
            result[std_name] = {"qty": total, "mode": "add"}
        else:
            # 全是覆盖 → 取最后一个（最终总数）
            result[std_name] = {"qty": entries[-1][1], "mode": "set"}
    return result


def _parse_extra_ingredients(note):
    """从备注解析额外加菜需求，如 "加一份牛骰子，一份牛肋条"
    返回 [{name: 食材名(备注原文), qty: 份数}]
    识别模式：
      - 加一份牛骰子 / 加2份牛肋条 / 加一份新牛骰子
      - 一份牛骰子，一份牛肋条（加号/逗号/顿号分隔）
      - 多加一份牛骰子
    """
    if not note:
        return []
    results = []
    # 按加号/逗号/顿号/分号/空格切分
    segments = re.split(r"[+，,。；;、\s]+", note)
    # 过滤时间相关段（含"X点""X点钟"等时间表达，非食材需求）
    segments = [s for s in segments if not re.search(r"\d+\s*点", s)]

    for seg in segments:
        mode = _detect_mode(seg)
        # 匹配 "加一份XX" / "加2份XX" / "多加一份XX" / "一份XX"
        m = re.search(r"(?:多加|加|再加)?\s*(\d+|一|两|二|三|四|五|六|七|八|九|十)\s*[份盘份盘]?\s*(.+)", seg)
        if not m:
            # 也匹配 "XX一份" 倒装
            m2 = re.search(r"(.+?)\s*(\d+|一|两|二|三|四|五|六|七|八|九|十)\s*份", seg)
            if m2:
                n = _num_from_str(m2.group(2))
                name = m2.group(1).strip()
                # 去掉前面的"加""多加"等
                name = re.sub(r"^(多加|加|再加|配|带|拿|加一份|加两份)", "", name).strip()
                if name and len(name) >= 2 and n:
                    results.append({"name": name, "qty": n, "mode": mode})
                continue
            continue
        n = _num_from_str(m.group(1))
        name = m.group(2).strip()
        # 去掉尾部"一份""两份"等残留
        name = re.sub(r"(一份|两份|二份|三份|四份|五份|六份|七份|八份|九份|十份|1份|2份)$", "", name).strip()
        # 去掉前缀动词
        name = re.sub(r"^(新|特选|精选|奶香|原切|薄荷拌|爆汁|麻辣|葱香|蒜香|南美|沙葱|安格斯|谷饲|好|的|个|份|盘)$", "", name).strip()
        # 排除工具类（卡式炉/烤盘/桌子/椅子等已由 _parse_extra_tools 处理）
        tool_keywords = ["卡式炉", "烤盘", "桌子", "椅子", "天幕", "炉", "盘", "夹", "刷", "剪", "燃气", "气罐"]
        if any(kw in name for kw in tool_keywords):
            continue
        # 排除纯数量/无意义词
        if not name or len(name) < 2 or name in ("一份", "两份", "无", "没有", "地址", "时间"):
            continue
        if n:
            results.append({"name": name, "qty": n, "mode": mode})
    return results


def match_extra_ingredients_to_db(extra_ings):
    """把备注解析出的加菜需求匹配到数据库 ingredients 表。
    模糊匹配：备注"牛骰子" → 数据库"奶香牛骰子"。
    返回 [{matched_id, matched_name, unit, qty(份数), per_package(每份克数), total(总克数)}]
    """
    if not extra_ings:
        return []
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, unit, stock, threshold, category FROM ingredients")
        all_ings = [dict(r) for r in cur.fetchall()]
        # 查每个食材在 package_ingredients 里的典型 per_package（取最常见的）
        cur.execute("""
            SELECT ingredient_id, per_package, COUNT(*) as cnt
            FROM package_ingredients
            GROUP BY ingredient_id, per_package
            ORDER BY ingredient_id, cnt DESC
        """)
        typical_per_pkg = {}  # ingredient_id → per_package
        for r in cur.fetchall():
            if r["ingredient_id"] not in typical_per_pkg:
                typical_per_pkg[r["ingredient_id"]] = r["per_package"]

    results = []
    for ei in extra_ings:
        name = ei["name"]
        qty = ei["qty"]
        mode = ei.get("mode", "add")
        matched = None
        # 1. 精确匹配
        for ing in all_ings:
            if ing["name"] == name:
                matched = ing
                break
        # 2. 包含匹配：备注名是食材名的一部分，或食材名包含备注名
        if not matched:
            for ing in all_ings:
                if name in ing["name"] or ing["name"] in name:
                    matched = ing
                    break
        if matched:
            # 每份克数：优先用套餐里的典型 per_package，否则默认 150g（肉类）/200g（蔬菜）
            per_pkg = typical_per_pkg.get(matched["id"])
            if not per_pkg:
                cat = matched["category"] or "other"
                unit = matched["unit"] or ""
                if unit == "g":
                    per_pkg = 150 if cat in ("meat", "beef", "pork", "chicken") else 200
                else:
                    per_pkg = 1
            total = per_pkg * qty
            results.append({
                "matched_id": matched["id"],
                "matched_name": matched["name"],
                "unit": matched["unit"],
                "stock": matched["stock"],
                "threshold": matched["threshold"],
                "category": matched["category"],
                "qty": qty,
                "per_package": per_pkg,
                "total": round(total, 2),
                "mode": mode,
            })
        else:
            results.append({
                "matched_id": None,
                "matched_name": None,
                "unit": "份",
                "qty": qty,
                "per_package": 1,
                "total": qty,
                "raw_name": name,
                "mode": mode,
            })
    return results


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
