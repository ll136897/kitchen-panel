"""数据库模型与初始化（SQLite）"""
import sqlite3
import os
from contextlib import closing

# 默认用项目目录下的 kitchen.db；可用环境变量 KITCHEN_DB_PATH 指向别的库
# （部署前自检 predeploy_check.py 会指向一份副本，这样既不动线上库、又能测真实数据量）
DB_PATH = os.environ.get("KITCHEN_DB_PATH") or os.path.join(os.path.dirname(__file__), "kitchen.db")


class _ClosingConn(sqlite3.Connection):
    """会自己关闭的连接（2026-09-28 加）

    背景（踩过的坑）：sqlite3 的 `with conn:` **只负责提交/回滚事务，不会关闭连接**。
    代码里到处是 `with get_db() as conn:` 的写法，于是每调用一次就漏一个连接 +
    一个文件句柄，越积越多：先是变慢，攒到进程句柄上限后 sqlite 连不上库，
    before_request 一炸 → **全站所有路由一起 500**（2026-09-28 下午那次事故）。
    这里给连接加"退出即关闭"的行为，老写法 `with get_db() as conn:` 自动修好，
    不用去改那十几处调用点。
    """
    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            try:
                self.close()
            except Exception:
                pass


def get_db():
    """获取数据库连接（用完要关：`with get_db() as conn:` 退出时已自动关闭）

    关键：设 busy_timeout=5000，让并发写（Render 多进程 gunicorn）遇到写锁时
    **排队等 5 秒**而不是立刻抛 'database is locked'。这是线上改密码 500 的根因之一。
    """
    conn = sqlite3.connect(DB_PATH, factory=_ClosingConn, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def purchase_defaults(unit):
    """食材"采购口径"的默认值：克→公斤、毫升→升（1:1000），其余单位 1:1。
    新建食材时用它填默认值，保证界面永远有合理的采购单位可显示。"""
    u = (unit or "").strip()
    if u == "g":
        return "公斤", 1000.0
    if u == "ml":
        return "升", 1000.0
    return u, 1.0


def init_db():
    """初始化表结构"""
    with closing(get_db()) as conn:
        cur = conn.cursor()
        # 食材库
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ingredients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                unit TEXT NOT NULL,              -- 单位：克/份/个/瓶
                stock REAL NOT NULL DEFAULT 0,    -- 当前库存
                threshold REAL NOT NULL DEFAULT 0,-- 预警阈值
                cost REAL NOT NULL DEFAULT 0,      -- 单位成本（用于定价）
                category TEXT NOT NULL DEFAULT 'other'  -- meat/vegetable/staple/side/sauce/drink/tableware
            )
        """)
        # 迁移：老库补 category 字段
        cols = [r[1] for r in cur.execute("PRAGMA table_info(ingredients)").fetchall()]
        if "category" not in cols:
            cur.execute("ALTER TABLE ingredients ADD COLUMN category TEXT NOT NULL DEFAULT 'other'")

        # 迁移：食材"采购口径"（按公斤/斤/袋填价）+ "每份克数"
        #   为什么加这三个：
        #     · 用户脑子里是采购价（"牛肋条 105 一公斤"），而系统里存的是元/克（0.105），
        #       录入和核对都不顺手 → 记下采购单位与折算系数，界面按采购口径录入/显示。
        #     · "份"是固定克数（配方里 用量=份数×每份克数，同一食材各套餐一致），
        #       把每份克数显式存下来，才能显示"每份成本""库存≈几份"。
        #   注意：**基础单位（unit/cost）保持不变**，全部换算都在这层之上做，不影响任何计算。
        cols = [r[1] for r in cur.execute("PRAGMA table_info(ingredients)").fetchall()]
        if "purchase_unit" not in cols:
            cur.execute("ALTER TABLE ingredients ADD COLUMN purchase_unit TEXT")
        if "purchase_factor" not in cols:
            cur.execute("ALTER TABLE ingredients ADD COLUMN purchase_factor REAL")
        if "portion_grams" not in cols:
            cur.execute("ALTER TABLE ingredients ADD COLUMN portion_grams REAL")

        # 工具库
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                stock REAL NOT NULL DEFAULT 0,
                threshold REAL NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0          -- 单位成本（用于损耗计算）
            )
        """)
        # 迁移：老库补 cost 字段
        cols = [r[1] for r in cur.execute("PRAGMA table_info(tools)").fetchall()]
        if "cost" not in cols:
            cur.execute("ALTER TABLE tools ADD COLUMN cost REAL NOT NULL DEFAULT 0")

        # 采购记录（进货入库带单价）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT NOT NULL,              -- ingredient/tool
                item_id INTEGER NOT NULL,
                quantity REAL NOT NULL,               -- 采购数量（基础单位：克/个）
                unit_price REAL NOT NULL,             -- 采购单价（基础单位）
                total_cost REAL NOT NULL,             -- 总金额 = quantity * unit_price
                supplier TEXT,                        -- 供应商
                note TEXT,
                purchased_at TEXT DEFAULT (datetime('now','localtime')),
                purchase_qty REAL,                    -- 原始录入口径的数量（如 5 公斤）
                purchase_unit TEXT                    -- 原始录入口径的单位（公斤/斤/袋）
            )
        """)
        # 迁移：老库补"原始采购口径"两列（只用于显示，算账仍用上面的基础单位）
        cols = [r[1] for r in cur.execute("PRAGMA table_info(purchases)").fetchall()]
        if "purchase_qty" not in cols:
            cur.execute("ALTER TABLE purchases ADD COLUMN purchase_qty REAL")
        if "purchase_unit" not in cols:
            cur.execute("ALTER TABLE purchases ADD COLUMN purchase_unit TEXT")

        # 全局设置（配送费等）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                note TEXT
            )
        """)
        # 套餐定义
        cur.execute("""
            CREATE TABLE IF NOT EXISTS packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                min_people INTEGER NOT NULL,
                max_people INTEGER NOT NULL,
                base_price REAL NOT NULL DEFAULT 0, -- 菜品/套餐本身价格（不含配送费）
                price REAL NOT NULL DEFAULT 0,      -- 对外总价 = base_price + delivery_fee
                service_type TEXT,
                is_team INTEGER NOT NULL DEFAULT 0
            )
        """)

        # 迁移：老库没有 base_price 字段时补一下
        cols = [r[1] for r in cur.execute("PRAGMA table_info(packages)").fetchall()]
        if "base_price" not in cols:
            cur.execute("ALTER TABLE packages ADD COLUMN base_price REAL NOT NULL DEFAULT 0")
            cur.execute("UPDATE packages SET base_price = price WHERE base_price = 0")
            cur.execute("UPDATE packages SET price = base_price + COALESCE((SELECT CAST(value AS REAL) FROM settings WHERE key='delivery_fee'), 0)")
        # 迁移：加兼职人工成本字段（后厨+配送，每单固定）
        if "kitchen_labor_cost" not in cols:
            cur.execute("ALTER TABLE packages ADD COLUMN kitchen_labor_cost REAL NOT NULL DEFAULT 0")
        if "delivery_labor_cost" not in cols:
            cur.execute("ALTER TABLE packages ADD COLUMN delivery_labor_cost REAL NOT NULL DEFAULT 0")
        # 老库没有 settings 表已在上面建过
        cur.execute("INSERT OR IGNORE INTO settings (key,value,note) VALUES ('delivery_fee','100','基础配送费，总价=base_price+delivery_fee')")

        # 套餐→食材关联（每套餐固定配量，不是按人数线性放大）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS package_ingredients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_id INTEGER NOT NULL,
                ingredient_id INTEGER NOT NULL,
                per_package REAL NOT NULL,        -- 每套餐的食材总用量
                portion_count REAL NOT NULL DEFAULT 1,  -- 份数(如100g×2=200g, portion_count=2)
                cost_only INTEGER NOT NULL DEFAULT 0,   -- 0=正常备餐项 1=仅成本损耗(如摆盘生菜不上备餐表)
                FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE,
                FOREIGN KEY (ingredient_id) REFERENCES ingredients(id)
            )
        """)
        # 迁移：老库补 portion_count / cost_only 字段
        cols = [r[1] for r in cur.execute("PRAGMA table_info(package_ingredients)").fetchall()]
        if "portion_count" not in cols:
            cur.execute("ALTER TABLE package_ingredients ADD COLUMN portion_count REAL NOT NULL DEFAULT 1")
        if "cost_only" not in cols:
            cur.execute("ALTER TABLE package_ingredients ADD COLUMN cost_only INTEGER NOT NULL DEFAULT 0")

        # 套餐→工具关联（每份套餐用量）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS package_tools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_id INTEGER NOT NULL,
                tool_id INTEGER NOT NULL,
                per_package REAL NOT NULL DEFAULT 1,
                FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE,
                FOREIGN KEY (tool_id) REFERENCES tools(id)
            )
        """)

        # 单点菜品
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dishes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                price REAL NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0
            )
        """)

        # 单点菜品→食材关联
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dish_ingredients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dish_id INTEGER NOT NULL,
                ingredient_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                FOREIGN KEY (dish_id) REFERENCES dishes(id) ON DELETE CASCADE,
                FOREIGN KEY (ingredient_id) REFERENCES ingredients(id)
            )
        """)

        # 订单
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_text TEXT NOT NULL,           -- 原始微信文本
                booking_date TEXT,                 -- 预约日期
                booking_time TEXT,                -- 预约时间
                address TEXT,                     -- 预约地址
                contact_name TEXT,                 -- 联系人
                contact_phone TEXT,                -- 联系电话
                amount REAL,                      -- 金额
                deposit REAL,                     -- 押金
                meal_time TEXT,                   -- 用餐时间
                pickup_time TEXT,                 -- 收餐时间
                note TEXT,                        -- 备注
                status TEXT DEFAULT 'pending',    -- pending/preparing/done
                payment_status TEXT DEFAULT 'unpaid',  -- unpaid/paid/partial 货款状态
                deposit_status TEXT DEFAULT 'pending', -- pending/returned/forfeited 押金状态
                created_at TEXT DEFAULT (datetime('now','localtime')),
                deleted_at TEXT,                   -- 回收站：软删除时间，NULL=正常
                discount REAL NOT NULL DEFAULT 0,  -- 优惠/折扣：正数=优惠，负数=加收
                stock_deducted_at TEXT             -- 已按本单出库(扣库存)的时间，NULL=未出库
            )
        """)
        # 迁移：老库补 payment_status / deposit_status / deleted_at（回收站）/ discount（优惠）/ stock_deducted_at（出库）
        cols = [r[1] for r in cur.execute("PRAGMA table_info(orders)").fetchall()]
        if "payment_status" not in cols:
            cur.execute("ALTER TABLE orders ADD COLUMN payment_status TEXT DEFAULT 'unpaid'")
        if "deposit_status" not in cols:
            cur.execute("ALTER TABLE orders ADD COLUMN deposit_status TEXT DEFAULT 'pending'")
        if "deleted_at" not in cols:
            cur.execute("ALTER TABLE orders ADD COLUMN deleted_at TEXT")
        if "stock_deducted_at" not in cols:
            cur.execute("ALTER TABLE orders ADD COLUMN stock_deducted_at TEXT")
        if "discount" not in cols:
            cur.execute("ALTER TABLE orders ADD COLUMN discount REAL NOT NULL DEFAULT 0")
        # 实际录入人（2026-10-02 晚）：谁在系统里敲进这一单，纯审计用。
        # 和 created_by 的区别：created_by = **署名**（这单算谁的，可以在录入时选给另一个人，
        # 也可以事后改），entered_by = 真正操作的人（代对方录单时两者不同）。
        # 权限只看署名（created_by）；entered_by 只在订单详情里提示"由谁代录"。
        if "entered_by" not in cols:
            cur.execute("ALTER TABLE orders ADD COLUMN entered_by INTEGER")
        if "entered_by_name" not in cols:
            cur.execute("ALTER TABLE orders ADD COLUMN entered_by_name TEXT")
        # 老数据：created_by 当初既是"录入人"也是"归属"，两者补成一致
        # （放在下面 created_by 建好之后再补，见后文）
        # 署名（2026-10-02）：订单归属到"谁的署名"，用于权限（只能改自己署名的单）+ 独立统计
        _was_created_by = "created_by" in cols
        if not _was_created_by:
            cur.execute("ALTER TABLE orders ADD COLUMN created_by INTEGER")
        if "created_by_name" not in cols:
            cur.execute("ALTER TABLE orders ADD COLUMN created_by_name TEXT")
        # 首次加这个字段时，把历史订单统一归到第一个老板账号名下：
        # 这样店员改不到这些单，老板仍全权处理；之后老板可在订单页把某单「改录入人」转给店员。
        # （新库此刻 users 表还没建 → 抛错被忽略，正常）
        if not _was_created_by:
            try:
                _boss = cur.execute(
                    "SELECT id, name FROM users WHERE role='boss' ORDER BY id LIMIT 1").fetchone()
                if _boss:
                    cur.execute(
                        "UPDATE orders SET created_by=?, created_by_name=? WHERE created_by IS NULL",
                        (_boss["id"], (_boss["name"] or "老板")))
                    print("[迁移] 历史订单已归到老板名下：%s" % (_boss["name"] or _boss["id"]))
            except Exception as _e:
                print("[迁移] 历史订单归属跳过：", _e)
        # 老数据的 entered_by 补成和署名一致（当初两者就是一个字段）
        try:
            cur.execute("UPDATE orders SET entered_by=created_by, entered_by_name=created_by_name "
                        "WHERE entered_by IS NULL AND created_by IS NOT NULL")
        except Exception as _e2:
            print("[迁移] entered_by 回填跳过：", _e2)

        # 订单→套餐明细
        cur.execute("""
            CREATE TABLE IF NOT EXISTS order_packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                package_id INTEGER NOT NULL,
                people INTEGER NOT NULL,          -- 实际人数（取max）
                quantity INTEGER NOT NULL DEFAULT 1, -- 份数
                price REAL,                       -- 本单该套餐单价（可改；NULL=用套餐标准价）
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
                FOREIGN KEY (package_id) REFERENCES packages(id)
            )
        """)
        # 迁移：老库补 price 字段（每单可单独改套餐单价）
        _opc = [r[1] for r in cur.execute("PRAGMA table_info(order_packages)").fetchall()]
        if "price" not in _opc:
            cur.execute("ALTER TABLE order_packages ADD COLUMN price REAL")

        # 订单→单点明细
        cur.execute("""
            CREATE TABLE IF NOT EXISTS order_dishes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                dish_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
                FOREIGN KEY (dish_id) REFERENCES dishes(id)
            )
        """)

        # 库存调整记录（补货/报损）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT NOT NULL,          -- ingredient/tool
                item_id INTEGER NOT NULL,
                delta REAL NOT NULL,              -- 正=入库/退回  负=出库/报损
                reason TEXT,
                ref TEXT,                         -- 来源标记，如 order:123（便于按单撤销出库）
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # 迁移：老库补 ref 字段
        _lc = [r[1] for r in cur.execute("PRAGMA table_info(stock_logs)").fetchall()]
        if "ref" not in _lc:
            cur.execute("ALTER TABLE stock_logs ADD COLUMN ref TEXT")

        # 工具借出归还记录（户外烤肉工具要回收）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tool_loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                tool_id INTEGER NOT NULL,
                quantity REAL NOT NULL,           -- 借出数量
                returned_qty REAL NOT NULL DEFAULT 0,  -- 已归还
                lost_qty REAL NOT NULL DEFAULT 0,      -- 丢失/损坏
                status TEXT NOT NULL DEFAULT 'borrowed', -- borrowed/returned/lost/partial
                note TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                returned_at TEXT,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
                FOREIGN KEY (tool_id) REFERENCES tools(id)
            )
        """)

        # 订单成本项（利润计算用）：只存"手工改过/额外加"的项，没存的按配置自动算
        cur.execute("""
            CREATE TABLE IF NOT EXISTS order_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                kind TEXT NOT NULL,               -- outsource/fuel/labor/consume/other
                name TEXT,                        -- other 自定义项显示名
                qty REAL,                         -- 数量（外包单数等）
                unit_price REAL,                  -- 单价
                amount REAL NOT NULL DEFAULT 0,   -- 金额
                note TEXT,
                updated_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_order_costs_oid ON order_costs(order_id)")

        # 备餐勾选清单（后厨在线打勾用）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS prep_checklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                item_type TEXT NOT NULL,          -- ingredient/tool
                item_id INTEGER NOT NULL,
                quantity REAL NOT NULL,
                checked INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            )
        """)

        # 账号与角色（分权限用）：role = 'boss'(老板/合伙人，全权限) | 'staff'(店员/帮手)
        #   店员只看：订单 / 备餐 / 库存，不能进财务、设置、菜单。
        #   active=0 表示被禁用（等于删号，但保留历史记录归属）。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'staff',
                pw_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT
            )
        """)

        # 一次性迁移（2026-10-02）：把合伙人账号从 staff 升为 partner。
        # 用户明确要求：孙梦是**合伙人**，所有页面都能看、功能都能用，
        # 只是不能改「原数据」和老板录的订单 —— 这跟"店员(staff)"不是一回事。
        # 只按用户名精确匹配，一次生效；以后新增的用户不受影响（可在「账号」页选角色）。
        try:
            cur.execute("UPDATE users SET role='partner' WHERE username='sunmeng' AND role='staff'")
            if cur.rowcount:
                print("[迁移] 已把 sunmeng 升级为 partner（合伙人）")
        except Exception as _e:
            print("[迁移] 合伙人角色升级跳过：", _e)

        # 一次性迁移（2026-10-02）："署名"维度只用两个人名区分 —— 刘（老板）、孙（合伙人）。
        # ⚠️ 刘和牛是**门店名**，不是人名，绝不能当账号显示名用（之前误用过，已纠正）。
        # 只改账号的 name（id 不变 → 历史订单的 created_by 不变 → 筛选/权限完全不受影响），
        # 绝不批量改写历史订单的 created_by_name 快照（那等于篡改历史数据，用户明确不要）。
        try:
            cur.execute("UPDATE users SET name='刘' WHERE username='admin' AND name<>'刘'")
            if cur.rowcount:
                print("[迁移] 已把老板账号显示名规范为 刘")
        except Exception as _e:
            print("[迁移] 老板账号改名跳过：", _e)
        try:
            cur.execute("UPDATE users SET name='孙' WHERE username='sunmeng' AND name<>'孙'")
            if cur.rowcount:
                print("[迁移] 已把合伙人账号显示名规范为 孙")
        except Exception as _e:
            print("[迁移] 合伙人账号改名跳过：", _e)

        # 默认设置：备餐提前小时数
        cur.execute("INSERT OR IGNORE INTO settings (key,value,note) VALUES ('prep_lead_hours','2','备餐提前小时数，用餐时间-该值=应开始备餐时间')")

        # ---- 收尾回填（必须放在所有表都建好之后）----
        backfill_purchase(cur)

        conn.commit()
    print(f"[OK] 数据库已初始化: {DB_PATH}")


def backfill_purchase(cur):
    """回填"采购口径"三列（幂等，只补空的）。

    ⚠️ 必须在**所有表建好之后**调（`package_ingredients` 比 `ingredients` 晚建）；
    而且**新建库走 seed 造完食材后要再调一次**——否则那些食材的 purchase_unit 一直是空
    （init_db 时表还是空的，回填等于没跑）。
    """
    # 食材的采购口径：g→公斤、ml→升（系数 1000），其余（个/瓶/包/份…）1:1
    cur.execute("""UPDATE ingredients SET purchase_unit =
                     CASE WHEN unit='g' THEN '公斤' WHEN unit='ml' THEN '升' ELSE unit END
                   WHERE purchase_unit IS NULL OR purchase_unit=''""")
    cur.execute("""UPDATE ingredients SET purchase_factor =
                     CASE WHEN unit='g' THEN 1000 WHEN unit='ml' THEN 1000 ELSE 1 END
                   WHERE purchase_factor IS NULL OR purchase_factor<=0""")
    # 每份克数：取配方里"套餐用量 ÷ 份数"（同一食材各套餐一致）
    cur.execute("""
        UPDATE ingredients SET portion_grams = (
            SELECT ROUND(pi.per_package * 1.0 / NULLIF(pi.portion_count, 0), 2)
            FROM package_ingredients pi
            WHERE pi.ingredient_id = ingredients.id AND pi.portion_count > 0
            ORDER BY pi.id LIMIT 1
        )
        WHERE unit='g' AND (portion_grams IS NULL OR portion_grams<=0)
    """)


if __name__ == "__main__":
    init_db()
