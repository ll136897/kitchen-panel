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
                threshold REAL NOT NULL DEFAULT 0
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
        # 老库没有 settings 表已在上面建过
        cur.execute("INSERT OR IGNORE INTO settings (key,value,note) VALUES ('delivery_fee','100','基础配送费，总价=base_price+delivery_fee')")

        # 套餐→食材关联（每套餐固定配量，不是按人数线性放大）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS package_ingredients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_id INTEGER NOT NULL,
                ingredient_id INTEGER NOT NULL,
                per_package REAL NOT NULL,        -- 每套餐的食材总用量
                FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE,
                FOREIGN KEY (ingredient_id) REFERENCES ingredients(id)
            )
        """)

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
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)

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
                delta REAL NOT NULL,              -- 正=补货 负=报损
                reason TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)

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
