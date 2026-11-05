/* dsh-kb — ui.js（源自 BetterLife v0.76 前的脚本副本）
 * 用户体验组件：提示条（toast）、离线状态条、统一错误文案、主题切换
 *
 * ⚠️ 这里是这些组件的**唯一实现处**（tests/check_frontend_ux.py 钉住「不许出现第二份定义」）。
 * 背景：改造前 `showToast` 在 core.js / ui.js / app.js 各有一份（签名还互相冲突），
 * 经典脚本共享全局词法作用域、**后加载的静默覆盖前面的** —— 生效的永远是最后那份。
 *
 * v0.67.0：个人 OS 领域下架后，本文件收敛到「页面真正还会用到」的四件事；
 * 命令面板、全局搜索、快捷键、语音采集、弹窗/表单校验随领域一起归档
 * （原文见 retired/frontend/static/ui.js）。
 */

// ============ 统一提示（toast）============
// type ∈ success | error | warning | info；未知值按 info 处理（不抛错，提示组件不该反过来搞崩页面）
// 时长：显式传入优先；否则 成功/信息 2s、警告/错误 4s —— 报错需要更多阅读时间。
var BL_TOAST_MS = { success: 2000, info: 2000, warning: 4000, error: 4000 };
var BL_TOAST_TYPES = ['success', 'error', 'warning', 'info'];

// v0.53.0 (S8.4)：**串行队列**。
// 改造前只有一个 `.bl-toast` 节点，每次提示都直接改写它并重置计时器 —— 于是
// `showToast('保存失败', 'error')` 后紧跟一个 2 秒的成功提示，**错误提示会被顶掉并重新计时**，
// 用户根本没看到错误（提示组件把「最重要的信息」丢了，这是最不该发生的一种失败）。
// 现在：正在显示的那条**绝不被打断**，后来的排进队列，各自计各自的时长；
// 错误插到已排队的普通提示**前面**（优先，但不打断当前），同内容同类型的提示不重复排队。
var BL_TOAST_GAP = 250;      // 两条之间留一点空隙，让人看清「换了一条」而不是文案闪烁
var _toastQueue = [];
var _toastCurrent = null;

function _blToastEl() {
  var el = document.querySelector('.bl-toast');
  if (!el) {
    // 模板里通常已有这个节点（带 role/aria-live）；万一没有（如 offline.html），
    // 现场建一个 —— 旧实现是 `if (!el) return;`，于是提示**静默消失**，最难查。
    el = document.createElement('div');
    el.className = 'bl-toast';
    document.body.appendChild(el);
  }
  return el;
}

function _blToastNext() {
  _toastCurrent = null;
  if (!_toastQueue.length) return;          // 队列空：保持隐藏，下次 showToast 再唤醒
  var item = _toastQueue.shift();
  _toastCurrent = item;
  var el = _blToastEl();
  el.textContent = item.message;
  el.setAttribute('data-type', item.type);
  // 无障碍：错误用 assertive 打断播报，普通提示用 polite 排队
  el.setAttribute('role', item.type === 'error' ? 'alert' : 'status');
  el.setAttribute('aria-live', item.type === 'error' ? 'assertive' : 'polite');
  // 显隐只走 .bl-hidden（`display:none !important`）——不碰内联 style，
  // 否则会和 Alpine 的 x-show / !important 互相覆盖（改造前的写法正是靠内联 display，
  // 谁最后写谁赢，最难查）。
  el.classList.remove('bl-hidden');
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(function () {
    el.classList.add('bl-hidden');
    window._toastTimer = setTimeout(_blToastNext, BL_TOAST_GAP);
  }, item.ms);
}

