/* dsh-kb — kb.js
 * 知识库界面：左导航（视图 / 分类 / 时间）+ 主区（搜索、排序、分页、卡片）+ 详情抽屉。
 *
 * 后端契约（见 server/app/api/routes/kb.py）：
 *   GET  api/kb/tree          树与计数
 *   GET  api/kb               列表（q/category/series/date_from/date_to/view/sort/offset/limit/refresh）
 *   GET  api/kb/report/<path> 单篇详情（含 preview 摘要正文）
 *   POST api/kb/state         {path, read?, starred?}
 * 鉴权由 DSH 反代负责，本页没有登录概念（core.js 的 safeFetch 统一处理 401）。
 */

var kbState = {
  q: '',
  category: '',
  series: '',
  tag: '',
  date: '',
  dateFrom: '',
  dateTo: '',
  view: 'all',
  sort: 'date_desc',
  mode: 'hybrid',
  offset: 0,
  limit: 20,
};

var _kbTree = null;
var _kbItems = [];
var _kbTotal = 0;
var _kbTimer = null;
var _kbDrawerPath = '';
var _kbDrawerReturnFocus = null;
var _kbLastClosedPath = '';
var _kbEmbedAvailable = false;
var _kbGlobalTotal = 0;
var _kbSearchMode = '';
var _kbPublicBase = '';
var _kbPublicPaths = {};
var _kbSelectMode = false;
var _kbSelected = {};
var _kbHist = [];
var _kbSaved = [];
var KB_HIST_KEY = 'kb:search-history';
var KB_SAVED_KEY = 'kb:saved-filters';

var KB_CAT_SLUG = {
  '市场全景': 'market',
  '个股分析': 'stock',
  '策略研究': 'strategy',
  '话题精读': 'essay',
  'Minecraft 版本': 'mc',
  '旅行影像': 'travel',
  '影像收录': 'photo',
  '技术演示': 'tech',
  '其他': 'other',
};

function kbCatSlug(cat) { return KB_CAT_SLUG[cat] || 'other'; }

