// The upload panel: Cliquet's own page, which a host that draws MCP Apps shows in a frame
// when the assistant calls ask_for_file. The person chooses a file, and the panel sends it to
// the builder's upload_file tool in parts, in base64. Once the file is in, it tells the
// assistant the file's path and size, never anything from the file itself.
//
// The file panel is the same page in a second mode, `forAction`, which ask_for_app_file shows:
// it sends the file to upload_app_file for the reference the assistant asked for, and tells
// the assistant only the reference, the file's name and its size.
(() => {
  "use strict";

  // What the builder's panels share: see panel.js.
  const { request, $, isObject, textOf, callsTools } = window.CliquetPanel;
  const settings = window.CLIQUET_UPLOAD;
  const forAction = settings.forAction === true;

  const state = {
    asked: null, // what was answered: the app and the path, or the reference, and the link
    host: null, // what the host can do, from the handshake, if it answered
    sending: false,
  };

  // ---------------------------------------------------------------- What the panel shows

  const bytes = (size) => `${Number(size).toLocaleString("en")} bytes`;

  function say(text, kind) {
    const box = $("said");
    box.className = `alert ${kind === "error" ? "alert-error" : "alert-success"}`;
    box.firstElementChild.textContent = text;
    box.hidden = !text;
  }

  function showFiles(found) {
    const list = $("files");
    list.replaceChildren();
    for (const file of Array.isArray(found) ? found : []) {
      if (!isObject(file)) continue;
      const row = document.createElement("li");
      row.className = "list-row";
      const path = document.createElement("code");
      path.className = "list-col-grow";
      path.textContent = String(file.path);
      const size = document.createElement("span");
      size.className = "note";
      size.textContent = bytes(file.size);
      row.append(path, size);
      list.append(row);
    }
    $("none").hidden = list.children.length > 0;
    $("draft").hidden = false;
  }

  // Whether the answer names what to upload: a draft's path, or an action's reference.
  function offers(asked) {
    if (!isObject(asked) || typeof asked.link !== "string") return false;
    if (forAction) {
      return typeof asked.reference === "string" && typeof asked.action === "string" &&
        typeof asked.argument === "string";
    }
    return typeof asked.path === "string";
  }

  // What was answered: a refusal, in the server's words, or the upload to offer.
  function onAsked(result) {
    if (!isObject(result)) return;
    const asked = result.structuredContent;
    if (result.isError || !offers(asked)) {
      $("lead").textContent = textOf(result) || "The assistant's request didn't reach Cliquet.";
      $("choose").hidden = true;
      return;
    }
    state.asked = asked;
    if (forAction) {
      $("title").textContent = `Upload a file for ${asked.action}`;
      $("lead").textContent =
        `The file goes to ${asked.app} straight from your computer, and your assistant ` +
        "never reads it: the app does.";
      $("path").textContent = `${asked.action}, as ${asked.argument}`;
    } else {
      $("title").textContent = `Upload a data file to ${asked.app}`;
      $("lead").textContent =
        "The file goes into the draft, straight from your computer. Nothing is live until " +
        "your assistant publishes the app.";
      $("path").textContent = asked.path;
    }
    $("limit").textContent = `At most ${bytes(asked.limit)}.`;
    $("link").href = asked.link;
    $("elsewhere").hidden = false;
    if (!callsTools(state.host)) {
      // The host lets the frame call no tool, so the file can only go through the link.
      $("lead").textContent = "This assistant can't send a file from here. Open the upload page.";
      return;
    }
    $("choose").hidden = false;
    if (!forAction) showFiles(asked.files);
  }

  // ---------------------------------------------------------------- Sending the file

  // A part of the file, in base64.
  function base64(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
      reader.onerror = () => reject(new Error("The file couldn't be read. Choose it again."));
      reader.readAsDataURL(blob);
    });
  }

  // Send one part to upload_file, or upload_app_file, and give back what it answered, or
  // throw its refusal.
  async function sendPart(call) {
    const name = forAction ? "upload_app_file" : "upload_file";
    const result = await request("tools/call", { name, arguments: call });
    if (!isObject(result) || result.isError || !isObject(result.structuredContent)) {
      throw new Error(textOf(result) || "Cliquet didn't take the file.");
    }
    return result.structuredContent;
  }

  // The frame's sandbox sends no form, so the button sends the file itself.
  async function onSend() {
    if (state.sending || !state.asked) return;
    const file = $("file").files[0];
    $("tell").hidden = true;
    if (!file) {
      say("Choose a file to upload.", "error");
      return;
    }
    if (file.size > state.asked.limit) {
      // Refused before anything is sent.
      say(settings.tooBig, "error");
      return;
    }
    state.sending = true;
    $("send").disabled = true;
    say("", "");
    const progress = $("progress");
    progress.value = 0;
    progress.hidden = false;
    const { app, path, reference } = state.asked;
    try {
      let offset = 0;
      let upload = null;
      for (;;) {
        const data = await base64(file.slice(offset, offset + settings.part));
        const call = forAction
          ? { reference, name: file.name, size: file.size, offset, data }
          : { app, path, size: file.size, offset, data };
        if (upload !== null) call.upload = upload;
        const answer = await sendPart(call);
        if (answer.done && forAction) {
          given(String(answer.name), file.size);
          break;
        }
        if (answer.done) {
          finished(app, path, file.size, answer.files);
          break;
        }
        upload = answer.upload;
        offset = answer.received;
        progress.value = Math.floor((100 * offset) / file.size);
      }
    } catch (error) {
      say(error.message, "error");
    } finally {
      state.sending = false;
      $("send").disabled = false;
      progress.hidden = true;
    }
  }

  // The file is in: show it, and tell the assistant its path and size, never its contents.
  function finished(app, path, size, found) {
    say(`${path} is in the draft: ${bytes(size)}.`, "success");
    showFiles(found);
    const text = `The person uploaded ${path} to the draft of ${app}: ${bytes(size)}.`;
    const message =
      `I uploaded ${path} to the draft of ${app}: ${bytes(size)}. ` +
      `Read its first lines with read_file, then call check_app to see that ${app} reads it.`;
    tell(text, { app, path, size }, message);
  }

  // The file for an action is in: tell the assistant the reference, the file's name, as
  // Cliquet cleaned it, and its size, never its contents.
  function given(name, size) {
    const { action, argument, reference } = state.asked;
    say(`${name} is uploaded: ${bytes(size)}.`, "success");
    const text = `The person uploaded ${name} for ${action}: ${bytes(size)}.`;
    const message =
      `I uploaded ${name} for ${action}: ${bytes(size)}. ` +
      `Call ${action} with ${argument} "${reference}".`;
    tell(text, { reference, name, size }, message);
  }

  // The person's click on Upload asked for it, so the panel sends its one message for this
  // upload now, and the model context stays for a host that ignores the message.
  function tell(text, structuredContent, message) {
    request("ui/update-model-context", {
      content: [{ type: "text", text }],
      structuredContent,
    }).catch(() => {});
    const askPerson = () => {
      $("tell").hidden = false;
    };
    if (!isObject(state.host?.message)) {
      askPerson();
      return;
    }
    request("ui/message", { role: "user", content: [{ type: "text", text: message }] }).then(
      (answer) => {
        if (isObject(answer) && answer.isError) askPerson();
      },
      askPerson,
    );
  }

  async function start() {
    $("send").addEventListener("click", onSend);
    window.CliquetPanel.openLink(
      () => state.asked?.link,
      (text) => say(text, "error"),
    );
    state.host = await window.CliquetPanel.start(
      forAction ? "Cliquet file" : "Cliquet upload",
      onAsked,
    );
  }

  start();
})();
