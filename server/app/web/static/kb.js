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
  date: '',
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
var _kbEmbedAvailable = false;

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
  if (kbState.date) { params.set('date_from', kbState.date); params.set('date_to', kbState.date); }
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
  ['hybrid', 'lexical'].forEach(function (mode) {
    var btn = kbEl('kb-mode-' + mode);
    if (!btn) return;
    var on = kbState.mode === mode;
    btn.classList.toggle('is-on', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.disabled = mode === 'hybrid' && !_kbEmbedAvailable;
    if (mode === 'hybrid') btn.title = _kbEmbedAvailable
      ? '用本地向量模型做语义检索，适合描述性提问'
      : '语义索引还在生成中，暂时用关键词检索';
  });
}

function kbSetMode(mode) {
  if (mode === 'hybrid' && !_kbEmbedAvailable) { showToast('语义索引还在生成中，先用关键词检索'); return; }
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

function kbBindKeys() {
  document.addEventListener('keydown', function (event) {
    var meta = event.metaKey || event.ctrlKey;
    if (meta && String(event.key).toLowerCase() === 'k') {
      event.preventDefault();
      var input = kbEl('kb-search');
      if (input) { input.focus(); input.select(); }
      return;
    }
    if (event.key === 'Escape') {
      var drawer = kbEl('kb-drawer');
      if (drawer && !drawer.classList.contains('bl-hidden')) { kbCloseDrawer(); return; }
      var search = kbEl('kb-search');
      if (search && document.activeElement === search && kbState.q) { kbClearSearch(); }
    }
  });
}

// ---------- 左导航（树 / 计数） ----------

async function kbLoadTree(force) {
  var url = API + '/kb/tree' + (force ? '?refresh=1' : '');
  var data = await safeFetch(url);
  if (!data) return;
  _kbTree = data;
  kbRenderCounts();
  kbRenderCats();
  kbRenderDates();
  var hint = kbEl('kb-side-hint');
  if (hint) hint.textContent = data.total + ' 篇 · ' + (data.dates || []).length + ' 个日期';
}

function kbRenderCounts() {
  var tree = _kbTree || { total: 0, unread: 0, starred: 0 };
  var map = { all: tree.total, unread: tree.unread, starred: tree.starred };
  Object.keys(map).forEach(function (view) {
    var node = kbEl('count-' + view);
    if (node) node.textContent = map[view];
    var btn = document.querySelector('.kb-navitem[data-view="' + view + '"]');
    if (btn) btn.setAttribute('aria-pressed', kbState.view === view ? 'true' : 'false');
  });
  var subtotal = kbEl('kb-subtotal');
  if (subtotal && tree.total) subtotal.textContent = tree.total + ' 篇报告 · 分类浏览与全文检索';
}

function kbRenderCats() {
  var box = kbEl('kb-cats');
  if (!box) return;
  var tree = _kbTree || { categories: [], series: [] };
  var html = '<button type="button" class="kb-cat" data-cat="" aria-pressed="' +
    (kbState.category ? 'false' : 'true') + '" onclick="kbSetCategory(\'\')">' +
    '<span class="kb-cat-name">全部分类</span><span class="kb-cat-n">' + (tree.total || 0) + '</span></button>';
  (tree.categories || []).forEach(function (cat) {
    var active = kbState.category === cat.name;
    html += '<button type="button" class="kb-cat kb-cat-' + kbCatSlug(cat.name) + '" ' +
      'data-cat="' + escAttr(cat.name) + '" aria-pressed="' + (active ? 'true' : 'false') + '" ' +
      'onclick="kbSetCategory(this.dataset.cat)">' +
      '<span class="kb-cat-name">' + esc(cat.name) + '</span>' +
      '<span class="kb-cat-n">' + cat.count + '</span></button>';
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
  var url = (searching && kbState.mode === 'hybrid')
    ? API + '/kb/search?' + kbQuery() + '&mode=hybrid'
    : API + '/kb?' + kbQuery() + (force ? '&refresh=1' : '');
  var data = await safeFetch(url);
  if (data === null) return;
  _kbItems = data.items || [];
  _kbTotal = data.total || 0;
  kbRenderList();
  kbRenderFilterbar();
  kbRenderPager();
  var result = kbEl('kb-result');
  if (result) result.textContent = kbResultLabel();
}

function kbResultLabel() {
  if (!_kbTotal) return '没有结果';
  var from = kbState.offset + 1;
  var to = Math.min(kbState.offset + _kbItems.length, _kbTotal);
  return from + '–' + to + ' / ' + _kbTotal;
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
    return '<span class="kb-tag" data-tag="' + escAttr(tag) + '">#' + esc(tag) + '</span>';
  }).join('') + '</div>';
}

function kbCard(item, index) {
  var searching = !!kbState.q;
  // 搜索时优先显示命中片段；平时优先显示 AI 摘要（信息密度高于正文首个 <p>）。
  var summary = searching
    ? ((item.match === 'body' && item.snippet) ? item.snippet : (item.snippet || item.summary || ''))
    : (item.summary_ai || item.summary || '');
  var hasAi = !!item.summary_ai;
  return '<article class="kb-item' + (item.read ? '' : ' is-unread') + '" data-path="' + escAttr(item.path) + '">' +
    '<div class="kb-item-top">' +
      '<span class="kb-badge kb-badge-' + kbCatSlug(item.category) + '">' + esc(item.category) + '</span>' +
      (item.series ? '<span class="kb-item-series">' + esc(item.series) + '</span>' : '') +
      (hasAi ? '<span class="kb-ai-mark" title="AI 已生成结构化摘要">AI</span>' : '') +
      (item.match === 'semantic' ? '<span class="kb-semantic-mark" title="语义相似命中">语义</span>' : '') +
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

function kbSetDate(date) {
  kbState.date = kbState.date === date ? '' : (date || '');
  kbState.offset = 0;
  kbRenderDates();
  kbLoad();
}

function kbSetView(view) {
  kbState.view = view;
  kbState.offset = 0;
  kbRenderCounts();
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
  clearTimeout(_kbTimer);
  _kbTimer = setTimeout(function () {
    kbState.offset = 0;
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
  kbState.date = '';
  kbState.view = 'all';
  kbState.sort = 'date_desc';
  kbState.offset = 0;
  var input = kbEl('kb-search');
  if (input) input.value = '';
  var sort = kbEl('kb-sort');
  if (sort) sort.value = 'date_desc';
  kbRenderCounts();
  kbRenderCats();
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
  if (kbState.date) chips.push(['日期：' + kbFmtDate(kbState.date), 'kbResetDates']);
  if (kbState.view !== 'all') chips.push([kbState.view === 'unread' ? '仅未读' : '仅收藏', 'kbClearView']);
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
  kbFillDrawer(item);
  kbLoadDetail(item.path);
  kbMarkRead(item, item.path);
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
    kicker.innerHTML = '<span class="kb-badge kb-badge-' + kbCatSlug(item.category) + '">' +
      esc(item.category) + '</span>' + (item.series ? '<span class="kb-item-series">' + esc(item.series) + '</span>' : '');
  }
  if (title) title.textContent = item.title;
  if (meta) {
    meta.innerHTML = (item.date ? '<span>' + kbFmtDate(item.date) + '</span>' : '') +
      '<span>' + kbFmtSize(item.size) + '</span>' +
      '<span>' + esc(item.path) + '</span>';
  }
  var source = detail || item;
  if (actions) {
    actions.innerHTML =
      '<a class="kb-btn kb-btn-primary" href="' + escAttr('reports/' + item.path) + '" target="_blank" rel="noopener">打开原文 ↗</a>' +
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
  _kbDrawerPath = '';
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

async function kbAskSubmit() {
  var input = kbEl('kb-ask-input');
  var question = input ? input.value.trim() : '';
  if (!question) { showToast('先写下你的问题'); if (input) input.focus(); return; }
  var btn = kbEl('kb-ask-submit');
  if (btn) { btn.disabled = true; btn.textContent = '思考中…'; }
  kbAskRender('<div class="kb-loading"><span class="kb-spinner" aria-hidden="true"></span>正在检索报告并组织答案…</div>');
  var data = await safeFetch(API + '/kb/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question: question }),
  });
  if (btn) { btn.disabled = false; btn.textContent = '提问'; }
  if (!data) { kbAskRender('<p class="kb-ask-error">请求失败，请稍后重试。</p>'); return; }
  if (!data.ok) {
    kbAskRender('<p class="kb-ask-error">' + esc(data.error || '问答失败') + '</p>');
    return;
  }
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