function kbFmtSize(n) {
  if (n === null || n === undefined) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function kbFmtDate(iso) {
  if (!iso) return '';
  return iso.replace(/-/g, '.');
}

function kbEl(id) { return document.getElementById(id); }

function kbQuery() {
  var params = new URLSearchParams();
  if (kbState.q) params.set('q', kbState.q);
  if (kbState.category) params.set('category', kbState.category);
  if (kbState.series) params.set('series', kbState.series);
  if (kbState.tag) params.set('tag', kbState.tag);
  if (kbState.date) {
    params.set('date_from', kbState.date);
    params.set('date_to', kbState.date);
  } else {
    if (kbState.dateFrom) params.set('date_from', kbState.dateFrom);
    if (kbState.dateTo) params.set('date_to', kbState.dateTo);
  }
  if (kbState.view !== 'all') params.set('view', kbState.view);
  if (kbState.sort !== 'date_desc') params.set('sort', kbState.sort);
  if (kbState.offset) params.set('offset', String(kbState.offset));
  params.set('limit', String(kbState.limit));
  return params.toString();
}

function kbHighlight(text, q) {
  var safe = esc(text || '');
  if (!q) return safe;
  var needle = String(q).trim();
  if (!needle) return safe;
  var pattern = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
  return safe.replace(pattern, function (hit) { return '<mark>' + hit + '</mark>'; });
}

// ---------- 初始化 ----------

function kbInit() {
  initOfflineWatch();
  kbBindSearch();
  kbBindKeys();
  kbLoadEmbedStatus();
  kbLoadPublicState();
  kbSyncDateInputs();
  kbLoadHistory();
  kbLoadSaved();
  kbBindHistory();
  kbRenderSaved();
  kbLoadTree();
  kbLoad();
}

async function kbLoadEmbedStatus() {
  var data = await safeFetch(API + '/kb/embed/stats');
  if (!data) return;
  _kbEmbedAvailable = !!data.available && (data.stats || {}).embedded > 0;
  kbRenderMode();
}

function kbRenderMode() {
  ['hybrid', 'lexical', 'semantic'].forEach(function (mode) {
    var btn = kbEl('kb-mode-' + mode);
    if (!btn) return;
    var on = kbState.mode === mode;
    btn.classList.toggle('is-on', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.disabled = (mode === 'hybrid' || mode === 'semantic') && !_kbEmbedAvailable;
  });
}

function kbSetMode(mode) {
  if ((mode === 'hybrid' || mode === 'semantic') && !_kbEmbedAvailable) { showToast('语义索引还在生成中，先用关键词检索'); return; }
  if (kbState.mode === mode) return;
  kbState.mode = mode;
  kbState.offset = 0;
  kbRenderMode();
  kbLoad();
}

function kbBindSearch() {
  var input = kbEl('kb-search');
  if (!input) return;
  input.addEventListener('input', function (event) {
    kbOnSearch(event.target.value);
  });
}


// ---------- P2：相对时间 / 撤销提示 / 快捷键 ----------

function kbAgo(ts) {
  if (!ts || isNaN(ts)) return '';
  var diff = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.round(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.round(diff / 3600) + ' 小时前';
  return Math.round(diff / 86400) + ' 天前';
}

function kbUndoToast(message, undoFn) {
  var el = document.createElement('div');
  el.className = 'kb-undo';
  el.setAttribute('role', 'status');
  var text = document.createElement('span');
  text.textContent = message;
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'kb-undo-btn';
  btn.textContent = '撤销';
  btn.addEventListener('click', async function () {
    btn.disabled = true;
    try { await undoFn(); } finally { el.remove(); }
  });
  el.appendChild(text);
  el.appendChild(btn);
  document.body.appendChild(el);
  window.setTimeout(function () { if (el.parentNode) el.remove(); }, 9000);
}

var _kbCursor = -1;

function kbMoveCursor(delta) {
  if (!_kbItems.length) return;
  _kbCursor = Math.max(0, Math.min(_kbItems.length - 1, _kbCursor + delta));
  var cards = document.querySelectorAll('#kb-list .kb-item');
  for (var i = 0; i < cards.length; i++) cards[i].classList.toggle('is-cursor', i === _kbCursor);
  if (cards[_kbCursor] && cards[_kbCursor].scrollIntoView) cards[_kbCursor].scrollIntoView({ block: 'nearest' });
}

function kbCursorAction(kind) {
  if (_kbCursor < 0) { kbMoveCursor(1); return; }
  if (!_kbItems[_kbCursor]) return;
  if (kind === 'open') kbOpenDrawer(_kbCursor);
  else if (kind === 'star') kbToggleStar(null, _kbCursor);
}

function kbIsTyping(target) {
  var el = target || document.activeElement;
  if (!el) return false;
  var tag = String(el.tagName || '').toUpperCase();
  return tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable === true;
}

function kbShowShortcutHelp() {
  if (kbEl('kb-help')) return;
  var rows = [
    ['⌘K / Ctrl+K', '聚焦搜索框'],
    ['/', '聚焦搜索框'],
    ['J / K', '下一条 / 上一条'],
    ['O 或 Enter', '打开当前条详情'],
    ['S', '收藏 / 取消收藏当前条'],
    ['Esc', '关闭抽屉或帮助'],
    ['?', '打开本帮助'],
  ];
  var wrap = document.createElement('div');
  wrap.id = 'kb-help';
  wrap.className = 'kb-help';
  wrap.setAttribute('role', 'dialog');
  wrap.setAttribute('aria-modal', 'true');
  wrap.setAttribute('aria-label', '键盘快捷键');
  wrap.innerHTML = '<div class="kb-help-card" tabindex="-1">' +
    '<div class="kb-help-head"><strong>键盘快捷键</strong>' +
    '<button type="button" class="kb-iconbtn" onclick="kbCloseShortcutHelp()" aria-label="关闭帮助">✕</button></div>' +
    '<dl>' + rows.map(function (row) {
      return '<div class="kb-help-row"><dt>' + row[0] + '</dt><dd>' + row[1] + '</dd></div>';
    }).join('') + '</dl><p class="kb-help-foot">按 Esc 关闭</p></div>';
  wrap.addEventListener('click', function (event) { if (event.target === wrap) kbCloseShortcutHelp(); });
  document.body.appendChild(wrap);
  var card = wrap.querySelector('.kb-help-card');
  if (card && card.focus) card.focus();
}

function kbCloseShortcutHelp() {
  var el = kbEl('kb-help');
  if (el) el.remove();
}

function kbBindKeys() {
  document.addEventListener('keydown', kbTrapDrawerKeys);
  document.addEventListener('keydown', function (event) {
    var meta = event.metaKey || event.ctrlKey;
    if (meta && String(event.key).toLowerCase() === 'k') {
      event.preventDefault();
      var input = kbEl('kb-search');
      if (input) { input.focus(); input.select(); }
      return;
    }
    if (event.key === 'Escape') {
      if (kbEl('kb-help')) { kbCloseShortcutHelp(); return; }
      var drawer = kbEl('kb-drawer');
      if (drawer && !drawer.classList.contains('bl-hidden')) { kbCloseDrawer(); return; }
      var search = kbEl('kb-search');
      if (search && document.activeElement === search && kbState.q) { kbClearSearch(); }
      return;
    }
    if (kbIsTyping(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
    var key = event.key;
    if (key === '?') { event.preventDefault(); kbShowShortcutHelp(); return; }
    if (key === '/') {
      event.preventDefault();
      var box = kbEl('kb-search');
      if (box) box.focus();
      return;
    }
    if (key === 'j' || key === 'k') {
      event.preventDefault();
      kbMoveCursor(key === 'j' ? 1 : -1);
      return;
    }
    if (key === 'o' || key === 'Enter') {
      if (_kbCursor >= 0 && !kbEl('kb-help')) { event.preventDefault(); kbCursorAction('open'); }
      return;
    }
    if (key === 's' && _kbCursor >= 0) {
      event.preventDefault();
      kbCursorAction('star');
    }
  });
}

// ---------- 左导航（树 / 计数） ----------

function kbTreeParams() {
  var params = new URLSearchParams();
  if (kbState.q) params.set('q', kbState.q);
  if (kbState.category) params.set('category', kbState.category);
  if (kbState.view !== 'all') params.set('view', kbState.view);
  if (kbState.date) {
    params.set('date_from', kbState.date);
    params.set('date_to', kbState.date);
  } else {
    if (kbState.dateFrom) params.set('date_from', kbState.dateFrom);
    if (kbState.dateTo) params.set('date_to', kbState.dateTo);
  }
  return params;
}

async function kbLoadTree(force) {
  var params = kbTreeParams();
  if (force) params.set('refresh', '1');
  var query = params.toString();
  var url = API + '/kb/tree' + (query ? '?' + query : '');
  var data = await safeFetch(url);
  if (!data) return;
  _kbTree = data;
  if (!kbState.q) _kbGlobalTotal = data.total || 0;
  kbRenderCounts();
  kbRenderCats();
  kbRenderTags();
  kbRenderDates();
  var hint = kbEl('kb-side-hint');
  if (hint) {
    var bits = [data.total + ' 篇', (data.dates || []).length + ' 个日期'];
    if (data.new_recent) bits.push('24h 新增 ' + data.new_recent + ' 篇');
    if (data.synced_at) bits.push('索引于 ' + kbAgo(Date.parse(data.synced_at)));
    hint.textContent = bits.join(' · ');
  }
}

function kbVisibleTotal() {
  // 语义检索的命中数（含文章/节点）可能大于纯词法的 tree.total；搜索时以结果数为准，
  // 否则侧栏「全部 1」会和列表「1–20 / 826」互相打架。
  var tree = _kbTree || { total: 0 };
  if (!kbState.q) return tree.total || 0;
  return Math.max(tree.total || 0, _kbTotal || 0);
}

function kbRenderCounts() {
  var tree = _kbTree || { total: 0, unread: 0, starred: 0 };
  var visible = kbVisibleTotal();
  var map = {
    all: visible,
    unread: kbState.q ? '—' : tree.unread,
    starred: kbState.q ? '—' : tree.starred,
    public: tree.public || 0,
  };
  Object.keys(map).forEach(function (view) {
    var node = kbEl('count-' + view);
    if (node) node.textContent = map[view];
    var btn = document.querySelector('.kb-navitem[data-view="' + view + '"]');
    if (btn) {
      btn.setAttribute('aria-pressed', kbState.view === view ? 'true' : 'false');
      if (view === 'all') btn.title = kbState.q ? '搜索结果（含报告、LLM Wiki 文章与图谱节点）' : '';
    }
  });
  var allLabel = document.querySelector('.kb-navitem[data-view="all"] .kb-navlabel');
  if (allLabel) allLabel.textContent = kbState.q ? '搜索结果' : '全部';
  var subtotal = kbEl('kb-subtotal');
  if (subtotal && visible) subtotal.textContent = visible + (kbState.q ? ' 条结果 · 含 Wiki/图谱条目' : ' 篇报告 · 分类浏览与全文检索');
}

function kbRenderCats() {
  var box = kbEl('kb-cats');
  if (!box) return;
  var tree = _kbTree || { categories: [], series: [] };
  var allCount = kbState.q ? (_kbGlobalTotal || tree.total || 0) : kbVisibleTotal();
  var allTitle = kbState.q ? '全库报告数（不随搜索变化）' : '';
  var html = '<button type="button" class="kb-cat" data-cat="" title="' + allTitle + '" aria-pressed="' +
    (kbState.category ? 'false' : 'true') + '" onclick="kbSetCategory(\'\')">' +
    '<span class="kb-cat-name">全部分类</span><span class="kb-cat-n">' + allCount + '</span></button>';
  (tree.categories || []).forEach(function (cat) {
    var active = kbState.category === cat.name;
    html += '<button type="button" class="kb-cat kb-cat-' + kbCatSlug(cat.name) + '" ' +
      'data-cat="' + escAttr(cat.name) + '" aria-pressed="' + (active ? 'true' : 'false') + '" ' +
      'title="' + (kbState.q ? '全库该分类数（不随搜索变化）' : '') + '" ' +
      'onclick="kbSetCategory(this.dataset.cat)">' +
      '<span class="kb-cat-name">' + esc(cat.name) + '</span>' +
      '<span class="kb-cat-n">' + (kbState.q ? '—' : cat.count) + '</span></button>';
    if (active) {
      var series = (tree.series || []).filter(function (row) { return row.category === cat.name; });
      if (series.length) {
        html += '<div class="kb-series-list" role="group" aria-label="' + escAttr(cat.name) + ' 系列">';
        html += '<button type="button" class="kb-series" data-series="" aria-pressed="' +
          (kbState.series ? 'false' : 'true') + '" onclick="kbSetSeries(\'\')">全部系列</button>';
        series.forEach(function (row) {
          var on = kbState.series === row.name;
          html += '<button type="button" class="kb-series" data-series="' + escAttr(row.name) + '" ' +
            'aria-pressed="' + (on ? 'true' : 'false') + '" ' +
            'onclick="kbSetSeries(this.dataset.series)">' + esc(row.name) +
            '<span class="kb-series-n">' + row.count + '</span></button>';
        });
        html += '</div>';
      }
    }
  });
  box.innerHTML = html;
}

function kbRenderTags() {
  var box = kbEl('kb-tags');
  if (!box) return;
  var tags = ((_kbTree || {}).tags || []).slice(0, 15);
  if (!tags.length) {
    box.innerHTML = '<span class="kb-tags-empty">还没有标签（等 AI 抽取出标签后出现）</span>';
    return;
  }
  box.innerHTML = tags.map(function (row) {
    var on = kbState.tag === row.name;
    return '<button type="button" class="kb-tag-chip' + (on ? ' is-on' : '') + '"' +
      ' data-tag="' + escAttr(row.name) + '" aria-pressed="' + (on ? 'true' : 'false') + '"' +
      ' onclick="kbSetTag(this.dataset.tag)" title="按标签筛选：' + escAttr(row.name) + '">' +
      '#' + esc(row.name) + '<span class="kb-tag-n">' + row.reports + '</span></button>';
  }).join('');
}

function kbSetTag(name) {
  var next = String(name || '');
  if (kbState.tag === next) next = '';
  kbState.tag = next;
  kbState.offset = 0;
  kbRenderFilterbar();
  kbLoadTree();
  kbLoad();
}

function kbRenderDates() {
  var box = kbEl('kb-dates');
  if (!box) return;
  var dates = ((_kbTree || {}).dates || []).slice(0, 10);
  var html = '<button type="button" class="kb-date" data-date="" aria-pressed="' +
    (kbState.date ? 'false' : 'true') + '" onclick="kbSetDate(\'\')">全部时间</button>';
  dates.forEach(function (row) {
    var on = kbState.date === row.date;
    html += '<button type="button" class="kb-date" data-date="' + escAttr(row.date) + '" ' +
      'aria-pressed="' + (on ? 'true' : 'false') + '" onclick="kbSetDate(this.dataset.date)">' +
      kbFmtDate(row.date) + '<span class="kb-date-n">' + row.count + '</span></button>';
  });
  box.innerHTML = html;
}

// ---------- 列表 ----------

async function kbLoad(force) {
  var list = kbEl('kb-list');
  if (list) list.innerHTML = '<div class="kb-loading"><span class="kb-spinner" aria-hidden="true"></span>加载知识库…</div>';
  var searching = !!kbState.q;
  var semanticMode = searching && (kbState.mode === 'hybrid' || kbState.mode === 'semantic');
  var url = semanticMode
    ? API + '/kb/search?' + kbQuery() + '&mode=' + encodeURIComponent(kbState.mode)
    : API + '/kb?' + kbQuery() + (force ? '&refresh=1' : '');
  var data = await safeFetch(url);
  if (data === null) return;
  _kbItems = data.items || [];
  _kbTotal = data.total || 0;
  _kbSearchMode = data.mode || '';
  kbRenderList();
  kbRenderBatchbar();
  kbRenderFilterbar();
  kbRenderPager();
  kbRenderCounts();
  kbRenderCats();
  var result = kbEl('kb-result');
  if (result) result.textContent = kbResultLabel();
  if (kbState.q) kbPushHistory(kbState.q);
}

function kbResultLabel() {
  if (!_kbTotal) return '没有结果';
  var from = kbState.offset + 1;
  var to = Math.min(kbState.offset + _kbItems.length, _kbTotal);
  var suffix = '';
  if (kbState.q) {
    if (_kbSearchMode === 'semantic') suffix = ' 篇语义相关';
    else if (_kbSearchMode === 'hybrid') suffix = ' 篇含关键词';
    else suffix = ' 篇匹配';
  }
  return from + '–' + to + ' / ' + _kbTotal + suffix;
}

function kbRenderList() {
  var list = kbEl('kb-list');
  if (!list) return;
  if (!_kbItems.length) {
    list.innerHTML = '<div class="kb-empty"><strong>没有匹配的报告</strong>' +
      '<span>试试清空搜索词、切换分类，或清除日期筛选。</span>' +
      '<button type="button" class="kb-btn kb-btn-ghost" onclick="kbResetFilters()">重置筛选</button></div>';
    return;
  }
  list.innerHTML = _kbItems.map(kbCard).join('');
}

function kbTagChips(tags, cls) {
  var list = (tags || []).filter(Boolean).slice(0, 4);
  if (!list.length) return '';
  return '<div class="' + (cls || 'kb-item-tags') + '">' + list.map(function (tag) {
    return '<button type="button" class="kb-tag" data-tag="' + escAttr(tag) + '"' +
      ' onclick="kbSetTag(this.dataset.tag)" title="按标签筛选">#' + esc(tag) + '</button>';
  }).join('') + '</div>';
}

function kbDocCard(item) {
  var isArticle = item.kind === 'article';
  var kindLabel = isArticle ? 'LLM Wiki' : '图谱节点';
  var meta = isArticle
    ? ((item.source_count || 0) + ' 篇来源 · ' + (item.series || ''))
    : ((item.entity_type || '节点') + ' · ' + (item.report_count || 0) + ' 篇报告提及');
  var summary = item.summary || item.snippet || '';
  return '<article class="kb-item kb-item-doc" data-kind="' + escAttr(item.kind) + '">' +
    '<div class="kb-item-top">' +
      '<span class="kb-badge ' + (isArticle ? 'kb-badge-wiki' : 'kb-badge-graph') + '">' + kindLabel + '</span>' +
      '<span class="kb-item-series">' + esc(meta) + '</span>' +
      '<span class="kb-item-spacer"></span>' +
      (item.date ? '<time class="kb-item-date">' + kbFmtDate(item.date) + '</time>' : '') +
    '</div>' +
    '<h3 class="kb-item-title"><a class="kb-item-open" href="' + escAttr(item.url || ('wiki/' + item.slug)) + '">' +
      kbHighlight(item.title, kbState.q) + '</a></h3>' +
    '<p class="kb-item-summary">' + kbHighlight(summary, kbState.q) + '</p>' +
    '<div class="kb-item-foot"><span class="kb-item-size">' +
      (isArticle ? '模型撰写 · 可点击阅读全文' : '确定性聚合 · 可点击查看节点') + '</span></div>' +
  '</article>';
}

function kbCard(item, index) {
  if (item.kind === 'article' || item.kind === 'node') return kbDocCard(item);
  var searching = !!kbState.q;
  // 搜索时优先显示命中片段；平时优先显示 AI 摘要（信息密度高于正文首个 <p>）。
  var summary = searching
    ? ((item.match === 'body' && item.snippet) ? item.snippet : (item.snippet || item.summary || ''))
    : (item.summary_ai || item.summary || '');
  var hasAi = !!item.summary_ai;
  var selected = !!_kbSelected[item.path];
  var check = _kbSelectMode
    ? '<input type="checkbox" class="kb-check" data-path="' + escAttr(item.path) + '"' +
      (selected ? ' checked' : '') + ' aria-label="选择这篇报告" onchange="kbToggleSelect(this)">'
    : '';
  return '<article class="kb-item' + (item.read ? '' : ' is-unread') + (_kbSelectMode ? ' is-selecting' : '') +
    (selected ? ' is-selected' : '') + '" data-path="' + escAttr(item.path) + '">' +
    '<div class="kb-item-top">' + check +
      '<span class="kb-badge kb-badge-' + kbCatSlug(item.category) + '">' + esc(item.category) + '</span>' +
      (item.series ? '<span class="kb-item-series">' + esc(item.series) + '</span>' : '') +
      (hasAi ? '<span class="kb-ai-mark" title="AI 已生成结构化摘要">AI</span>' : '') +
      (item.match === 'semantic' ? '<span class="kb-semantic-mark" title="语义相似命中">语义</span>' : '') +
      (item.public ? '<span class="kb-pub-mark" title="已公开：拿到链接的人无需账号即可阅读">公开</span>' : '') +
      '<span class="kb-item-spacer"></span>' +
      (item.date ? '<time class="kb-item-date">' + kbFmtDate(item.date) + '</time>' : '') +
      '<button type="button" class="kb-star' + (item.starred ? ' is-on' : '') + '" ' +
        'aria-pressed="' + (item.starred ? 'true' : 'false') + '" aria-label="收藏" ' +
        'onclick="kbToggleStar(event, ' + index + ')">' + (item.starred ? '★' : '☆') + '</button>' +
    '</div>' +
    '<h3 class="kb-item-title">' +
      '<button type="button" class="kb-item-open" onclick="kbOpenDrawer(' + index + ')">' +
        kbHighlight(item.title, kbState.q) + '</button>' +
    '</h3>' +
    '<p class="kb-item-summary">' + kbHighlight(summary, kbState.q) + '</p>' +
    (searching ? '' : kbTagChips(item.tags)) +
    '<div class="kb-item-foot">' +
      '<span class="kb-item-size">' + kbFmtSize(item.size) + '</span>' +
      '<span class="kb-item-state">' + (item.read ? '已读' : '未读') + '</span>' +
    '</div>' +
  '</article>';
}

// ---------- 筛选/排序/视图 ----------

function kbSetCategory(cat) {
  kbState.category = kbState.category === cat ? '' : (cat || '');
  kbState.series = '';
  kbState.offset = 0;
  kbRenderCats();
  kbLoad();
}

function kbSetSeries(series) {
  kbState.series = kbState.series === series ? '' : (series || '');
  kbState.offset = 0;
  kbRenderCats();
  kbLoad();
}

function kbSyncDateInputs() {
  var from = kbEl('kb-from');
  var to = kbEl('kb-to');
  if (from) from.value = kbState.dateFrom;
  if (to) to.value = kbState.dateTo;
}

function kbSetDate(date) {
  var next = kbState.date === date ? '' : (date || '');
  kbState.date = next;
  kbState.dateFrom = next;
  kbState.dateTo = next;
  kbSyncDateInputs();
  kbState.offset = 0;
  kbRenderDates();
  kbRenderFilterbar();
  kbLoadTree();
  kbLoad();
}

// 自定义日期区间：两个日期输入框 + 「应用」。只填一边也允许（半开区间）。
function kbApplyDateRange() {
  var fromEl = kbEl('kb-from');
  var toEl = kbEl('kb-to');
  var from = fromEl ? fromEl.value : '';
  var to = toEl ? toEl.value : '';
  if (from && to && from > to) {
    showToast('起始日期不能晚于截止日期', 'error');
    return;
  }
  kbState.date = '';
  kbState.dateFrom = from;
  kbState.dateTo = to;
  kbState.offset = 0;
  kbRenderDates();
  kbRenderFilterbar();
  kbLoadTree();
  kbLoad();
}

function kbClearDateRange() {
  kbState.date = '';
  kbState.dateFrom = '';
  kbState.dateTo = '';
  kbSyncDateInputs();
  kbState.offset = 0;
  kbRenderDates();
  kbRenderFilterbar();
  kbLoadTree();
  kbLoad();
}

function kbSetView(view) {
  kbState.view = view;
  kbState.offset = 0;
  kbLoadTree();
  kbLoad();
}

function kbSetSort(sort) {
  kbState.sort = sort || 'date_desc';
  kbState.offset = 0;
  kbLoad();
}

function kbOnSearch(value) {
  kbState.q = value.trim();
  var clear = kbEl('kb-search-clear');
  if (clear) clear.classList.toggle('bl-hidden', !kbState.q);
  if (!kbState.q) kbRenderHistory();
  clearTimeout(_kbTimer);
  _kbTimer = setTimeout(function () {
    kbState.offset = 0;
    kbLoadTree();
    kbLoad();
  }, 250);
}

function kbClearSearch() {
  var input = kbEl('kb-search');
  if (input) { input.value = ''; input.focus(); }
  kbOnSearch('');
}

function kbResetFilters() {
  kbState.q = '';
  kbState.category = '';
  kbState.series = '';
  kbState.tag = '';
  kbState.date = '';
  kbState.dateFrom = '';
  kbState.dateTo = '';
  kbSyncDateInputs();
  kbState.view = 'all';
  kbState.sort = 'date_desc';
  kbState.offset = 0;
  var input = kbEl('kb-search');
  if (input) input.value = '';
  var sort = kbEl('kb-sort');
  if (sort) sort.value = 'date_desc';
  kbRenderCounts();
  kbRenderCats();
  kbRenderTags();
  kbRenderDates();
  kbLoad();
}

function kbResetDates() { kbSetDate(''); }

function kbRefresh() {
  kbLoadTree(true);
  kbLoad(true);
  showToast('已重新扫描 reports/', 'info');
}

function kbRenderFilterbar() {
  var bar = kbEl('kb-filterbar');
  if (!bar) return;
  var chips = [];
  if (kbState.q) chips.push(['搜索：' + kbState.q, 'kbResetSearch']);
  if (kbState.category) chips.push([kbState.category, 'kbClearCategory']);
  if (kbState.series) chips.push([kbState.series, 'kbClearSeries']);
  if (kbState.tag) chips.push(['标签：#' + kbState.tag, 'kbClearTag']);
  if (kbState.date) chips.push(['日期：' + kbFmtDate(kbState.date), 'kbResetDates']);
  else if (kbState.dateFrom || kbState.dateTo) {
    var rangeLabel = (kbState.dateFrom ? kbFmtDate(kbState.dateFrom) : '最早') + ' → ' +
      (kbState.dateTo ? kbFmtDate(kbState.dateTo) : '最新');
    chips.push(['日期：' + rangeLabel, 'kbClearDateRange']);
  }
  if (kbState.view !== 'all') chips.push([kbState.view === 'unread' ? '仅未读' : (kbState.view === 'starred' ? '仅收藏' : '仅公开'), 'kbClearView']);
  if (!chips.length) {
    bar.classList.add('bl-hidden');
    bar.innerHTML = '';
    return;
  }
  bar.classList.remove('bl-hidden');
  bar.innerHTML = chips.map(function (chip) {
    return '<button type="button" class="kb-chip" onclick="' + chip[1] + '()">' + esc(chip[0]) +
      '<span aria-hidden="true">✕</span></button>';
  }).join('') + '<button type="button" class="kb-chip kb-chip-clear" onclick="kbResetFilters()">清除全部</button>';
}

function kbResetSearch() { kbClearSearch(); }
function kbClearCategory() { kbSetCategory(''); }
function kbClearSeries() { kbSetSeries(''); }
function kbClearTag() { kbSetTag(''); }
function kbClearView() { kbSetView('all'); }

// ---------- 分页 ----------

function kbRenderPager() {
  var pager = kbEl('kb-pager');
  if (!pager) return;
  if (_kbTotal <= kbState.limit) {
    pager.classList.add('bl-hidden');
    return;
  }
  pager.classList.remove('bl-hidden');
  var info = kbEl('kb-pager-info');
  var page = Math.floor(kbState.offset / kbState.limit) + 1;
  var pages = Math.max(1, Math.ceil(_kbTotal / kbState.limit));
  if (info) info.textContent = '第 ' + page + ' / ' + pages + ' 页';
  var prev = kbEl('kb-prev');
  var next = kbEl('kb-next');
  if (prev) prev.disabled = kbState.offset <= 0;
  if (next) next.disabled = kbState.offset + kbState.limit >= _kbTotal;
}

function kbPrevPage() {
  if (kbState.offset <= 0) return;
  kbState.offset = Math.max(0, kbState.offset - kbState.limit);
  kbLoad();
  kbScrollTop();
}

function kbNextPage() {
  if (kbState.offset + kbState.limit >= _kbTotal) return;
  kbState.offset += kbState.limit;
  kbLoad();
  kbScrollTop();
}

function kbScrollTop() {
  var main = kbEl('kb-main');
  if (main) main.scrollTop = 0;
}

// ---------- P6：公开分享（匿名只读外链） ----------

async function kbLoadPublicState() {
  var data = await safeFetch(API + '/kb/public');
  if (!data) return;
  _kbPublicBase = data.base || '';
  _kbPublicPaths = {};
  (data.paths || []).forEach(function (path) { _kbPublicPaths[path] = true; });
  _kbItems.forEach(function (item) {
    if (_kbPublicPaths[item.path]) item.public = true;
  });
}

function kbPublicUrl(path) {
  return (_kbPublicBase || '') + '/reports/' + path.split('/').map(encodeURIComponent).join('/');
}

async function kbDrawerTogglePublic() {
  var item = kbFindItem(_kbDrawerPath);
  if (!item) return;
  var next = !item.public;
  var data = await safeFetch(API + '/kb/public', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: item.path, public: next }),
  });
  if (!data || data.ok === false) {
    showToast((data && data.error) || '公开状态没有保存成功', 'error');
    return;
  }
  item.public = data.public;
  if (data.public) _kbPublicPaths[item.path] = true;
  else delete _kbPublicPaths[item.path];
  if (_kbTree) _kbTree.public = Math.max(0, (_kbTree.public || 0) + (data.public ? 1 : -1));
  kbRenderCounts();
  kbFillDrawer(item, { preview: item.preview });
  kbRenderList();
  showToast(data.public ? '已公开 · 点「复制链接」发出去' : '已取消公开', data.public ? 'success' : 'info');
}

function kbCopyPublicLink() {
  var item = kbFindItem(_kbDrawerPath);
  if (!item) return;
  var url = kbPublicUrl(item.path);
  function copied() { showToast('公开链接已复制', 'success'); }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(copied, function () { window.prompt('复制公开链接：', url); });
  } else {
    window.prompt('复制公开链接：', url);
  }
}

async function kbTogglePublic(event, index) {
  if (event) event.stopPropagation();
  var item = _kbItems[index];
  if (!item) return;
  _kbDrawerPath = item.path;
  await kbDrawerTogglePublic();
}

// ---------- 状态（已读 / 收藏） ----------

async function kbSetState(path, patch) {
  var data = await safeFetch(API + '/kb/state', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(Object.assign({ path: path }, patch)),
  });
  if (!data || data.ok === false) return null;
  return data;
}

