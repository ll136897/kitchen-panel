// 备餐表统一渲染：订单页 / 录入预览 / 订单详情 共用，保证"算一次、渲一处"。
// 单一真相源：后端 calc_order_requirements()。本文件只负责把同一份 req 渲成一致的表。
//
// 入参 input 两种形状（都来自 calc_order_requirements / preview_parse）：
//   已存订单：{ingredients:[{name,unit,need,stock,shortage,portion_size,per_package,total_portions,order_qty,swapped_in,category,...}], tools:[...]}
//   录入预览：{preview_ingredients:{category:[items]}, preview_tools:[...], matched_packages:[...]}
//
// opts:
//   pkgHtml  : 预生成的套餐 HTML（已存单用 fmtPkgsTotal；预览单用 matched_packages）
//   noteHtml : 预生成的备注 HTML（预览单用 noteRecogHtml）
//   note     : 原始备注字符串（自动转成备注框，二选一；与 noteHtml 互斥）
//   footer   : 追加在底部的额外 HTML（如"打开完整备餐页"按钮）
//   fixedSort: true 时启用详情页专属固定排序（与历史 order_detail 一致）
//   hideStock: true 时不显示"库存"列
(function () {
  function escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }
  // 页面若已定义 escHtml 则不覆盖，避免重复声明
  if (typeof window.escHtml !== 'function') { window.escHtml = escHtml; }

  var CAT_LABEL = {
    beef: '🥩 牛肉', pork: '🥓 猪肉', chicken: '🍗 鸡肉', vegetable: '🥬 素菜',
    side: '🥗 小菜', sauce: '🧂 小料', drink: '🎁 赠品', other: '📦 其他',
    packaging: '📦 食材包装', utensil: '🍱 客户餐具'
  };
  var CAT_COLOR = {
    beef: '#8e1e1a', pork: '#c75d3e', chicken: '#c9962b', vegetable: '#3a8a3a',
    side: '#5a8a3a', sauce: '#7a5a3a', drink: '#666', other: '#666',
    packaging: '#d35400', utensil: '#8e44ad'
  };
  var CAT_ORDER = ['beef', 'pork', 'chicken', 'vegetable', 'side', 'sauce', 'drink', 'other', 'packaging', 'utensil'];

  // 详情页专属固定排序（与历史 order_detail.html 一致：先按品类内固定名单，其余按用量降序）
  var FIXED_ORDER = [
    ['beef', '肥牛'], ['beef', '拌牛肉'], ['beef', '牛肋条'], ['beef', '牛骰子'], ['beef', '小肠'],
    ['pork', '五花肉'], ['pork', '小香猪'], ['pork', '松板肉'], ['pork', '风味肠'], ['pork', '梅花肉'],
    ['chicken', '鸡尖'], ['chicken', '郡肝'], ['chicken', '鸡腿肉'], ['chicken', '鸡翅根'], ['chicken', '掌中宝'], ['chicken', '鸡脚筋'],
    ['vegetable', '生菜'], ['vegetable', '豆腐'], ['vegetable', '西葫芦'], ['vegetable', '土豆'], ['vegetable', '馒头'], ['vegetable', '韭菜'], ['vegetable', '杏鲍菇'], ['vegetable', '洋葱'],
    ['side', '海带丝'], ['side', '辣椒段'], ['side', '辣白菜'], ['side', '蒜片'],
    ['sauce', '川香'], ['sauce', '五香'], ['sauce', '酸辣']
  ];
  function getFixedIdx(name, cat) {
    for (var i = 0; i < FIXED_ORDER.length; i++) {
      if (FIXED_ORDER[i][0] === cat && (name || '').indexOf(FIXED_ORDER[i][1]) > -1) return i;
    }
    return 999;
  }

  function normalize(input) {
    var byCat = {};
    if (Array.isArray(input.ingredients)) {
      input.ingredients.forEach(function (i) {
        var c = i.category || 'other';
        (byCat[c] = byCat[c] || []).push(i);
      });
    } else if (input.preview_ingredients) {
      // 预览单已是 {category:[items]}
      Object.keys(input.preview_ingredients).forEach(function (c) {
        byCat[c] = input.preview_ingredients[c] || [];
      });
    }
    var tools = input.tools || input.preview_tools || [];
    return { byCat: byCat, tools: tools };
  }

  function renderPrepTable(input, opts) {
    opts = opts || {};
    var pkgHtml = (opts.pkgHtml != null) ? opts.pkgHtml :
      (input.matched_packages && input.matched_packages.length ?
        input.matched_packages.map(function (p) {
          return '<span class="chip pkg">' + escHtml(p.name) + ' × ' + p.quantity + (p.setup ? '（搭建）' : '') + '</span>';
        }).join(' ') : '<span class="muted">无套餐</span>');

    var noteHtml;
    if (opts.noteHtml != null) noteHtml = opts.noteHtml;
    else if (opts.note != null) noteHtml = '<div class="note-box"><b>📝 备注：</b>' + escHtml(opts.note) + '</div>';
    else noteHtml = '';

    var norm = normalize(input);
    var byCat = norm.byCat, tools = norm.tools;

    var rows = '';
    CAT_ORDER.forEach(function (cat) {
      var items = byCat[cat];
      if (!items || !items.length) return;
      if (opts.fixedSort) {
        items = items.slice().sort(function (a, b) {
          var ia = getFixedIdx(a.name, cat), ib = getFixedIdx(b.name, cat);
          if (ia !== ib) return ia - ib;
          return (b.need || 0) - (a.need || 0);
        });
      }
      rows += '<tr><td colspan="7" style="background:' + (CAT_COLOR[cat] || '#666') +
        ';color:#fff;font-weight:700;padding:5px 8px;font-size:13px">' +
        (CAT_LABEL[cat] || cat) + ' · ' + items.length + '项</td></tr>';
      items.forEach(function (i) {
        var sc = (i.shortage > 0) ? 'txt-red' : '';
        var tp = i.total_portions || i.total_packages || i.order_qty || 1;
        var isSimple = (cat === 'drink' && /可乐|雪碧/.test(i.name || '')) || (cat === 'packaging' || cat === 'utensil');
        var ps = isSimple ? '—' : (i.portion_size || i.per_package || 0);
        var unit = i.unit || '';
        var out = i.swapped_out, inn = i.swapped_in;
        var badge = inn ? '<span class="swap-badge in">换入</span>' : (out ? '<span class="swap-badge out">换出</span>' : '');
        var style = out ? 'text-decoration:line-through;opacity:.55' : '';
        var needStr = (i.need || 0) + unit;
        var sub = '';
        if (inn && i.base_need != null) {
          var added = Math.max(0, (i.need || 0) - (i.base_need || 0));
          sub = (i.base_need > 0) ?
            '<div style="font-size:11px;color:#389e0d;margin-top:2px">🥩原套餐' + i.base_need + unit + ' ＋ 🟢换入' + added + unit + '</div>' :
            '<div style="font-size:11px;color:#389e0d;margin-top:2px">🟢换入 ' + needStr + '</div>';
        } else if (out) {
          sub = '<div style="font-size:11px;color:#a8071a;margin-top:2px">🔴 原' + (i.base_need || 0) + unit + '，已换出</div>';
        }
        var stockCell = opts.hideStock ? '' : '<td style="text-align:center">' + (i.stock != null ? i.stock : '-') + '</td>';
        var gap = i.shortage > 0 ? (i.shortage + unit) : '✓';
        rows += '<tr style="' + style + '">' +
          '<td style="text-align:left">' + escHtml(i.name) + ' ' + badge + sub + '</td>' +
          '<td style="text-align:center;font-weight:700">' + tp + '</td>' +
          '<td style="text-align:center">' + (ps || '—') + '</td>' +
          '<td style="text-align:center">' + unit + '</td>' +
          '<td style="text-align:center;font-weight:800;color:var(--primary)">' + needStr + '</td>' +
          stockCell +
          '<td style="text-align:center" class="' + sc + '">' + gap + '</td>' +
          '</tr>';
      });
    });

    var toolRows = tools.map(function (t) {
      return '<tr><td style="text-align:left">' + escHtml(t.name) + '</td>' +
        '<td style="text-align:center;font-weight:700">' + t.need + '</td>' +
        '<td style="text-align:center">' + t.stock + '</td>' +
        '<td style="text-align:center" class="' + (t.shortage > 0 ? 'txt-red' : '') + '">' + (t.shortage > 0 ? t.shortage : '✓') + '</td></tr>';
    }).join('');

    if (!rows && !toolRows) {
      return '<div style="padding:20px;color:var(--muted);text-align:center">无备餐需求</div>';
    }
    var html = '';
    if (pkgHtml) html += '<div style="margin-bottom:10px"><b>🍱 套餐</b>：' + pkgHtml + '</div>';
    if (noteHtml) html += noteHtml;
    html += '<h4 style="margin:10px 0 4px;font-size:13px;color:var(--primary)">🥩 备餐表（与备餐页最终一致）</h4>' +
      '<table class="tbl"><thead><tr><th>食材</th><th>份数</th><th>克数/份</th><th>单位</th><th>总克数</th>' +
      (opts.hideStock ? '' : '<th>库存</th>') + '<th>缺口</th></tr></thead><tbody>' + rows + '</tbody></table>';
    if (tools.length && !opts.hideTools) {
      html += '<h4 style="margin:10px 0 4px;font-size:13px;color:var(--primary)">🔧 工具 / 装备</h4>' +
        '<table class="tbl"><thead><tr><th>工具</th><th>需求</th><th>库存</th><th>缺口</th></tr></thead><tbody>' + toolRows + '</tbody></table>';
    }
    html += '<div style="margin-top:10px;font-size:12px;color:var(--muted)">🔴 换出 = 被换掉的菜（不再备）；🟢 换入 = 换进来的菜。核对无误可去备餐页整体勾选。</div>';
    if (opts.footer) html += opts.footer;
    return html;
  }

  window.renderPrepTable = renderPrepTable;
})();
