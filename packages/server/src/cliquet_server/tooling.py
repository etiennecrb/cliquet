"""What the tools on the person's endpoint share, the builder's among them: the hints that tell
a host what each tool does, the sentences that keep access the person's to give, and the shape
of an answer."""

from typing import Any

from mcp_types import CallToolResult, TextContent, ToolAnnotations

# What a tool does, for the host that decides when to ask the person. Each changes nothing
# outside this Cliquet server.
READS = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
DESTROYS = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)


def idempotent(hints: ToolAnnotations) -> ToolAnnotations:
    """`hints`, for a tool that writes and whose call made again with the same arguments
    changes nothing more: it does the same again, or is refused, as a second delete of the same
    file is. A tool that only reads needs no such hint: MCP reads it only for one that writes."""
    return hints.model_copy(update={"idempotent_hint": True})


# Who decides who gets in: in the endpoint's instructions, and again after an app's own in
# `describe_app`.
ONLY_THE_PERSON = (
    "Only the person decides who gets in. Accept or decline a share, share an app or revoke a "
    "share, and create or revoke a token only when the person asks for it in this conversation, "
    "and say first what you will do."
)
# The same, short, in each tool that hands out or takes back access: a model reads every
# tool's description in every conversation.
WHEN_ASKED = "Only when the person asks for it in this conversation."


def answer(text: str, data: dict[str, Any] | None = None) -> CallToolResult:
    """A tool's answer: its text, in one block for the model, and its data, if any, as its
    structured content. Never a list of strings: FastMCP sends one as a JSON text, `["..."]`,
    which a model reads as code."""
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)
