"""Recovery helpers for Codex Responses stream schema drift."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Sequence


_NONE_OUTPUT_ITERABLE_ERROR = "'NoneType' object is not iterable"


class CodexStreamSchemaError(RuntimeError):
    """Raised when a Codex stream schema failure cannot be safely backfilled."""

    def __init__(
        self,
        message: str,
        *,
        output_items: int,
        text_deltas: int,
        text_chars: int,
        has_function_calls: bool,
    ) -> None:
        super().__init__(
            f"{message} "
            f"(output_items={output_items}, text_deltas={text_deltas}, "
            f"text_chars={text_chars}, has_function_calls={has_function_calls})"
        )
        self.output_items = output_items
        self.text_deltas = text_deltas
        self.text_chars = text_chars
        self.has_function_calls = has_function_calls


def is_none_output_iterable_typeerror(exc: BaseException) -> bool:
    """Return True only for the OpenAI SDK response.output=None parse failure."""

    return isinstance(exc, TypeError) and str(exc) == _NONE_OUTPUT_ITERABLE_ERROR


def synthesize_codex_response_from_stream(
    *,
    output_items: Sequence[Any],
    text_deltas: Sequence[str],
    has_function_calls: bool,
    model: Any = None,
    usage: Any = None,
    status: str = "completed",
) -> Any | None:
    """Build a terminal Responses-like object from already-received stream data.

    Completed output items are safest and win over text deltas. Text-only
    synthesis is allowed only when no function/tool call was observed; otherwise
    collapsing the stream into assistant text can hide an incomplete tool call.
    """

    if output_items:
        return SimpleNamespace(
            output=list(output_items),
            usage=usage,
            status=status,
            model=model,
        )
    if text_deltas and not has_function_calls:
        assembled = "".join(text_deltas)
        if assembled:
            return SimpleNamespace(
                output=[
                    SimpleNamespace(
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[
                            SimpleNamespace(type="output_text", text=assembled),
                        ],
                    )
                ],
                output_text=assembled,
                usage=usage,
                status=status,
                model=model,
            )
    return None


def codex_stream_event_has_function_call(event: Any) -> bool:
    """Return True when a stream event or its payload marks function/tool work."""

    event_type = getattr(event, "type", None)
    if event_type is None and isinstance(event, dict):
        event_type = event.get("type")
    if "function_call" in str(event_type):
        return True

    item = getattr(event, "item", None)
    if item is None and isinstance(event, dict):
        item = event.get("item")
    item_type = getattr(item, "type", None)
    if item_type is None and isinstance(item, dict):
        item_type = item.get("type")
    return item_type in {"function_call", "custom_tool_call"}


def get_nonempty_response_output_text(response: Any) -> str:
    """Return terminal output_text when present, without exposing content in logs."""

    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    return ""


def synthesize_codex_response_from_text(
    text: str,
    *,
    model: Any = None,
    usage: Any = None,
    status: str = "completed",
) -> Any:
    """Build a terminal Responses-like object from safe assistant text."""

    return SimpleNamespace(
        output=[
            SimpleNamespace(
                type="message",
                role="assistant",
                status="completed",
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
        output_text=text,
        usage=usage,
        status=status,
        model=model,
    )


def raise_no_safe_codex_stream_backfill(
    *,
    output_items: Sequence[Any],
    text_deltas: Sequence[str],
    has_function_calls: bool,
) -> None:
    """Raise a structured schema error with counts only, never stream content."""

    raise CodexStreamSchemaError(
        "Codex Responses stream failed SDK schema parsing, but no safe output backfill was possible",
        output_items=len(output_items),
        text_deltas=len(text_deltas),
        text_chars=sum(len(part) for part in text_deltas),
        has_function_calls=has_function_calls,
    )


def output_needs_stream_backfill(output: Any) -> bool:
    """Return True for malformed or empty terminal output shapes."""

    return output is None or (isinstance(output, list) and not output)
