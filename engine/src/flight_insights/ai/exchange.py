"""What one exchange with Claude is made of (spec: AI layer, "Request flow"): the system
prompt, the tools, the reply's fixed schema, and the instruction for the reader's
language. The shell builds the request from these and from checked packets only.

Everything here is static: the same for every flight and laptop, so it caches, and it
holds nothing about any aircraft. The shell still runs its location detectors over it
before the first request, like any other text it sends.

The reply schema is kept twice: with limits (lengths, list sizes, reference forms) for
the local check, and without them for the API, whose structured outputs do not take
string or array limits; there each length and list limit is said in the field's
description, text with some room to spare. The local check is the one that decides.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from flight_insights.ai.schema import PARAMETER, REF, strict
from flight_insights.ai.tools import DEFINITIONS
from flight_insights.knowledge import Pack

EXCHANGE_ID = "fi.ai.exchange/1"
MAX_TOOL_ROUNDS = 12


def _text(limit: int) -> dict[str, Any]:
    return {"type": "string", "maxLength": limit}


def _refs() -> dict[str, Any]:
    return {"type": "array", "maxItems": 16, "items": REF}


def _list(items: dict[str, Any], n: int) -> dict[str, Any]:
    return {"type": "array", "maxItems": n, "items": items}


REPLY: dict[str, Any] = strict(
    {
        "summary": _text(2000),
        "root_causes": _list(
            strict({"title": _text(120), "explanation": _text(1200), "refs": _refs()},
                   "title", "explanation", "refs"),
            8,
        ),
        "recommendations": _list(
            strict(
                {"param": {"type": "string", "pattern": PARAMETER}, "value": {"type": "number"},
                 "confidence": {"enum": ["low", "medium"]}, "reason": _text(600),
                 "refs": _refs()},
                "param", "value", "confidence", "reason", "refs",
            ),
            16,
        ),
        "disagreements": _list(
            strict({"ref": {"type": "string", "pattern": r"^F[0-9]{1,4}$"},
                    "view": _text(800)}, "ref", "view"),
            8,
        ),
        "questions": _list(
            strict({"question": _text(300), "why": _text(300)}, "question", "why"), 6
        ),
        "tests_to_fly": _list(
            strict({"test": _text(300), "look_for": _text(300)}, "test", "look_for"), 6
        ),
    },
    "summary", "root_causes", "recommendations", "disagreements", "questions", "tests_to_fly",
)  # fmt: skip

# A chat turn's reply: the answer, and the references it rests on.
CHAT_REPLY: dict[str, Any] = strict({"answer": _text(4000), "refs": _refs()}, "answer", "refs")
CHAT = (
    "The packet's \"question\" is the engineer's follow-up question about this flight, and "
    '"conversation" holds the earlier turns. Answer the question in "answer", briefly and '
    'concretely, citing the packet\'s references in "refs". Propose no parameter changes here: '
    "say what to try, and the plan will carry changes from the analysis."
)

LIMITS = ("maxLength", "maxItems", "maxProperties", "pattern")
# The share of a text limit the API schema asks for. Claude cannot count characters
# exactly, and one text over its limit makes the local check refuse the whole reply.
ASKED = 0.8
LANGUAGE = {
    "en": "Write every text in the reply in English.",
    "uk": "Пиши всі тексти відповіді українською мовою.",
}


def _asked(node: Mapping[str, Any]) -> list[str]:
    """A node's length and list limits, said in words for its description."""
    said: list[str] = []
    if "maxLength" in node:
        said.append(f"At most {int(node['maxLength'] * ASKED)} characters.")
    if "maxItems" in node:
        said.append(f"At most {node['maxItems']} items.")
    return said


def api_schema(schema: Mapping[str, Any]) -> Any:
    """The schema as the API's structured outputs take it: the same shape, without the
    limits they refuse. A text or list limit is said in the field's description instead,
    so Claude keeps to it; text is asked for within ASKED of its limit."""

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            obj = cast(dict[str, Any], node)
            out: dict[str, Any] = {k: strip(v) for k, v in obj.items() if k not in LIMITS}
            if said := _asked(obj):
                out["description"] = " ".join([*filter(None, [out.get("description")]), *said])
            if "enum" in out:
                values = cast(list[Any], out["enum"])
                kinds = sorted(
                    {
                        "string" if isinstance(v, str) else "null" if v is None else ""
                        for v in values
                    }
                )
                if all(kinds):
                    out["type"] = kinds[0] if len(kinds) == 1 else kinds
            return out
        if isinstance(node, list):
            return [strip(v) for v in cast(list[Any], node)]
        return node

    return strip(schema)


def system_prompt(pack: Pack) -> str:
    return (pack.root / "ai" / "system.md").read_text(encoding="utf-8").strip()


def spec(pack: Pack) -> dict[str, Any]:
    """Everything static in a request, for the shell."""
    return {
        "exchange": EXCHANGE_ID,
        "system": system_prompt(pack),
        "tools": DEFINITIONS,
        "reply_schema": api_schema(REPLY),
        "chat_reply_schema": api_schema(CHAT_REPLY),
        "chat": CHAT,
        "language": LANGUAGE,
        "max_tool_rounds": MAX_TOOL_ROUNDS,
    }