function kbFindItem(path) {
  for (var i = 0; i < _kbItems.length; i++) {
    if (_kbItems[i].path === path) return _kbItems[i];
  }
  return null;
}

async function kbToggleStar(event, index) {
  if (event) event.stopPropagation();
  var item = _kbItems[index];
  if (!item) return;
  var next = !item.starred;
  var data = await kbSetState(item.path, { starred: next });
  if (!data) return;
  item.starred = data.starred;
  if (_kbTree) {
    _kbTree.starred = Math.max(0, (_kbTree.starred || 0) + (data.starred ? 1 : -1));
    kbRenderCounts();
  }
  var card = document.querySelector('.kb-item[data-path="' + CSS.escape(item.path) + '"]');
  if (card) {
    var btn = card.querySelector('.kb-star');
    if (btn) {
      btn.classList.toggle('is-on', data.starred);
      btn.setAttribute('aria-pressed', data.starred ? 'true' : 'false');
      btn.textContent = data.starred ? '★' : '☆';
    }
  }
  if (kbState.view === 'starred' && !data.starred) kbLoad();
  showToast(data.starred ? '已加入收藏' : '已取消收藏', 'success');
}

function kbMarkRead(item, path) {
  if (!item || item.read) return Promise.resolve();
  item.read = true;
  if (_kbTree && _kbTree.unread > 0) {
    _kbTree.unread -= 1;
    kbRenderCounts();
  }
  var card = document.querySelector('.kb-item[data-path="' + CSS.escape(path) + '"]');
  if (card) {
    card.classList.remove('is-unread');
    var state = card.querySelector('.kb-item-state');
    if (state) state.textContent = '已读';
  }
  return kbSetState(path, { read: true });
}

