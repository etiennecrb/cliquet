"""The download: the live app as a zip of its repository, for its owner only."""

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

import anyio
import anyio.lowlevel
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from cliquet_server.repos import AppRepo, NoCommit

from .common import Site, no_such_app

# The size of each part of a download sent.
_PART = 1024 * 1024
# How many downloads are made at once: each clones and zips a whole repository.
_DOWNLOADS = 2


class Download:
    """The download route, which makes at most `_DOWNLOADS` zips at once."""

    def __init__(self, site: Site) -> None:
        self.site = site
        self.apps = site.apps
        self.limiter = anyio.CapacityLimiter(_DOWNLOADS)

    async def download(self, request: Request) -> Response:
        """The live app as a zip of its repository, for its owner only, whether or not they
        may build: the account page offers it before the account is deleted."""
        found = await self.site.owner_of(request, builds=False)
        if isinstance(found, Response):
            return found
        login, name = found[1].login, found[1].name
        filename = f"{name}-{self.apps.now():%Y-%m-%d}.zip"
        repo = self.apps.repo(login, name)
        if request.method == "HEAD":
            # What a GET would send, without copying and zipping the repository.
            if await anyio.to_thread.run_sync(repo.head) is None:
                return no_such_app(request, login, name)
            headers = {"content-disposition": f'attachment; filename="{filename}"'}
            return Response(media_type="application/zip", headers=headers)
        try:
            zipped = await _open_export(repo, name, self.limiter)
        except NoCommit:
            return no_such_app(request, login, name)
        return zip_response(zipped, filename)


class _OpenFileResponse(StreamingResponse):
    """A streamed response that closes its file when it ends, however it ends."""

    def __init__(self, file: BinaryIO, headers: dict[str, str], media_type: str) -> None:
        self.file = file

        def parts() -> Iterator[bytes]:
            while part := file.read(_PART):
                yield part

        super().__init__(parts(), media_type=media_type, headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # The client leaving, at any point, or a cancel, ends up here too.
            self.file.close()


def zip_response(zipped: BinaryIO, filename: str) -> StreamingResponse:
    """Send an open zip file as an attachment, in parts, and close it once the response ends."""
    headers = {
        "content-disposition": f'attachment; filename="{filename}"',
        "content-length": str(os.fstat(zipped.fileno()).st_size),
    }
    return _OpenFileResponse(zipped, headers, "application/zip")


async def _open_export(repo: AppRepo, name: str, limiter: anyio.CapacityLimiter) -> BinaryIO:
    """The app's zip, made in a thread and open for reading; closed if the wait is cancelled."""
    opened: list[BinaryIO] = []
    try:
        file = await anyio.to_thread.run_sync(_exported, repo, name, opened, limiter=limiter)
        # The thread isn't stopped by a cancel: the cancel is raised here, once it has finished.
        await anyio.lowlevel.checkpoint_if_cancelled()
        return file
    except BaseException:
        for file in opened:
            file.close()
        raise


def _exported(repo: AppRepo, name: str, opened: list[BinaryIO]) -> BinaryIO:
    """The app's zip, open for reading and added to `opened`, its temporary folder already gone."""
    folder = Path(tempfile.mkdtemp(prefix="cliquet-download-"))
    try:
        # On Linux an open file keeps its data once its folder is removed.
        file = repo.export(folder, name).open("rb")
        opened.append(file)
        return file
    finally:
        shutil.rmtree(folder, ignore_errors=True)
