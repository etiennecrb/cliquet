// Cliquet's renderer: it draws a screen from its tree, and speaks MCP Apps to the host.
//
// This script is the security boundary of every screen. It builds only the elements and
// attributes of the vocabulary Python gives it, with createElement, setAttribute and text
// nodes, and it never parses HTML. An icon is built the same way, from the drawing the server
// sent beside the tree. Forms and links call the app's tools through the host.
// See the builder's views guide and the Screens rules in ARCHITECTURE.md.
(() => {
  "use strict";

  const VOCABULARY = window.CLIQUET_VOCABULARY || {};
  const Chart = window.Chart;
  const html = document.documentElement;
  const root = document.getElementById("screen");
  // The bridge to the host, shared with Cliquet's other trusted pages: see bridge.js.
  const { request, notify } = window.CliquetBridge;
  const FORMAT = VOCABULARY.format;

  // What a screen may be made of. The server checked the tree already: this is the second lock.
  const ELEMENTS = new Map(
    Object.entries(VOCABULARY.elements).map(([tag, names]) => [tag, new Set(names)]),
  );
  const GLOBAL = new Set(VOCABULARY.global);
  const VOID = new Set(VOCABULARY.void);
  const SCHEMES = VOCABULARY.link_schemes;
  const OPENS = new Set(["http:", "https:", "mailto:"]);
  // Never set, whatever the vocabulary says: they load, run or send something.
  const NEVER = new Set([
    "style", "src", "srcset", "srcdoc", "href", "action", "formaction", "formtarget",
    "formmethod", "target", "ping", "background", "poster", "xlink:href", "xmlns",
  ]);
  // What an icon's parts may be: Lucide's shapes and their geometry, nothing that loads or runs.
  const ICON_PARTS = new Map(
    Object.entries(VOCABULARY.icon_parts).map(([tag, names]) => [tag, new Set(names)]),
  );
  const SVG = "http://www.w3.org/2000/svg";
  const INPUT_TYPES = new Set(VOCABULARY.input_types);
  const BUTTON_TYPES = new Set(VOCABULARY.button_types);
  const MAX_DEPTH = 100;
  // How long a click works before the bar at the top of the screen shows it: a quick one shows
  // nothing, so the bar doesn't flicker on every click.
  const SLOW_MS = 150;
  const FONT =
    'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';

  // A form's own methods can be hidden by a field named like them (`reset`, `elements`...):
  // call them from the prototypes instead.
  const ELEMENT = Element.prototype;
  const FORM = HTMLFormElement.prototype;
  const on = (method, target, ...args) => method.apply(target, args);

  // What the script knows about the elements it built, without reading their attributes back.
  const forms = new WeakMap(); // form -> {call, view, args, then, picker}
  const viewLinks = new WeakMap(); // a -> {view, args}
  const openLinks = new WeakMap(); // a -> the address it opens
  const laidOpen = new WeakMap(); // details -> whether the layout opened it
  const problems = new WeakMap(); // form -> the alert it shows
  const busy = new WeakSet(); // the forms whose call is running

  const state = {
    screen: null, // the screen drawn now
    replayed: false, // whether the host's first tool-result came
    ticket: 0, // the latest layout asked for: an older answer is dropped
    charts: [], // {canvas, spec, chart} for each chart on the screen
    grids: [], // each grid on the screen, with its sort, search, filters and page
    problem: null, // the alert at the top of the screen
    working: 0, // the clicks whose calls run now
    slow: 0, // the timer that shows they work, once they work long enough to notice
    hostShows: false, // whether the host shows it, rather than the bar
  };
  // The slim bar along the top of the screen while a click works: daisyUI's progress, with no
  // value, so it runs. Outside the screen's body, which no call rewrites: see `working`.
  const bar = document.createElement("progress");
  bar.className = "progress progress-primary s-working";
  bar.hidden = true;
  bar.setAttribute("aria-hidden", "true");

  // ---------------------------------------------------------------- Talking to the host

  // Shake hands with the host, then wait for the screen it replays.
  function start() {
    // The script sends every form itself: see `runForm`.
    root.addEventListener("submit", (event) => event.preventDefault());
    root.addEventListener("click", onClick);
    root.addEventListener("keydown", onKeyDown);
    root.addEventListener("change", onChange);
    document.body.append(bar);
    if (typeof Chart === "function") {
      Chart.defaults.font.family = FONT;
      Chart.defaults.font.size = 12;
    }
    window.CliquetBridge.start(
      { name: "Cliquet", version: String(FORMAT) },
      { onToolResult, onHostContext: applyHostContext },
    ).then((capabilities) => {
      // A page of Cliquet's own may show that a click works at the top of its window, which a
      // long screen in a frame as tall as itself scrolls past: then the renderer tells it.
      state.hostShows = isObject(capabilities) && capabilities.cliquetShowsWork === true;
    });
  }

  // Follow the host's theme. A change of context may leave the theme out.
  function applyHostContext(context) {
    if (!isObject(context)) return;
    const theme = context.theme;
    if ((theme === "light" || theme === "dark") && html.getAttribute("data-theme") !== theme) {
      html.setAttribute("data-theme", theme);
      redrawCharts();
    }
  }

  // Write the renderer's own words, numbers and dates in the app's language, English without a
  // language it knows. The page's lang says it too, for screen readers and hyphens.
  function applyLanguage(code) {
    const known = has(LANGUAGES, code) ? code : "en";
    html.lang = known;
    if (language === LANGUAGES[known]) return;
    language = LANGUAGES[known];
    words = language.words;
    collator = sorting(language.numbers);
    labelStyles = new Map();
  }

  // Give the theme the app's accent as its primary colour, or daisyUI's own without one. The
  // accent is a dark shade, for white text on it: in the dark theme, a lighter one with dark
  // text reads better, as a link or a soft button on a dark page.
  function applyAccent(accent) {
    const valid = typeof accent === "string" && /^#[0-9a-f]{6}$/i.test(accent);
    if (valid) html.style.setProperty("--s-accent", accent);
    else html.style.removeProperty("--s-accent");
    html.classList.toggle("s-accent", valid);
  }

  // Tell the assistant what the person sees, in Cliquet's own words rather than the app's.
  function tellModel(screen) {
    const args = isObject(screen.arguments) ? screen.arguments : {};
    const text =
      `The person sees the screen ${JSON.stringify(String(screen.view))} of the app ` +
      `${JSON.stringify(String(screen.app ?? ""))}, laid out with the arguments ` +
      `${JSON.stringify(args)}.`;
    request("ui/update-model-context", {
      content: [{ type: "text", text }],
      structuredContent: { view: screen.view, arguments: args },
    }).catch(() => {});
  }

  // ---------------------------------------------------------------- Calls and layouts

  // Call one of the screen's tools, in its transaction and through its relay.
  function callTool(name, args) {
    const screen = state.screen || {};
    const tools = isObject(screen.tools) ? screen.tools : {};
    const tool = has(tools, name) && isObject(tools[name]) ? tools[name] : {};
    const params = { ...args };
    // Every call of a screen shown in a transaction stays in it.
    if (typeof screen.transaction === "string") params.app_transaction = screen.transaction;
    if (tool.kind === "action") params.app_request_id = newRequestId();
    // A screen shown by another server than the app's reaches the app through that server's
    // tool for each kind of call, which takes the app's tool by name.
    const relay = isObject(screen.relay) ? screen.relay : {};
    const route = isObject(relay.routes) ? relay.routes[tool.kind] : null;
    if (!isObject(route) || typeof route.tool !== "string") {
      return request("tools/call", { name, arguments: params });
    }
    const fixed = isObject(relay.fixed) ? relay.fixed : {};
    const call = { ...fixed, [route.key]: name, arguments: params };
    return request("tools/call", { name: route.tool, arguments: call });
  }

  // Show that a click works, from a form's call to the layout after it, or a link's layout,
  // and return what ends it. The bar shows once the click has worked `SLOW_MS`, until the
  // last one ends, so every app shows it without a loading state of its own. The layout the
  // frame asks for when it opens is Cliquet's, and quiet: it shows nothing.
  function working() {
    if (state.working++ === 0) state.slow = setTimeout(() => showWork(true), SLOW_MS);
    return () => {
      if (--state.working > 0) return;
      clearTimeout(state.slow);
      showWork(false);
    };
  }

  // Show the bar, or tell a host that shows it itself.
  function showWork(shown) {
    if (state.hostShows) notify("cliquet/notifications/working", { working: shown });
    else bar.hidden = !shown;
  }

  // Lay a view out with these arguments and draw the screen it returns. True if it was drawn.
  // `before` runs just before the new screen replaces the old one. A `quiet` layout shows no
  // problem when the host can't make the call.
  async function layOut(view, args, { before = null, quiet = false } = {}) {
    const ticket = ++state.ticket;
    let result;
    try {
      result = await callTool(view, args);
    } catch (error) {
      if (ticket === state.ticket && !quiet) {
        showScreenProblem(`The call couldn't be made: ${error.message}`);
      }
      return false;
    }
    if (ticket !== state.ticket) return false;
    if (!isObject(result) || result.isError) {
      showScreenProblem(problemText(result));
      return false;
    }
    if (!isScreen(result.structuredContent)) {
      showScreenProblem(`The view ${view} answered without a screen to draw.`);
      return false;
    }
    if (before) before();
    draw(result.structuredContent);
    return true;
  }

  // Draw the screen the host replays when the frame opens, then lay it out again, once, to
  // show today's data. Later results are drawn as they come.
  function onToolResult(result) {
    const first = !state.replayed;
    state.replayed = true;
    if (!isObject(result) || result.isError) {
      showScreenProblem(problemText(result));
      return;
    }
    const screen = result.structuredContent;
    if (!isScreen(screen)) {
      showScreenProblem("This result has no screen this page can draw. Open the view again.");
      return;
    }
    draw(screen);
    if (first) layOut(screen.view, screen.arguments, { quiet: true });
  }

  // A form is sent by the script, never by the browser: a host's frame may leave out
  // `allow-forms`, and then the browser sends no form and fires no `submit` event. A click on
  // a form's submit button runs the form instead. Enter in a field needs nothing of its own:
  // the browser presses the form's first submit button, sandbox or not.
  async function runForm(form, submitter) {
    const info = forms.get(form);
    // A picker's new choice goes at once: the layout drawn is the latest asked for.
    if (!info || (busy.has(form) && !info.picker) || !state.screen) return;
    if (!form.noValidate && !on(FORM.reportValidity, form)) return;
    if (info.call) await submitCall(form, info, submitter);
    else if (info.view !== null) await submitView(form, info, submitter);
  }

  // A `data-call` form: call the tool, then show its refusal or lay the view out again.
  async function submitCall(form, info, submitter) {
    const args = { ...info.args, ...readFields(form, submitter, inputSchema(info.call)) };
    const done = setBusy(form, submitter);
    try {
      let result;
      try {
        result = await callTool(info.call, args);
      } catch (error) {
        showFormProblem(form, `The call couldn't be made: ${error.message}`);
        return;
      }
      if (!isObject(result) || result.isError) {
        showFormProblem(form, problemText(result));
        return;
      }
      clearFormProblem(form);
      on(FORM.reset, form);
      const screen = state.screen;
      await layOut(screen.view, { ...screen.arguments, ...info.then });
    } finally {
      done();
    }
  }

  // A `data-view` form: lay this view out again, or another one, with the form's fields.
  async function submitView(form, info, submitter) {
    const screen = state.screen;
    const here = info.view === "";
    const view = here ? screen.view : info.view;
    const cleared = new Set();
    const fields = readFields(form, submitter, inputSchema(view), cleared);
    const args = { ...(here ? screen.arguments : {}), ...info.args, ...fields };
    // A field the person emptied clears that argument, so a cleared search shows everything.
    for (const name of cleared) if (!has(info.args, name)) delete args[name];
    const done = setBusy(form, submitter);
    const ticket = state.ticket + 1; // the one `layOut` takes
    try {
      const drawn = await layOut(view, args, { before: () => on(FORM.reset, form) });
      // A picker has no button to press again: when its choice wasn't laid out, it shows the
      // one the screen does, so the person can make it again.
      if (!drawn && info.picker && ticket === state.ticket) on(FORM.reset, form);
    } finally {
      done();
    }
  }

  // Show a form as busy while its call runs, and return what undoes it.
  function setBusy(form, submitter) {
    busy.add(form);
    const ended = working();
    on(ELEMENT.setAttribute, form, "aria-busy", "true");
    let spinner = null;
    const wasDisabled = submitter ? submitter.disabled : false;
    if (submitter) {
      submitter.disabled = true;
      spinner = document.createElement("span");
      spinner.className = "loading loading-spinner loading-xs";
      spinner.setAttribute("aria-hidden", "true");
      submitter.prepend(spinner);
    }
    return () => {
      ended();
      busy.delete(form);
      on(ELEMENT.removeAttribute, form, "aria-busy");
      if (submitter) {
        submitter.disabled = wasDisabled;
        spinner.remove();
      }
    };
  }

  // Follow a link to a view, or open an address through the host.
  function follow(link) {
    const view = viewLinks.get(link);
    if (view && state.screen) {
      const screen = state.screen;
      const here = view.view === "";
      const args = { ...(here ? screen.arguments : {}), ...view.args };
      layOut(here ? screen.view : view.view, args).finally(working());
      return true;
    }
    const url = openLinks.get(link);
    if (url) {
      request("ui/open-link", { url }).catch(() => {});
      return true;
    }
    return false;
  }

  // A click on one of the screen's links, or on a form's submit button.
  function onClick(event) {
    const target = event.target instanceof Element ? event.target : null;
    const button = target ? target.closest("button") : null;
    if (button && button.type === "submit" && forms.has(button.form)) {
      event.preventDefault();
      runForm(button.form, button);
      return;
    }
    const link = target ? target.closest("a") : null;
    if (link && root.contains(link) && follow(link)) event.preventDefault();
  }

  // A form's picker lays the view out as soon as it changes, so a choice of year or category
  // is one gesture: see `pickerOf`.
  function onChange(event) {
    const select = event.target;
    const form = select instanceof HTMLSelectElement ? select.form : null;
    if (form && forms.get(form)?.picker === select) runForm(form, null);
  }

  // Enter on a focused link follows it, as it would a real one.
  function onKeyDown(event) {
    if (event.key !== "Enter" || !(event.target instanceof HTMLAnchorElement)) return;
    if (follow(event.target)) event.preventDefault();
  }

  // ---------------------------------------------------------------- Problems

  // The words of a refusal, without the prefixes meant for an agent.
  function problemText(result) {
    let text = textOf(result).trim();
    for (let round = 0; round < 2; round++) {
      text = text.replace(/^(Refused|Denied): /, "").replace(/^In transaction t-[^:]*: /, "");
    }
    return text || "The call didn't work, and said nothing about why.";
  }

  // An alert that shows a problem in daisyUI's soft error style.
  function problemAlert(text) {
    const alert = document.createElement("div");
    alert.className = "alert alert-error alert-soft s-problem";
    alert.setAttribute("role", "alert");
    const words = document.createElement("span");
    words.append(text);
    alert.append(words);
    return alert;
  }

  // Show a problem as the form's last child, in place of an earlier one.
  function showFormProblem(form, text) {
    clearFormProblem(form);
    const alert = problemAlert(text);
    problems.set(form, alert);
    on(ELEMENT.append, form, alert);
  }

  // Remove a form's problem, if it shows one.
  function clearFormProblem(form) {
    const alert = problems.get(form);
    if (alert) alert.remove();
    problems.delete(form);
  }

  // Show a problem at the top of the screen, keeping the screen as it is.
  function showScreenProblem(text) {
    if (state.problem) state.problem.remove();
    state.problem = problemAlert(text);
    root.prepend(state.problem);
  }

  // ---------------------------------------------------------------- Fields

  // The input schema of one of the screen's tools.
  function inputSchema(name) {
    const tools = state.screen && isObject(state.screen.tools) ? state.screen.tools : {};
    const tool = has(tools, name) ? tools[name] : null;
    return isObject(tool) && isObject(tool.input) ? tool.input : {};
  }

  // The values of a form's named fields, and of its submitter, converted by the schema. The
  // names of the optional fields left empty go in `cleared`, when it is given.
  function readFields(form, submitter, schema, cleared = new Set()) {
    const values = {};
    for (const field of on(ELEMENT.querySelectorAll, form, "input, select, textarea")) {
      const name = field.name;
      if (!name || field.matches(":disabled") || field.closest("form") !== form) continue;
      const property = propertyOf(schema, name);
      if (field.type === "checkbox") {
        readCheckbox(values, schema, property, field);
      } else if (field.type === "radio") {
        if (field.checked) values[name] = convert(schema, property, field.value);
      } else if (field instanceof HTMLSelectElement && field.multiple) {
        const items = itemsOf(schema, property);
        values[name] = Array.from(field.selectedOptions, (o) => convert(schema, items, o.value));
      } else {
        putText(values, schema, name, field.value, cleared);
      }
    }
    if (submitter && submitter.name) {
      putText(values, schema, submitter.name, submitter.value, cleared);
    }
    return values;
  }

  // A checkbox: true or false for a boolean, the values of the checked ones for a list, else
  // its value only if it is checked.
  function readCheckbox(values, schema, property, field) {
    const types = typesOf(schema, property);
    if (types.has("boolean")) {
      values[field.name] = field.checked;
    } else if (types.has("array")) {
      if (!Array.isArray(values[field.name])) values[field.name] = [];
      const items = itemsOf(schema, property);
      if (field.checked) values[field.name].push(convert(schema, items, field.value));
    } else if (field.checked) {
      values[field.name] = convert(schema, property, field.value);
    }
  }

  // A typed value. Left empty, an optional one is left out, so that its default applies.
  function putText(values, schema, name, raw, cleared) {
    if (raw === "") {
      if (Array.isArray(schema.required) && schema.required.includes(name)) values[name] = "";
      else {
        delete values[name];
        if (cleared) cleared.add(name);
      }
      return;
    }
    values[name] = convert(schema, propertyOf(schema, name), raw);
  }

  // A string from a field, as the type its schema asks for. What doesn't convert is sent as it
  // is, and the server explains.
  function convert(schema, property, raw) {
    const types = typesOf(schema, property);
    if (types.has("string")) return raw;
    if (types.has("integer") && /^-?\d+$/.test(raw)) return Number.parseInt(raw, 10);
    const number = raw.trim() === "" ? NaN : Number(raw);
    if (types.has("number") && Number.isFinite(number)) return number;
    if (types.has("boolean")) {
      if (["true", "on", "1"].includes(raw.toLowerCase())) return true;
      if (["false", "off", "0"].includes(raw.toLowerCase())) return false;
    }
    return raw;
  }

  // A schema node with its `$ref` followed into `$defs`.
  function resolve(schema, node) {
    for (let hops = 0; isObject(node) && typeof node.$ref === "string" && hops < 20; hops++) {
      const found = /^#\/(\$defs|definitions)\/(.+)$/.exec(node.$ref);
      const defs = found && isObject(schema[found[1]]) ? schema[found[1]] : null;
      node = defs && has(defs, found[2]) ? defs[found[2]] : null;
    }
    return isObject(node) ? node : null;
  }

  // The schema of one property of a tool's input.
  function propertyOf(schema, name) {
    const properties = isObject(schema) && isObject(schema.properties) ? schema.properties : {};
    return has(properties, name) ? resolve(schema, properties[name]) : null;
  }

  // Every type a schema node allows, looking through `anyOf`, `oneOf` and `allOf`.
  function typesOf(schema, node, depth = 0) {
    const types = new Set();
    node = resolve(schema, node);
    if (!node || depth > 10) return types;
    for (const type of [node.type].flat()) if (typeof type === "string") types.add(type);
    for (const key of ["anyOf", "oneOf", "allOf"]) {
      if (!Array.isArray(node[key])) continue;
      for (const part of node[key]) {
        for (const type of typesOf(schema, part, depth + 1)) types.add(type);
      }
    }
    return types;
  }

  // The schema of the items of an array property.
  function itemsOf(schema, node, depth = 0) {
    node = resolve(schema, node);
    if (!node || depth > 10) return null;
    if (isObject(node.items)) return resolve(schema, node.items);
    for (const part of [node.anyOf, node.oneOf].flat()) {
      const items = isObject(part) ? itemsOf(schema, part, depth + 1) : null;
      if (items) return items;
    }
    return null;
  }

  // ---------------------------------------------------------------- Drawing

  // Draw a screen in place of the one shown, keeping what the person typed, chose and opened.
  function draw(screen) {
    const kept = captureState();
    const context = {
      charts: [],
      grids: [],
      icons: isObject(screen.icons) ? screen.icons : {},
      // What the person did to each grid, kept for the grid laid out in its place.
      kept: new Map(state.grids.map((grid) => [grid.key, gridState(grid)])),
    };
    // The grids and charts are built in the screen's language.
    applyLanguage(screen.language);
    const fragment = document.createDocumentFragment();
    buildInto(fragment, screen.body, 0, context);
    for (const item of state.charts) if (item.chart) item.chart.destroy();
    for (const grid of state.grids) grid.watch.disconnect();
    state.screen = screen;
    state.problem = null;
    applyAccent(screen.accent);
    root.replaceChildren(fragment);
    // A grid's box scrolls once it is in the page: it goes back to where the person left it.
    for (const grid of context.grids) {
      grid.parts.scroll.scrollTop = grid.top;
      grid.parts.scroll.scrollLeft = grid.left;
    }
    restoreState(kept);
    state.charts = context.charts;
    state.grids = context.grids;
    for (const item of state.charts) item.chart = makeChart(item);
    tellModel(screen);
  }

  // Build a list of tree nodes into a parent, skipping what isn't allowed.
  function buildInto(parent, nodes, depth, context) {
    if (!Array.isArray(nodes)) return;
    for (const node of nodes) {
      const built = build(node, depth, context);
      if (built) parent.append(built);
    }
  }

  // Build one node: text as text, an element only if the vocabulary allows it.
  function build(node, depth, context) {
    if (typeof node === "string") return document.createTextNode(node);
    if (!Array.isArray(node) || typeof node[0] !== "string" || depth > MAX_DEPTH) return null;
    const tag = node[0];
    if (!ELEMENTS.has(tag)) return null;
    const element = document.createElement(tag);
    const withAttributes = isObject(node[1]);
    const attributes = withAttributes ? node[1] : {};
    for (const [name, value] of Object.entries(attributes)) setAllowed(element, tag, name, value);
    if (!VOID.has(tag)) buildInto(element, node.slice(withAttributes ? 2 : 1), depth + 1, context);
    wire(element, tag, attributes, context);
    return element;
  }

  // Set an attribute only if the vocabulary allows it on this element, and it is safe.
  function setAllowed(element, tag, name, value) {
    if (typeof name !== "string" || typeof value !== "string") return;
    if (NEVER.has(name) || name.startsWith("on")) return;
    const own = ELEMENTS.get(tag);
    if (!GLOBAL.has(name) && !own.has(name) && !/^aria-[a-z]+$/.test(name)) return;
    if (name === "type" && tag === "input" && !INPUT_TYPES.has(value)) return;
    if (name === "type" && tag === "button" && !BUTTON_TYPES.has(value)) return;
    try {
      element.setAttribute(name, value);
    } catch {
      // A name the DOM refuses is left out, as anything else not allowed.
    }
  }

  // Remember what the script needs from an element it built: a form's call, a link's target,
  // a details' laid-out state, a chart.
  function wire(element, tag, attributes, context) {
    const text = (name) => (typeof attributes[name] === "string" ? attributes[name] : null);
    if (tag === "form") {
      const call = text("data-call");
      const view = text("data-view");
      const picker = !call && view !== null ? pickerOf(element) : null;
      if (picker) element.classList.add("s-picker");
      forms.set(element, {
        call: call || null,
        view,
        args: parseObject(text("data-args")),
        then: parseObject(text("data-then")),
        picker,
      });
    } else if (tag === "a") {
      wireLink(element, text("data-view"), text("data-args"), text("href"));
    } else if (tag === "details") {
      laidOpen.set(element, has(attributes, "open"));
    }
    const chart = text("data-chart");
    if (chart !== null && ELEMENTS.get(tag).has("data-chart")) {
      prepareChart(element, chart, context);
    }
    const grid = text("data-grid");
    if (grid !== null && ELEMENTS.get(tag).has("data-grid")) {
      prepareGrid(element, grid, context);
    }
    const icon = text("data-icon");
    if (icon !== null && ELEMENTS.get(tag).has("data-icon") && has(context.icons, icon)) {
      element.append(drawIcon(context.icons[icon]));
    }
  }

  // An icon, built from the drawing the server sent beside the tree: Lucide's line, 1em high
  // in the text's colour. Only the shapes and the geometry of ICON_PARTS are built.
  function drawIcon(parts) {
    const svg = document.createElementNS(SVG, "svg");
    const frame = {
      viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2",
      "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true",
      focusable: "false", class: "s-icon",
    };
    for (const [name, value] of Object.entries(frame)) svg.setAttribute(name, value);
    for (const part of Array.isArray(parts) ? parts : []) {
      if (!Array.isArray(part) || !ICON_PARTS.has(part[0]) || !isObject(part[1])) continue;
      const allowed = ICON_PARTS.get(part[0]);
      const shape = document.createElementNS(SVG, part[0]);
      for (const [name, value] of Object.entries(part[1])) {
        if (!allowed.has(name) || typeof value !== "string") continue;
        if (name === "fill" && value !== "currentColor") continue;
        shape.setAttribute(name, value);
      }
      svg.append(shape);
    }
    return svg;
  }

  // The select that sends a `data-view` form as it changes: its one field, named, in a form
  // with no submit button, or null. The check refuses any other form without one.
  function pickerOf(form) {
    if (on(ELEMENT.querySelector, form, "button:not([type]), button[type=submit]")) return null;
    const fields = on(ELEMENT.querySelectorAll, form, "input, select, textarea");
    const [field] = fields;
    return fields.length === 1 && field instanceof HTMLSelectElement && field.name ? field : null;
  }

  // A link never gets a real address: it lays out a view, or asks the host to open one.
  function wireLink(element, view, args, href) {
    if (view !== null) {
      viewLinks.set(element, { view, args: parseObject(args) });
    } else if (href !== null) {
      const url = linkAddress(href);
      if (!url) return;
      openLinks.set(element, url);
      element.setAttribute("title", url);
    } else {
      return;
    }
    element.setAttribute("role", "link");
    element.setAttribute("tabindex", "0");
  }

  // A web or mail address a link may open, or null.
  function linkAddress(value) {
    const address = value.trim();
    if (!SCHEMES.some((scheme) => address.toLowerCase().startsWith(scheme))) return null;
    try {
      const url = new URL(address);
      return OPENS.has(url.protocol) ? url.href : null;
    } catch {
      return null;
    }
  }

  // ---------------------------------------------------------------- Keeping state

  // Each field of the screen with a key that finds it again in the next layout: its form, its
  // name and, for a choice, its value, then its place among fields with the same key.
  function fieldEntries() {
    const formKeys = new Map();
    const formCounts = new Map();
    for (const form of root.querySelectorAll("form")) {
      const info = forms.get(form) || {};
      let kind = info.view != null ? `view:${info.view}` : "form";
      if (info.call) kind = `call:${info.call}`;
      const index = formCounts.get(kind) || 0;
      formCounts.set(kind, index + 1);
      formKeys.set(form, `${kind}#${index}`);
    }
    const entries = [];
    const counts = new Map();
    for (const field of root.querySelectorAll("input, select, textarea")) {
      // A grid keeps what the person did to it itself: see `gridState`.
      if (field.closest("[data-grid]")) continue;
      const form = field.closest("form");
      const choice = field.type === "radio" || field.type === "checkbox" ? field.value : "";
      const where = form ? formKeys.get(form) : "none";
      const base = [where, field.name || field.id, choice].join("\u0001");
      const index = counts.get(base) || 0;
      counts.set(base, index + 1);
      entries.push([`${base}\u0001${index}`, field]);
    }
    return entries;
  }

  // Each details of the screen with its key: its id, else its name and place, else its place.
  function detailsEntries() {
    const counts = new Map();
    return Array.from(root.querySelectorAll("details"), (details) => {
      const id = details.getAttribute("id");
      if (id) return [`id:${id}`, details];
      const base = `name:${details.getAttribute("name") || ""}`;
      const index = counts.get(base) || 0;
      counts.set(base, index + 1);
      return [`${base}#${index}`, details];
    });
  }

  // What kind of field this is, so that a value is only given back to the same kind.
  function kindOf(field) {
    return `${field.tagName}:${field.type}`;
  }

  // The value a field had as laid out: its attributes, not what the person did.
  function laidValue(field) {
    if (field.type === "checkbox" || field.type === "radio") return field.defaultChecked;
    if (field instanceof HTMLSelectElement) {
      const chosen = Array.from(field.options).filter((o) => o.defaultSelected);
      if (chosen.length || field.multiple) return chosen.map((o) => o.value);
      const first = Array.from(field.options).find((o) => !o.disabled);
      return first ? [first.value] : [];
    }
    return field.defaultValue;
  }

  // The value a field has now.
  function currentValue(field) {
    if (field.type === "checkbox" || field.type === "radio") return field.checked;
    if (field instanceof HTMLSelectElement) {
      return Array.from(field.options).filter((o) => o.selected).map((o) => o.value);
    }
    return field.value;
  }

  // Give a field a value it had before.
  function setValue(field, value) {
    if (field.type === "checkbox" || field.type === "radio") field.checked = value;
    else if (field instanceof HTMLSelectElement) {
      for (const option of field.options) option.selected = value.includes(option.value);
    } else field.value = value;
  }

  // What the person changed on the screen: fields, open details, and the field with the focus.
  function captureState() {
    const entries = fieldEntries();
    const fields = new Map();
    let focus = null;
    for (const [key, field] of entries) {
      if (field === document.activeElement) focus = { key, selection: selectionOf(field) };
      if (field.type === "radio") continue;
      const laid = laidValue(field);
      const now = currentValue(field);
      if (!same(laid, now)) fields.set(key, { kind: kindOf(field), laid, now });
    }
    const details = new Map();
    for (const [key, element] of detailsEntries()) {
      details.set(key, { laid: laidOpen.get(element), open: element.open });
    }
    return { fields, radios: radioGroups(entries), details, focus };
  }

  // The radios of the screen by group, their form and name: the key of the one the layout
  // checked, and of the one checked now. A group is kept only if the layout checks the same.
  function radioGroups(entries) {
    const groups = new Map();
    for (const [key, field] of entries) {
      if (field.type !== "radio") continue;
      const name = key.split("\u0001").slice(0, 2).join("\u0001");
      const group = groups.get(name) || { laid: null, now: null, radios: new Map() };
      group.radios.set(key, field);
      if (field.defaultChecked) group.laid = key;
      if (field.checked) group.now = key;
      groups.set(name, group);
    }
    return groups;
  }

  // Give back what the person changed, where the new layout lays a thing out as the old one
  // did. Where the layout changed it, the new layout wins.
  function restoreState(kept) {
    const entries = fieldEntries();
    for (const [key, field] of entries) {
      const old = kept.fields.get(key);
      if (old && old.kind === kindOf(field) && same(old.laid, laidValue(field))) {
        setValue(field, old.now);
      }
    }
    for (const [name, group] of radioGroups(entries)) {
      const old = kept.radios.get(name);
      if (!old || old.laid !== group.laid || old.now === old.laid) continue;
      if (group.radios.has(old.now)) group.radios.get(old.now).checked = true;
      else if (old.now === null) for (const radio of group.radios.values()) radio.checked = false;
    }
    for (const [key, field] of entries) {
      if (!kept.focus || kept.focus.key !== key) continue;
      field.focus({ preventScroll: true });
      restoreSelection(field, kept.focus.selection);
    }
    for (const [key, element] of detailsEntries()) {
      const old = kept.details.get(key);
      if (old && old.laid === laidOpen.get(element)) element.open = old.open;
    }
  }

  // Where the caret is in a text field, if it has one.
  function selectionOf(field) {
    try {
      return field.selectionStart === null ? null : [field.selectionStart, field.selectionEnd];
    } catch {
      return null;
    }
  }

  // Put the caret back where it was.
  function restoreSelection(field, selection) {
    if (!selection) return;
    try {
      field.setSelectionRange(selection[0], selection[1]);
    } catch {
      // A field without a caret, such as a number, keeps the browser's.
    }
  }

  // ---------------------------------------------------------------- Charts

  // Make the room a chart is drawn in: a canvas of a fixed height, and a table of its numbers
  // for screen readers. The chart itself is drawn once the screen is in the page.
  function prepareChart(element, raw, context) {
    const spec = parseObject(raw);
    if (!Array.isArray(spec.series)) return;
    if (spec.kind === "scatter") {
      // A scatter has no labels: each of its series has points, each with its own x and y.
      spec.series = spec.series.filter((one) => isObject(one) && Array.isArray(one.points));
      for (const series of spec.series) series.points = series.points.filter(isObject);
    } else {
      if (!Array.isArray(spec.labels)) return;
      spec.series = spec.series.filter((one) => isObject(one) && Array.isArray(one.values));
    }
    const plot = document.createElement("div");
    plot.className = spec.kind === "donut" ? "s-plot s-plot-donut" : "s-plot";
    plot.setAttribute("aria-hidden", "true");
    const canvas = document.createElement("canvas");
    plot.append(canvas);
    const last = element.lastElementChild;
    if (last && last.tagName === "FIGCAPTION") element.insertBefore(plot, last);
    else element.append(plot);
    const axes = chartAxes(spec);
    const labels = chartLabels(spec, axes);
    element.append(chartTable(spec, axes, labels));
    context.charts.push({ canvas, spec, axes, labels, chart: null });
  }

  // A chart's labels, written once in the format its x axis gives them: the x's ticks, the
  // tooltip's title and the screen reader's table all read them.
  function chartLabels(spec, axes) {
    if (spec.kind === "scatter") return [];
    const write = axes.x.label ?? String;
    return spec.labels.map((label) => write(String(label)));
  }

  // The numbers of a chart as a table, hidden from sight but read by screen readers: one row
  // per label, one column per series. A scatter has one row per point instead: its series,
  // its label, its x and its y.
  function chartTable(spec, axes, labels) {
    const formats = seriesFormats(spec, axes);
    const table = document.createElement("table");
    table.className = "s-sr-only";
    if (isText(spec.title)) {
      const caption = document.createElement("caption");
      caption.append(spec.title);
      table.append(caption);
    }
    const cell = (tag, text, scope) => {
      const element = document.createElement(tag);
      if (scope) element.setAttribute("scope", scope);
      element.append(text);
      return element;
    };
    const head = document.createElement("tr");
    const body = document.createElement("tbody");
    if (spec.kind === "scatter") {
      const labelled = spec.series.some((series) =>
        series.points.some((point) => isText(point.label)),
      );
      head.append(cell("th", words.series, "col"));
      if (labelled) head.append(cell("th", words.label, "col"));
      head.append(cell("th", axes.x.title ?? "x", "col"));
      head.append(cell("th", axes.y.title ?? "y", "col"));
      spec.series.forEach((series, index) => {
        for (const point of series.points) {
          const row = document.createElement("tr");
          row.append(cell("th", String(series.name ?? ""), "row"));
          if (labelled) row.append(cell("td", isText(point.label) ? point.label : ""));
          row.append(cell("td", axes.x.write(point.x)), cell("td", formats[index](point.y)));
          body.append(row);
        }
      });
    } else {
      head.append(cell("th", axes.x.title ?? "", "col"));
      for (const series of spec.series) head.append(cell("th", String(series.name ?? ""), "col"));
      labels.forEach((label, index) => {
        const row = document.createElement("tr");
        // A row's name is its label as the x axis writes it, under the same ticks.
        row.append(cell("th", label, "row"));
        spec.series.forEach((series, column) =>
          row.append(cell("td", formats[column](series.values[index]))),
        );
        body.append(row);
      });
    }
    const thead = document.createElement("thead");
    thead.append(head);
    table.append(thead, body);
    // A table grows to its rows whatever its height: a box around it keeps it to one pixel.
    const box = document.createElement("div");
    box.className = "s-sr-only";
    box.append(table);
    return box;
  }

  // Draw every chart again, in the colours of the theme now.
  function redrawCharts() {
    for (const item of state.charts) {
      if (item.chart) item.chart.destroy();
      item.chart = makeChart(item);
    }
  }

  // Draw one chart with Chart.js.
  function makeChart({ canvas, spec, axes, labels }) {
    if (typeof Chart !== "function" || !canvas.isConnected) return null;
    const kind = spec.kind;
    const palette = chartPalette();
    const content = cssColor("--color-base-content");
    const ink = rgb(content, 0.7);
    const grid = rgb(content, 0.08);
    const surface = rgb(cssColor("--color-base-100"), 1);
    const formats = seriesFormats(spec, axes);
    const scatter = kind === "scatter";
    const series = spec.series;
    // A line on y2 is drawn against a second axis, on the right.
    const right = series.some(onY2);
    const stacked = kind === "stacked-bar" || (kind === "area" && series.length > 1);
    // A series with a tone takes no slot, and the others take theirs by name.
    const slots = colourSlots(series.map((s) => (TONES.has(s.tone) ? null : String(s.name ?? ""))));
    // A label's colour comes from the label the view gave, not the one the axis wrote.
    const labelSlots = colourSlots(scatter ? [] : spec.labels.map(String));
    const lineKind = kind === "line" || kind === "area";
    // A series with a tone takes its colour, and the others keep theirs by name.
    const colours = series.map((s, index) =>
      slots[index] === null ? cssColor(`--s-tone-${s.tone}`) : palette[slots[index]],
    );
    // A point is ringed in the page's colour, so that it stands apart from the lines under it.
    const points = { pointBorderColor: surface, pointBorderWidth: 2, pointHoverBorderWidth: 2 };
    const datasets = series.map((s, index) => {
      const colour = colours[index];
      const label = String(s.name ?? "");
      if (scatter) {
        // A scatter's points are its data, so they are always shown.
        return {
          label,
          data: s.points,
          backgroundColor: rgb(colour, 1),
          borderColor: surface,
          ...points,
          pointBorderWidth: 1.5,
          pointRadius: s.points.length > 200 ? 3 : 5,
          pointHoverRadius: 7,
        };
      }
      const yAxisID = onY2(s) ? "y2" : "y";
      const base = { label, data: s.values, yAxisID };
      const line = lineKind || s.kind === "line";
      if (kind === "donut") {
        return {
          ...base,
          backgroundColor: labelSlots.map((slot) => rgb(palette[slot], 1)),
          borderColor: surface,
          borderWidth: 2,
          hoverOffset: 6,
        };
      }
      if (line) {
        const area = kind === "area";
        return {
          ...base,
          // A line over bars is drawn on top of them. Chart.js stacks a line apart from bars, and
          // a stack of its own keeps it apart from the other lines too.
          ...(s.kind === "line" ? { type: "line", order: -1, stack: `line ${index}` } : {}),
          borderColor: rgb(colour, 1),
          backgroundColor: area ? fading(colour) : rgb(colour, 1),
          fill: area ? (stacked && index > 0 ? "-1" : "origin") : false,
          borderWidth: 2.5,
          // A monotone curve never overshoots a point, as a looser one would between two.
          cubicInterpolationMode: "monotone",
          // The points show where the pointer is, and only there, except a lone value, which has
          // no line to show it.
          ...points,
          pointRadius: (context) => (isLonePoint(context) ? 3 : 0),
          pointHoverRadius: 5,
          pointBackgroundColor: rgb(colour, 1),
        };
      }
      return {
        ...base,
        backgroundColor: rgb(colour, 1),
        hoverBackgroundColor: rgb(colour, 0.85),
        borderColor: surface,
        borderWidth: stacked ? { top: 2 } : 0,
        borderSkipped: "start",
        // Only the end of a bar is rounded: the top one of a stack, or a bar alone.
        borderRadius: stacked ? (context) => (isStackEnd(context) ? 4 : 0) : 4,
        maxBarThickness: 40,
      };
    });
    // A bar's length is its value, and an area fills from zero, so their axis starts at zero.
    // A line's axis fits its values, and so do a scatter's and the second axis, which only lines
    // use. A scatter's x is a value axis too. An axis's min and max are a span it covers at
    // least: it grows to show the values beyond them.
    const axis = (id) => {
      const onRight = id === "y2";
      const isValue = id !== "x" || scatter;
      const own = axes[id];
      // Chart.js keeps its default for a key left out, not for one set to undefined.
      return {
        ...(scatter ? { type: "linear" } : {}),
        ...(isValue && Number.isFinite(own.min) ? { suggestedMin: own.min } : {}),
        ...(isValue && Number.isFinite(own.max) ? { suggestedMax: own.max } : {}),
        stacked: stacked && !onRight,
        position: onRight ? "right" : undefined,
        beginAtZero: isValue && kind !== "line" && !scatter && !onRight,
        title: own.title ? { display: true, text: own.title, color: ink } : { display: false },
        // Light dashed lines across, none down, and a solid line along the x axis.
        border: { display: id === "x", color: grid, dash: [4, 4] },
        // The second axis draws no grid of its own: the first one's lines are enough.
        grid: { display: id !== "x" && !onRight, color: grid, drawTicks: false },
        ticks: isValue
          ? {
              color: ink,
              padding: 6,
              maxTicksLimit: 6,
              ...tickPrecision(own.format),
              callback: tickLabels(own.format),
            }
          : { color: ink, padding: 6, autoSkipPadding: 12, maxRotation: 0 },
      };
    };
    // The legend and the tooltip follow the series' order, though a line over bars is drawn
    // last.
    const bySeries = (a, b) => a.datasetIndex - b.datasetIndex;
    // The solid colour of an item of the legend or the tooltip: its slice's, or its series'.
    const solid = (datasetIndex, index) =>
      rgb(kind === "donut" ? palette[labelSlots[index]] : colours[datasetIndex], 1);
    const dots = {
      labelColor: (item) => {
        const colour = solid(item.datasetIndex, item.dataIndex);
        return { borderColor: colour, backgroundColor: colour };
      },
      labelPointStyle: () => ({ pointStyle: "circle", rotation: 0 }),
    };
    const plugins = [tooltipShadow];
    if (kind === "donut") plugins.push(donutTotal(centreFormat(spec.format), content));
    else if (!scatter) plugins.push(crosshair(rgb(content, 0.35), rgb(content, 0.05)));
    return new Chart(canvas, {
      type: scatter ? "scatter" : kind === "donut" ? "doughnut" : lineKind ? "line" : "bar",
      data: { labels, datasets },
      plugins,
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        cutout: kind === "donut" ? "70%" : undefined,
        // A scatter's tooltip shows the point nearest the pointer, and its label.
        interaction: scatter
          ? { mode: "nearest", axis: "xy", intersect: false }
          : { mode: kind === "donut" ? "nearest" : "index", intersect: kind === "donut" },
        layout: { padding: { top: 4 } },
        plugins: {
          legend: {
            display: kind === "donut" || series.length > 1,
            position: "bottom",
            labels: {
              // A donut's items are its slices, already in order.
              sort: kind === "donut" ? undefined : bySeries,
              color: ink,
              usePointStyle: true,
              pointStyle: "circle",
              boxWidth: 8,
              boxHeight: 8,
              padding: 14,
              // An area's own colour fades: its item takes the solid one.
              ...(kind === "area"
                ? {
                    generateLabels: (chart) =>
                      Chart.defaults.plugins.legend.labels.generateLabels(chart).map((item) => ({
                        ...item,
                        fillStyle: solid(item.datasetIndex),
                        strokeStyle: solid(item.datasetIndex),
                      })),
                  }
                : {}),
            },
          },
          // A card in the theme's colours, with a round dot in each series' colour.
          tooltip: {
            backgroundColor: surface,
            borderColor: rgb(content, 0.12),
            borderWidth: 1,
            titleColor: rgb(content, 1),
            bodyColor: rgb(content, 0.85),
            titleFont: { weight: "600" },
            titleMarginBottom: 6,
            bodySpacing: 5,
            padding: { x: 12, y: 10 },
            cornerRadius: 8,
            caretSize: 0,
            caretPadding: 10,
            usePointStyle: true,
            boxWidth: 8,
            boxHeight: 8,
            boxPadding: 6,
            itemSort: bySeries,
            callbacks: scatter
              ? {
                  ...dots,
                  title: (items) => items.map((item) => item.raw.label).filter(isText),
                  label: (item) =>
                    ` ${item.dataset.label}: ${axes.x.write(item.raw.x)}, ` +
                    formats[item.datasetIndex](item.raw.y),
                }
              : {
                  ...dots,
                  // A slice's line names it already.
                  ...(kind === "donut" ? { title: () => "" } : {}),
                  label: (item) =>
                    ` ${kind === "donut" ? item.label : item.dataset.label}: ` +
                    formats[item.datasetIndex](item.raw),
                },
          },
        },
        scales:
          kind === "donut"
            ? {}
            : {
                x: axis("x"),
                y: axis("y"),
                ...(right ? { y2: axis("y2") } : {}),
              },
      },
    });
  }

  // Whether a series is drawn against the second axis: the check lets only a line there.
  function onY2(series) {
    return series.axis === "y2";
  }

  // An area's colour for Chart.js: the series' colour at the top of the chart, fading to
  // nothing at its foot. The chart has no area yet when its first draw is being laid out.
  function fading(colour) {
    return ({ chart }) => {
      const area = chart.chartArea;
      if (!area) return rgb(colour, 0.2);
      const gradient = chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
      gradient.addColorStop(0, rgb(colour, 0.4));
      gradient.addColorStop(1, rgb(colour, 0));
      return gradient;
    };
  }

  // Whether a bar is the end of its stack: the last one shown on its side of zero.
  function isStackEnd({ chart, datasetIndex, dataIndex }) {
    const value = chart.data.datasets[datasetIndex].data[dataIndex];
    if (!Number.isFinite(value) || value === 0) return false;
    return !chart.data.datasets.some(
      (dataset, index) =>
        index > datasetIndex &&
        dataset.type !== "line" &&
        chart.isDatasetVisible(index) &&
        Math.sign(dataset.data[dataIndex]) === Math.sign(value),
    );
  }

  // Whether a point of a line has no value on either side, so that no line reaches it.
  function isLonePoint(context) {
    const data = context.dataset.data;
    const index = context.dataIndex;
    const present = (at) => at >= 0 && at < data.length && Number.isFinite(data[at]);
    return present(index) && !present(index - 1) && !present(index + 1);
  }

  // A soft shadow under the tooltip, drawn just before it: Chart.js has none of its own.
  const tooltipShadow = {
    id: "cliquetTooltipShadow",
    beforeTooltipDraw(chart, { tooltip }) {
      const { ctx } = chart;
      ctx.save();
      ctx.shadowColor = "rgba(0, 0, 0, 0.12)";
      ctx.shadowBlur = 16;
      ctx.shadowOffsetY = 4;
      ctx.fillStyle = tooltip.options.backgroundColor;
      ctx.globalAlpha = tooltip.opacity;
      ctx.beginPath();
      // An older browser has no roundRect: its shadow is square.
      if (typeof ctx.roundRect === "function") {
        ctx.roundRect(tooltip.x, tooltip.y, tooltip.width, tooltip.height, 8);
      } else {
        ctx.rect(tooltip.x, tooltip.y, tooltip.width, tooltip.height);
      }
      ctx.fill();
      ctx.restore();
    },
  };

  // Where the pointer is on a chart with an x axis: a dashed line down a line's points, or a
  // soft band behind a column of bars.
  function crosshair(line, band) {
    return {
      id: "cliquetCrosshair",
      beforeDatasetsDraw(chart) {
        const active = chart.getActiveElements();
        if (active.length === 0) return;
        const { ctx, chartArea: area } = chart;
        const x = active[0].element.x;
        ctx.save();
        if (chart.config.type === "bar") {
          const width = (area.right - area.left) / Math.max(chart.data.labels.length, 1);
          ctx.fillStyle = band;
          ctx.fillRect(x - width / 2, area.top, width, area.bottom - area.top);
        } else {
          ctx.strokeStyle = line;
          ctx.lineWidth = 1;
          ctx.setLineDash([4, 4]);
          ctx.beginPath();
          ctx.moveTo(Math.round(x) + 0.5, area.top);
          ctx.lineTo(Math.round(x) + 0.5, area.bottom);
          ctx.stroke();
        }
        ctx.restore();
      },
    };
  }

  // How a donut writes its total: in its format, and a currency without digits with its cents
  // at most, so that the noise floats leave when added up never shows.
  function centreFormat(format) {
    return numberFormat(format, numberStyle(format)?.currency ? { most: 2 } : {});
  }

  // A donut's total in its middle, large and in the chart's format, with its series' name
  // under it. Both shrink to fit the hole.
  function donutTotal(format, content) {
    return {
      id: "cliquetDonutTotal",
      afterDatasetsDraw(chart) {
        const arc = chart.getDatasetMeta(0).data[0];
        if (!arc) return;
        const dataset = chart.data.datasets[0];
        const total = dataset.data
          .filter((value, index) => Number.isFinite(value) && chart.getDataVisibility(index))
          .reduce((sum, value) => sum + value, 0);
        const room = arc.innerRadius * 1.6;
        const { ctx } = chart;
        const fit = (text, size, weight) => {
          ctx.font = `${weight} ${size}px ${FONT}`;
          const width = ctx.measureText(text).width;
          if (width > room) ctx.font = `${weight} ${Math.floor((size * room) / width)}px ${FONT}`;
        };
        const written = format(total);
        const name = dataset.label;
        const size = Math.min(26, Math.round(arc.innerRadius * 0.36));
        ctx.save();
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        fit(written, size, 700);
        ctx.fillStyle = rgb(content, 1);
        ctx.fillText(written, arc.x, arc.y - (name ? size * 0.35 : 0));
        if (name) {
          fit(name, 12, 400);
          ctx.fillStyle = rgb(content, 0.6);
          ctx.fillText(name, arc.x, arc.y + size * 0.6);
        }
        ctx.restore();
      },
    };
  }

  // A chart's axes, as the view wrote them, each with its title or null, its span, its
  // format, and how it writes its numbers, made once: x, y (also spelled y1) and y2, the second
  // axis. A donut has no axes: its values are in the chart's own format. A scatter's x holds a
  // span of numbers, and every other chart's x holds its labels, which it writes in its format.
  function chartAxes(spec) {
    const of = (key) => (isObject(spec[key]) ? spec[key] : undefined);
    const resolve = (own = {}, labels = false) => ({
      ...own,
      title: isText(own.title) ? own.title : null,
      write: labels ? null : numberFormat(own.format),
      label: labels ? labelWriter(own.format) : null,
    });
    const y = spec.kind === "donut" ? { format: spec.format } : (of("y") ?? of("y1"));
    return { x: resolve(of("x"), spec.kind !== "scatter"), y: resolve(y), y2: resolve(of("y2")) };
  }

  // How each series writes its numbers: in the format of its axis.
  function seriesFormats(spec, axes) {
    return spec.series.map((series) => (onY2(series) ? axes.y2 : axes.y).write);
  }

  // The tones a series may have, each a colour of cliquet.css. No colour of the palette reads
  // as one, in either theme, so a series without a tone never looks like one.
  const TONES = new Set(["success", "warning", "error", "info", "neutral"]);
  // How many colours the palette has, in cliquet.css.
  const PALETTE_SIZE = 7;

  // The palette's slot of each name: a hash of the name, so that a series keeps its colour in
  // every chart of the app, whatever tones the chart uses, and the next free slot when two
  // names meet in one chart. A null name takes no slot.
  function colourSlots(names) {
    const used = new Set();
    return names.map((name) => {
      if (name === null) return null;
      // When every slot is taken, the next names share them.
      if (used.size >= PALETTE_SIZE) used.clear();
      let slot = hash(name) % PALETTE_SIZE;
      while (used.has(slot)) slot = (slot + 1) % PALETTE_SIZE;
      used.add(slot);
      return slot;
    });
  }

  // FNV-1a: a small, stable hash of a string.
  function hash(text) {
    let value = 0x811c9dc5;
    for (let index = 0; index < text.length; index++) {
      value ^= text.charCodeAt(index);
      value = Math.imul(value, 0x01000193);
    }
    return value >>> 0;
  }

  // The chart colours of the theme now, as red, green and blue.
  function chartPalette() {
    return Array.from({ length: PALETTE_SIZE }, (_, index) => cssColor(`--s-chart-${index + 1}`));
  }

  const probe = document.createElement("canvas").getContext("2d", { willReadFrequently: true });

  // A CSS colour variable of the page as [red, green, blue], whatever notation it uses.
  function cssColor(variable) {
    const value = getComputedStyle(html).getPropertyValue(variable).trim();
    probe.clearRect(0, 0, 1, 1);
    probe.fillStyle = "#808080";
    probe.fillStyle = value || "#808080";
    probe.fillRect(0, 0, 1, 1);
    const [red, green, blue] = probe.getImageData(0, 0, 1, 1).data;
    return [red, green, blue];
  }

  // A colour as Chart.js reads it.
  function rgb([red, green, blue], alpha) {
    return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
  }

  // A tick callback for Chart.js: each tick in the format, short, with the fewest decimals
  // that tell the axis's ticks apart, such as 99.2% and 99.4% on an axis from 99% to 100%.
  function tickLabels(format) {
    let seen = null;
    let written = [];
    return (value, index, ticks) => {
      if (ticks !== seen) {
        seen = ticks;
        const values = ticks.map((t) => t.value);
        for (let digits = 0; digits <= 4; digits += 1) {
          const write = numberFormat(format, { tick: digits });
          written = values.map(write);
          if (new Set(written).size === written.length) break;
        }
      }
      return written[index];
    };
  }

  // A number format and the digits it may take after a colon, as Angular's digitsInfo writes
  // them: {minIntegerDigits}.{minFractionDigits}-{maxFractionDigits}, such as
  // currency:EUR:1.0-0. Integer takes none: it is number:1.0-0 already.
  const NUMBER_FORMAT =
    /^(?:integer|(number|percent|currency:([A-Z]{3}))(?::([1-9])\.([0-6])-([0-6]))?)$/;

  // A number format as Intl reads it: its options; whether it is whole, a percent or a
  // currency; the digits it fixes, or null when it lets a number show every decimal it has;
  // and the most decimals of a value it shows, or undefined for every one: two more than its
  // digits for a percent, as 0.255 is 25.5%. Null for a format that isn't a number's. A value
  // that rounds to zero is written without its minus: 0, never -0.
  function numberStyle(format) {
    const found = typeof format === "string" ? NUMBER_FORMAT.exec(format) : null;
    if (!found) return null;
    const [, kind, currency, integers, least, most] = found;
    const whole = !kind;
    const percent = kind === "percent";
    let options = {};
    if (whole) options = { maximumFractionDigits: 0, signDisplay: "negative" };
    else if (percent) options = { style: "percent" };
    else if (currency) options = { style: "currency", currency };
    const digits = integers && {
      minimumIntegerDigits: Number(integers),
      minimumFractionDigits: Number(least),
      maximumFractionDigits: Number(most),
      signDisplay: "negative",
    };
    let places;
    if (digits) places = Number(most) + (percent ? 2 : 0);
    else if (whole) places = 0;
    return { options, whole, percent, currency: Boolean(currency), digits, places };
  }

  // A format with digits steps an axis's ticks by its most decimals at least: whole euros have
  // no tick at €1.50. Integer keeps Chart.js's own steps, as before digits.
  function tickPrecision(format) {
    const style = numberStyle(format);
    return style?.digits ? { precision: style.places } : {};
  }

  // A function that writes a number in its format. A format with digits rounds to them, half
  // away from zero. Without, a number shows every decimal it has: an uptime of 99.998% reads
  // 99.998%, never 100%, and a currency at least its cents. A whole number is written whole.
  // `most` caps those decimals, for a number worked out, such as an average. A tick is written
  // short, with `tick` decimals at most: see tickLabels.
  function numberFormat(format, { most = MAX_PLACES, tick } = {}) {
    const style = numberStyle(format) ?? numberStyle("number");
    const make = (more) => {
      try {
        return new Intl.NumberFormat(language.numbers, { ...style.options, ...more });
      } catch {
        return new Intl.NumberFormat(language.numbers, { maximumFractionDigits: 2 });
      }
    };
    const written = (formatter) => (value) =>
      Number.isFinite(value) ? formatter.format(value) : "–";
    if (tick !== undefined) {
      return written(
        make({ notation: "compact", minimumFractionDigits: 0, maximumFractionDigits: tick }),
      );
    }
    if (style.whole) return written(make({}));
    if (style.digits) return written(make(style.digits));
    const least = make({}).resolvedOptions().minimumFractionDigits;
    const made = new Map();
    return (value) => {
      if (!Number.isFinite(value)) return "–";
      const places = Math.max(least, Math.min(most, placesOf(style.percent, value)));
      if (!made.has(places)) {
        made.set(places, make({ minimumFractionDigits: least, maximumFractionDigits: places }));
      }
      return made.get(places).format(value);
    };
  }

  // The most decimals a number is written with.
  const MAX_PLACES = 20;

  // How many decimals a number has, as its format shows it: 0.99998 has three as a percent.
  // The last digits that arithmetic leaves in a float, as in 0.1 + 0.2, are not its own.
  function placesOf(percent, value) {
    const shown = percent ? value * 100 : value;
    const [digits, exponent = "0"] = String(Number(shown.toPrecision(12))).split("e");
    const fraction = (digits.split(".")[1] || "").length;
    return Math.min(MAX_PLACES, Math.max(0, fraction - Number(exponent)));
  }

  // ---------------------------------------------------------------- Languages

  // The words the renderer writes itself, and the locales of its numbers and dates, in each
  // language an app may be written in: see `App(language=...)`. An app's own words are its own.
  const LANGUAGES = {
    en: {
      numbers: "en",
      dates: "en-GB", // 9 Nov 2026
      words: {
        search: "Search…", searchRows: "Search the rows", clearSearch: "Clear the search",
        filters: "Filters", filtersOn: (count) => `Filters, ${count} on`, chips: "Filters on",
        remove: (text) => `Remove ${text}`, colon: ": ", quoted: (text) => `“${text}”`,
        from: "From", to: "To", contains: "Contains", clear: "Clear filters", table: "Table",
        previous: "Previous", next: "Next", first: "First page", last: "Last page",
        shown: (from, to, all) => `${from}–${to} of ${all}`,
        none: "No rows", noMatch: "No row matches. ", yes: "Yes", no: "No",
        total: "Total", avg: "Avg", min: "Min", max: "Max", count: "Count",
        series: "Series", label: "Label",
      },
    },
    fr: {
      numbers: "fr",
      dates: "fr", // 9 nov. 2026
      words: {
        search: "Rechercher…", searchRows: "Rechercher dans les lignes",
        clearSearch: "Effacer la recherche", filters: "Filtres",
        filtersOn: (count) => `Filtres, ${count} ${count > 1 ? "actifs" : "actif"}`,
        chips: "Filtres actifs", remove: (text) => `Retirer ${text}`,
        // French sets a colon and its quotes apart with spaces that never break.
        colon: "\u00a0: ", quoted: (text) => `«\u00a0${text}\u00a0»`,
        from: "De", to: "À", contains: "Contient",
        clear: "Effacer les filtres", table: "Tableau", previous: "Précédente",
        next: "Suivante", first: "Première page", last: "Dernière page",
        shown: (from, to, all) => `${from}–${to} sur ${all}`,
        none: "Aucune ligne", noMatch: "Aucune ligne ne correspond. ", yes: "Oui", no: "Non",
        total: "Total", avg: "Moy.", min: "Min", max: "Max", count: "Nombre",
        series: "Série", label: "Libellé",
      },
    },
  };
  let language = LANGUAGES.en;
  let words = language.words;
  let collator = sorting(language.numbers);
  let labelStyles = new Map();

  // How a language sorts text: numbers by value, and without case or accents.
  function sorting(locale) {
    return new Intl.Collator(locale, { numeric: true, sensitivity: "base" });
  }

  // A label's format: the kind of label a chart's x axis or a grid's date column shows, and how
  // much of it, short unless it says long. A bare name is short, so date is date:short.
  const LABEL_FORMAT = /^(date|month|weekday)(?::(short|long))?$/;
  const LABEL_NUMBER = /^[0-9]{1,2}$/;
  const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
  // The part of a date each kind of label names, and the language's own words for it. A month
  // or a weekday alone keeps the language's casing, as French writes nov. and lundi.
  const LABEL_OPTIONS = {
    date: {
      short: { day: "numeric", month: "short", year: "numeric" }, // 9 Nov 2026
      long: { day: "numeric", month: "long", year: "numeric" }, // 9 November 2026
    },
    month: { short: { month: "short" }, long: { month: "long" } },
    weekday: { short: { weekday: "short" }, long: { weekday: "long" } },
  };

  // How the app's language writes one kind of label in one style, made the first time a screen
  // asks for it: a screen with no labelled chart and no date column writes none.
  function labelStyle(kind, style) {
    const key = `${kind}:${style}`;
    if (!labelStyles.has(key)) {
      const options = { ...LABEL_OPTIONS[kind][style], timeZone: "UTC" };
      labelStyles.set(key, new Intl.DateTimeFormat(language.dates, options));
    }
    return labelStyles.get(key);
  }

  // What a label's format writes, and what it reads: a grid's date column reads dates whatever
  // it shows of them. Null for a format that isn't a label's.
  function labelFormat(format, reads) {
    const found = typeof format === "string" ? LABEL_FORMAT.exec(format) : null;
    if (!found) return null;
    const kind = found[1];
    return { reads: reads ?? kind, write: labelStyle(kind, found[2] ?? "short") };
  }

  // The day a label names, as UTC: a date as YYYY-MM-DD, a month as 1 to 12, a weekday as 1 to 7
  // from Monday. Null when the label isn't one its kind reads.
  function labelDay(kind, label) {
    if (kind === "date") {
      const found = ISO_DATE.exec(label);
      if (!found) return null;
      const day = Date.UTC(Number(found[1]), Number(found[2]) - 1, Number(found[3]));
      return Number.isFinite(day) ? day : null;
    }
    if (!LABEL_NUMBER.test(label)) return null;
    const number = Number(label);
    // 2024 begins on a Monday, so its first week reads 1 for Monday and 7 for Sunday.
    if (kind === "weekday") return number >= 1 && number <= 7 ? Date.UTC(2024, 0, number) : null;
    return number >= 1 && number <= 12 ? Date.UTC(2024, number - 1, 1) : null;
  }

  // A label as its format writes it, or `unreadable` when the format doesn't read it.
  function labelText(form, label, unreadable) {
    const day = labelDay(form.reads, label);
    return day === null ? unreadable : form.write.format(day);
  }

  // How a format writes a label, or null for a format that isn't a label's. A label the format
  // doesn't read is left as the view wrote it.
  function labelWriter(format) {
    const form = labelFormat(format);
    if (!form) return null;
    return (label) => labelText(form, label, label);
  }

  // ---------------------------------------------------------------- Grids

  // A grid's rows per page; the most rows a grid shows without search, filters and pages, when
  // it doesn't list its tools; and the most values a text column has to be filtered by ticking
  // them.
  const GRID_PAGE = 25;
  const GRID_SMALL = 10;
  const GRID_TOOLS = ["sort", "search", "filters", "pages"];
  const GRID_CHOICES = 12;
  // The title of the group of a grouped grid's rows that have no value of the field.
  const GRID_NONE = "—";
  const GRID_NO_GROUP = Symbol("no group");
  // A text column is a category when each of its values comes back this often, on average.
  const GRID_REPEATS = 4;
  // A cell's text longer than this may be cut short, so it is given whole as a title too.
  const LONG_TEXT = 24;
  const TOTALS = new Set(["sum", "avg", "min", "max", "count"]);
  // How a number column colours its cells by their sign: signed colours both, negative only the
  // negatives.
  const CELL_TONES = new Set(["signed", "negative"]);
  // The words of a total other than a sum, which the first cell's "Total" names.
  const TOTAL_WORDS = new Set(["avg", "min", "max", "count"]);
  let grids = 0; // gives each grid's filters an id of their own

  // Build a grid from Cliquet's grid format: a table the person sorts, searches, filters and
  // pages through in the browser, on the rows the view sent. Every cell is written as text.
  function prepareGrid(element, raw, context) {
    const spec = parseObject(raw);
    if (!Array.isArray(spec.columns) || !Array.isArray(spec.rows)) return;
    const columns = spec.columns
      .filter((column) => isObject(column) && typeof column.field === "string")
      .map(gridColumn);
    if (!columns.length) return;
    const grouped = typeof spec.group_by === "string";
    // A grid's groups, by their value of the field it groups by, in the order each first
    // appears: one group of every row, with no title, when it groups none.
    const groups = new Map();
    const rows = spec.rows.filter(isObject).map((row, index) => {
      const values = columns.map((c) => cellValue(c, has(row, c.field) ? row[c.field] : null));
      const shown = columns.map((column, at) => column.write(values[at]));
      // Rows with no value of their own share one group, titled with a dash: its key is no
      // text, so a group whose value is a dash stays apart from it.
      const value = row[spec.group_by];
      const key = grouped ? (isText(value) ? value : GRID_NO_GROUP) : null;
      if (!groups.has(key)) {
        const title = key === GRID_NO_GROUP ? GRID_NONE : key;
        groups.set(key, { title, words: fold(title || ""), rows: [] });
      }
      const group = groups.get(key);
      const indent = grouped && [1, 2].includes(row.indent) ? row.indent : 0;
      const made = { index, values, shown, words: shown.map(fold), indent, group };
      group.rows.push(made);
      return made;
    });
    for (const column of columns) column.filter = gridFilter(column, rows);
    const grid = {
      key: `${context.grids.length}:${columns.map((c) => c.field).join(",")}`,
      id: `s-grid-${++grids}`,
      columns,
      rows,
      groups: [...groups.values()],
      tools: gridTools(spec.tools, rows.length),
      compact: spec.density === "compact",
      icons: context.icons, // the drawings of the icons its bar and its chips show
      sort: null, // {column, direction}, direction "ascending" or "descending"
      search: "",
      filters: new Map(), // a column's index -> a Set of values, {from, to}, or a text
      page: 0,
      open: false,
      parts: {},
      showing: "", // the rows drawn now, by their place in the view's rows
      top: 0, // how far its box is scrolled, down and sideways, to give back once drawn
      left: 0,
      watch: null, // the ResizeObserver of its scrolling box, stopped when the screen goes
    };
    restoreGrid(grid, context.kept.get(grid.key));
    element.append(buildGrid(grid, element.getAttribute("aria-label")));
    context.grids.push(grid);
    renderGrid(grid);
  }

  // The tools a grid shows: those it lists, or, when it lists none, every tool past a few
  // rows, and only sort for a small grid, which is read at a glance.
  function gridTools(tools, count) {
    if (Array.isArray(tools)) return new Set(GRID_TOOLS.filter((tool) => tools.includes(tool)));
    return new Set(count <= GRID_SMALL ? ["sort"] : GRID_TOOLS);
  }

  // A column of a grid as the script uses it: its kind (number, date or text) and how it
  // writes a cell.
  function gridColumn(spec, index) {
    const format = typeof spec.format === "string" ? spec.format : "text";
    // A label's format is a date column, however much of the date it shows: its cells are the
    // dates they are, so it still sorts and filters by them.
    const dates = labelFormat(format, "date");
    const number = numberStyle(format);
    let kind = "text";
    if (number) kind = "number";
    else if (dates) kind = "date";
    // Without a header, a column reads as its field, as a view's tab does: paid_to is "Paid to".
    const named = spec.field.replace(/_/g, " ");
    const header = typeof spec.header === "string" ? spec.header : capitalized(named);
    let write = cellText;
    if (kind === "number") write = cellNumbers(format);
    else if (dates) write = cellDates(dates);
    const total = TOTALS.has(spec.total) ? spec.total : null;
    // An amount coloured by its sign, and a state by a soft badge of its tone.
    const tone = kind === "number" && CELL_TONES.has(spec.tone) ? spec.tone : null;
    const tones =
      kind === "text" && isObject(spec.tones)
        ? new Map(Object.entries(spec.tones).filter(([, named]) => TONES.has(named)))
        : null;
    return {
      index,
      field: spec.field,
      header,
      format,
      kind,
      number, // how a number column writes its cells: see numberStyle
      write,
      total,
      tone, // signed or negative: how a number column colours its text
      tones, // a text column's values -> the tone of their badge
      filter: null,
    };
  }

  // A text with its first letter in capitals.
  function capitalized(text) {
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  // A cell's value, as its column's kind reads it: null where it has none.
  function cellValue(column, value) {
    if (column.kind === "number") return Number.isFinite(value) ? value : null;
    if (column.kind === "date") {
      return typeof value === "string" && ISO_DATE.test(value) ? value : null;
    }
    if (typeof value === "string") return value === "" ? null : value;
    return typeof value === "number" || typeof value === "boolean" ? value : null;
  }

  // A function that writes a number cell in its column's format.
  function cellNumbers(format) {
    const write = numberFormat(format);
    return (value) => (value === null ? "" : write(value));
  }

  // How a column writes its date cells in its label's format: the whole date, as date:short
  // writes 9 Nov 2026, or its month or its weekday, as Nov or Mon.
  function cellDates(form) {
    return (value) => (value === null ? "" : labelText(form, value, ""));
  }

  // A text cell: true and false read as Yes and No, in the app's language.
  function cellText(value) {
    if (value === null) return "";
    if (typeof value === "boolean") return value ? words.yes : words.no;
    return String(value);
  }

  // Text as a search compares it: without case or accents.
  function fold(text) {
    // French writes its numbers with unbreakable spaces, which a person types as plain ones.
    return text.normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[\u00a0\u202f]/g, " ")
      .toLowerCase();
  }

  // How a column is filtered, from its kind and its data: from–to for numbers and dates, the
  // values to tick for text with few of them, such as a category, and "contains" for other
  // text. Null for a column with nothing to filter.
  function gridFilter(column, rows) {
    const present = rows.filter((row) => row.values[column.index] !== null);
    if (!present.length) return null;
    if (column.kind !== "text") {
      const values = present.map((row) => row.values[column.index]);
      const low = values.reduce((a, b) => (b < a ? b : a));
      const high = values.reduce((a, b) => (b > a ? b : a));
      return low === high ? null : { kind: "range", low, high };
    }
    const values = [...new Set(present.map((row) => row.shown[column.index]))];
    if (values.length < 2) return null;
    if (values.length <= GRID_CHOICES && values.length * GRID_REPEATS <= rows.length) {
      return { kind: "values", values: values.sort(collator.compare) };
    }
    return { kind: "contains" };
  }

  // What the person did to a grid, to give back to the grid laid out in its place.
  function gridState(grid) {
    const filters = new Map();
    for (const [at, filter] of grid.filters) {
      if (filter instanceof Set) filters.set(at, new Set(filter));
      else filters.set(at, isObject(filter) ? { ...filter } : filter);
    }
    const { sort, search, page, open, showing } = grid;
    const { scrollTop: top, scrollLeft: left } = grid.parts.scroll;
    return { sort, search, filters, page, open, showing, top, left };
  }

  // Give a grid what the person did to the one in its place before, where it still has the
  // tool and its columns still filter the same way.
  function restoreGrid(grid, kept) {
    if (!kept) return;
    const tools = grid.tools;
    if (tools.has("sort")) grid.sort = kept.sort;
    if (tools.has("search")) grid.search = kept.search;
    if (tools.has("pages")) grid.page = kept.page;
    Object.assign(grid, { showing: kept.showing, top: kept.top, left: kept.left });
    if (!tools.has("filters")) return;
    grid.open = kept.open;
    for (const [at, filter] of kept.filters) {
      const kind = grid.columns[at] && grid.columns[at].filter && grid.columns[at].filter.kind;
      if (kind === "values" && filter instanceof Set) {
        const values = new Set(grid.columns[at].filter.values);
        grid.filters.set(at, new Set([...filter].filter((value) => values.has(value))));
      } else if (kind === "range" && isObject(filter)) grid.filters.set(at, filter);
      else if (kind === "contains" && typeof filter === "string") grid.filters.set(at, filter);
    }
  }

  // A small helper: an element of this tag with these classes, and its text or child, if any.
  function make(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.append(text);
    return element;
  }

  // A button that does something in the page, never a form's submit.
  function pageButton(className, text, onPress) {
    const button = make("button", className, text);
    button.type = "button";
    button.addEventListener("click", onPress);
    return button;
  }

  // Build a grid's parts: its search and filters, a table that scrolls in its own box with its
  // header row and its first column pinned, and its pages, each as its tools say. `renderGrid`
  // fills in the rows.
  function buildGrid(grid, label) {
    const { parts, tools } = grid;
    const box = make("div", "s-grid");
    // Enter in the grid's own fields never sends a form the grid sits in.
    box.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && event.target instanceof HTMLInputElement) event.preventDefault();
    });
    const filtered = tools.has("filters") ? grid.columns.filter((column) => column.filter) : [];
    const bar = make("div", "s-grid-bar");
    // The search is a field of its own, with a magnifier and a button that empties it, set
    // apart from the Filters button, so that nobody takes the button for its submit.
    if (tools.has("search")) {
      const search = make("input");
      search.type = "search";
      search.placeholder = words.search;
      search.setAttribute("aria-label", words.searchRows);
      search.value = grid.search;
      const searched = () => {
        grid.search = search.value;
        grid.page = 0;
        renderGrid(grid);
      };
      search.addEventListener("input", searched);
      parts.unsearch = pageButton("btn btn-ghost btn-xs btn-circle", "", () => {
        search.value = "";
        searched();
        search.focus();
      });
      parts.unsearch.setAttribute("aria-label", words.clearSearch);
      parts.unsearch.append(...gridIcon(grid, "x"));
      const field = make("label", "input input-sm s-grid-search");
      field.append(...gridIcon(grid, "search"), search, parts.unsearch);
      bar.append(field);
      parts.search = search;
    }
    if (filtered.length) {
      const toggle = pageButton("btn btn-sm btn-ghost s-grid-toggle", "", () => {
        grid.open = !grid.open;
        renderGrid(grid);
      });
      toggle.setAttribute("aria-controls", `${grid.id}-filters`);
      parts.count = make("span", "badge badge-sm badge-primary s-grid-count");
      // A phone shows the icon and the count alone: cliquet.css hides the word from sight only.
      const word = make("span", "s-grid-word", words.filters);
      toggle.append(...gridIcon(grid, "funnel"), word, parts.count);
      parts.toggle = toggle;
      bar.append(toggle);
    }
    if (bar.childElementCount) box.append(bar);
    if (parts.toggle) {
      // Each filter on shows under the bar, panel open or closed, as a chip that removes it.
      parts.chips = make("div", "s-grid-chips");
      parts.chips.setAttribute("role", "group");
      parts.chips.setAttribute("aria-label", words.chips);
      parts.panel = filterPanel(grid, filtered);
      box.append(parts.chips, parts.panel);
    }
    const scroll = make("div", "layout-scroll-x s-grid-scroll");
    // A box that scrolls is reached by the keyboard, to scroll it with the arrows.
    scroll.tabIndex = 0;
    scroll.setAttribute("role", "region");
    scroll.setAttribute("aria-label", label || words.table);
    // Each edge the table scrolls under shows a shadow: on the left, the pinned column's
    // border; on the right, a sign that more columns wait there.
    const edges = () => {
      const left = Math.abs(scroll.scrollLeft);
      scroll.classList.toggle("s-grid-scrolled", left > 0);
      scroll.classList.toggle("s-grid-more", left + scroll.clientWidth < scroll.scrollWidth - 1);
    };
    scroll.addEventListener("scroll", edges);
    const thead = make("thead");
    // A group's title is pinned just under the header row, as tall as cliquet.css is told.
    grid.watch = new ResizeObserver(() => {
      edges();
      scroll.style.setProperty("--s-grid-head", `${thead.getBoundingClientRect().height}px`);
    });
    grid.watch.observe(scroll);
    grid.watch.observe(thead);
    parts.scroll = scroll;
    // A compact grid has the rows of daisyUI's smallest table.
    const table = make("table", `table table-pin-cols${grid.compact ? " table-xs" : ""}`);
    const head = make("tr");
    parts.headers = grid.columns.map((column) => {
      const th = make("th", column.kind === "number" ? "layout-end" : "");
      th.setAttribute("scope", "col");
      if (tools.has("sort")) {
        const sort = pageButton("s-grid-sort", column.header, () => sortBy(grid, column.index));
        const arrow = make("span", "s-grid-arrow");
        arrow.setAttribute("aria-hidden", "true");
        sort.append(arrow);
        th.append(sort);
      } else {
        th.append(column.header);
      }
      head.append(th);
      return th;
    });
    thead.append(head);
    table.append(thead);
    parts.head = thead;
    parts.bodies = [];
    if (grid.columns.some((column) => column.total)) {
      parts.totals = make("tr");
      const tfoot = make("tfoot");
      tfoot.append(parts.totals);
      table.append(tfoot);
    }
    scroll.append(table);
    box.append(scroll);
    // How many rows show, said aloud as a search or a filter changes them: in sight with the
    // pages, and to a screen reader alone without them.
    if (tools.has("pages") || tools.has("search") || parts.toggle) {
      parts.shown = make("span", tools.has("pages") ? "s-grid-shown" : "s-grid-shown s-sr-only");
      parts.shown.setAttribute("aria-live", "polite");
    }
    if (tools.has("pages")) {
      const pager = make("div", "s-grid-pager");
      pager.append(parts.shown);
      if (grid.rows.length > GRID_PAGE) {
        // Each page button is a chevron named by its label, which a script's click reads too.
        const turn = (name, word, to) => {
          const kind = `btn btn-sm btn-square join-item s-grid-page ${name}`;
          const button = pageButton(kind, "", () => {
            grid.page = to(grid);
            renderGrid(grid);
          });
          button.setAttribute("aria-label", word);
          button.title = word;
          return button;
        };
        parts.previous = turn("s-grid-previous", words.previous, (g) => g.page - 1);
        parts.next = turn("s-grid-next", words.next, (g) => g.page + 1);
        parts.pages = make("div", "join");
        // A grid of more than two pages also goes straight to its first and its last.
        if (grid.rows.length > 2 * GRID_PAGE) {
          parts.first = turn("s-grid-first", words.first, () => 0);
          parts.last = turn("s-grid-last", words.last, () => Infinity);
          parts.pages.append(parts.first, parts.previous, parts.next, parts.last);
        } else {
          parts.pages.append(parts.previous, parts.next);
        }
        pager.append(parts.pages);
      }
      parts.pager = pager;
      box.append(pager);
    } else if (parts.shown) {
      box.append(parts.shown);
    }
    return box;
  }

  // The drawing of an icon a grid shows, as a list to append: none when the screen lacks it.
  function gridIcon(grid, name) {
    return has(grid.icons, name) ? [drawIcon(grid.icons[name])] : [];
  }

  // The panel of a grid's filters, behind its Filters button: one for each column that has one.
  function filterPanel(grid, columns) {
    const panel = make("div", "s-grid-panel");
    panel.id = `${grid.id}-filters`;
    panel.setAttribute("role", "group");
    panel.setAttribute("aria-label", words.filters);
    const fields = make("div", "s-grid-fields");
    grid.parts.inputs = new Map(); // a column's index -> the panel's fields of its filter
    const changed = () => {
      grid.page = 0;
      renderGrid(grid);
    };
    for (const column of columns) {
      const fieldset = make("fieldset", "fieldset");
      fieldset.append(make("legend", "fieldset-legend", column.header));
      const at = column.index;
      const inputs = [];
      grid.parts.inputs.set(at, inputs);
      if (column.filter.kind === "values") {
        const chosen = grid.filters.get(at) || new Set();
        grid.filters.set(at, chosen);
        const choices = make("div", "s-grid-choices");
        for (const value of column.filter.values) {
          const box = make("input", "checkbox checkbox-sm");
          box.type = "checkbox";
          box.checked = chosen.has(value);
          box.addEventListener("change", () => {
            if (box.checked) chosen.add(value);
            else chosen.delete(value);
            changed();
          });
          const label = make("label", "label s-grid-choice");
          label.append(box, value);
          choices.append(label);
          inputs.push(box);
        }
        fieldset.append(choices);
      } else if (column.filter.kind === "range") {
        const range = grid.filters.get(at) || { from: "", to: "" };
        grid.filters.set(at, range);
        const bounds = make("div", "layout-stack layout-gap-2");
        const { low, high } = column.filter;
        const ends = [["from", words.from, low], ["to", words.to, high]];
        for (const [end, word, shown] of ends) {
          const input = make("input");
          input.type = column.kind === "date" ? "date" : "number";
          if (column.kind === "number") {
            input.step = "any";
            // A percent reads as people write it, without a float's last digits: 7, not
            // 7.000000000000001.
            const percent = column.number.percent;
            input.placeholder = String(percent ? Number((shown * 100).toPrecision(12)) : shown);
          } else {
            input.min = column.filter.low;
            input.max = column.filter.high;
          }
          input.value = range[end];
          input.addEventListener("input", () => {
            range[end] = input.value;
            changed();
          });
          const label = make("label", "input input-sm");
          label.append(make("span", "label", word), input);
          bounds.append(label);
          inputs.push(input);
        }
        fieldset.append(bounds);
      } else {
        const input = make("input");
        input.type = "text";
        input.value = grid.filters.get(at) || "";
        input.addEventListener("input", () => {
          grid.filters.set(at, input.value);
          changed();
        });
        const label = make("label", "input input-sm");
        label.append(make("span", "label", words.contains), input);
        fieldset.append(label);
        inputs.push(input);
      }
      fields.append(fieldset);
    }
    grid.parts.clear = pageButton("btn btn-sm btn-ghost", words.clear, () => clearGrid(grid));
    const actions = make("div", "layout-row layout-end");
    actions.append(grid.parts.clear);
    panel.append(fields, actions);
    return panel;
  }

  // Sort a grid by a column: ascending, then descending, then as the view sent it.
  function sortBy(grid, column) {
    const sort = grid.sort;
    if (!sort || sort.column !== column) grid.sort = { column, direction: "ascending" };
    else if (sort.direction === "ascending") grid.sort = { column, direction: "descending" };
    else grid.sort = null;
    grid.page = 0;
    renderGrid(grid);
  }

  // Clear a grid's search and filters.
  function clearGrid(grid) {
    grid.search = "";
    if (grid.parts.search) grid.parts.search.value = "";
    for (const at of grid.filters.keys()) clearFilter(grid, at);
    grid.page = 0;
    renderGrid(grid);
  }

  // Clear one column's filter, and its fields in the panel. The panel's fields hold on to the
  // filter's set of values or its range, so those are emptied in place.
  function clearFilter(grid, at) {
    const filter = grid.filters.get(at);
    if (filter instanceof Set) filter.clear();
    else if (isObject(filter)) Object.assign(filter, { from: "", to: "" });
    else grid.filters.set(at, "");
    for (const input of grid.parts.inputs?.get(at) ?? []) {
      if (input.type === "checkbox") input.checked = false;
      else input.value = "";
    }
  }

  // The filters that narrow a grid's rows now, each as its column's index and the words of
  // its chip: "Category: Food, Rent", "Amount: €2.00–€20.00", "Paid to: “shop”".
  function filtersOn(grid) {
    const on = [];
    // In the columns' order, whatever order they were set in.
    for (const column of grid.columns) {
      const at = column.index;
      const filter = grid.filters.get(at);
      let shown = null;
      if (filter instanceof Set) {
        // The values in the panel's order, whatever order they were ticked in.
        const values = column.filter.values.filter((value) => filter.has(value));
        if (values.length) shown = values.join(", ");
      } else if (isObject(filter)) {
        // A bound that reads as no value narrows nothing, so it shows nothing either.
        const [from, to] = rangeOf(column, filter).map((b) => b !== null && bound(column, b));
        if (from && to) shown = `${from}–${to}`;
        else if (from) shown = `≥ ${from}`;
        else if (to) shown = `≤ ${to}`;
      } else if (typeof filter === "string" && filter.trim()) {
        shown = words.quoted(filter.trim());
      }
      if (shown !== null) on.push({ at, text: `${column.header}${words.colon}${shown}` });
    }
    return on;
  }

  // A range's bound as its chip writes it: in its column's format, a date always in full and
  // short, as 1 Dec 2026, even in a column that shows only its month.
  function bound(column, value) {
    if (column.kind === "date") return labelStyle("date", "short").format(labelDay("date", value));
    return column.write(value);
  }

  // The chip of a filter on, which removes it. The focus moves to the next chip, or to the
  // Filters button once none is left, so that it never falls back to the page.
  function filterChip(grid, { at, text }, place) {
    const chip = pageButton("btn btn-xs btn-soft s-grid-chip", make("span", "", text), () => {
      clearFilter(grid, at);
      grid.page = 0;
      renderGrid(grid);
      const left = grid.parts.chips.children;
      (left[Math.min(place, left.length - 1)] || grid.parts.toggle).focus();
    });
    chip.title = text;
    chip.setAttribute("aria-label", words.remove(text));
    chip.append(...gridIcon(grid, "x"));
    return chip;
  }

  // A range filter's bounds, as its column's values: null for a bound left empty. The person
  // types a percent as they read it: 25 for 25%.
  function rangeOf(column, range) {
    return [range.from, range.to].map((text) => {
      if (typeof text !== "string" || text.trim() === "") return null;
      if (column.kind === "date") return ISO_DATE.test(text) ? text : null;
      const number = Number(text);
      if (!Number.isFinite(number)) return null;
      return column.number.percent ? number / 100 : number;
    });
  }

  // Whether a row passes a grid's search and every filter.
  function passes(grid, row) {
    const search = fold(grid.search.trim());
    // A search matches a row's group too, as its title reads above it.
    const found = (words) => words.includes(search);
    if (search && !row.words.some(found) && !found(row.group.words)) return false;
    for (const [at, filter] of grid.filters) {
      const value = row.values[at];
      if (filter instanceof Set) {
        if (filter.size && !filter.has(row.shown[at])) return false;
      } else if (isObject(filter)) {
        const [from, to] = rangeOf(grid.columns[at], filter);
        if (from === null && to === null) continue;
        if (value === null || (from !== null && value < from) || (to !== null && value > to)) {
          return false;
        }
      } else {
        const text = fold(String(filter).trim());
        if (text && !row.words[at].includes(text)) return false;
      }
    }
    return true;
  }

  // A group's rows in its grid's sort: numbers and dates by value, text as people sort words.
  // Empty cells go last either way, and rows that tie keep the view's order. A row sits under
  // the nearest row above it with a smaller indent: it sorts among the rows under that one,
  // and moves with it.
  function sorted(grid, rows) {
    if (!grid.sort) return rows;
    const at = grid.sort.column;
    const column = grid.columns[at];
    const sign = grid.sort.direction === "ascending" ? 1 : -1;
    const compare = (a, b) => {
      const x = a.row.values[at];
      const y = b.row.values[at];
      const index = a.row.index - b.row.index;
      if (x === null || y === null) return (x === null) - (y === null) || index;
      let order;
      if (column.kind === "number") order = x - y;
      else if (column.kind === "date") order = x < y ? -1 : x > y ? 1 : 0;
      else order = collator.compare(a.row.shown[at], b.row.shown[at]);
      return sign * order || index;
    };
    const top = { indent: -1, under: [] };
    const above = [top];
    for (const row of rows) {
      const node = { row, indent: row.indent, under: [] };
      while (above.at(-1).indent >= row.indent) above.pop();
      above.at(-1).under.push(node);
      above.push(node);
    }
    const order = [];
    const walk = (nodes) => {
      for (const node of nodes.sort(compare)) {
        order.push(node.row);
        walk(node.under);
      }
    };
    walk(top.under);
    return order;
  }

  // A column's total over these rows, written as its cells are, and the value it writes: null
  // for a count, or when no row has a value.
  function total(column, rows) {
    const values = rows.map((row) => row.values[column.index]).filter((value) => value !== null);
    const written = (value, text = column.write(value)) => ({ text, value });
    if (column.total === "count") return written(null, numberFormat("integer")(values.length));
    if (!values.length) return written(null, "");
    if (column.total === "min") return written(values.reduce((a, b) => (b < a ? b : a)));
    if (column.total === "max") return written(values.reduce((a, b) => (b > a ? b : a)));
    // A sum has the decimals of its values, without the noise floats leave as they add up:
    // 0.1, 0.2 and -0.3 add up to 0.
    const exact = Math.max(...values.map((value) => placesOf(false, value)));
    const sum = Number(values.reduce((a, b) => a + b, 0).toFixed(exact));
    if (column.total === "sum") return written(sum);
    const mean = sum / values.length;
    // An average is written in the column's digits, when it has some. Without, it has one
    // decimal more than its most precise value, and at least two, or one as a percent: an
    // average of whole numbers has decimals too.
    if (column.number.digits) return written(mean);
    const format = column.number.whole ? "number" : column.format;
    const percent = column.number.percent;
    const places = Math.max(...values.map((value) => placesOf(percent, value)));
    const most = Math.max(percent ? 1 : 2, places + 1);
    return written(mean, numberFormat(format, { most })(mean));
  }

  // The class that colours a number of a column with a tone by its sign, or "" for none. The
  // number keeps its minus, so the colour is never the only sign. The sign is the number's as
  // its cell shows it: a value its format rounds to zero is zero.
  function signClass(column, value) {
    if (!column.tone || typeof value !== "number") return "";
    const { places } = column.number;
    const shown = places === undefined ? value : Number(value.toFixed(places));
    if (shown < 0) return "text-error";
    return column.tone === "signed" && shown > 0 ? "text-success" : "";
  }

  // A cell's text as its column draws it: a value its column gives a tone as a soft badge.
  function cellContent(column, value, text) {
    const tone = column.tones && typeof value === "string" ? column.tones.get(value) : undefined;
    if (!tone) return text;
    const named = tone === "neutral" ? "" : ` badge-${tone}`;
    return make("span", `badge badge-soft${named}`, text);
  }

  // The cells of a row of totals over these rows: each column's total, and the label in the
  // first cell when that column has none.
  function totalCells(grid, rows, label) {
    return grid.columns.map((column) => {
      const cell = make(column.index === 0 ? "th" : "td", cellClass(column));
      if (column.index === 0) cell.setAttribute("scope", "row");
      if (column.total) {
        if (TOTAL_WORDS.has(column.total)) {
          cell.append(make("span", "s-grid-total-word", `${words[column.total]} `));
        }
        const { text, value } = total(column, rows);
        cell.append(text);
        const sign = signClass(column, value);
        if (sign) cell.classList.add(sign);
      } else if (column.index === 0) {
        cell.append(label);
        if (label.length > LONG_TEXT) cell.setAttribute("title", label);
      }
      return cell;
    });
  }

  // A row of a grid's body, its first cell naming it and staying in sight as the table scrolls
  // sideways, in from the left as far as its indent says.
  function gridRow(grid, row) {
    const tr = make("tr");
    for (const column of grid.columns) {
      const first = column.index === 0;
      const text = row.shown[column.index];
      const value = row.values[column.index];
      const cell = make(first ? "th" : "td", cellClass(column), cellContent(column, value, text));
      const sign = signClass(column, value);
      if (sign) cell.classList.add(sign);
      if (first) cell.setAttribute("scope", "row");
      if (first && row.indent) cell.classList.add(`s-grid-indent-${row.indent}`);
      // A long text is cut short on its line: the whole of it shows on hover.
      if (text.length > LONG_TEXT) cell.setAttribute("title", text);
      tr.append(cell);
    }
    return tr;
  }

  // The row that titles a group, pinned under the header row while the group's rows scroll:
  // one cell across the row, so a long title reads past the first column without widening it.
  function titleRow(grid, title) {
    const tr = make("tr", "s-grid-title");
    const th = make("th", "", make("span", "", title));
    th.colSpan = grid.columns.length;
    th.setAttribute("scope", "rowgroup");
    if (title.length > LONG_TEXT) th.setAttribute("title", title);
    tr.append(th);
    return tr;
  }

  // Draw a grid's rows as its sort, search, filters and page say, with its totals and its
  // place among the pages. Each group is a body of its own: its title, its rows on this page,
  // and its subtotal where it ends, over all of its rows that pass.
  function renderGrid(grid) {
    const parts = grid.parts;
    const groups = grid.groups
      .map((group) => ({ group, rows: sorted(grid, group.rows).filter((r) => passes(grid, r)) }))
      .filter((part) => part.rows.length);
    const matching = groups.flatMap((part) => part.rows);
    const paged = Boolean(parts.next);
    const pages = Math.max(1, Math.ceil(matching.length / GRID_PAGE));
    grid.page = Math.max(0, Math.min(grid.page, pages - 1));
    const first = paged ? grid.page * GRID_PAGE : 0;
    const shown = paged ? matching.slice(first, first + GRID_PAGE) : matching;
    const bodies = [];
    let start = 0; // where the group starts among the matching rows
    for (const { group, rows } of groups) {
      const from = Math.max(first - start, 0);
      const to = Math.min(first + shown.length - start, rows.length);
      start += rows.length;
      if (from >= to) continue;
      const body = make("tbody");
      // A group cut by a page titles the next one too.
      if (group.title !== null) body.append(titleRow(grid, group.title));
      body.append(...rows.slice(from, to).map((row) => gridRow(grid, row)));
      if (group.title !== null && parts.totals && to === rows.length) {
        const subtotal = make("tr", "s-grid-subtotal");
        subtotal.append(...totalCells(grid, rows, `${words.total} ${group.title}`));
        body.append(subtotal);
      }
      bodies.push(body);
    }
    const on = parts.toggle ? filtersOn(grid) : [];
    const active = on.length + (grid.search.trim() ? 1 : 0);
    if (!bodies.length) bodies.push(make("tbody", "", emptyRow(grid, active)));
    for (const body of parts.bodies) body.remove();
    parts.head.after(...bodies);
    parts.bodies = bodies;
    // Other rows, from another page, sort or filter, are read from their top.
    const showing = shown.map((row) => row.index).join(",");
    if (showing !== grid.showing) {
      parts.scroll.scrollTop = 0;
      grid.top = 0;
    }
    grid.showing = showing;
    // Each header of a grid that sorts says how the rows are sorted: cliquet.css draws its
    // arrow from aria-sort.
    if (grid.tools.has("sort")) {
      grid.parts.headers.forEach((th, at) => {
        const direction = grid.sort && grid.sort.column === at ? grid.sort.direction : "none";
        th.setAttribute("aria-sort", direction);
      });
    }
    if (parts.totals) {
      parts.totals.replaceChildren(...totalCells(grid, matching, words.total));
      parts.totals.hidden = !matching.length;
    }
    if (parts.unsearch) parts.unsearch.hidden = grid.search === "";
    if (parts.toggle) {
      parts.toggle.setAttribute("aria-expanded", String(grid.open));
      parts.toggle.classList.toggle("btn-active", grid.open);
      parts.panel.hidden = !grid.open;
      // cliquet.css writes the number, so the button reads as its word alone to a script's click.
      // The badge counts the panel's own filters: the search shows what it holds itself.
      parts.count.setAttribute("data-count", String(on.length));
      parts.count.hidden = on.length === 0;
      const named = on.length ? words.filtersOn(on.length) : words.filters;
      parts.toggle.setAttribute("aria-label", named);
      parts.chips.replaceChildren(...on.map((filter, place) => filterChip(grid, filter, place)));
      parts.chips.hidden = on.length === 0;
      parts.clear.disabled = active === 0;
    }
    if (parts.shown) {
      // The count of a grid without pages stays, to say aloud that no row matches.
      if (parts.pager) parts.pager.hidden = !matching.length;
      if (matching.length) {
        parts.shown.textContent = words.shown(first + 1, first + shown.length, matching.length);
      } else {
        parts.shown.textContent = grid.rows.length ? words.noMatch.trim() : words.none;
      }
    }
    if (paged) {
      const focused = document.activeElement;
      const back = [parts.first, parts.previous].filter(Boolean);
      const ahead = [parts.next, parts.last].filter(Boolean);
      for (const button of back) button.disabled = grid.page === 0;
      for (const button of ahead) button.disabled = grid.page >= pages - 1;
      // Rows that fit on one page need no page buttons, and the ends only past two pages.
      parts.pages.hidden = pages < 2;
      for (const button of [parts.first, parts.last].filter(Boolean)) button.hidden = pages < 3;
      // A page button that turns off hands the focus to one of the other side.
      if (ahead.includes(focused) && focused.disabled && !parts.previous.disabled) {
        parts.previous.focus();
      } else if (back.includes(focused) && focused.disabled && !parts.next.disabled) {
        parts.next.focus();
      }
    }
  }

  // The classes of a column's cells: numbers at the end, numbers and dates on one line.
  function cellClass(column) {
    if (column.kind === "number") return "layout-end layout-nowrap";
    return column.kind === "date" ? "layout-nowrap" : "";
  }

  // The row a grid shows when it shows none: it has none, or none matches, with a way to
  // clear the filters.
  function emptyRow(grid, active) {
    const cell = make("td", "s-grid-empty");
    cell.colSpan = grid.columns.length;
    if (!grid.rows.length || !active) {
      cell.append(words.none);
    } else {
      cell.append(words.noMatch);
      const clear = pageButton("btn btn-link btn-sm", words.clear, () => clearGrid(grid));
      cell.append(clear);
    }
    const tr = make("tr");
    tr.append(cell);
    return tr;
  }

  // ---------------------------------------------------------------- Small helpers

  // Whether a value is a plain object, not null nor a list.
  function isObject(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
  }

  // Whether a value is text that says something.
  function isText(value) {
    return typeof value === "string" && value !== "";
  }

  // Whether an object has a key of its own, never one from its prototype.
  function has(object, key) {
    return isObject(object) && Object.hasOwn(object, key);
  }

  // Whether a value is a screen in the format this script draws. Its arguments become {} if
  // they are missing.
  function isScreen(value) {
    if (!isObject(value) || value.screen !== FORMAT || !Array.isArray(value.body)) return false;
    if (!isObject(value.arguments)) value.arguments = {};
    return typeof value.view === "string";
  }

  // A JSON object from an attribute, or an empty one.
  function parseObject(text) {
    if (!isText(text)) return {};
    try {
      const value = JSON.parse(text);
      return isObject(value) ? value : {};
    } catch {
      return {};
    }
  }

  // Whether two field values are the same.
  function same(a, b) {
    return JSON.stringify(a) === JSON.stringify(b);
  }

  // The text of a tool's result.
  function textOf(result) {
    const content = isObject(result) && Array.isArray(result.content) ? result.content : [];
    return content
      .filter((item) => isObject(item) && typeof item.text === "string")
      .map((item) => item.text)
      .join("\n");
  }

  // A fresh id for a request, so the server runs each action once.
  function newRequestId() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)]
      .join("-");
  }

  start();
})();
