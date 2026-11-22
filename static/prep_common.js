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
    ['beef', '南美安格斯肥牛'], ['beef', '薄荷拌牛肉'], ['beef', '特选谷饲180天牛肋条'], ['beef', '奶香牛骰子'], ['beef', '爆汁烤小肠'],
    ['pork', '精选五花肉薄切'], ['pork', '沙葱小香猪'], ['pork', '原切松板肉'], ['pork', '韩式风味肠'], ['pork', '原切梅花肉'],
    ['chicken', '麻辣鸡尖'], ['chicken', '葱香小郡肝'], ['chicken', '蒜香鸡腿肉'], ['chicken', '奥尔良鸡翅根'], ['chicken', '秘制掌中宝'], ['chicken', '麻辣鸡脚筋'],
    ['vegetable', '生菜'], ['vegetable', '包浆豆腐'], ['vegetable', '厚切西葫芦'], ['vegetable', '土豆片'], ['vegetable', '奶香小馒头'], ['vegetable', '韭菜'], ['vegetable', '杏鲍菇'], ['vegetable', '洋葱'],
    ['side', '海带丝'], ['side', '辣椒段'], ['side', '辣白菜'], ['side', '蒜片'],
    ['sauce', '川香经典蘸料'], ['sauce', '坚果五香蘸料'], ['sauce', '酸辣烤肉汁']
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
        // ⚠️ 餐具/包装/工具（及可乐雪碧）按「个数」计，没有「份 × 克/份 = 总量」这层关系。
        //    旧版把套餐自带配量(total_portions)填进「份数」列，与「总量」并排出现两个都像数量的数字
        //    （如 6人餐纸杯：份数8、总量12个），用户会以为算错。现在按个数计的行只留「需备量」一个数字。
        var isSimple = (cat === 'drink' && /可乐|雪碧/.test(i.name || '')) || (cat === 'packaging' || cat === 'utensil');
        var out = i.swapped_out, inn = i.swapped_in;
        // ⭐ 整份换出的菜：份数与每份克数都显示 0（名字划线、需备量0），方便核对"这道已正确换掉"
        var tp = out ? 0 : (isSimple ? '—' : (i.total_portions || i.total_packages || i.order_qty || 1));
        var ps = out ? 0 : (isSimple ? '—' : (i.portion_size || i.per_package || 0));
        var unit = i.unit || '';
        // ⭐ 2026-11-15：加菜/换菜徽标直接把「几份」写在图标旁边，不用悬停、不另起一行。
        //    紧凑内联样式（nowrap + line-height:1 + vertical-align:middle）保证不撑高行、不新增版面。
        var _q = function(n){ return (n && n > 0) ? n : 1; };
        var _tight = 'white-space:nowrap;vertical-align:middle;line-height:1;';
        var badge = inn
          ? '<span class="swap-badge in" title="按备注换入 ' + _q(i.swap_qty) + ' 份" style="' + _tight + 'color:#389e0d">+' + _q(i.swap_qty) + '份</span>'
          : (out ? '<span class="swap-badge out" title="按备注换出 ' + _q(i.swap_out_qty) + ' 份" style="' + _tight + 'color:#a8071a">-' + _q(i.swap_out_qty) + '份</span>' : '');
        // ⭐ 2026-11-20：加料徽标全系统统一为「+N」橙底（与 toolExtraBadge 工具加料完全同款）——
        //    旧版食材加菜写「➕加N份」(emoji加号+加字重复、还带份)，与工具「+2」不统一、不精炼。
        var extraBadge = i.extra_added ? ' <span title="备注加菜（含赠送）追加 ' + _q(i.extra_qty) + ' 份" style="display:inline-block;background:#fff7e6;border:1px solid #ffd591;color:#d46b08;border-radius:5px;padding:0 4px;font-size:11px;font-weight:700;' + _tight + '">+' + _q(i.extra_qty) + '</span>' : '';
        // ⭐ 2026-11-20：只划掉「菜名文字」，不划 -N份 徽标——徽标被划线+淡化会被误读成"减2份已撤销"。
        var nameDeco = out ? 'text-decoration:line-through;opacity:.55' : '';
        var nameHtml = '<span style="' + nameDeco + '">' + escHtml(i.name) + '</span>';
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
        rows += '<tr>' +
          '<td style="text-align:left">' + nameHtml + ' ' + badge + extraBadge + utBadge(i) + sub + '</td>' +
          '<td style="text-align:center;font-weight:700">' + tp + '</td>' +
          '<td style="text-align:center">' + (out ? 0 : (ps || '—')) + '</td>' +
          '<td style="text-align:center">' + unit + '</td>' +
          '<td style="text-align:center;font-weight:800;color:var(--primary)">' + needStr + '</td>' +
          stockCell +
          '<td style="text-align:center" class="' + sc + '">' + gap + '</td>' +
          '</tr>';
      });
    });

  var toolRows = tools.map(function (t) {
    return '<tr><td style="text-align:left">' + escHtml(t.name) + toolExtraBadge(t) + '</td>' +
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
      '<table class="tbl"><thead><tr><th>食材</th><th>份数</th><th>克数/份</th><th>单位</th><th>需备量</th>' +
      (opts.hideStock ? '' : '<th>库存</th>') + '<th>缺口</th></tr></thead><tbody>' + rows + '</tbody></table>';
    if (tools.length && !opts.hideTools) {
      html += '<h4 style="margin:10px 0 4px;font-size:13px;color:var(--primary)">🔧 工具 / 装备</h4>' +
        '<table class="tbl"><thead><tr><th>工具</th><th>需求</th><th>库存</th><th>缺口</th></tr></thead><tbody>' + toolRows + '</tbody></table>';
    }
    html += '<div style="margin-top:10px;font-size:12px;color:var(--muted)">🔴 换出 = 被换掉的菜（不再备）；🟢 换入 = 换进来的菜。核对无误可去备餐页整体勾选。</div>';
    if (opts.footer) html += opts.footer;
    return html;
  }

  // ⭐ 备注真追加装备的徽标（2026-11-15）：「+2把椅子/加2把椅子/多2个天幕」是套餐配量上真加，
  //    数量会变多（椅子6→8），标「+2」说明来源。⚠️ 2026-11-16 用户定稿：写「+2」，严禁「➕加2」。
  //    紧凑内联、不换行、不撑高行，与加菜徽标同款样式；口径只此一处，三处渲染共用。
  //    ⚠️ 必须定义在 renderPrepTable 之外（IIFE 顶层）——window 导出语句在外层，嵌套在函数体里会 ReferenceError。
  function toolExtraBadge(t) {
    if (!(t && t.extra_added)) return '';
    var n = Number(t.extra_qty) || 1;
    return ' <span title="备注追加装备 ' + n + '（在套餐配量上加的）" style="display:inline-block;background:#fff7e6;border:1px solid #ffd591;color:#d46b08;border-radius:5px;padding:0 4px;font-size:11px;font-weight:700;white-space:nowrap;vertical-align:middle;line-height:1">+' + n + '</span>';
  }

  // ⭐ 客户报餐具数的徽标（2026-11-15）：餐具数量是「按客户报的数覆盖套餐配量」，
  //    不是「加 N 份」，所以另用一个图标 + 文案，三处共用渲染都调这个函数，口径只此一处。
  //    紧凑内联样式与加菜/换菜徽标一致：不换行、不撑高行、不新增版面。
  //    ⭐ 文案分两种写法（用户定：只显示最终数看不出加了多少）：
  //      增量（多拿4套餐具）→「📋多拿4套」＝显示相较原数量的增量，与备注原文对得上；
  //      总数（餐具11/14份餐具）→「📋报11份」＝客户报的就是最终数；
  //      多单混合（写法不同/增量不同）→「📋按客户数」兜底。
  function utBadgeText(i) {
    var n = Number((i && i.ut_specified) || 0);
    if (!n) return '';
    var exact = (i.ut_specified_exact === undefined) ? true : !!i.ut_specified_exact;
    if (!exact) return '📋按客户数';
    // ⭐ 2026-11-22（用户定，#445「5餐具」引出的歧义）：
    //    增量「多拿N套餐具」→ 统一显示「+N」(与加料徽标同款，用户要的格式)；
    //    总数「N份餐具」    → 中性标签「📋N份」，去掉「报」字——「报5份」让人误以为比默认多，
    //      其实 3-4人餐默认就是5份、客户报5=没多，文案必须中性不带「多/报」暗示。
    if (i.ut_mode === 'inc' && Number(i.ut_inc) > 0) return '+' + Number(i.ut_inc);
    return '📋' + n + '份';
  }
  function utBadge(i) {
    var txt = utBadgeText(i);
    if (!txt) return '';
    var tip = (i && i.ut_mode === 'inc')
      ? '备注写的是「多拿N套餐具」：三项统一按 人数+N 备（覆盖套餐原配量），徽标显示的是增量'
      : '备注报了餐具数：按客户确定的数量备，不再叠加商家兜底';
    // ⭐ 增量「+N」用橙底，与加料/工具徽标完全同款（符号统一）；总数「📋N份」保留蓝底
    var isInc = (i && i.ut_mode === 'inc' && Number(i.ut_inc) > 0);
    var sty = isInc
      ? 'display:inline-block;background:#fff7e6;border:1px solid #ffd591;color:#d46b08;border-radius:5px;padding:0 4px;font-size:11px;font-weight:700;white-space:nowrap;vertical-align:middle;line-height:1'
      : 'display:inline-block;background:#eef4ff;border:1px solid #adc6ff;color:#1d4ed8;border-radius:5px;padding:0 4px;font-size:11px;font-weight:700;white-space:nowrap;vertical-align:middle;line-height:1';
    return ' <span title="' + tip + '" style="' + sty + '">' + txt + '</span>';
  }

  window.toolExtraBadge = toolExtraBadge;
  window.utBadgeText = utBadgeText;
  window.utBadge = utBadge;
  window.renderPrepTable = renderPrepTable;
})();
