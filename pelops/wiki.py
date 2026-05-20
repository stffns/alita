"""Wiki memory layer for Alita.

A directory of markdown files with YAML frontmatter and `[[backlinks]]`.
Mutable pages, slug-as-filename. Designed to be Obsidian-compatible.

See `docs/wiki-plan.md` and the seed page `wiki.md` inside the vault for
the broader architecture. This module is the data layer; tool wrappers
live in `pelops/tools.py` (`wiki_read`, `wiki_write`, ...).

Invariants:
  * Slugs are lowercase kebab-case (regex: ^[a-z0-9][a-z0-9-]*$).
  * Every page has YAML frontmatter (slug, created, updated, sources).
  * Anti-orphan rule: creating a NEW page requires the body to link to
    at least one EXISTING page via `[[slug]]`. Updates are exempt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from pelops.config import Settings

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
BACKLINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9-]*)\]\]")
# Typed-link variant inspired by gbrain. `[[slug]] (relation)` parses as
# (slug, relation). Plain `[[slug]]` is still matched by BACKLINK_RE and
# does NOT match here. Relation is kebab/snake-case identifier.
TYPED_LINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9-]*)\]\]\s*\(([a-z][a-z0-9_-]*)\)")


class WikiError(ValueError):
    """Raised when a wiki operation violates an invariant."""


@dataclass(frozen=True)
class Page:
    slug: str
    body: str
    created: date
    updated: date
    sources: list[str] = field(default_factory=list)

    def render(self) -> str:
        fm = {
            "slug": self.slug,
            "created": self.created.isoformat(),
            "updated": self.updated.isoformat(),
            "sources": list(self.sources),
        }
        return "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + self.body.rstrip() + "\n"


def _root() -> Path:
    return Settings.load().wiki_dir


def _validate_slug(slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise WikiError(f"invalid slug: {slug!r} (must match {SLUG_RE.pattern})")


def _path(slug: str) -> Path:
    _validate_slug(slug)
    return _root() / f"{slug}.md"


def _parse_date(v: object) -> date:
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError:
            pass
    return date.today()


def _parse(text: str, slug: str) -> Page:
    if not text.startswith("---\n"):
        raise WikiError(f"page {slug!r} missing YAML frontmatter")
    end = text.find("\n---", 4)
    if end < 0:
        raise WikiError(f"page {slug!r} has unterminated frontmatter")
    fm = yaml.safe_load(text[4:end]) or {}
    body = text[end + 4 :].lstrip("\n")
    return Page(
        slug=fm.get("slug") or slug,
        body=body,
        created=_parse_date(fm.get("created")),
        updated=_parse_date(fm.get("updated")),
        sources=list(fm.get("sources") or []),
    )


def list_pages() -> list[str]:
    """Return all wiki slugs in alphabetical order. Empty if the vault
    directory does not exist yet."""
    root = _root()
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.md") if SLUG_RE.match(p.stem))


def read(slug: str) -> Page | None:
    """Return the page or None if it does not exist.

    Raises `WikiError` only for malformed pages, not for missing ones --
    a missing page is a normal "not found" signal for callers.
    """
    p = _path(slug)
    if not p.exists():
        return None
    return _parse(p.read_text(encoding="utf-8"), slug)


def write(slug: str, body: str, sources: list[str] | None = None) -> Page:
    """Create or update a page. Returns the persisted Page.

    Anti-orphan rule: when CREATING a new page, the body must reference
    at least one existing page via `[[other-slug]]`. Updates to existing
    pages skip this check (they may keep or change their backlinks freely).

    The first existing-page directory is auto-created if missing. The
    write is atomic via tmp file + rename.
    """
    p = _path(slug)
    today = date.today()
    is_new = not p.exists()

    if is_new:
        existing = set(list_pages())
        # Bootstrap case: the FIRST page in an empty vault cannot link
        # anywhere because nothing exists yet. A 1-node graph is
        # trivially connected, so the anti-orphan rule starts applying
        # from the second page onward.
        if existing:
            linked = set(BACKLINK_RE.findall(body))
            if not (linked & existing):
                raise WikiError(
                    f"refusing to create orphan page {slug!r}: the body "
                    f"must link to at least one existing page via "
                    f"[[other-slug]]. Found backlinks: "
                    f"{sorted(linked) or 'none'}. Existing pages: "
                    f"{sorted(existing) or 'none'}. Adopt the new page "
                    f"into the graph by referencing a parent."
                )
        created = today
    else:
        existing_page = _parse(p.read_text(encoding="utf-8"), slug)
        created = existing_page.created

    page = Page(
        slug=slug,
        body=body.strip() + "\n",
        created=created,
        updated=today,
        sources=list(sources or []),
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(page.render(), encoding="utf-8")
    tmp.replace(p)
    return page


def delete(slug: str, force: bool = False) -> dict:
    """Delete a wiki page. Returns a status dict.

    By default refuses to delete if other pages reference the slug via
    `[[slug]]` -- that would leave dangling backlinks. Pass `force=True`
    to delete anyway; the caller gets back the list of pages now holding
    a dangling reference so they can clean up explicitly.

    The vault is a git repo, so deletes are recoverable via `git restore`
    or `git revert` on the resulting commit. No soft-delete here.

    Returns:
      {"status": "deleted", "dangling_in": [...]} on success
      {"status": "not_found"} if the slug has no page
      {"status": "has_backlinks", "backlinks": [...]} if blocked
    """
    _validate_slug(slug)
    p = _path(slug)
    if not p.exists():
        return {"status": "not_found"}
    inbound = backlinks(slug)
    if inbound and not force:
        return {"status": "has_backlinks", "backlinks": inbound}
    p.unlink()
    return {"status": "deleted", "dangling_in": inbound if force else []}


def search(query: str, limit: int = 10) -> list[tuple[str, str]]:
    """Substring search across page bodies (case-insensitive).

    Returns `[(slug, snippet), ...]` ordered by slug. Snippet is a
    ~80-char window centered on the first match in each page.
    """
    q = query.lower().strip()
    if not q:
        return []
    out: list[tuple[str, str]] = []
    for slug in list_pages():
        page = read(slug)
        if page is None:
            continue
        idx = page.body.lower().find(q)
        if idx < 0:
            continue
        start = max(0, idx - 30)
        end = min(len(page.body), idx + len(query) + 50)
        snippet = page.body[start:end].replace("\n", " ").strip()
        out.append((slug, snippet))
        if len(out) >= limit:
            break
    return out


def backlinks(slug: str) -> list[str]:
    """Return slugs of pages that link to the given one. Self-links are
    excluded."""
    out: list[str] = []
    for other in list_pages():
        if other == slug:
            continue
        page = read(other)
        if page is None:
            continue
        if slug in BACKLINK_RE.findall(page.body):
            out.append(other)
    return out


def typed_links(slug: str) -> list[tuple[str, str]]:
    """Return (from_slug, relation) tuples for pages that link to `slug`
    via the typed-link syntax `[[slug]] (relation)`. Self-links excluded.

    Plain `[[slug]]` links do NOT appear here -- use `backlinks()` for
    untyped references. The two together give the full entity graph.
    """
    out: list[tuple[str, str]] = []
    for other in list_pages():
        if other == slug:
            continue
        page = read(other)
        if page is None:
            continue
        for target, relation in TYPED_LINK_RE.findall(page.body):
            if target == slug:
                out.append((other, relation))
    return out


def entity_graph() -> dict[str, dict[str, list[str]]]:
    """Build the full entity graph in one pass.

    Returns `{slug: {'inbound_plain': [...], 'inbound_typed': [(from, rel), ...]}}`
    so a caller can introspect the whole vault structure without N
    walks. Useful for `wiki_graph_stats` and offline audits.

    Single scan of all pages. O(pages). No LLM calls (gbrain-style).
    """
    pages = list_pages()
    graph: dict[str, dict[str, list]] = {
        s: {"inbound_plain": [], "inbound_typed": []} for s in pages
    }
    for from_slug in pages:
        page = read(from_slug)
        if page is None:
            continue
        body = page.body
        # Untyped backlinks
        for target in BACKLINK_RE.findall(body):
            if target == from_slug or target not in graph:
                continue
            graph[target]["inbound_plain"].append(from_slug)
        # Typed backlinks (also matched by BACKLINK_RE; dedupe in caller)
        for target, relation in TYPED_LINK_RE.findall(body):
            if target == from_slug or target not in graph:
                continue
            graph[target]["inbound_typed"].append((from_slug, relation))
    return graph
