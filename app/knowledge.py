"""Local knowledge: docs you drop into the knowledge/ folder (.md or .txt).

Simple keyword scoring keeps this dependency-free. Swap in embeddings later
if your docs grow beyond a few hundred files.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import settings

log = logging.getLogger("knowledge")

_docs: list[tuple[str, str]] = []  # (name, text)


def load_docs() -> int:
    _docs.clear()
    kdir = Path(settings.knowledge_dir)
    if not kdir.exists():
        return 0
    for p in sorted(kdir.rglob("*")):
        if p.suffix.lower() in (".md", ".txt") and p.is_file():
            try:
                _docs.append((str(p.relative_to(kdir)), p.read_text(errors="ignore")))
            except OSError:
                log.warning("Could not read %s", p)
    log.info("Loaded %d knowledge docs", len(_docs))
    return len(_docs)


def search_docs(query: str, top_k: int = 3, max_chars: int = 4000) -> str:
    """Return the top-k docs by keyword overlap, trimmed."""
    if not _docs:
        return "No local docs loaded."
    terms = {w for w in re.findall(r"[a-z0-9]{3,}", query.lower())}
    scored = []
    for name, text in _docs:
        low = text.lower()
        score = sum(low.count(t) for t in terms)
        if score:
            scored.append((score, name, text))
    scored.sort(reverse=True)
    if not scored:
        return "No matching docs."
    out = []
    for _, name, text in scored[:top_k]:
        out.append(f"### {name}\n{text[:max_chars]}")
    return "\n\n".join(out)
