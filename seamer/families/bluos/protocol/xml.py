"""BluOS XML framing (bl2:B27).

Real players send ``<?xml version="1.0" encoding="UTF-8"?>`` plus a newline, then the
whole document on one line. Empty elements are written as pairs (``<sleep></sleep>``),
attribute values and text are XML-escaped.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

PROLOG = '<?xml version="1.0" encoding="UTF-8"?>'

Attrs = Mapping[str, Any] | Iterable[tuple[str, Any]] | None


def esc(value: Any) -> str:
    s = str(value)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


class Xml:
    """Element builder that honours the framing knobs."""

    def __init__(self, prolog: bool = True, selfclose: bool = False) -> None:
        self.prolog = prolog
        self.selfclose = selfclose

    def el(self, tag: str, text: Any = None, attrs: Attrs = None, inner: str = "") -> str:
        """``<tag a="..">text-or-inner</tag>``. ``text`` is escaped, ``inner`` is raw XML."""
        items = attrs.items() if isinstance(attrs, Mapping) else (attrs or ())
        a = "".join(f' {k}="{esc(v)}"' for k, v in items if v is not None)
        body = inner if text is None else esc(text) + inner
        if not body and self.selfclose:
            return f"<{tag}{a}/>"
        return f"<{tag}{a}>{body}</{tag}>"

    def fields(self, values: Mapping[str, Any]) -> str:
        """Child elements in ASCII order of tag (the order real /Status uses).

        A value wrapped in :class:`Raw` is inserted as XML, not escaped.
        """
        out = []
        for tag in sorted(values):
            v = values[tag]
            if isinstance(v, Raw):
                out.append(self.el(tag, inner=v.xml))
            else:
                out.append(self.el(tag, "" if v is None else v))
        return "".join(out)

    def doc(self, body: str) -> str:
        return f"{PROLOG}\n{body}" if self.prolog else body


class Raw:
    """Pre-built XML for :meth:`Xml.fields`."""

    __slots__ = ("xml",)

    def __init__(self, xml: str) -> None:
        self.xml = xml
