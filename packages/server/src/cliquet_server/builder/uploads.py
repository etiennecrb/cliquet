"""A data file the upload panel sends in parts, kept outside the draft until its last part.

The panel is the builder's trusted page in the conversation. It reads the file the person
chose and sends it to `upload_file` in parts, which `cliquet_server.parts` keeps, outside the
drafts, and the last one moves it into the draft as the upload page does.
"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cliquet.limits import MAX_DATA_FILE
from cliquet_server.parts import PartRefused, Parts

from .errors import BuilderError
from .files import TOO_BIG, DraftFiles


class Uploads:
    """The uploads on their way from the panel, for every owner."""

    def __init__(
        self, root: Path, files: DraftFiles, clock: Callable[[], float] = time.time
    ) -> None:
        self.files = files
        self.parts = Parts(root, MAX_DATA_FILE, TOO_BIG, clock)

    def receive(
        self,
        login: str,
        app: str,
        path: str,
        size: int,
        offset: int,
        data: str,
        upload: str | None = None,
    ) -> dict[str, Any]:
        """Take one part of a file: the first starts an upload, the last puts the file in the
        draft. Each answer says what comes next."""

        def check() -> None:
            self.files.check_upload(self.files.workshop.find(login, app), path)

        try:
            got = self.parts.receive(login, (app, path), size, offset, data, upload, check)
        except PartRefused as error:
            raise BuilderError(str(error)) from None
        if got.file is None:
            return {"upload": got.upload, "received": got.received, "done": False}
        try:
            info = self.files.workshop.find(login, app)
            with got.file.open("rb") as source:
                self.files.upload(info, path, source, size)
        finally:
            got.file.unlink(missing_ok=True)
        return {"path": path, "size": size, "done": True, "files": self.files.uploaded(info)}

    def drop(self, login: str) -> None:
        """Drop every upload of this person on its way, with its parts."""
        self.parts.drop(login)
