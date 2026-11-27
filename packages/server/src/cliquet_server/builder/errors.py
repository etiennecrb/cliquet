"""The errors the builder's tools raise for a request that can't be done."""

from cliquet_server.db import AppInfo


def no_such_app(app: str) -> str:
    """Why a builder's tool finds no app of the owner's named `app`, and how to find or start
    one."""
    return (
        f"You have no app named {app}. Call list_apps to see your apps, or create_app to start one."
    )


class BuilderError(Exception):
    """A tool can't do what was asked. The message says why, and what to call next."""


class LiveData(BuilderError):
    """A write to the data of an app that is published, as `info` says it is now."""

    def __init__(self, message: str, info: AppInfo) -> None:
        super().__init__(message)
        self.info = info

    def __reduce__(self) -> tuple[type, tuple[str, AppInfo]]:
        return type(self), (str(self), self.info)