function showToast(message, type = 'info', ms = null) {
  if (BL_TOAST_TYPES.indexOf(type) === -1) type = 'info';
  var item = {
    message: message,
    type: type,
    ms: ms == null ? (BL_TOAST_MS[type] || 2000) : ms,
  };

  // 同一条提示（内容 + 类型）已经在显示或排队 → 不重复排，免得连点按钮刷屏
  if (_toastCurrent && _toastCurrent.message === message && _toastCurrent.type === type) {
    return _blToastEl();
  }
  for (var i = 0; i < _toastQueue.length; i++) {
    if (_toastQueue[i].message === message && _toastQueue[i].type === type) return _blToastEl();
  }

  if (type === 'error') {
    // 错误插到已排队的普通提示前面（同类错误之间保持先后顺序）
    var at = 0;
    while (at < _toastQueue.length && _toastQueue[at].type === 'error') at++;
    _toastQueue.splice(at, 0, item);
  } else {
    _toastQueue.push(item);
  }

  if (!_toastCurrent) _blToastNext();       // 当前没有在显示的，立刻开播
  return _blToastEl();
}

// 供测试 / 诊断观察队列状态（也可在控制台里看：BLToastQueue.pending()）
window.BLToastQueue = {
  pending: function () { return _toastQueue.length; },
  showing: function () { return _toastCurrent ? _toastCurrent.message : null; },
  clear: function () {
    _toastQueue.length = 0;
    clearTimeout(window._toastTimer);
    _toastCurrent = null;
    _blToastEl().classList.add('bl-hidden');
  },
};

// ============ 离线状态条（S6.1）============
// navigator.onLine 只说明「网卡连着」，不等于「服务器可达」（在线也可能是 502/超时）。
// 所以状态条由两个信号驱动：online/offline 事件 + safeFetch 里真实的网络失败。
// v0.67.0：这里仍是唯一实现处，页面只剩知识库与四个子站，故只保留这一个信号源。
var _offlineBarWarned = false;
function updateOfflineBar(offline) {
  var bar = document.getElementById('offline-bar');
  if (bar) bar.classList.toggle('bl-hidden', !offline);
  if (offline && !_offlineBarWarned) {
    _offlineBarWarned = true;
    showToast('离线：界面还能打开，但数据读写都不可用', 'warning', 4000);
  }
  if (!offline) _offlineBarWarned = false;
}

function initOfflineWatch() {
  updateOfflineBar(!navigator.onLine);
  window.addEventListener('online', function () { updateOfflineBar(false); showToast('网络已恢复'); });
  window.addEventListener('offline', function () { updateOfflineBar(true); });
}

// ============ 统一错误文案 ============
// 面向用户的错误文案必须**全中文**（与后端 check_error_language.py 同一条口径）。
// 优先用后端给的说法（v0.48.4 起错误体统一为 {"ok":false,"error":"<中文说明>"}），
// 拿不到或不是中文时按状态码兜底。旧的 `操作失败: HTTP 404` 把后端写好的
// 中文原因丢掉了，用户只能看到一串数字。
var _BL_CJK = /[\u4e00-\u9fff]/;
var BL_ERROR_TEXT = {
  400: '请求有问题，检查一下再试',
  403: '没有权限做这个操作',
  404: '资源不存在',
  405: '这个操作不支持',
  409: '数据有冲突，刷新后再试',
  413: '内容太大了，精简一下',
  422: '填写内容不合法',
  429: '操作太频繁了，稍等一下',
  500: '服务器开小差了，稍后再试',
  502: '服务器开小差了，稍后再试',
  503: '被限流了，稍等几秒再试',
  504: '服务器响应超时，稍后再试'
};

function blErrorText(status, payload) {
  var msg = '';
  if (payload && typeof payload === 'object') msg = payload.error || payload.message || '';
  else if (typeof payload === 'string') msg = payload;
  if (msg && _BL_CJK.test(msg)) return msg;          // 后端文案优先（且必须是中文）
  if (status && BL_ERROR_TEXT[status]) return BL_ERROR_TEXT[status];
  return '操作失败，请稍后再试';
}


function toggleTheme() {
  const currentTheme = document.documentElement.getAttribute('data-theme');
  const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
  
  document.documentElement.setAttribute('data-theme', newTheme);
  localStorage.setItem('bl_theme', newTheme);
  
  showToast(`已切换到${newTheme === 'dark' ? '深色' : '浅色'}模式`);
}

// ============ 显式导出（全局函数声明已在 window 上，这里只补队列调试入口） ============
window.showToast = showToast;
window.updateOfflineBar = updateOfflineBar;
window.initOfflineWatch = initOfflineWatch;
window.toggleTheme = toggleTheme;