// ---------- 详情抽屉 ----------

function kbOpenDrawer(index) {
  var item = _kbItems[index];
  if (!item) return;
  _kbDrawerPath = item.path;
  var drawer = kbEl('kb-drawer');
  if (!drawer) return;
  drawer.classList.remove('bl-hidden');
  if (document.activeElement && document.activeElement !== document.body) {
    _kbDrawerReturnFocus = document.activeElement;
  }
  kbFillDrawer(item);
  kbLoadDetail(item.path);
  var wasRead = !!item.read;
  kbMarkRead(item, item.path).then(function () {
    if (!wasRead) {
      kbUndoToast('已标记为已读：' + item.title, function () {
        return kbSetState(item.path, { read: false });
      });
    }
  });
  var panel = drawer.querySelector('.kb-drawer-panel');
  if (panel) panel.focus();
}

function kbFillDrawer(item, detail) {
  var kicker = kbEl('kb-drawer-kicker');
  var title = kbEl('kb-drawer-title');
  var meta = kbEl('kb-drawer-meta');
  var actions = kbEl('kb-drawer-actions');
  var body = kbEl('kb-drawer-body');
  if (kicker) {
    var byAi = !!item.category_ai;
    var kickerTitle = byAi ? 'AI 分类' : '规则分类';
    if (byAi && item.category_rule && item.category_rule !== item.category_ai) {
      kickerTitle += '（规则原判为 ' + item.category_rule + '）';
    }
    kicker.innerHTML = '<span class="kb-badge kb-badge-' + kbCatSlug(item.category) + '" title="' + escAttr(kickerTitle) + '">' +
      esc(item.category) + '</span>' + (item.series ? '<span class="kb-item-series">' + esc(item.series) + '</span>' : '');
  }
  if (title) title.textContent = item.title;
  if (meta) {
    var metaParts = [];
    if (item.date) metaParts.push('<span>' + kbFmtDate(item.date) + '</span>');
    metaParts.push('<span>' + kbFmtSize(item.size) + '</span>');
    metaParts.push('<span>' + esc(item.path) + '</span>');
    if (item.created_at) metaParts.push('<span title="首次进入索引">入库 ' + kbFmtDate(String(item.created_at).slice(0, 10)) + '</span>');
    if (item.updated_at && item.updated_at !== item.created_at) {
      metaParts.push('<span title="最近一次索引更新">更新 ' + kbFmtDate(String(item.updated_at).slice(0, 10)) + '</span>');
    }
    if (item.ai_model) metaParts.push('<span title="生成 AI 摘要所用模型">模型 ' + esc(item.ai_model) + '</span>');
    if (item.category_ai) metaParts.push('<span title="AI 分类与二级系列">分类 AI</span>');
    meta.innerHTML = metaParts.join('');
  }
  var source = detail || item;
  if (actions) {
    var isPublic = !!item.public;
    actions.innerHTML =
      '<a class="kb-btn kb-btn-primary" href="' + escAttr('reports/' + item.path) + '" target="_blank" rel="noopener">打开原文 ↗</a>' +
      '<button type="button" class="kb-btn kb-btn-ghost' + (isPublic ? ' is-on' : '') + '" id="kb-drawer-public" ' +
        'aria-pressed="' + (isPublic ? 'true' : 'false') + '" onclick="kbDrawerTogglePublic()" ' +
        'title="公开后可用外链分享，无需 DSH 账号">' + (isPublic ? '◉ 已公开' : '◌ 公开') + '</button>' +
      (isPublic ? '<button type="button" class="kb-btn kb-btn-ghost" id="kb-drawer-copy" onclick="kbCopyPublicLink()">复制链接</button>' : '') +
      '<button type="button" class="kb-btn kb-btn-ghost" id="kb-drawer-star" aria-pressed="' +
        (item.starred ? 'true' : 'false') + '" onclick="kbDrawerToggleStar()">' +
        (item.starred ? '★ 已收藏' : '☆ 收藏') + '</button>' +
      '<button type="button" class="kb-btn kb-btn-ghost" id="kb-drawer-read" ' +
        'onclick="kbDrawerMarkRead()">' + (item.read ? '标记为未读' : '标记为已读') + '</button>';
  }
  if (body) {
    body.innerHTML = kbDrawerBodyHtml(item, source);
  }
}

