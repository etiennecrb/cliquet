"""The upload page of a reference: where the person sends the file an action takes, from
their browser, when their assistant draws no panel or its file chooser doesn't open.

It is open to whoever the reference is for, a member of the app as much as its owner: anyone
else is told it is missing.
"""

from urllib.parse import urlencode

import anyio
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from cliquet.limits import MAX_FILE_ARGUMENT, MB
from cliquet_server.db import PersonInfo
from cliquet_server.inputs import TOO_BIG, Pending, References, upload_path
from cliquet_server.pages import problem
from cliquet_server.parts import PartRefused

from .build import TOO_LARGE
from .common import (
    Site,
    content_length,
    form_expired,
    form_value,
    other_site,
    readable_size,
    to_login,
)

# The page of a reference: its body may pass `MAX_BODY`, up to `MAX_FILE_UPLOAD`, which the
# server's `BodyLimit` checks.
FILE_UPLOAD_PATH = upload_path("{reference}")
MAX_FILE_UPLOAD = MAX_FILE_ARGUMENT + MB
"""An upload's request body: the file, and room for the form's token and the multipart
framing."""


class FileUploads:
    """The upload pages of the references to the files people upload for an action."""

    def __init__(self, site: Site, inputs: References) -> None:
        self.site = site
        self.inputs = inputs

    async def show(self, request: Request) -> Response:
        """The upload page of one of the person's references."""
        found = await self._found(request)
        if isinstance(found, Response):
            return found
        message = ""
        if request.query_params.get("uploaded") and found[1].file_name is not None:
            message = (
                f"{found[1].file_name} is uploaded: {readable_size(found[1].size or 0)}. Go "
                "back to your assistant and tell it the file is there."
            )
        return await self._page(request, *found, message, done=bool(message))

    async def receive(self, request: Request) -> Response:
        """Keep the file the person sent for their reference.

        Everything that needs no body is checked before the body is read: the site the form
        came from, the person, the reference and the body's size. The form's token is checked
        right after.
        """
        if other_site(request, self.site.settings):
            return form_expired(request)
        found = await self._found(request)
        if isinstance(found, Response):
            return found
        person, pending = found
        if request.scope.get(TOO_LARGE):
            return await self._page(request, person, pending, TOO_BIG, status=413)
        try:
            length = content_length(request.headers)
        except ValueError:
            length = None
        if length is None:
            return problem(
                request, 411, "The upload has no size", "Send the file from the upload page."
            )
        async with request.form(max_files=1, max_fields=2) as form:
            if not self.site.auth.csrf_ok(request, form_value(form.get("csrf"))):
                return form_expired(request)
            file = form.get("file")
            # A form sent with no file chosen holds an empty part with no name.
            if not isinstance(file, UploadFile) or not file.filename or not file.size:
                message = "Choose a file to upload."
                return await self._page(request, person, pending, message, status=400)
            if file.size > MAX_FILE_ARGUMENT:
                return await self._page(request, person, pending, TOO_BIG, status=413)
            try:
                # Starlette spooled the file: it is copied in a thread.
                await anyio.to_thread.run_sync(
                    self.inputs.receive,
                    person.login,
                    pending.reference,
                    file.filename,
                    file.file,
                    file.size,
                )
            except PartRefused as error:
                return await self._page(request, person, pending, str(error), status=400)
        # Post, redirect, get: reloading the page doesn't send the file again.
        target = f"{upload_path(pending.reference)}?{urlencode({'uploaded': 1})}"
        return RedirectResponse(target, status_code=303)

    async def _found(self, request: Request) -> tuple[PersonInfo, Pending] | Response:
        """The signed-in person and their reference, or the page that refuses them: anyone
        else's reference, or one that ended, is missing."""
        person = await self.site.auth.person(request)
        if person is None:
            return to_login(request)
        reference = request.path_params["reference"]
        pending = self.inputs.find(person.login, reference)
        if pending is None:
            return problem(
                request,
                404,
                "No such upload",
                "This upload has ended, or isn't yours. Ask your assistant for a new one.",
            )
        return person, pending

    async def _page(
        self,
        request: Request,
        person: PersonInfo,
        pending: Pending,
        message: str = "",
        *,
        status: int = 200,
        done: bool = False,
    ) -> Response:
        """The page of a reference, with its file once one came, and what the upload did."""
        context = {
            "here": "",
            "app": f"{pending.login}/{pending.name}",
            "action": pending.action,
            "argument": pending.argument,
            "reference": pending.reference,
            "file_name": pending.file_name,
            "size": None if pending.size is None else readable_size(pending.size),
            "limit": readable_size(MAX_FILE_ARGUMENT),
            # The page refuses a bigger file before sending it, in the server's words.
            "max_size": MAX_FILE_ARGUMENT,
            "too_big": TOO_BIG,
            "message": message,
            "done": done,
        }
        response = await self.site.console(request, person, "upload_file.html", context)
        response.status_code = status
        return response
