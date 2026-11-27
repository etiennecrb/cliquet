"""The builder's panels: Cliquet's own pages in the conversation, where the person uploads a
data file, or creates an API token, and the file panel, where anyone uploads a file for an
action.

`ask_for_file` names the upload panel, and a host that draws MCP Apps shows it in a frame. Its
script reads the file the person chose and sends it to `upload_file` in parts. The file panel
is the same page and script in a second mode, for the person's endpoint's `ask_for_app_file`:
it sends the file to `upload_app_file`, for a reference, and is no builder's. `create_token`
names the token panel, whose Create button calls `issue_token`, and which alone sees the new
token's secret. Like the renderer, each is a trusted page, built by the same shell and speaking
to the host through the same bridge, with the script the panels share, `assets/panel.js`:
everything is inlined, so it loads nothing from the network, and the page itself holds no
secret.
"""

from functools import cache
from importlib.resources import files
from typing import Any

from cliquet import views, vocabulary
from cliquet_server.inputs import TOO_BIG as FILE_TOO_BIG
from cliquet_server.parts import PART

from .files import TOO_BIG

# What the panel changes of a screen's look, `.s-screen`: closer parts and smaller titles.
STYLE = """\
body{margin:0;background:var(--color-base-100)}
.panel{gap:.75rem;overflow-wrap:anywhere}
.panel h1{font-size:1.125rem}
.panel h2{font-size:1rem}
.panel .note{color:color-mix(in oklab,currentcolor 70%,transparent)}
.panel .choose{display:flex;flex-direction:column;gap:.75rem}
.panel .file-input{width:100%}
.panel .actions{display:flex;flex-wrap:wrap;gap:.5rem}
.panel [hidden]{display:none !important}
.panel .secret{display:flex}
.panel .secret code{flex:1;padding:.5rem .75rem;background:var(--color-base-200)}
"""

BODY = """\
<main class="s-screen panel">
  <h1 id="title">Upload a data file</h1>
  <p class="note" id="lead">Waiting for the assistant…</p>
  <div class="choose" id="choose" hidden>
    <p>It goes to <code id="path"></code> in the draft.</p>
    <fieldset class="fieldset">
      <label class="fieldset-legend" for="file">File</label>
      <input class="file-input" id="file" type="file">
      <p class="note" id="limit"></p>
    </fieldset>
    <progress class="progress progress-primary" id="progress" value="0" max="100" hidden>
    </progress>
    <div class="alert" id="said" role="status" hidden><span></span></div>
    <p id="tell" hidden>Tell your assistant the file is in.</p>
    <div class="actions">
      <button class="btn btn-primary" id="send" type="button">Upload</button>
    </div>
  </div>
  <p class="note" id="elsewhere" hidden>If the file chooser doesn't open here,
    <a class="link" id="link" href="#">open the upload page</a> in your browser.</p>
  <section id="draft" hidden>
    <h2>Data in the draft</h2>
    <ul class="list" id="files"></ul>
    <p class="note" id="none">No data file yet.</p>
  </section>
</main>
"""


FILE_BODY = """\
<main class="s-screen panel">
  <h1 id="title">Upload a file</h1>
  <p class="note" id="lead">Waiting for the assistant…</p>
  <div class="choose" id="choose" hidden>
    <p>It goes to <code id="path"></code>, for one call: Cliquet doesn't keep it after.</p>
    <fieldset class="fieldset">
      <label class="fieldset-legend" for="file">File</label>
      <input class="file-input" id="file" type="file">
      <p class="note" id="limit"></p>
    </fieldset>
    <progress class="progress progress-primary" id="progress" value="0" max="100" hidden>
    </progress>
    <div class="alert" id="said" role="status" hidden><span></span></div>
    <p id="tell" hidden>Tell your assistant the file is in.</p>
    <div class="actions">
      <button class="btn btn-primary" id="send" type="button">Upload</button>
    </div>
  </div>
  <p class="note" id="elsewhere" hidden>If the file chooser doesn't open here,
    <a class="link" id="link" href="#">open the upload page</a> in your browser.</p>
</main>
"""


TOKEN_BODY = """\
<main class="s-screen panel">
  <h1 id="title">Create an API token</h1>
  <p class="note" id="lead">Waiting for the assistant…</p>
  <div class="choose" id="ask" hidden>
    <p>Name: <strong id="name"></strong></p>
    <ul class="list" id="apps"></ul>
    <div class="alert alert-error" id="said" role="status" hidden><span></span></div>
    <div class="actions">
      <button class="btn btn-primary" id="create" type="button">Create</button>
    </div>
  </div>
  <section class="choose" id="made" hidden>
    <div class="join secret">
      <code class="join-item" id="secret" aria-label="The new token"></code>
      <button class="btn join-item" id="copy" type="button">Copy</button>
    </div>
    <p class="note">It is shown only this once. Anyone who has it acts as you, so keep it out
      of chats and shared files.</p>
    <p id="where"></p>
    <ul class="list" id="addresses"></ul>
    <p id="tell">Tell your assistant the token is made.</p>
  </section>
  <p class="note" id="elsewhere" hidden>If you can't create it here,
    <a class="link" id="link" href="#">open the API tokens page</a> in your browser.</p>
</main>
"""


def _panel(title: str, body: str, script: str, settings: dict[str, Any]) -> str:
    """One of the builder's panels: daisyUI, Cliquet's bridge to the host, the panels' shared
    script and the panel's own, `script`, inlined, so it loads nothing from the network."""
    assets = files("cliquet_server.builder") / "assets"
    scripts = [views.bridge_js(), (assets / "panel.js").read_text(), (assets / script).read_text()]
    styles = [vocabulary.daisyui_css(), views.cliquet_css(), STYLE]
    return views.trusted_page(title, styles, body, settings, scripts)


@cache
def panel_html() -> str:
    """The upload panel, which sends the file the person chooses to `upload_file`."""
    settings = {"CLIQUET_UPLOAD": {"part": PART, "tooBig": TOO_BIG}}
    return _panel("Upload a data file", BODY, "upload_panel.js", settings)


@cache
def panel_uri() -> str:
    """The `ui://` resource of the upload panel, named by its content, so that a host never
    shows a panel it kept from another version."""
    return views.page_uri("upload", panel_html())


@cache
def file_panel_html() -> str:
    """The file panel: the upload panel's page and script, which send the file the person
    chooses for an action to `upload_app_file`."""
    settings = {"CLIQUET_UPLOAD": {"part": PART, "tooBig": FILE_TOO_BIG, "forAction": True}}
    return _panel("Upload a file", FILE_BODY, "upload_panel.js", settings)


@cache
def file_panel_uri() -> str:
    """The `ui://` resource of the file panel, named by its content."""
    return views.page_uri("file", file_panel_html())


@cache
def token_panel_html() -> str:
    """The token panel, which never holds a token but the one it shows once."""
    return _panel("Create an API token", TOKEN_BODY, "token_panel.js", {})


@cache
def token_panel_uri() -> str:
    """The `ui://` resource of the token panel, named by its content."""
    return views.page_uri("token", token_panel_html())


def panel_uris() -> frozenset[str]:
    """The `ui://` resources of the builder's panels, which only a builder may read."""
    return frozenset({panel_uri(), token_panel_uri()})