async function kbLoadDetail(path) {
  var data = await safeFetch(API + '/kb/report/' + encodeURI(path.split('/').map(encodeURIComponent).join('/')));
  if (!data || !data.item) return;
  var item = kbFindItem(path);
  if (item) {
    item.summary = data.item.summary || item.summary;
    item.preview = data.item.preview || '';
  }
  if (_kbDrawerPath === path) kbFillDrawer(item || data.item, data.item);
}

async function kbDrawerToggleStar() {
  var item = kbFindItem(_kbDrawerPath);
  if (!item) return;
  var data = await kbSetState(item.path, { starred: !item.starred });
  if (!data) return;
  item.starred = data.starred;
  if (_kbTree) _kbTree.starred = Math.max(0, (_kbTree.starred || 0) + (data.starred ? 1 : -1));
  kbRenderCounts();
  kbFillDrawer(item, { preview: item.preview });
  kbLoad();
}

async function kbDrawerMarkRead() {
  var item = kbFindItem(_kbDrawerPath);
  if (!item) return;
  var next = !item.read;
  item.read = next;
  if (_kbTree) _kbTree.unread = Math.max(0, (_kbTree.unread || 0) + (next ? -1 : 1));
  kbRenderCounts();
  kbFillDrawer(item, { preview: item.preview });
  await kbSetState(item.path, { read: next });
  kbLoad();
}

function kbCloseDrawer() {
  var drawer = kbEl('kb-drawer');
  if (drawer) drawer.classList.add('bl-hidden');
  _kbLastClosedPath = _kbDrawerPath;
  _kbDrawerPath = '';
  // 焦点归还：优先回到打开抽屉的那张卡片，其次回到之前聚焦的元素。
  var card = _kbDrawerReturnFocus && document.contains(_kbDrawerReturnFocus)
    ? _kbDrawerReturnFocus
    : null;
  if (!card) {
    var path = _kbLastClosedPath;
    var idx = _kbItems.findIndex(function (item) { return item.path === path; });
    card = idx >= 0 ? document.querySelectorAll('.kb-item-open')[idx] : null;
  }
  _kbDrawerReturnFocus = null;
  if (card && typeof card.focus === 'function') card.focus();
}

// P1-1：抽屉打开时把 Tab / Shift+Tab 限制在抽屉内，并支持 Esc 关闭
function kbTrapDrawerKeys(event) {
  var drawer = kbEl('kb-drawer');
  if (!drawer || drawer.classList.contains('bl-hidden')) return;
  if (event.key === 'Escape') { event.preventDefault(); kbCloseDrawer(); return; }
  if (event.key !== 'Tab') return;
  var focusables = Array.prototype.slice.call(drawer.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )).filter(function (el) { return el.offsetParent !== null; });
  if (!focusables.length) return;
  var first = focusables[0], last = focusables[focusables.length - 1];
  if (event.shiftKey && (document.activeElement === first || document.activeElement === drawer)) {
    event.preventDefault(); last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault(); first.focus();
  }
}


