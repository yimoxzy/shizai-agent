"""Optional site references selected from live page evidence.

References may provide proven recovery behavior, but generic capture and
authoring code must continue to work when no reference matches.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

JD_PRODUCT_LINK_REFERENCE = "jd_product_card_link_v1"


def match_product_link_reference(
    page_url: str,
    *,
    page_title: str = "",
    card_context: Any = None,
) -> str | None:
    """Return a product-link reference only for an observed supported host."""
    host = (urlparse(str(page_url or "").strip()).hostname or "").casefold()
    if host == "jd.com" or host.endswith(".jd.com"):
        return JD_PRODUCT_LINK_REFERENCE
    context = card_context if isinstance(card_context, dict) else {}
    marker = str(context.get("marker") or "").casefold()
    if "京东" in str(page_title or "") and marker == "plugingoodscardwrapper":
        return JD_PRODUCT_LINK_REFERENCE
    return None


def selector_from_card_context(context: Any) -> str | None:
    """Build a conservative CSS class selector from an observed card context."""
    if not isinstance(context, dict):
        return None
    tokens = [
        token
        for token in str(context.get("class") or "").split()
        if re.fullmatch(r"-?[_A-Za-z][_A-Za-z0-9-]*", token)
    ]
    return "." + ".".join(tokens) if tokens else None
