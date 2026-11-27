"""The build pages of a draft, for its owner: its preview, and the upload page that puts a data
file in it."""

from functools import partial
from typing import Any
from urllib.parse import urlencode

import anyio
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from cliquet.host import answer_rpc, host_page
from cliquet.limits import MAX_DATA_FILE
from cliquet.tools import NoSuchRole, app_role
from cliquet_server.builder import Builder, BuilderError
from cliquet_server.builder.errors import LiveData
from cliquet_server.builder.files import TOO_BIG
from cliquet_server.builder.previews import PreviewEnded, PreviewSession
from cliquet_server.db import AppInfo, PersonInfo
from cliquet_server.endpoints import role_of
from cliquet_server.manifest import accent
from cliquet_server.pages import problem, render

from .common import (
    Site,
    app_identity,
    app_label,
    content_length,
    foreign_call,
    form_expired,
    form_value,
    other_site,
    pick_view,
    readable_size,
)

# The upload page: the one route whose body may pass `MAX_BODY`, up to `MAX_UPLOAD`, which the
# server's `BodyLimit` checks.
UPLOAD_PATH = "/build/{app}/upload"
# Set in the scope of an upload whose stated size is over `MAX_UPLOAD`: `BodyLimit` hands it on
# without its body, so that the page says the file is too big.
TOO_LARGE = "cliquet.too_large"


class DraftPages:
    """A draft's preview in the browser. Without the builder, no preview is ever running."""

    def __init__(self, site: Site, builder: Builder | None) -> None:
        self.site = site
        self.settings = site.settings
        self.builder = builder
        self.previews = builder.previews if builder is not None else None

    async def draft_preview(self, request: Request) -> Response:
        """Send the owner to the running preview's first role."""
        found = await self.site.draft(request)
        if isinstance(found, Response):
            return found
        _, info = found
        # Building the draft runs in the sandbox: refused once the month's time is used.
        over = self.site.apps.over_budget(info.login)
        if over is not None:
            refused = problem(request, 429, "Sandbox time used", str(over))
            refused.headers["Retry-After"] = str(over.retry_after)
            return refused
        # The first role of the draft as it is now, which the role's page then runs.
        shown = await self._following(info)
        if shown is None:
            return self._no_preview(request, info)
        roles = shown[0].roles or ["-"]
        return RedirectResponse(f"/build/{info.name}/preview/{roles[0]}", status_code=303)

    async def draft_role_page(self, request: Request) -> Response:
        """The draft's page for one role, on its preview."""
        found = await self.site.draft(request)
        if isinstance(found, Response):
            return found
        _, info = found
        role = request.path_params["role"]
        shown = await self._following(info)
        if shown is None:
            return self._no_preview(request, info)
        session, notice = shown

        def pick() -> dict[str, Any] | Response:
            # The role is checked against the runtime that lays the views out.
            try:
                with session.using(role_of(role)) as server:
                    return pick_view(request, server)
            except NoSuchRole as error:
                return _no_role(request, error)
            except PreviewEnded:
                return self._no_preview(request, info)

        picked = await anyio.to_thread.run_sync(pick)
        if isinstance(picked, Response):
            return picked
        context = {
            **app_identity(info, session.manifest),
            **picked,
            "accent": accent(session.manifest),
            "console_href": _console_href(info),
            "running": True,
            "notice": notice,
            "role": role,
            "roles": session.roles,
        }
        return render(request, "build_preview.html", context)

    async def draft_screen(self, request: Request) -> Response:
        """The frame that draws the draft's screens, on its preview."""
        found = await self.site.draft(request)
        if isinstance(found, Response):
            return found
        person, info = found
        role = request.path_params["role"]
        session = await self._running(person, info)
        if session is None:
            return self._no_preview(request, info)
        try:
            app_role(session.manifest, role_of(role))
        except NoSuchRole as error:
            return _no_role(request, error)
        return HTMLResponse(host_page())

    async def draft_rpc(self, request: Request) -> Response:
        """Answer a call from a draft's screen, on its preview."""
        role = request.path_params["role"]
        refused = foreign_call(request, self.settings)
        if refused is not None:
            return JSONResponse({"error": refused}, status_code=403)
        found = await self.site.draft(request, json=True)
        if isinstance(found, Response):
            return found
        _, info = found
        key = f"draft {info.login}/{info.name}"
        # Counted before the draft is built, so a refused call costs no build.
        with self.site.screen_call(
            key, f"the preview of {info.name}", info.login, info.login
        ) as over:
            if over is not None:
                return over
            # When the draft doesn't build, the call runs the last version that did, which the
            # page says once it is loaded again.
            shown = await self._following(info)
            if shown is None:
                return self._no_preview(request, info, json=True)
            session, _ = shown
            body = await request.body()
            try:
                with session.using(role_of(role)) as server:
                    return await answer_rpc(server, body)
            except NoSuchRole as error:
                return JSONResponse({"error": str(error)}, status_code=404)
            except PreviewEnded:
                return self._no_preview(request, info, json=True)

    def _no_preview(self, request: Request, info: AppInfo, json: bool = False) -> Response:
        """The answer when no preview is running: the page that says how to start one, or
        that a publish ended it, or, for a view's call, JSON."""
        previews = self.previews
        published = previews is not None and previews.ended_by_publish(info.login, info.name)
        if json:
            why = (
                f"{info.name} was published, which ended its preview"
                if published
                else f"no preview of {info.name} is running"
            )
            return JSONResponse(
                {"error": f"{why}: ask your assistant for a new one with start_preview"},
                status_code=404,
            )
        return render(
            request,
            "build_preview.html",
            {
                **app_identity(info),
                "console_href": _console_href(info),
                "running": False,
                "published": published,
                "roles": [],
                "views": [],
                "role": "-",
                "view": None,
                "label": "",
            },
        )

    async def _running(self, person: PersonInfo, info: AppInfo) -> PreviewSession | None:
        """The draft's preview, if one is running. Looking never starts one."""
        if self.previews is None:
            return None
        find = partial(self.previews.find, person.login, info.name)
        return await anyio.to_thread.run_sync(find, limiter=self.site.apps.builder_calls)

    async def _following(self, info: AppInfo) -> tuple[PreviewSession, str | None] | None:
        """The draft's preview, running the draft's code as it is now, as the builder's tools
        do, and what the page says about it. It builds the draft, but never starts a preview."""
        if self.builder is None:
            return None
        build = partial(self.builder.draft_preview, info)
        return await anyio.to_thread.run_sync(build, limiter=self.site.apps.builder_calls)


