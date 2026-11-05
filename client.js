/**
 * dsh-kb —— client 半（浏览器）。
 *
 * 手写 ModuleLoader 工厂，不经打包：只依赖宿主已提供的 react，用 h() 直接构造元素。
 * 做两件事：
 *   1. 在侧栏常驻行（sidebar.panellist）注册一行「知识库」（order 15，落在
 *      「Automation tasks」(10) 与「紫微斗数」(20) 之间）。
 *   2. 在 main 座位注册 keyed 面板页：同源 iframe 指向宿主反代的 /kb/，
 *      工具条提供 返回对话 / 刷新 / 新窗口打开；报告正文也在 iframe 内打开。
 */
window.__ModuleLoader__.load({
  id: "dsh-kb",
  factory: function (require) {
    var module = { exports: {} };
    var exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });

    var react = require("react");
    var h = react.createElement;

    /** 侧栏行 id —— 同时是 main 座位的 key 与图标上的 data 锚点。 */
    var PANEL_ID = "kb";
    /** 侧栏顺序：Automation tasks = 10，紫微斗数 = 20。 */
    var PANEL_ORDER = 15;
    /** 宿主反代的地址（带尾斜杠，应用内部全用相对路径）。 */
    var APP_URL = "/kb/";

    var COLORS = {
      border: "color-mix(in srgb, currentColor 18%, transparent)",
      muted: "color-mix(in srgb, currentColor 58%, transparent)",
      chip: "color-mix(in srgb, currentColor 8%, transparent)",
    };

    /** 侧栏行图标：四宫格（与知识库卡片列表的观感一致）。 */
    function KbPanelIcon(props) {
      var size = (props && props.size) || 16;
      return h(
        "svg",
        {
          "data-dsh-panel-entry": PANEL_ID,
          viewBox: "0 0 16 16",
          width: size,
          height: size,
          fill: "none",
          stroke: "currentColor",
          strokeWidth: "1.3",
          strokeLinecap: "round",
          strokeLinejoin: "round",
          "aria-hidden": "true",
        },
        h("rect", { x: "2", y: "2", width: "4.8", height: "4.8", rx: "1" }),
        h("rect", { x: "9.2", y: "2", width: "4.8", height: "4.8", rx: "1" }),
        h("rect", { x: "2", y: "9.2", width: "4.8", height: "4.8", rx: "1" }),
        h("rect", { x: "9.2", y: "9.2", width: "4.8", height: "4.8", rx: "1" }),
      );
    }

    function ToolbarButton(props) {
      var disabled = !!props.disabled;
      return h(
        "button",
        {
          type: "button",
          onClick: props.onClick,
          disabled: disabled,
          title: props.title || "",
          "data-dsh-kb-action": props.action || "",
          style: {
            padding: "5px 12px",
            borderRadius: 7,
            border: "1px solid " + COLORS.border,
            background:
              props.primary && !disabled
                ? "color-mix(in srgb, currentColor 12%, transparent)"
                : "transparent",
            color: "inherit",
            font: "inherit",
            fontSize: 13,
            cursor: disabled ? "default" : "pointer",
            opacity: disabled ? 0.45 : 1,
            whiteSpace: "nowrap",
          },
        },
        props.children,
      );
    }

    /** 主面板页：工具条 + 同源 iframe。 */
    function KbPanelPage(props) {
      var injected = props || {};
      var frameRef = react.useRef(null);

      var reload = function () {
        var frame = frameRef.current;
        // 直接改 DOM src：只重载 iframe，不重挂整个面板组件。
        if (frame) frame.src = APP_URL + "?t=" + Date.now();
      };
      var backToConversation = function () {
        try {
          if (injected.selectConversation) injected.selectConversation();
        } catch (error) {
          console.warn("[dsh-kb] 返回对话失败:", error);
        }
      };

      return h(
        "div",
        {
          "data-dsh-kb-panel": "",
          style: { display: "flex", flexDirection: "column", height: "100%", minHeight: 0 },
        },
        h(
          "div",
          {
            style: {
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "8px 14px",
              borderBottom: "1px solid " + COLORS.border,
              flexShrink: 0,
            },
          },
          h("span", { style: { fontSize: 14, fontWeight: 600 } }, "知识库"),
          h(
            "span",
            { style: { fontSize: 12, color: COLORS.muted } },
            "报告 · 搜索 · 分类浏览",
          ),
          h("span", { style: { flex: 1 } }),
          h(
            ToolbarButton,
            { action: "back", onClick: backToConversation, title: "回到对话" },
            "返回对话",
          ),
          h(ToolbarButton, { action: "reload", onClick: reload, title: "重新加载知识库" }, "刷新"),
          h(
            "a",
            {
              "data-dsh-kb-action": "open",
              href: APP_URL,
              target: "_blank",
              rel: "noreferrer",
              style: { fontSize: 12, color: COLORS.muted, textDecoration: "none" },
            },
            "新窗口打开",
          ),
        ),
        h("iframe", {
          ref: frameRef,
          src: APP_URL,
          title: "知识库",
          "data-dsh-kb-frame": "",
          style: { flex: 1, minHeight: 0, width: "100%", border: 0, background: "transparent" },
        }),
      );
    }

    var inject = ["slots", "layout"];

    function apply(ctx) {
      var disposers = [];

      var selectConversation = function () {
        try {
          ctx.layout.selectPanel(null);
        } catch (error) {
          console.warn("[dsh-kb] 返回对话失败:", error);
        }
      };

      try {
        var slots = ctx.slots;
        disposers.push(
          slots.inject("sidebar.panellist", function () {
            return slots.register(
              {
                name: "sidebar.panellist",
                id: PANEL_ID,
                order: PANEL_ORDER,
                label: function () {
                  return "知识库";
                },
              },
              KbPanelIcon,
            );
          }),
        );
        disposers.push(
          slots.inject("main", function () {
            return slots.register(
              {
                name: "main",
                key: PANEL_ID,
                inject: function () {
                  return { selectConversation: selectConversation };
                },
              },
              KbPanelPage,
            );
          }),
        );
      } catch (error) {
        console.warn("[dsh-kb] panel registration failed:", error);
      }

      ctx.effect(function () {
        return function () {
          for (var i = 0; i < disposers.length; i += 1) {
            try {
              disposers[i]();
            } catch {}
          }
          disposers.length = 0;
        };
      }, "dsh-kb: ui mounts");
    }

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  },
});
