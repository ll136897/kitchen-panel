"""数据库模型与初始化（SQLite）"""
import sqlite3
import os
from contextlib import closing

DB_PATH = os.path.join(os.path.dirname(__file__), "kitchen.db")


def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
                quantity REAL NOT NULL,               -- 采购数量
                unit_price REAL NOT NULL,             -- 采购单价
                total_cost REAL NOT NULL,             -- 总金额 = quantity * unit_price
                supplier TEXT,                        -- 供应商
                note TEXT,
                purchased_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)

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

        # 订单→套餐明细
        cur.execute("""
            CREATE TABLE IF NOT EXISTS order_packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                package_id INTEGER NOT NULL,
                people INTEGER NOT NULL,          -- 实际人数（取max）
                quantity INTEGER NOT NULL DEFAULT 1, -- 份数
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
                FOREIGN KEY (package_id) REFERENCES packages(id)
            )
        """)

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

        # 默认设置：备餐提前小时数
        cur.execute("INSERT OR IGNORE INTO settings (key,value,note) VALUES ('prep_lead_hours','2','备餐提前小时数，用餐时间-该值=应开始备餐时间')")

        conn.commit()
    print(f"[OK] 数据库已初始化: {DB_PATH}")


if __name__ == "__main__":
    init_db()