function kbDrawerBodyHtml(item, source) {
  var parts = [];
  var hasAi = !!(item.summary_ai);
  if (hasAi) {
    parts.push('<div class="kb-ai-block">' +
      '<div class="kb-ai-head"><span class="kb-ai-mark">AI</span><span>结构化摘要</span></div>' +
      '<p class="kb-drawer-summary">' + kbHighlight(item.summary_ai, kbState.q) + '</p>');
    var points = item.key_points || [];
    if (points.length) {
      parts.push('<ul class="kb-points">' + points.map(function (point) {
        return '<li>' + esc(point) + '</li>';
      }).join('') + '</ul>');
    }
    parts.push('</div>');
    if ((item.tags || []).length) parts.push(kbTagChips(item.tags, 'kb-drawer-tags'));
    var entities = (item.entities || []).slice(0, 12);
    if (entities.length) {
      parts.push('<div class="kb-entities">' + entities.map(function (entity) {
        return '<span class="kb-entity"><i>' + esc(entity.type || '主题') + '</i>' + esc(entity.name) + '</span>';
      }).join('') + '</div>');
    }
    parts.push('<p class="kb-drawer-hint">以上由 AI 从正文抽取；原文信息以此为准。</p>');
  } else {
    var text = (source && source.preview) || (source && source.snippet) || item.snippet || item.summary || '';
    parts.push('<p class="kb-drawer-summary">' + kbHighlight(text, kbState.q) + '</p>');
    parts.push('<p class="kb-drawer-hint">还没有 AI 摘要。</p>' +
      '<button type="button" class="kb-btn kb-btn-ghost" id="kb-drawer-enrich" ' +
      'onclick="kbEnrichOne()">生成 AI 摘要</button>');
  }
  return parts.join('');
}

async function kbEnrichOne() {
  var item = kbFindItem(_kbDrawerPath);
  if (!item) return;
  var button = kbEl('kb-drawer-enrich');
  if (button) { button.disabled = true; button.textContent = '生成中…'; }
  var data = await safeFetch(API + '/kb/enrich', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: item.path })
  });
  if (!data || !data.ok) {
    if (button) { button.disabled = false; button.textContent = '生成 AI 摘要'; }
    showToast((data && data.error) || 'AI 摘要生成失败');
    return;
  }
  await kbLoadDetail(item.path);
  showToast('AI 摘要已生成');
  kbLoad();
}

// ---------- P3：AI 问答（RAG） ----------

function kbAskOpen() {
  var panel = kbEl('kb-ask');
  if (!panel) return;
  panel.classList.remove('bl-hidden');
  var input = kbEl('kb-ask-input');
  if (input) { input.focus(); input.select(); }
  kbEl('kb-ask').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function kbAskClose() {
  var panel = kbEl('kb-ask');
  if (panel) panel.classList.add('bl-hidden');
}

function kbAskRender(html) {
  var result = kbEl('kb-ask-result');
  if (!result) return;
  result.classList.remove('bl-hidden');
  result.innerHTML = html;
}

var _kbAskAbort = null;
var _kbAskTimer = null;
var _kbAskStartedAt = 0;

function kbAskResetControls() {
  var btn = kbEl('kb-ask-submit');
  var cancel = kbEl('kb-ask-cancel');
  var hint = kbEl('kb-ask-hint');
  if (btn) { btn.disabled = false; btn.textContent = '提问'; }
  if (cancel) cancel.classList.add('bl-hidden');
  if (hint) hint.textContent = '';
  if (_kbAskTimer) { clearInterval(_kbAskTimer); _kbAskTimer = null; }
  _kbAskAbort = null;
}

function kbAskCancel() {
  if (_kbAskAbort) _kbAskAbort.abort();
  kbAskRender('<p class="kb-ask-error">已停止生成。已找到的候选报告仍可点开。</p>');
  kbAskResetControls();
}

async function kbAskSubmit() {
  var input = kbEl('kb-ask-input');
  var question = input ? input.value.trim() : '';
  if (!question) { showToast('先写下你的问题'); if (input) input.focus(); return; }
  if (_kbAskAbort) _kbAskAbort.abort();
  var btn = kbEl('kb-ask-submit');
  var cancel = kbEl('kb-ask-cancel');
  var hint = kbEl('kb-ask-hint');
  if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
  if (cancel) cancel.classList.remove('bl-hidden');
  _kbAskStartedAt = Date.now();
  if (hint) hint.textContent = '已提交，正在检索相关报告…';
  if (_kbAskTimer) clearInterval(_kbAskTimer);
  _kbAskTimer = setInterval(function () {
    var seconds = Math.round((Date.now() - _kbAskStartedAt) / 1000);
    if (hint && _kbAskAbort) hint.textContent = '已用 ' + seconds + ' 秒 · 正在组织答案（通常 10–20 秒）';
  }, 1000);
  kbAskRender('<div class="kb-loading"><span class="kb-spinner" aria-hidden="true"></span>正在检索相关报告…</div>');

  // P1-2：检索本身 50ms 左右，先给候选人反馈，再等模型组织答案。
  var candidates = [];
  var searchData = await safeFetch(API + '/kb/search?q=' + encodeURIComponent(question) + '&mode=semantic&limit=6');
  if (searchData && (searchData.items || []).length) {
    candidates = searchData.items.slice(0, 4);
    var list = candidates.map(function (item) {
      return '<li><button type="button" class="kb-linkbtn" onclick="kbOpenPath(\'' + escAttr(item.path) + '\')">' +
        esc(item.title) + '</button></li>';
    }).join('');
    kbAskRender('<div class="kb-ask-cand"><div class="kb-ask-cand-head">已找到 ' + searchData.total +
      ' 篇候选报告，正在据此组织答案…</div><ul>' + list + '</ul></div>' +
      '<div class="kb-loading"><span class="kb-spinner" aria-hidden="true"></span>模型组织答案中（可随时停止）</div>');
  }

  var controller = new AbortController();
  _kbAskAbort = controller;
  var data = null;
  try {
    var response = await fetch(API + '/kb/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question }),
      signal: controller.signal,
    });
    data = await response.json().catch(function () { return null; });
  } catch (error) {
    if (error && error.name === 'AbortError') return;
    data = null;
  }
  if (controller.signal.aborted) return;
  kbAskResetControls();
  var seconds = Math.max(1, Math.round((Date.now() - _kbAskStartedAt) / 1000));
  if (!data) { kbAskRender('<p class="kb-ask-error">请求失败，请稍后重试（已用 ' + seconds + ' 秒）。</p>'); return; }
  if (!data.ok) {
    kbAskRender('<p class="kb-ask-error">' + esc(data.error || '问答失败') + '（已用 ' + seconds + ' 秒）</p>');
    return;
  }
  var hint2 = kbEl('kb-ask-hint');
  if (hint2) hint2.textContent = '用时 ' + seconds + ' 秒 · 答案只依据引用的报告';
  var html = '<div class="kb-ask-answer">' + kbAskFormat(data.answer || '') + '</div>';
  var citations = data.citations || [];
  if (citations.length) {
    html += '<div class="kb-ask-cites"><div class="kb-ask-cites-head">来源</div><ul>' +
      citations.map(function (cite) {
        return '<li><span class="kb-cite-n">[' + cite.n + ']</span>' +
          '<button type="button" class="kb-linkbtn" onclick="kbOpenPath(\'' + escAttr(cite.path) + '\')">' +
          esc(cite.title) + '</button>' +
          (cite.date ? ' <time>' + kbFmtDate(cite.date) + '</time>' : '') + '</li>';
      }).join('') + '</ul></div>';
  }
  var related = data.related || [];
  if (related.length) {
    html += '<div class="kb-ask-related"><div class="kb-ask-cites-head">相关报告</div><ul>' +
      related.map(function (item) {
        return '<li><button type="button" class="kb-linkbtn" onclick="kbOpenPath(\'' + escAttr(item.path) + '\')">' +
          esc(item.title) + '</button>' + (item.date ? ' <time>' + kbFmtDate(item.date) + '</time>' : '') + '</li>';
      }).join('') + '</ul></div>';
  }
  kbAskRender(html);
}

