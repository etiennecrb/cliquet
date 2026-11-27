"""The server's web pages: the console, where people build and manage apps, and each app's own
pages, where people use it.

Each area is in its own module: `console`, `data_files`, `app_pages`, `build` and `download`,
with what they share in `common`. This module puts their routes together.
"""

from collections.abc import Awaitable, Callable
from functools import partial

from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles

from cliquet_server.accounts import Accounts
from cliquet_server.app_icon import HOME_ICONS, ICON
from cliquet_server.apps import Apps
from cliquet_server.builder import Builder
from cliquet_server.endpoints import Registry
from cliquet_server.mark import FILES, STATIC, MarkFile
from cliquet_server.settings import Settings
from cliquet_server.web_sign_in import CHOOSE_LOGIN_PATH, WebAuth

from .account import AccountPage
from .app_pages import WEB_MANIFEST, AppPages
from .build import TOO_LARGE, UPLOAD_PATH, DraftPages, Upload
from .common import Site
from .console import Console
from .data_files import DataFiles
from .download import Download
from .icon import AppIcon
from .uploads import FILE_UPLOAD_PATH, MAX_FILE_UPLOAD, FileUploads

__all__ = ["FILE_UPLOAD_PATH", "MAX_FILE_UPLOAD", "TOO_LARGE", "UPLOAD_PATH", "web_routes"]

type _Endpoint = Callable[[Request], Awaitable[Response]]


def _mark(file: MarkFile) -> Route:
    """One of the mark's files at the root, where a browser, a crawler and an MCP client look
    for it, served from the static folder as the pages' other assets are."""
    path = STATIC / file.name

    async def answer(request: Request) -> Response:
        return FileResponse(path, media_type=file.media_type)

    return Route(f"/{file.name}", answer, methods=["GET"])


def _page(apps: Apps, path: str, endpoint: _Endpoint, *, json: bool = False) -> Route:
    """A page's route, for a GET only: a HEAD gets a 405. It waits for an app to start as
    `Apps.page` says, then shows that the app is starting; one that answers `json` waits as a
    call does, since what fetches it doesn't ask again.

    Starlette answers a HEAD by running the GET and dropping the body. Many pages start an
    app, build a draft or read git, so that would be slow work for nothing. Every page here
    refuses a HEAD; the download, the mark's files and an app's icon answer one with their own
    headers.
    """

    async def answer(request: Request) -> Response:
        if request.method == "HEAD":
            return Response(status_code=405, headers={"allow": "GET"})
        if json:
            return await endpoint(request)
        with apps.page():
            return await endpoint(request)

    return Route(path, answer)


def web_routes(
    apps: Apps,
    registry: Registry,
    settings: Settings,
    auth: WebAuth,
    builder_core: Builder | None,
    accounts: Accounts,
) -> list[BaseRoute]:
    """The web app's routes: sign-in, the console, the account page, each app's own pages,
    the upload pages of the files people upload for an action, and the static files.

    With the builder, the owner of a draft also sees its preview under /build.
    """
    previews = builder_core.previews if builder_core is not None else None
    page = partial(_page, apps)
    site = Site(apps, settings, auth, registry.limits, previews)
    console = Console(site, builder_core, registry)
    drafts = DraftPages(site, builder_core)
    download = Download(site)
    data_files = DataFiles(site)
    pages = AppPages(site, registry)
    icon = AppIcon(apps)
    account = AccountPage(site, accounts)
    files = FileUploads(site, registry.inputs)
    upload = []
    if builder_core is not None:
        uploads = Upload(site, builder_core)
        upload = [
            page(UPLOAD_PATH, uploads.show),
            Route(UPLOAD_PATH, uploads.receive, methods=["POST"]),
        ]
    manage = "/manage/{login}/{app}"
    draft_page = "/build/{app}/preview"
    return [
        page("/login", auth.login),
        page("/signup", auth.sign_up),
        page("/auth/callback", auth.callback),
        page(CHOOSE_LOGIN_PATH, auth.choose_login),
        Route(CHOOSE_LOGIN_PATH, auth.take_login, methods=["POST"]),
        Route("/logout", auth.logout, methods=["POST"]),
        page("/", console.home),
        page("/build", console.build),
        page("/assistants", console.assistants),
        page("/tokens", console.tokens),
        Route("/tokens", console.make_token, methods=["POST"]),
        Route("/tokens/{token_id:int}/revoke", console.revoke_token, methods=["POST"]),
        page("/account", account.show),
        Route("/account/delete", account.delete, methods=["POST"]),
        Route("/account/verification-email", account.send_email, methods=["POST"]),
        page(draft_page, drafts.draft_preview),
        page(f"{draft_page}/{{role}}", drafts.draft_role_page),
        page(f"{draft_page}/{{role}}/screen", drafts.draft_screen),
        Route(f"{draft_page}/{{role}}/rpc", drafts.draft_rpc, methods=["POST"]),
        *upload,
        page(manage, console.overview),
        page(f"{manage}/views", console.views_tab),
        page(f"{manage}/preview", console.old_views_tab),
        page(f"{manage}/sharing", console.sharing),
        page(f"{manage}/tools", console.tools),
        page(f"{manage}/history", console.history),
        page(f"{manage}/history/{{commit}}", console.change),
        Route(f"{manage}/history/{{commit}}/undo", console.undo, methods=["POST"]),
        Route(f"{manage}/versions/{{commit}}/restore", console.restore, methods=["POST"]),
        page(f"{manage}/errors", console.errors),
        page(f"{manage}/errors/{{error}}", console.error),
        Route(f"{manage}/download", download.download),
        page(f"{manage}/delete", console.delete_page),
        Route(f"{manage}/delete", console.delete_app, methods=["POST"]),
        page(f"{manage}/data/{{file:path}}", data_files.show),
        Route(f"{manage}/share", console.share, methods=["POST"]),
        Route(
            f"{manage}/members/{{person_id}}/{{role}}/remove",
            console.remove_member,
            methods=["POST"],
        ),
        Route(
            # An email may hold a slash, which the address carries decoded.
            f"{manage}/shares/{{email:path}}/{{role}}/remove",
            console.remove_share,
            methods=["POST"],
        ),
        Route("/shares/{login}/{app}/accept", console.accept_share, methods=["POST"]),
        Route("/shares/{login}/{app}/decline", console.decline_share, methods=["POST"]),
        page(FILE_UPLOAD_PATH, files.show),
        Route(FILE_UPLOAD_PATH, files.receive, methods=["POST"]),
        page("/apps/{login}/{app}", pages.open_app),
        Route(f"/apps/{{login}}/{{app}}/{ICON}", icon.answer, methods=["GET"]),
        *(
            Route(f"/apps/{{login}}/{{app}}/{file}", icon.home(home), methods=["GET"])
            for file, home in HOME_ICONS.items()
        ),
        page("/apps/{login}/{app}/{role}", pages.role_page),
        page(f"/apps/{{login}}/{{app}}/{{role}}/{WEB_MANIFEST}", pages.web_manifest, json=True),
        page("/apps/{login}/{app}/{role}/screen", pages.screen),
        Route("/apps/{login}/{app}/{role}/rpc", pages.rpc, methods=["POST"]),
        *(_mark(file) for file in FILES),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
