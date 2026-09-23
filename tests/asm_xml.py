"""Parse the committed nmap XML fixture into the nested mapping the coverage harness flattens.

The standard-library parser is the one XML dependency the project needs, and it only ever reads
fixture bytes committed to this repository. That is the reason for the one rule-specific suppression
below, of bandit's untrusted-XML parse rule (S314), approved for this helper and nowhere else. The
matching import rule (S405) is preview-only in ruff 0.16 and needs none.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from typing import Any


def _element(node: ElementTree.Element) -> dict[str, Any]:
    """Attributes become `@name` keys, child elements lists under their tag, text `#text`."""
    result: dict[str, Any] = {f"@{key}": value for key, value in node.attrib.items()}
    for child in node:
        result.setdefault(child.tag, []).append(_element(child))
    text = (node.text or "").strip()
    if text:
        result["#text"] = text
    return result


def parse_xml(data: str) -> dict[str, Any]:
    """Return `{root_tag: element}` for one XML document."""
    root = ElementTree.fromstring(data)  # noqa: S314 - parses only committed, repository-owned fixture bytes
    return {root.tag: _element(root)}
