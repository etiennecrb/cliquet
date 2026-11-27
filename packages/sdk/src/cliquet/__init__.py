"""Cliquet: apps for agents and people.

An app is one module with one `App`. Its methods register actions, invariants,
queries and views. A view returns a `Screen`. See the builder's guides.
"""

__version__ = "0.0.1"

from pydantic import BaseModel, Field

from cliquet.app import App
from cliquet.context import ActionContext, Caller, InvariantContext, QueryContext
from cliquet.errors import Denied, Refused
from cliquet.files import File
from cliquet.screens import Screen

__all__ = [
    "ActionContext",
    "App",
    "BaseModel",
    "Caller",
    "Denied",
    "Field",
    "File",
    "InvariantContext",
    "QueryContext",
    "Refused",
    "Screen",
    "__version__",
]
