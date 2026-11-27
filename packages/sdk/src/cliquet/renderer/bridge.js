// Cliquet's bridge to an MCP Apps host, shared by its trusted pages: the renderer, which draws
// every screen, and the server's upload panel. It sends the host JSON-RPC messages, answers
// its pings, shakes hands, and tells it how big the page is. Each page's own script gives
// `start` what to do with a tool's result and with a change of the host's context.
window.CliquetBridge = (() => {
  "use strict";

  const PROTOCOL = "2026-01-26";
  const html = document.documentElement;
  const isObject = (value) => typeof value === "object" && value !== null && !Array.isArray(value);
  const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

  let nextId = 1;
  const pending = new Map();
  let handlers = { onToolResult() {}, onHostContext() {} };

  // Send one JSON-RPC message to the host.
  function post(message) {
    window.parent.postMessage({ jsonrpc: "2.0", ...message }, "*");
  }

  // Ask the host something, and wait for its answer.
  function request(method, params) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      post({ id, method, params });
    });
  }

  // Tell the host something that needs no answer.
  function notify(method, params) {
    post(params === undefined ? { method } : { method, params });
  }

  // Handle one message from the host: an answer, a request or a notification.
  function onMessage(event) {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!isObject(message) || message.jsonrpc !== "2.0") return;
    if (typeof message.method === "string") {
      if (has(message, "id")) answerHost(message);
      else onNotification(message.method, isObject(message.params) ? message.params : {});
      return;
    }
    const waiting = pending.get(message.id);
    if (!waiting) return;
    pending.delete(message.id);
    if (message.error) {
      const said = isObject(message.error) ? message.error.message : null;
      waiting.reject(new Error(typeof said === "string" ? said : "the host refused the request"));
    } else {
      waiting.resolve(message.result);
    }
  }

  // Answer a request from the host: a ping, or a teardown before the frame goes.
  function answerHost(message) {
    if (message.method === "ping" || message.method === "ui/resource-teardown") {
      post({ id: message.id, result: {} });
    } else {
      const text = `Cliquet's page doesn't handle ${message.method}.`;
      post({ id: message.id, error: { code: -32601, message: text } });
    }
  }

  // Handle a notification: a tool's result, or a change of the host's context.
  function onNotification(method, params) {
    if (method === "ui/notifications/tool-result") handlers.onToolResult(params);
    else if (method === "ui/notifications/host-context-changed") handlers.onHostContext(params);
  }

  // Tell the host how big the document is, now and whenever that changes. The document is
  // measured at its content's height, so that it can shrink as well as grow.
  function watchSize() {
    let queued = false;
    let last = "";
    const measure = () => {
      queued = false;
      const saved = html.style.height;
      html.style.height = "max-content";
      const height = Math.ceil(html.getBoundingClientRect().height);
      html.style.height = saved;
      const width = Math.ceil(window.innerWidth);
      if (`${width}x${height}` === last) return;
      last = `${width}x${height}`;
      notify("ui/notifications/size-changed", { width, height });
    };
    const schedule = () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(measure);
    };
    const observer = new ResizeObserver(schedule);
    observer.observe(html);
    observer.observe(document.body);
    schedule();
  }

  // Listen to the host, shake hands as `app` ({name, version}), and watch the page's size.
  // `onHostContext` gets the handshake's context, then each change of it; `onToolResult`
  // gets each tool's result the host hands on. Gives the host's capabilities, or null when
  // it refused the handshake: it may still send the tool's result. They come before any
  // message the host sends once told the page is initialized, which is a task of its own.
  async function start(app, given) {
    handlers = { ...handlers, ...given };
    window.addEventListener("message", onMessage);
    let capabilities = null;
    try {
      const answer = await request("ui/initialize", {
        appInfo: app,
        appCapabilities: {},
        protocolVersion: PROTOCOL,
      });
      if (isObject(answer)) {
        handlers.onHostContext(answer.hostContext);
        capabilities = isObject(answer.hostCapabilities) ? answer.hostCapabilities : {};
      }
    } catch {
      // A host that refuses the handshake may still send the tool's result.
    }
    notify("ui/notifications/initialized");
    watchSize();
    return capabilities;
  }

  return Object.freeze({ request, notify, watchSize, start });
})();
