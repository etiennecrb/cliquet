"""The page that plays the MCP Apps host for a screen, and the answers to its calls.

The page loads the renderer in a sandboxed frame, answers its handshake, hands it the view,
and passes the view's tool calls to `/rpc`. `cliquet preview` serves it, and so does the
server, for an app's live screens and for a draft's.
"""

import json
from functools import partial
from typing import Any

import anyio
from mcp import types
from pydantic import BaseModel
from starlette.responses import JSONResponse

from cliquet.errors import shortened
from cliquet.mcp_server import AppServer

# What a caller who isn't the app's owner reads of a call Cliquet couldn't answer.
_FAILED = "Cliquet couldn't answer this call."
_NOT_AN_OBJECT = "a screen's call is a JSON object: send its method and params as one"


async def answer_rpc(
    server: AppServer, sent: bytes, allowed: set[str] | None = None
) -> JSONResponse:
    """Answer one call from the host page, the JSON object it `sent`: `tools/list`,
    `tools/call` or `resources/read`. A call of the wrong shape is refused, and keeps
    nothing: only a fault in answering one is kept for the app's owner."""
    try:
        body = json.loads(sent)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _refused(_NOT_AN_OBJECT)
    method, params = body.get("method"), body.get("params") or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        return _refused("a screen's call names its method, and its params are an object")
    match method:
        case "tools/list":
            answer = partial(_tools, server, allowed)
        case "tools/call":
            name, arguments = params.get("name"), params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _refused("a tools/call names its tool, and its arguments are an object")
            # The host page draws the screen itself.
            answer = partial(server.call, name, dict(arguments), draws_screens=True)
        case "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str):
                return _refused("a resources/read names its resource's uri")
            answer = partial(server.read_resource, uri)
        case _:
            return _refused(f"Cliquet doesn't handle {shortened(method, 100)}")
    try:
        return JSONResponse(_dump(await anyio.to_thread.run_sync(answer, limiter=server.limiter)))
    except Exception as e:  # around `AppServer.call`, which answers the app's own faults
        text = f"{type(e).__name__}: {e}"
        # Kept for the app's owner, as its server keeps the errors of a call, by the method.
        # The server may keep it in its database: never on the event loop.
        shown = partial(server.shown, method, e, text, _FAILED)
        told = await anyio.to_thread.run_sync(shown)
        return JSONResponse({"error": told}, status_code=500)


def _tools(server: AppServer, allowed: set[str] | None) -> types.ListToolsResult:
    """The tools a screen may list: all but the views that aren't `allowed`."""
    tools = [
        tool
        for tool in server.tools
        if allowed is None
        or not (tool.meta is not None and "ui" in tool.meta)
        or tool.name in allowed
    ]
    return types.ListToolsResult(tools=tools)


def _refused(message: str) -> JSONResponse:
    """A screen's call that Cliquet won't answer, in its own words."""
    return JSONResponse({"error": message}, status_code=400)


def host_page(driver: bool = False) -> str:
    """The page that plays the MCP Apps host, for `cliquet preview` and the server.

    Only with `driver` may `?driver` give the page the screen's document, for
    `cliquet preview --script`. Without it the screen's frame keeps its sandbox.
    """
    return _PAGE.replace("/*DRIVER*/", _DRIVER if driver else "")


def _dump(model: BaseModel) -> Any:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


# Only for `cliquet preview --script`: the page may then read the screen's document.
_DRIVER = """\
if (query.has("driver")) {
  document.getElementById("frame").setAttribute(
    "sandbox", "allow-scripts allow-same-origin");
}
"""

_PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cliquet.dev preview</title>
<style>
  :root { --bg: #f4f4f5; --ink: #18181b; --muted: #71717a; --line: #e4e4e7; --card: #fff; }
  :root.dark { --bg: #09090b; --ink: #fafafa; --muted: #a1a1aa; --line: #27272a; --card: #09090b; }
  body { margin: 0; font: 14px/1.5 system-ui, sans-serif; background: var(--bg);
         color: var(--ink); }
  body.log { display: grid; grid-template-columns: minmax(0, 1fr) 380px; }
  #bar { display: flex; flex-wrap: wrap; gap: 4px 16px; justify-content: space-between;
         max-width: var(--width); margin: 0 auto; padding: 12px 16px 0; box-sizing: border-box; }
  #bar a { color: var(--muted); text-decoration: none; margin-right: 12px; }
  #bar a.on { color: var(--ink); font-weight: 600; }
  /* The screen is as wide as a conversation, or a phone: never the whole window. */
  #stage { position: relative; max-width: var(--width); margin: 12px auto 24px; padding: 0 16px;
           box-sizing: border-box; }
  /* Until the screen is drawn, which waits for the app's sandbox and can take seconds: a
     spinner over the frame's top, shown only once the wait is long enough to notice. */
  body.loading #stage { min-height: 160px; }
  #loading { display: none; position: absolute; inset: 0 0 auto; height: 160px;
             place-items: center; pointer-events: none; }
  body.loading #loading { display: grid; }
  #loading::after { content: ""; width: 22px; height: 22px; box-sizing: border-box;
                    border: 2px solid var(--line); border-top-color: var(--muted);
                    border-radius: 50%;
                    animation: spin .8s linear infinite, appear .2s .3s backwards; }
  @keyframes spin { to { transform: rotate(1turn); } }
  @keyframes appear { from { opacity: 0; } }
  #frame { display: none; width: 100%; height: 60vh; border: 1px solid var(--line);
           border-radius: 12px; background: var(--card); }
  #error { display: none; max-width: var(--width); margin: 12px auto 0; padding: 8px 16px;
           box-sizing: border-box; background: #fde8e8; color: #9b1c1c; border-radius: 8px;
           font: 13px/1.4 ui-monospace, monospace; white-space: pre-wrap; }
  #log { display: none; margin: 0; padding: 8px; height: 100vh; overflow: auto;
         box-sizing: border-box; background: #111; color: #ddd;
         font: 11px/1.4 ui-monospace, monospace; white-space: pre-wrap; word-break: break-all; }
  body.log #log { display: block; position: sticky; top: 0; }
  /* For a screenshot: the screen alone. */
  body.shot #bar { display: none; }
  /* Inside another page: the screen alone, as tall as it is. */
  body.embed #bar { display: none; }
  body.embed #stage { margin: 0; padding: 0; }
  body.embed #frame { border: 0; border-radius: 0; background: transparent; }
  body.embed { background: transparent; }
</style>
</head>
<body class="loading">
<main>
  <nav id="bar"><span id="views"></span><span id="options"></span></nav>
  <pre id="error"></pre>
  <div id="stage">
    <div id="loading" role="status" aria-label="Loading the screen"></div>
    <iframe id="frame" sandbox="allow-scripts"></iframe>
  </div>
</main>
<pre id="log"></pre>
<script>
const query = new URLSearchParams(location.search);
const viewName = query.get("view");
const theme = query.get("theme") === "dark" ? "dark" : "light";
// As wide as a conversation in an assistant by default. `width=390` is a phone.
const width = query.get("width") || "768";
document.documentElement.style.setProperty("--width", /^\\d+$/.test(width) ? width + "px" : "none");
document.documentElement.classList.toggle("dark", theme === "dark");
if (query.has("log")) document.body.classList.add("log");
if (query.has("shot")) document.body.classList.add("shot");
if (query.has("embed")) document.body.classList.add("embed");
// Inside another page, that page shows that a click on the screen works, at the top of its
// window: the frame is as tall as its screen, so a long one would scroll the screen's own bar
// out of sight.
const inPage = window.parent !== window;
/*DRIVER*/window.cliquetCalls = [];
window.cliquetLinks = [];  // the links a picture or a script followed
window.cliquetBusy = 0;
window.cliquetLast = Date.now();
const frame = document.getElementById("frame");
const errorEl = document.getElementById("error");
const logEl = document.getElementById("log");

function log(text) {
  logEl.append(text + "\\n");
  logEl.scrollTop = logEl.scrollHeight;
}
// Inside another page: tell it something, such as how tall the screen is.
function tellPage(message) {
  if (inPage) window.parent.postMessage(message, location.origin);
}
// Say how tall the screen is, so that the page around can make room.
function tellParent() {
  const height = Math.ceil(document.getElementById("stage").getBoundingClientRect().bottom) + 1;
  tellPage({cliquet: "height", height});
}
function showError(text) {
  errorEl.textContent = text;
  errorEl.style.display = text ? "block" : "none";
  if (text) document.body.classList.remove("loading");
  window.cliquetError = text;  // read by `cliquet preview --screenshot`
  if (text) log("error: " + text);
  tellParent();
}
// A link to this page with some of its options changed. A null value removes the option.
function link(label, changes, on) {
  const next = new URLSearchParams(location.search);
  for (const [key, value] of Object.entries(changes)) {
    if (value === null) next.delete(key); else next.set(key, value);
  }
  const a = document.createElement("a");
  a.href = "?" + next;
  a.textContent = label;
  if (on) a.className = "on";
  return a;
}
async function rpc(method, params) {
  const response = await fetch("rpc", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({method, params}),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error);
  return body;
}
function textOf(result) {
  return (result.content || []).map((c) => c.text || "").join("\\n");
}
// Only a web or mail address opens. A `javascript:` one would run in this page.
const OPENS = ["http:", "https:", "mailto:"];
// Open a link from the screen in a new tab, with no way back to this page.
function openLink(url) {
  let address = null;
  try { address = new URL(url); } catch {}
  if (!address || !OPENS.includes(address.protocol)) {
    throw new Error(`the screen asked to open ${url}, but only a web or mail address opens. ` +
                    "Link to an http, https or mailto address.");
  }
  log("opening " + address.href);
  // A page for a picture or a script opens no tab: nobody would see it.
  if (query.has("shot")) window.cliquetLinks.push(address.href);
  else window.open(address.href, "_blank", "noopener,noreferrer");
  return {};
}
// The policy an MCP Apps host gives a page that names no domain: the renderer loads nothing.
function withCsp(html) {
  const policy = [
    "default-src 'none'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "connect-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'self'",
  ].join("; ");
  const meta = `<meta http-equiv="Content-Security-Policy" content="${policy}">`;
  return html.replace("<head>", "<head>" + meta);
}

