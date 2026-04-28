"""Canonical hashing helpers.

Every cache-keying decision in the pipeline runs through here so that
serialization order, whitespace, and float formatting can never silently
change a hash and invalidate downstream artifacts.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def canonical_json(obj: Any) -> str:
    """Deterministic JSON dump.

    - Keys sorted recursively.
    - No whitespace between separators.
    - Pydantic models go through model_dump(mode='json') for stable shapes.
    """
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    elif isinstance(obj, list):
        obj = [
            o.model_dump(mode="json") if isinstance(o, BaseModel) else o
            for o in obj
        ]
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(*parts: Any) -> str:
    """Hash any combination of strings/models/dicts/lists deterministically."""
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, (str, bytes)):
            h.update(p.encode("utf-8") if isinstance(p, str) else p)
        else:
            h.update(canonical_json(p).encode("utf-8"))
        h.update(b"\x1f")  # unit separator between parts
    return h.hexdigest()


def short_hash(*parts: Any, length: int = 10) -> str:
    return sha256_hex(*parts)[:length]
