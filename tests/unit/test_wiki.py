"""Tests for the wiki data layer.

Hermetic: each test points `PELOPS_WIKI_DIR` at a `tmp_path` directory
so nothing here touches Jay's real vault at ~/Documents/pelops-wiki/Alita/.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest


@pytest.fixture
def wiki_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point PELOPS_WIKI_DIR at a fresh temp directory per test.

    Also seeds GROQ_API_KEY so Settings load does not fail and clears the
    Settings lru_cache so the new env vars are picked up.
    """
    monkeypatch.setenv("GROQ_API_KEY", "test-dummy-key")
    monkeypatch.setenv("PELOPS_WIKI_DIR", str(tmp_path))
    from pelops.config import get_settings

    get_settings.cache_clear()
    yield tmp_path


def test_list_pages_empty(wiki_dir: Path) -> None:
    from pelops import wiki

    assert wiki.list_pages() == []


def test_write_then_read_roundtrip(wiki_dir: Path) -> None:
    from pelops import wiki

    # First page can be created without backlinks: there are no pages to
    # be orphan from. The check only triggers when at least one page exists.
    wiki.write("alita", "Bootstrap page. The agent.")
    page = wiki.read("alita")
    assert page is not None
    assert page.slug == "alita"
    assert "Bootstrap page" in page.body
    assert page.created == date.today()
    assert page.updated == date.today()


def test_anti_orphan_rejects_new_page_with_no_backlinks(wiki_dir: Path) -> None:
    from pelops import wiki

    wiki.write("alita", "First page.")
    with pytest.raises(wiki.WikiError, match="orphan"):
        wiki.write("vstash", "Brand new page that does not link to anyone.")


def test_anti_orphan_passes_when_new_page_links_to_existing(wiki_dir: Path) -> None:
    from pelops import wiki

    wiki.write("alita", "First page.")
    wiki.write("vstash", "vstash is used by [[alita]] for storage.")
    assert sorted(wiki.list_pages()) == ["alita", "vstash"]


def test_update_existing_page_skips_anti_orphan_check(wiki_dir: Path) -> None:
    from pelops import wiki

    wiki.write("alita", "First page.")
    # Updating "alita" does not require it to link anywhere -- the
    # anti-orphan rule only fires on CREATE.
    wiki.write("alita", "Revised body, still no backlinks.")
    page = wiki.read("alita")
    assert page is not None
    assert "Revised body" in page.body


def test_invalid_slug_rejected(wiki_dir: Path) -> None:
    from pelops import wiki

    for bad in ["UPPER", "has space", "with_underscore", "-leading-dash", ""]:
        with pytest.raises(wiki.WikiError, match="invalid slug"):
            wiki.write(bad, "body with [[parent]]")


def test_read_missing_returns_none(wiki_dir: Path) -> None:
    from pelops import wiki

    assert wiki.read("nonexistent") is None


def test_search_finds_substring(wiki_dir: Path) -> None:
    from pelops import wiki

    wiki.write("alita", "The agent loves to chew on heartbeat ideas.")
    wiki.write("vstash", "Storage layer. Links to [[alita]].")
    matches = wiki.search("heartbeat")
    assert len(matches) == 1
    assert matches[0][0] == "alita"
    assert "heartbeat" in matches[0][1].lower()


def test_search_case_insensitive(wiki_dir: Path) -> None:
    from pelops import wiki

    wiki.write("alita", "About OpenRouter and DeepSeek.")
    assert any(slug == "alita" for slug, _ in wiki.search("openrouter"))
    assert any(slug == "alita" for slug, _ in wiki.search("OPENROUTER"))


def test_search_empty_query(wiki_dir: Path) -> None:
    from pelops import wiki

    wiki.write("alita", "Something.")
    assert wiki.search("") == []
    assert wiki.search("   ") == []


def test_backlinks_finds_inbound_references(wiki_dir: Path) -> None:
    from pelops import wiki

    wiki.write("alita", "Hub page.")
    wiki.write("vstash", "vstash powers [[alita]].")
    wiki.write("heartbeat", "heartbeat lives inside [[alita]], not [[vstash]].")
    assert sorted(wiki.backlinks("alita")) == ["heartbeat", "vstash"]
    assert wiki.backlinks("vstash") == ["heartbeat"]
    assert wiki.backlinks("heartbeat") == []


def test_backlinks_excludes_self_references(wiki_dir: Path) -> None:
    from pelops import wiki

    wiki.write("alita", "First page.")
    wiki.write("vstash", "I am [[vstash]] and I refer to [[alita]].")
    assert wiki.backlinks("vstash") == []