let tool, toolResult;
function send(message) {
  log("host -> view " + JSON.stringify(message).slice(0, 400));
  frame.contentWindow.postMessage(message, "*");
}
async function answer(message) {
  const {method, params} = message;
  if (method === "ui/initialize") {
    return {
      protocolVersion: params.protocolVersion,
      hostInfo: {name: "cliquet-preview", version: "0"},
      hostCapabilities: {serverTools: {}, serverResources: {}, logging: {}, openLinks: {},
                         cliquetShowsWork: inPage},
      hostContext: {theme, displayMode: "inline", availableDisplayModes: ["inline"],
                    toolInfo: {tool}},
    };
  }
  if (method === "tools/call") {
    window.cliquetBusy += 1;
    try {
      const result = await rpc("tools/call", params);
      window.cliquetCalls.push({
        tool: params.name,
        arguments: params.arguments || {},
        error: result.isError ? textOf(result) : null,
        result: result.isError ? null : (result.structuredContent ?? textOf(result)),
      });
      // A refusal is the app's answer, shown by the screen itself: log it, it isn't an error.
      if (result.isError) log(`${params.name} refused: ${textOf(result)}`);
      return result;
    } finally {
      window.cliquetBusy -= 1;
      window.cliquetLast = Date.now();
    }
  }
  if (method === "resources/read") return await rpc("resources/read", params);
  if (method === "ui/request-display-mode") return {mode: "inline"};
  if (method === "ui/open-link") return openLink(params.url);
  if (method === "ui/update-model-context") {
    window.cliquetContext = params;  // what an assistant would be told the person sees
    return {};
  }
  if (method === "ui/message") return {};  // An assistant would act on it. The preview logs it.
  throw new Error("the preview doesn't handle " + method);
}
window.addEventListener("message", async (event) => {
  if (event.source !== frame.contentWindow) return;
  const message = event.data;
  if (!message || message.jsonrpc !== "2.0") return;
  log("view -> host " + JSON.stringify(message).slice(0, 400));
  if (message.method === "ui/notifications/initialized") {
    send({jsonrpc: "2.0", method: "ui/notifications/tool-input", params: {arguments: {}}});
    send({jsonrpc: "2.0", method: "ui/notifications/tool-result", params: toolResult});
  } else if (message.method === "cliquet/notifications/working") {
    tellPage({cliquet: "working", working: message.params?.working === true});
  } else if (message.method === "ui/notifications/size-changed") {
    frame.style.height = Math.max(message.params.height, 120) + "px";
    document.body.classList.remove("loading");  // the screen is drawn
    tellParent();
    window.cliquetDrawn = Date.now();  // a screenshot waits until the size settles
  } else if (message.method && message.id !== undefined) {
    try {
      send({jsonrpc: "2.0", id: message.id, result: await answer(message)});
    } catch (error) {
      showError(String(error));
      send({jsonrpc: "2.0", id: message.id, error: {code: -32603, message: String(error)}});
    }
  }
});

(async () => {
  try {
    const views = (await rpc("tools/list")).tools.filter((t) => t._meta?.ui?.resourceUri);
    const name = viewName || views[0]?.name;
    for (const view of views) {
      const here = view.name === name;
      const label = view.title || view.name;
      document.getElementById("views").append(link(label, {view: view.name}, here));
    }
    document.getElementById("options").append(
      link("Chat", {width: null}, width === "768"),
      link("Phone", {width: "390"}, width === "390"),
      link("Full", {width: "full"}, width === "full"),
      link(theme === "dark" ? "Light" : "Dark", {theme: theme === "dark" ? null : "dark"}),
      link("Log", {log: query.has("log") ? null : ""}, query.has("log")),
    );
    tool = views.find((t) => t.name === name);
    if (!tool) throw new Error(`there is no view ${name} for this role`);
    document.title = name + " - cliquet.dev preview";
    toolResult = await rpc("tools/call", {name, arguments: {}});
    if (toolResult.isError) throw new Error(textOf(toolResult));
    const page = (await rpc("resources/read", {uri: tool._meta.ui.resourceUri})).contents[0];
    log(`${name}: drawn by ${page.uri}, ${page.text.length} characters`);
    frame.style.display = "block";
    frame.srcdoc = withCsp(page.text);
  } catch (error) {
    showError(error.message || String(error));
  }
})();
</script>
</body>
</html>
"""