class Upload:
    """The upload page of a draft: only with the builder, which keeps the drafts."""

    def __init__(self, site: Site, builder: Builder) -> None:
        self.site = site
        self.settings = site.settings
        self.auth = site.auth
        self.builder = builder

    async def show(self, request: Request) -> Response:
        """The upload page of a draft."""
        found = await self.site.draft(request)
        if isinstance(found, Response):
            return found
        person, info = found
        return await self._page(request, person, info, request.query_params.get("path", "data/"))

    async def receive(self, request: Request) -> Response:
        """Copy the file the owner sent into the draft, never into a published app.

        Everything that needs no body is checked before the body is read: the site the form
        came from, the owner, a published app and the body's size. The form's token is
        checked right after.
        """
        if other_site(request, self.settings):
            return form_expired(request)
        found = await self.site.draft(request)
        if isinstance(found, Response):
            return found
        person, info = found
        if info.published_at is not None:
            return await self._page(request, person, info, "", status=409)
        if request.scope.get(TOO_LARGE):
            # Its body was never read: the path is the one the page was opened with.
            path = request.query_params.get("path", "data/")
            return await self._page(request, person, info, path, TOO_BIG, 413)
        # A body without a size was counted, up to `MAX_BODY`.
        try:
            length = content_length(request.headers)
        except ValueError:
            length = None
        if length is None:
            return problem(
                request, 411, "The upload has no size", "Send the file from the upload page."
            )
        async with request.form(max_files=1, max_fields=3) as form:
            if not self.auth.csrf_ok(request, form_value(form.get("csrf"))):
                return form_expired(request)
            path = form_value(form.get("path")).strip()
            file = form.get("file")
            # A form sent with no file chosen holds an empty part with no name.
            if not isinstance(file, UploadFile) or not file.filename or not file.size:
                message = "Choose a file to upload."
                return await self._page(request, person, info, path, message, 400)
            if file.size > MAX_DATA_FILE:
                return await self._page(request, person, info, path, TOO_BIG, 413)
            try:
                # Starlette spooled the file; copy it in the thread that holds the draft's lock.
                await anyio.to_thread.run_sync(
                    self.builder.upload, info, path, file.file, file.size
                )
            except LiveData as error:
                # The assistant published the app while the file was on its way.
                message = (
                    f"{app_label(info)} was published while the file was on its way, "
                    "so it isn't in it."
                )
                return await self._page(request, person, error.info, "", message, 409)
            except BuilderError as error:
                return await self._page(request, person, info, path, str(error), 400)
        # Post, redirect, get: reloading the page doesn't send the file again.
        query = urlencode({"path": path, "uploaded": path})
        return RedirectResponse(f"/build/{info.name}/upload?{query}", status_code=303)

    async def _page(
        self,
        request: Request,
        person: PersonInfo,
        info: AppInfo,
        path: str,
        message: str = "",
        status: int = 200,
    ) -> Response:
        """The upload page of a draft, with its data files, and what the last upload did."""
        files = []
        done = False
        if info.published_at is None:
            found = await anyio.to_thread.run_sync(self.builder.uploaded, info)
            files = [{"path": file["path"], "size": readable_size(file["size"])} for file in found]
            uploaded = request.query_params.get("uploaded")
            if not message and uploaded in {file["path"] for file in found}:
                message = (
                    f"{uploaded} is in the draft. "
                    "Go back to your assistant and tell it the file is there."
                )
                done = True
        context = {
            **app_identity(info),
            "path": path,
            "published": info.published_at is not None,
            "files": files,
            "limit": readable_size(MAX_DATA_FILE),
            # The page refuses a bigger file before sending it, in the server's words.
            "max_size": MAX_DATA_FILE,
            "too_big": TOO_BIG,
            "message": message,
            "done": done,
        }
        response = await self.site.console(request, person, "build_upload.html", context)
        response.status_code = status
        return response


def _no_role(request: Request, error: NoSuchRole) -> Response:
    """The page that says the draft has no such role, and which it has."""
    return problem(request, 404, "No such role", error.sentence)


def _console_href(info: AppInfo) -> str:
    """Where the preview's page leads back to: the app's page once it is published, else
    the person's home, which lists the draft."""
    return f"/manage/{info.login}/{info.name}" if info.published_at else "/"