// ---------- P5：周报 / 主题聚合 ----------

function kbDigestOpen() {
  kbAskClose();
  var panel = kbEl('kb-digest');
  if (!panel) return;
  panel.classList.remove('bl-hidden');
  panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  kbDigestLoad(false);
}

function kbDigestClose() {
  var panel = kbEl('kb-digest');
  if (panel) panel.classList.add('bl-hidden');
}

function kbDigestHint(text) {
  var hint = kbEl('kb-digest-hint');
  if (hint) hint.textContent = text || '';
}

function kbDigestRender(html) {
  var body = kbEl('kb-digest-body');
  if (body) body.innerHTML = html;
}

async function kbDigestLoad(refresh) {
  var body = kbEl('kb-digest-body');
  if (body) body.innerHTML = '<div class="kb-loading"><span class="kb-spinner" aria-hidden="true"></span>' +
    (refresh ? '正在重新统计并生成周报（约 20-40 秒）…' : '正在读取周报…') + '</div>';
  kbDigestHint('');
  var data = await safeFetch(API + '/kb/digest?days=7' + (refresh ? '&refresh=1' : ''));
  if (!data) {
    kbDigestRender('<p class="kb-ask-error">请求失败，请稍后重试。</p>');
    return;
  }
  if (!data.ok) {
    kbDigestRender('<p class="kb-ask-error">' + esc(data.error || '周报生成失败') + '</p>');
    return;
  }
  if (!data.total) {
    kbDigestRender('<p class="kb-ask-error">最近 7 天还没有新报告。</p>');
    return;
  }
  var meta = ['窗口 ' + kbFmtDate(data.since) + ' → ' + kbFmtDate(data.until), data.total + ' 篇报告'];
  if (data.ai_enriched) meta.push(data.ai_enriched + ' 篇有 AI 摘要');
  meta.push(data.mode === 'ai' ? '模型整理' : '统计视图');
  if (data.generated_at_ts) {
    var generatedTs = Date.parse(data.generated_at_ts);
    if (!isNaN(generatedTs)) meta.push('生成于 ' + kbAgo(generatedTs));
  } else if (data.cached) {
    meta.push('今天已生成');
  }
  if (data.new_since) meta.push('窗口内新增 ' + data.new_since + ' 篇，可重新生成');
  else if (data.cached && data.mode === 'ai') meta.push('暂无新增，可直接复用');
  var html = '<p class="kb-digest-meta">' + esc(meta.join(' · ')) + '</p>';
  html += '<p class="kb-digest-overview">' + kbAskFormat(data.overview || '') + '</p>';
  var themes = data.themes || [];
  if (themes.length) {
    html += '<div class="kb-digest-themes">' + themes.map(function (theme, index) {
      var reports = theme.reports || [];
      var links = reports.map(function (item) {
        return '<button type="button" class="kb-linkbtn" onclick="kbOpenPath(\'' + escAttr(item.path) + '\')">' +
          esc(item.title) + '</button>';
      }).join('、');
      return '<article class="kb-theme">' +
        '<div class="kb-theme-head"><span class="kb-theme-index">' + (index + 1) + '</span>' +
        '<span class="kb-theme-name">' + esc(theme.name) + '</span></div>' +
        '<p class="kb-theme-summary">' + esc(theme.summary || '') + '</p>' +
        (links ? '<p class="kb-theme-reports">相关报告：' + links + '</p>' : '') +
        '</article>';
    }).join('') + '</div>';
  }
  var chips = function (rows, cls) {
    return (rows || []).map(function (row) {
      return '<span class="kb-digest-chip ' + cls + '">' + esc(row.name) + '<em>' + row.count + '</em></span>';
    }).join('');
  };
  var stats = '';
  if (data.categories && data.categories.length) {
    stats += '<div class="kb-digest-stat"><div class="kb-digest-stat-head">分类</div><div class="kb-digest-chips">' +
      chips(data.categories, 'is-cat') + '</div></div>';
  }
  if (data.tags && data.tags.length) {
    stats += '<div class="kb-digest-stat"><div class="kb-digest-stat-head">高频标签</div><div class="kb-digest-chips">' +
      chips(data.tags, 'is-tag') + '</div></div>';
  }
  if (data.series && data.series.length) {
    stats += '<div class="kb-digest-stat"><div class="kb-digest-stat-head">系列</div><div class="kb-digest-chips">' +
      chips(data.series, 'is-series') + '</div></div>';
  }
  if (stats) html += '<div class="kb-digest-stats">' + stats + '</div>';
  kbDigestRender(html);
  kbDigestHint('生成于 ' + (data.generated_at || '') + (data.cached ? '（缓存）' : '') + (data.model ? ' · ' + data.model : ''));
}

// 极简 Markdown 渲染：只处理换行、粗体 **x** 与 [n] 引用样式，其余一律转义。
function kbAskFormat(text) {
  var safe = esc(text);
  safe = safe.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  safe = safe.replace(/\[\s*(\d+)\s*\]/g, '<span class="kb-cite-n">[$1]</span>');
  return safe.split(/\n{2,}/).map(function (block) {
    var lines = block.split(/\n/).filter(function (line) { return line.trim(); });
    if (!lines.length) return '';
    if (lines.length === 1) return '<p>' + lines[0] + '</p>';
    return '<ul>' + lines.map(function (line) {
      return '<li>' + line.replace(/^[-*·]\s*/, '') + '</li>';
    }).join('') + '</ul>';
  }).join('');
}

// 从问答/引用里按路径打开抽屉（不在当前列表里也能看）。
async function kbOpenPath(path) {
  var index = _kbItems.findIndex(function (item) { return item.path === path; });
  if (index >= 0) { kbOpenDrawer(index); return; }
  var item = { path: path, title: path.split('/').pop(), category: '', series: '', date: '', size: 0, read: true, starred: false };
  _kbDrawerPath = path;
  var drawer = kbEl('kb-drawer');
  if (!drawer) return;
  drawer.classList.remove('bl-hidden');
  kbFillDrawer(item);
  kbLoadDetail(path);
}


// ---------- 批量操作 ----------

function kbToggleSelectMode(force) {
  _kbSelectMode = typeof force === 'boolean' ? force : !_kbSelectMode;
  if (!_kbSelectMode) _kbSelected = {};
  var btn = kbEl('kb-select-toggle');
  if (btn) {
    btn.classList.toggle('is-on', _kbSelectMode);
    btn.setAttribute('aria-pressed', _kbSelectMode ? 'true' : 'false');
  }
  kbRenderList();
  kbRenderBatchbar();
}

function kbToggleSelect(input) {
  var path = input.getAttribute('data-path') || '';
  if (!path) return;
  if (input.checked) _kbSelected[path] = true;
  else delete _kbSelected[path];
  var card = input.closest('.kb-item');
  if (card) card.classList.toggle('is-selected', input.checked);
  kbRenderBatchbar();
}

function kbSelectedPaths() {
  return Object.keys(_kbSelected).filter(function (path) { return _kbSelected[path]; });
}

function kbRenderBatchbar() {
  var bar = kbEl('kb-batchbar');
  if (!bar) return;
  bar.classList.toggle('bl-hidden', !_kbSelectMode);
  if (!_kbSelectMode) return;
  var paths = kbSelectedPaths();
  var count = kbEl('kb-batch-count');
  if (count) count.textContent = '已选 ' + paths.length + ' 篇';
  bar.querySelectorAll('[data-batch="copy"]').forEach(function (btn) {
    var hasPublic = paths.some(function (path) { return !!_kbPublicPaths[path]; });
    btn.disabled = !hasPublic;
  });
  ['read', 'star', 'public', 'unpublic'].forEach(function (action) {
    bar.querySelectorAll('[data-batch="' + action + '"]').forEach(function (btn) {
      btn.disabled = paths.length === 0;
    });
  });
}

