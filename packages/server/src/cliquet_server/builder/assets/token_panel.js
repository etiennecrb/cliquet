// The token panel: Cliquet's own page, which a host that draws MCP Apps shows in a frame when
// the assistant of a person who signed in calls create_token. It shows the token's name and
// where it will work, and the person's click on Create calls the builder's issue_token tool,
// whose answer, the token's secret, only this panel sees. It tells the assistant the token's
// name and id, never its secret.
(() => {
  "use strict";

  // What the builder's panels share: see panel.js.
  const { request, $, isObject, textOf, callsTools } = window.CliquetPanel;

  const state = {
    asked: null, // what create_token answered: the name, the apps, its days, where, the link
    host: null, // what the host can do, from the handshake, if it answered
    sending: false,
    made: false, // once made, the panel never offers to make it again
  };

  // ---------------------------------------------------------------- What the panel shows

  function say(text) {
    const box = $("said");
    box.firstElementChild.textContent = text;
    box.hidden = !text;
  }

  // One row per line: an app and its role, or an app and its address.
  function showRows(list, rows) {
    list.replaceChildren();
    for (const [first, second] of rows) {
      const row = document.createElement("li");
      row.className = "list-row";
      const name = document.createElement("code");
      name.className = "list-col-grow";
      name.textContent = first;
      const note = document.createElement("span");
      note.className = "note";
      note.textContent = second;
      row.append(name, note);
      list.append(row);
    }
  }

  // What create_token answered: a refusal, in the server's words, or the token to create.
  function onAsked(result) {
    if (!isObject(result) || state.made) return;
    const asked = result.structuredContent;
    if (result.isError || !isObject(asked) || typeof asked.name !== "string" ||
      !Array.isArray(asked.apps) || typeof asked.days !== "number" ||
      typeof asked.link !== "string") {
      // A refusal, or a token made by the assistant's own token, which holds its secret.
      $("lead").textContent = result.isError
        ? textOf(result) || "The assistant's request didn't reach Cliquet."
        : "Your assistant made this token itself. Nothing to do here.";
      $("ask").hidden = true;
      return;
    }
    state.asked = asked;
    $("lead").textContent =
      "It lets an agent act as you, only in these apps and as these roles, for " +
      `${asked.days} days, unless you revoke it sooner on the API tokens page.`;
    $("name").textContent = asked.name;
    showRows($("apps"), asked.apps.filter(isObject).map((one) =>
      [String(one.app), one.role ? `as ${one.role}` : "without a role"]));
    $("link").href = asked.link;
    $("elsewhere").hidden = false;
    if (!callsTools(state.host)) {
      // The host lets the frame call no tool, so the token can only be made on the page.
      $("lead").textContent = "This assistant can't create it from here. Open the API tokens page.";
      return;
    }
    $("ask").hidden = false;
  }

  // ---------------------------------------------------------------- Creating it

  async function onCreate() {
    if (state.sending || state.made || !state.asked) return;
    state.sending = true;
    $("create").disabled = true;
    say("");
    const { name, apps, days } = state.asked;
    try {
      const call = { name: "issue_token", arguments: { name, apps, days } };
      const result = await request("tools/call", call);
      const made = isObject(result) && !result.isError ? result.structuredContent : null;
      if (!isObject(made) || typeof made.token !== "string") {
        throw new Error(textOf(result) || "Cliquet didn't create the token.");
      }
      finished(made);
    } catch (error) {
      say(error.message);
      $("create").disabled = false;
    } finally {
      state.sending = false;
    }
  }

  // The token is made: show its secret, this once, and tell the assistant its name and id.
  function finished(made) {
    state.made = true;
    $("ask").hidden = true;
    $("elsewhere").hidden = true;
    $("lead").textContent = `${made.name} is made, for ${state.asked.days} days.`;
    $("secret").textContent = made.token;
    const where = isObject(made.where) ? made.where : {};
    $("where").textContent = String(where.label || "");
    const addresses = Array.isArray(where.addresses) ? where.addresses.filter(isObject) : [];
    showRows($("addresses"), addresses.map((one) => [String(one.address), String(one.app)]));
    $("made").hidden = false;
    const text =
      `The person created the API token ${made.name}, ${made.token_id}. ${where.label || ""}. ` +
      "They hold its secret: you never see it.";
    request("ui/update-model-context", {
      content: [{ type: "text", text }],
      structuredContent: { name: made.name, token_id: made.token_id, where },
    }).catch(() => {});
  }

  // Copy the secret. Without the clipboard, select it, for the person to copy by hand.
  async function onCopy() {
    try {
      await navigator.clipboard.writeText($("secret").textContent);
      $("copy").textContent = "Copied";
    } catch {
      window.getSelection().selectAllChildren($("secret"));
    }
  }

  async function start() {
    $("create").addEventListener("click", onCreate);
    $("copy").addEventListener("click", onCopy);
    window.CliquetPanel.openLink(
      () => state.asked?.link,
      (text) => {
        $("lead").textContent = text;
      },
    );
    state.host = await window.CliquetPanel.start("Cliquet token", onAsked);
  }

  start();
})();
