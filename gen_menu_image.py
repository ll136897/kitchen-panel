# -*- coding: utf-8 -*-
"""生成宣传菜单图片"""
import sys, os, json
sys.path.insert(0, '_libs')

# 从数据库拿真实套餐数据
os.environ['FLASK_APP'] = 'app'
from app import app
c = app.test_client()
import io

FONT_REG = 'C:/Windows/Fonts/msyh.ttc'     # 微软雅黑 常规
FONT_BLD = 'C:/Windows/Fonts/msyhbd.ttc'    # 微软雅黑 粗体

# 获取数据
r = c.get('/api/packages')
pkgs_raw = r.get_json()['data']

# 细分肉类 & 过滤
def subcat(name, cat):
    n = name or ''
    if '馒头' in n: return 'vegetable'
    if cat != 'meat': return cat
    if '小肠' in n: return 'beef'
    if any(k in n for k in ['牛','肥牛','牛肋','牛骰','安格斯']): return 'beef'
    if any(k in n for k in ['鸡','鸡翅','鸡腿','郡肝','掌中宝','脚筋']): return 'chicken'
    if any(k in n for k in ['猪','五花','香猪','松板','梅花','风味肠','肠']): return 'pork'
    return 'beef'

SHOW_CATS = ['beef','pork','chicken','vegetable','side','sauce','drink']
CAT_NAMES = {'beef':'🥩 牛肉','pork':'🥓 猪肉','chicken':'🍗 鸡肉',
             'vegetable':'🥬 素菜','side':'🥗 小菜','sauce':'🧂 蘸料','drink':'🎁 赠饮'}

packages = []
for p in pkgs_raw:
    byCat = {}
    for ing in p['ingredients']:
        sc = subcat(ing['ing_name'], ing['category'])
        if sc not in SHOW_CATS: continue
        byCat.setdefault(sc, [])
        if ing['ing_name'] not in [x for x in byCat[sc]]:  # 去重
            byCat[sc].append(ing['ing_name'])
    packages.append({
        'name': p['name'],
        'price': int(p['base_price']),
        'min': p['min_people'], 'max': p['max_people'],
        'byCat': byCat
    })

# ===== 画图 =====
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 3200
img = Image.new('RGB', (W, H), '#1e1008')
draw = ImageDraw.Draw(img)

# 背景渐变（简单模拟）
for y in range(H):
    t = y / H
    r = int(30 + (20-30)*t)
    g = int(16 + (10-16)*t)
    b = int(8 + (5-8)*t)
    draw.line([(0,y),(W,y)], fill=(r,g,b))

# 金色装饰函数
def draw_gold_line(y, x1=60, x2=W-60, width=2):
    draw.line([(x1,y),(x2,y)], fill='#c9a04b', width=width)

def draw_card(x, y, w, h, radius=20, fill='#faf5ed', outline='#c9a04b', olw=3):
    # 圆角矩形
    draw.rounded_rectangle([x,y,x+w,y+h], radius=radius, fill=fill, outline=outline, width=olw)

# 字体
font_brand = ImageFont.truetype(FONT_BLD, 92)
font_sub = ImageFont.truetype(FONT_REG, 36)
font_pkg_name = ImageFont.truetype(FONT_BLD, 52)
font_price = ImageFont.truetype(FONT_BLD, 72)
font_cat = ImageFont.truetype(FONT_BLD, 36)
font_dish = ImageFont.truetype(FONT_REG, 32)
font_footer = ImageFont.truetype(FONT_REG, 30)

y = 80

# ===== Hero 区 =====
draw_gold_line(y, 140, W-140)
y += 40
# 品牌名
brand = '刘和牛'
bbox = draw.textbbox((0,0), brand, font=font_brand)
bw = bbox[2]-bbox[0]
draw.text(((W-bw)//2, y), brand, fill='#f5e6c8', font=font_brand)
y += 110
# 金色下划线
draw.line([(W//2-120, y),(W//2+120, y)], fill='#c9a04b', width=3)
y += 30
# 副标题
sub = '精品烤肉套餐 · 现切现送 · 新鲜直达'
bbox = draw.textbbox((0,0), sub, font=font_sub)
draw.text(((W-(bbox[2]-bbox[0]))//2, y), sub, fill='#c9a04b', font=font_sub)
y += 70
draw_gold_line(y, 140, W-140)
y += 50

# ===== 每个套餐卡片 =====
card_x = 60
card_w = W - 120

for pkg in packages:
    # 计算卡片高度
    lines_count = 0
    for cat in SHOW_CATS:
        if cat in pkg['byCat']:
            lines_count += 1  # 分类行
            # 菜名按14字左右换行
            dish_str = '，'.join(pkg['byCat'][cat])
            # 估算行数
            per_line = 18
            lines_count += max(1, (len(dish_str) + per_line - 1) // per_line)
    
    card_h = 100 + lines_count * 48 + 30
    card_h = max(card_h, 200)
    
    # 画卡片
    draw_card(card_x, y, card_w, card_h, radius=24)
    
    cy = y + 20
    
    # 套餐名 + 人数
    pkg_label = f"{pkg['name']}"
    draw.text((card_x+30, cy+8), pkg_label, fill='#2c1810', font=font_pkg_name)
    cy += 75
    
    # 金色分隔线
    draw.line([(card_x+30, cy),(card_x+card_w-30, cy)], fill='#d4b896', width=2)
    cy += 15
    
    # 价格（右上角）
    price_text = f"¥{pkg['price']}"
    pb = draw.textbbox((0,0), price_text, font=font_price)
    draw.text((card_x+card_w-(pb[2]-pb[0])-30, y+25), price_text, fill='#c0392b', font=font_price)
    
    # 分类+菜品
    for cat in SHOW_CATS:
        if cat not in pkg['byCat']: continue
        cat_label = CAT_NAMES[cat]
        dishes = pkg['byCat'][cat]
        dish_str = '，'.join(dishes)
        
        # 分类标签底色
        draw.text((card_x+30, cy), cat_label, fill='#6b4f3a', font=font_cat)
        cy += 44
        
        # 菜名（可能换行）
        x_cursor = card_x + 30
        max_w = card_w - 60
        per_line_chars = 20
        chunks = []
        for i in range(0, len(dish_str), per_line_chars):
            chunks.append(dish_str[i:i+per_line_chars])
        for chunk in chunks:
            draw.text((card_x+55, cy), chunk, fill='#3d2817', font=font_dish)
            cy += 38
        
        cy += 6
    
    # 卡片结束，更新 y
    y += card_h + 30

# ===== Footer =====
y += 10
draw_gold_line(y, 100, W-100)
y += 40

# 底部文案
footer1 = '🔥 下单即送精美餐具套装    🚗 支持全城配送'
bbox = draw.textbbox((0,0), footer1, font=font_footer)
draw.text(((W-(bbox[2]-bbox[0]))//2, y), footer1, fill='#c9a04b', font=font_footer)
y += 60

footer2 = '提前1天预订 · 扫码下单享优惠'
bbox = draw.textbbox((0,0), footer2, font=font_footer)
draw.text(((W-(bbox[2]-bbox[0]))//2, y), footer2, fill='#8a7a5a', font=font_footer)
y += 80

draw_gold_line(y, 100, W-100)

# 保存
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'promo_menu.png')
img.save(out, 'PNG', quality=95)
print(f'✓ 已生成: {out} ({W}x{H})')