async function kbBatchAction(action) {
  if (action === 'exit') { kbToggleSelectMode(false); return; }
  if (action === 'all') {
    _kbItems.forEach(function (item) { _kbSelected[item.path] = true; });
    kbRenderList();
    kbRenderBatchbar();
    return;
  }
  if (action === 'clear') {
    _kbSelected = {};
    kbRenderList();
    kbRenderBatchbar();
    return;
  }
  var paths = kbSelectedPaths();
  if (!paths.length) { showToast('先勾选要操作的报告'); return; }
  // P2-5：撤销要还原“操作前”的状态，而不是机械取反（否则会把本来就收藏的条目一起取消）。
  var before = {};
  _kbItems.forEach(function (item) {
    if (paths.indexOf(item.path) !== -1) {
      before[item.path] = { read: !!item.read, starred: !!item.starred, public: !!item.public };
    }
  });
  if (action === 'copy') {
    var urls = paths.filter(function (path) { return !!_kbPublicPaths[path]; }).map(kbPublicUrl);
    if (!urls.length) { showToast('选中的报告都还没公开'); return; }
    await kbCopyText(urls.join('\n'), '已复制 ' + urls.length + ' 条公开链接');
    return;
  }
  var payload = { paths: paths };
  var label = '';
  if (action === 'read') { payload.read = true; label = '已标记已读'; }
  else if (action === 'star') { payload.starred = true; label = '已收藏'; }
  else if (action === 'public') { payload.public = true; label = '已公开'; }
  else if (action === 'unpublic') { payload.public = false; label = '已取消公开'; }
  var data = await safeFetch(API + '/kb/bulk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!data) return;
  _kbSelected = {};
  showToast(label + '（' + (data.updated || 0) + ' 篇）' + ((data.missing || []).length ? '，' + data.missing.length + ' 篇未找到' : ''));
  if (action === 'read' || action === 'star' || action === 'public' || action === 'unpublic') {
    var field = action === 'read' ? 'read' : (action === 'star' ? 'starred' : 'public');
    var target = action === 'public';
    var revert = paths.filter(function (path) {
      var prev = before[path];
      if (!prev) return false;
      return action === 'unpublic' ? prev.public === true : prev[field] !== target;
    });
    if (revert.length) {
      kbUndoToast(label + '：' + (data.updated || 0) + ' 篇', function () {
        var body = { paths: revert };
        body[field] = action === 'unpublic';
        if (action === 'read') body.read = false;
        return fetch(API + '/kb/bulk', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }).then(function (resp) { return resp.json(); }).then(function () {
          kbLoadTree();
          kbLoad();
        });
      });
    }
  }
  await kbLoadPublicState();
  kbLoadTree();
  kbLoad();
}

async function kbCopyText(text, okMessage) {
  try {
    await navigator.clipboard.writeText(text);
    showToast(okMessage || '已复制');
  } catch (err) {
    window.prompt('复制下面的内容：', text);
  }
}

// ---------- 搜索历史 ----------

function kbReadStore(key, fallback) {
  try {
    var raw = window.localStorage.getItem(key);
    var value = raw ? JSON.parse(raw) : fallback;
    return Array.isArray(value) ? value : fallback;
  } catch (err) {
    return fallback;
  }
}

function kbWriteStore(key, value) {
  try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (err) { /* 隐私模式忽略 */ }
}

function kbLoadHistory() { _kbHist = kbReadStore(KB_HIST_KEY, []); }

function kbPushHistory(query) {
  var q = (query || '').trim();
  if (q.length < 2) return;
  _kbHist = _kbHist.filter(function (item) { return item !== q; });
  _kbHist.unshift(q);
  _kbHist = _kbHist.slice(0, 8);
  kbWriteStore(KB_HIST_KEY, _kbHist);
  kbRenderHistory();
}

function kbRenderHistory() {
  var box = kbEl('kb-search-hist');
  var input = kbEl('kb-search');
  if (!box || !input) return;
  if (!_kbHist.length || input.value) { box.classList.add('bl-hidden'); return; }
  box.innerHTML = _kbHist.map(function (q, index) {
    return '<span class="kb-hist-row" role="option">' +
      '<button type="button" class="kb-hist-item" data-hist="' + escAttr(q) + '" onclick="kbUseHistory(this.dataset.hist)">' +
        esc(q) + '</button>' +
      '<button type="button" class="kb-iconbtn kb-hist-del" aria-label="删除这条搜索历史" ' +
        'onclick="kbDeleteHistory(' + index + ')">✕</button></span>';
  }).join('');
  box.classList.remove('bl-hidden');
}

function kbUseHistory(q) {
  var input = kbEl('kb-search');
  if (input) input.value = q;
  kbState.q = q;
  kbState.offset = 0;
  var box = kbEl('kb-search-hist');
  if (box) box.classList.add('bl-hidden');
  kbLoadTree();
  kbLoad();
}

function kbDeleteHistory(index) {
  _kbHist.splice(index, 1);
  kbWriteStore(KB_HIST_KEY, _kbHist);
  kbRenderHistory();
}

function kbBindHistory() {
  var input = kbEl('kb-search');
  var box = kbEl('kb-search-hist');
  if (!input || !box) return;
  input.addEventListener('focus', function () { kbRenderHistory(); });
  input.addEventListener('blur', function () {
    setTimeout(function () {
      if (document.activeElement === input) return;
      box.classList.add('bl-hidden');
    }, 180);
  });
}

// ---------- 已保存筛选 ----------

function kbLoadSaved() { _kbSaved = kbReadStore(KB_SAVED_KEY, []); }

function kbFilterSnapshot() {
  return {
    q: kbState.q,
    category: kbState.category,
    series: kbState.series,
    dateFrom: kbState.dateFrom,
    dateTo: kbState.dateTo,
    view: kbState.view,
    sort: kbState.sort,
    mode: kbState.mode,
  };
}

function kbFilterLabel(snap) {
  var parts = [];
  if (snap.q) parts.push('「' + snap.q + '」');
  if (snap.category) parts.push(snap.category);
  if (snap.series) parts.push(snap.series);
  if (snap.dateFrom || snap.dateTo) parts.push((snap.dateFrom || '…') + '→' + (snap.dateTo || '…'));
  if (snap.view && snap.view !== 'all') parts.push({ unread: '未读', starred: '收藏', public: '公开' }[snap.view] || snap.view);
  return parts.join(' · ') || '全部报告';
}

function kbSaveFilter() {
  var name = window.prompt('给当前筛选起个名字：', kbFilterLabel(kbFilterSnapshot()));
  if (name === null) return;
  name = (name || '').trim();
  if (!name) { showToast('名字不能为空'); return; }
  _kbSaved.unshift({ name: name.slice(0, 40), snap: kbFilterSnapshot() });
  _kbSaved = _kbSaved.slice(0, 10);
  kbWriteStore(KB_SAVED_KEY, _kbSaved);
  kbRenderSaved();
  showToast('已保存筛选「' + name + '」');
}

async function kbApplySaved(index) {
  var entry = _kbSaved[index];
  if (!entry || !entry.snap) return;
  var snap = entry.snap;
  kbState.q = snap.q || '';
  kbState.category = snap.category || '';
  kbState.series = snap.series || '';
  kbState.date = '';
  kbState.dateFrom = snap.dateFrom || '';
  kbState.dateTo = snap.dateTo || '';
  kbState.view = snap.view || 'all';
  kbState.sort = snap.sort || 'date_desc';
  kbState.mode = snap.mode || 'hybrid';
  kbState.offset = 0;
  var input = kbEl('kb-search');
  if (input) input.value = kbState.q;
  var sort = kbEl('kb-sort');
  if (sort) sort.value = kbState.sort;
  var clear = kbEl('kb-search-clear');
  if (clear) clear.classList.toggle('bl-hidden', !kbState.q);
  kbSyncDateInputs();
  kbRenderMode();
  kbRenderDates();
  kbLoadTree();
  kbLoad();
}

function kbDeleteSaved(index) {
  _kbSaved.splice(index, 1);
  kbWriteStore(KB_SAVED_KEY, _kbSaved);
  kbRenderSaved();
}

function kbRenderSaved() {
  var box = kbEl('kb-saved');
  var wrap = kbEl('kb-saved-wrap');
  if (!box) return;
  if (wrap) wrap.classList.toggle('bl-hidden', !_kbSaved.length);
  box.innerHTML = _kbSaved.map(function (entry, index) {
    return '<span class="kb-saved-row">' +
      '<button type="button" class="kb-saved-item" data-saved="' + index + '" title="' + escAttr(kbFilterLabel(entry.snap || {})) + '" ' +
        'onclick="kbApplySaved(' + index + ')">' + esc(entry.name) + '</button>' +
      '<button type="button" class="kb-iconbtn kb-saved-del" aria-label="删除保存的筛选" ' +
        'onclick="kbDeleteSaved(' + index + ')">✕</button></span>';
  }).join('');
}
