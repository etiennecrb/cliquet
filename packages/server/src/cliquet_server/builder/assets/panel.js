// What the builder's panels share, inlined before each panel's own script: Cliquet's own pages
// that a host that draws MCP Apps shows in a frame. Each follows the host's theme, reads the
// text of a tool's answer, opens its link through the host, and starts with the handshake.
window.CliquetPanel = (() => {
  "use strict";

  // The bridge to the host, shared with the renderer: see cliquet/renderer/bridge.js.
  const { request } = window.CliquetBridge;
  const $ = (id) => document.getElementById(id);
  const isObject = (value) => typeof value === "object" && value !== null && !Array.isArray(value);

  // Follow the host's theme. A change of context may leave the theme out.
  function applyHostContext(context) {
    if (!isObject(context)) return;
    if (context.theme === "light" || context.theme === "dark") {
      document.documentElement.setAttribute("data-theme", context.theme);
    }
  }

  // The text of a tool's answer, as the host hands it on.
  function textOf(result) {
    const content = isObject(result) && Array.isArray(result.content) ? result.content : [];
    return content
      .filter((part) => isObject(part) && part.type === "text" && typeof part.text === "string")
      .map((part) => part.text)
      .join("\n");
  }

  // Whether the host lets the frame call the panel's tool: once it answered the handshake,
  // it says so.
  const callsTools = (host) => !host || isObject(host.serverTools);

  // Open the `#link` element's address through the host, which the frame may not open
  // itself: `url` gives it once the panel knows it, and `say` shows the address when the
  // host won't open it.
  function openLink(url, say) {
    $("link").addEventListener("click", (event) => {
      event.preventDefault();
      const address = url();
      if (!address) return;
      request("ui/open-link", { url: address }).catch(() =>
        say(`This assistant didn't open the link. Copy it into your browser: ${address}`),
      );
    });
  }

  // The handshake: what the host can do, known before its first tool result comes, which
  // `onToolResult` gets.
  const start = (name, onToolResult) =>
    window.CliquetBridge.start(
      { name, version: "1" },
      { onToolResult, onHostContext: applyHostContext },
    );

  return { request, $, isObject, textOf, callsTools, openLink, start };
})();
