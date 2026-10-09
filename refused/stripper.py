"""STREAM_STRIP: remove quality-signalling fields from MediaStreams entries."""
from __future__ import annotations

from typing import Any


def strip_json(data: Any, field_names_lower: frozenset[str]) -> tuple[int, int]:
    """Walk parsed JSON in place. Returns (streams_touched, fields_removed)."""
    streams = 0
    removed = 0
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(c for c in node if isinstance(c, (dict, list)))
            continue
        if not isinstance(node, dict):
            continue
        for key, child in node.items():
            if key.lower() == "mediastreams" and isinstance(child, list):
                for stream in child:
                    if not isinstance(stream, dict):
                        continue
                    streams += 1
                    for k in [k for k in stream if k.lower() in field_names_lower]:
                        del stream[k]
                        removed += 1
            if isinstance(child, (dict, list)):
                stack.append(child)
    return streams, removed
