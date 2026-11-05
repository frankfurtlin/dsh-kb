/* dsh-kb — core.js（源自 BetterLife v0.76 前的脚本副本）
 * 核心工具：API 基址、safeFetch、HTML 转义
 *
 * v0.67.0：个人 OS 领域下架后，本文件从「Alpine 应用初始化 + 数据层」缩回纯工具层；
 * 页面已无 Alpine（x-data 全部移除），所以也不再引 CDN 脚本。
 * 经典脚本共享全局词法作用域：**不要**再在别的文件里写 `const API = ...`，
 * 浏览器会以 "Identifier 'API' has already been declared" 让那个文件整体不执行
 * （tests/check_static_syntax.py 钉住）。
 */
// 相对路径：页面在 / 与 /kb/ 下都解析到同一套后端接口。
const API = 'api';

// ============ safeFetch ============
// 所有前端取数的唯一入口：401 提示刷新 DSH 会话、失败读后端中文说明、断网点亮离线条。
async function safeFetch(url, options = {}) {
  try {
    const r = await fetch(url, options);
    if (r.status === 401) {
      // 鉴权由 DSH 反代负责；这里收到 401 只可能是 DSH 会话失效，
      // 插件内没有登录页可跳，提示用户刷新 DSH 页面重新授权。
      showToast('DSH 会话已失效，请刷新页面后重试', 'error', 4000);
      return null;
    }
    if (!r.ok) {
      // 优先显示**后端写好的中文说明**（错误体统一为 {"ok":false,"error":"<中文>"}），
      // 拿不到时由 ui.js 的 blErrorText 按状态码兜底。旧实现把它丢掉、只显示
      // 「操作失败: HTTP 404」，于是用户看不到「报告目录不存在」这种真正有用的信息。
      var payload = null;
      try { payload = await r.json(); } catch (err) { payload = null; }
      showToast(blErrorText(r.status, payload), 'error');
      return null;
    }
    const ct = r.headers.get('content-type') || '';
    return ct.includes('json') ? r.json() : r.text();
  } catch (e) {
    // 断网和 5xx 是两回事。fetch 直接 reject（TypeError）或 navigator.onLine=false
    // 才算离线，此时把状态条点亮，别让用户以为是操作方式错了。
    const offline = (typeof navigator !== 'undefined' && navigator.onLine === false) || e instanceof TypeError;
    if (offline) updateOfflineBar(true);
    if (offline) {
      showToast('离线：数据在服务器上，恢复网络后再操作', 'warning', 4000);
    } else {
      showToast(blErrorText(null, e && e.message), 'error');
    }
    return null;
  }
}

function handleApiError(error) {
  if (error) showToast(blErrorText(null, error), 'error');
}

// ============ HTML 转义 ============
// esc() 走 textContent→innerHTML，只转 & < >，用于**文本位置**。
function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

// **属性位置**必须连引号一起转义：esc() 不转引号，放进 data-x="${esc(v)}" 仍能被一个 "
// 逃出属性（报告标题来自文件名，属于外部输入，渲染侧也要守住）。
// 用 split/join 而不是正则：`tests/tools/reach.py` 的轻量 tokenizer 不识别正则字面量，
// 正则里出现引号会让它算错函数边界（探针会直接报「无法确定边界」而不是给假结果）。
var _ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

function escAttr(s) {
  return String(s == null ? '' : s)
    .split('&').join('&amp;')
    .split('<').join('&lt;')
    .split('>').join('&gt;')
    .split('"').join('&quot;')
    .split("'").join('&#39;');
}

// 全局函数声明本身就会挂到 window 上；这里只显式导出两个别处会引用的入口。
window.safeFetch = safeFetch;
window.handleApiError = handleApiError;