def test_typed_links_extracts_relations(wiki_dir: Path) -> None:
    from pelops import wiki

    wiki.write("alita", "First page.")
    wiki.write("vstash", "vstash is the storage used by [[alita]] (uses).")
    wiki.write("heartbeat", "heartbeat lives in [[alita]] (part_of) and uses [[vstash]] (reads).")
    # Plain backlinks include both vstash and heartbeat
    assert sorted(wiki.backlinks("alita")) == ["heartbeat", "vstash"]
    # Typed links return (from, relation) pairs
    typed = wiki.typed_links("alita")
    assert sorted(typed) == [("heartbeat", "part_of"), ("vstash", "uses")]
    assert wiki.typed_links("vstash") == [("heartbeat", "reads")]


def test_typed_links_ignores_plain_links(wiki_dir: Path) -> None:
    """A `[[slug]]` without a `(relation)` suffix is NOT a typed link."""
    from pelops import wiki

    wiki.write("alita", "Origin page.")
    wiki.write("vstash", "Just a plain [[alita]] reference here.")
    assert wiki.backlinks("alita") == ["vstash"]
    assert wiki.typed_links("alita") == []


def test_entity_graph_aggregates_all_inbound(wiki_dir: Path) -> None:
    from pelops import wiki

    wiki.write("alita", "Hub.")
    wiki.write("vstash", "Storage for [[alita]] (used_by).")
    wiki.write("heartbeat", "Cron in [[alita]] and notes from [[vstash]].")
    wiki.write("orphan-page", "I reference nothing relevant. Plain text.")
    graph = wiki.entity_graph()
    # alita appears in graph; has 2 plain inbound (vstash, heartbeat),
    # 1 typed inbound (vstash, used_by)
    assert sorted(graph["alita"]["inbound_plain"]) == ["heartbeat", "vstash"]
    assert graph["alita"]["inbound_typed"] == [("vstash", "used_by")]
    # vstash has 1 plain inbound from heartbeat
    assert graph["vstash"]["inbound_plain"] == ["heartbeat"]
    # orphan-page has no inbound from anywhere
    assert graph["orphan-page"]["inbound_plain"] == []
    assert graph["orphan-page"]["inbound_typed"] == []


def test_entity_graph_single_scan_handles_unknown_targets(wiki_dir: Path) -> None:
    """Links to nonexistent slugs (`[[ghost]]`) do NOT crash the graph."""
    from pelops import wiki

    wiki.write("alita", "Hub.")
    wiki.write("vstash", "Refers to [[ghost]] which doesn't exist.")
    graph = wiki.entity_graph()
    assert "ghost" not in graph
    assert graph["alita"]["inbound_plain"] == []


def test_frontmatter_preserves_created_date_on_update(wiki_dir: Path) -> None:
    """Updates must NOT reset the `created` field; only `updated` changes."""
    from pelops import wiki

    page1 = wiki.write("alita", "v1")
    page2 = wiki.write("alita", "v2 of [[alita]]")
    assert page2.created == page1.created
    # `updated` is the same date when both writes happen the same day; the
    # invariant we care about is that we did not zero out `created`.
    assert page2.created == date.today()


def test_rendered_file_has_valid_frontmatter(wiki_dir: Path) -> None:
    """The on-disk file must start with --- and contain parseable YAML."""
    import yaml

    from pelops import wiki

    wiki.write("alita", "Hello.")
    text = (wiki_dir / "alita.md").read_text()
    assert text.startswith("---\n")
    end = text.find("\n---", 4)
    assert end > 0
    fm = yaml.safe_load(text[4:end])
    assert fm["slug"] == "alita"
    assert "created" in fm
    assert "updated" in fm


def test_write_is_atomic_no_partial_files(wiki_dir: Path) -> None:
    """No `.tmp` file should remain after a successful write."""
    from pelops import wiki

    wiki.write("alita", "First.")
    leftover = list(wiki_dir.glob("*.tmp")) + list(wiki_dir.glob("*.md.tmp"))
    assert leftover == []


def test_list_pages_ignores_non_slug_files(wiki_dir: Path) -> None:
    """Files like README.md or anything not matching the slug regex must
    be excluded from the page listing."""
    from pelops import wiki

    wiki.write("alita", "real page")
    (wiki_dir / "README.md").write_text("# not a wiki page\n")
    (wiki_dir / "UPPER.md").write_text("# also not\n")
    assert wiki.list_pages() == ["alita"]
