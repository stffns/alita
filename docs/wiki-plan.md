# Wiki Memory -- Implementation Plan

Status: draft, pending phase-1 kickoff after heartbeat validation.
Date: 2026-05-16
Owner: Jay (single author project)

## Context

Today Alita's memory is `vstash` only: append-only chunks indexed for
retrieval. Good as "what did source X say". Bad as "what do I know about Y".

Andrej Karpathy posted the "LLM Wiki Pattern" on 2026-04-03: the LLM
incrementally compiles fresh sources into a structured markdown wiki
(mutable pages with backlinks), then health-checks the result for
contradictions and orphans. Treat knowledge like code: sources are inputs,
wiki is the compiled artifact.

Alita already has a primitive form of this in `layer='consolidated'`, but
those are immutable snapshots. Promoting to a real wiki means:

  - Mutable pages with stable slugs (one canonical "deepagents" page, not
    five chunks).
  - `[[backlinks]]` between pages so the graph emerges.
  - Periodic health checks to keep the wiki honest.
  - A read path that prefers compiled knowledge but falls back to vstash
    for citations.

This is a bigger refactor than the heartbeat job we just shipped. Worth
doing, but worth scoping properly.

## Design decisions (confirm before Phase 1)

1. **Storage.** Markdown files in `~/Documents/pelops-wiki/Alita/`, configurable via
   `PELOPS_WIKI_DIR`. NOT inside the stilt repo. NOT in iCloud (yet).
   File-per-page, slug-as-filename: `deepagents.md`, `alita-arquitectura.md`.

2. **Page format.** YAML frontmatter (slug, created, updated, source_ids)
   + markdown body with `[[backlinks]]`. Obsidian-compatible.

3. **Wiki coexists with vstash.** vstash stays the immutable source-of-truth
   (the "disk with journaling"). Wiki is the compiled view. `consolidated`
   layer becomes a transition artifact -- new consolidations go to wiki,
   old `consolidated` rows can be migrated later or just left.

4. **Compiler step lives in the heartbeat.** A new action `wiki_update`
   replaces (or extends) the `mini_consolidate` action shipped today.

5. **Health checks are a separate weekly cron.** Not the heartbeat.
   Inspired by Karpathy's pattern.

6. **Cold start by hand.** Jay writes 3-5 seed pages himself before
   enabling auto-write. Avoids the agent inventing pages on topics it
   doesn't understand from cold.

7. **Anti-orphan rule.** Creating a new page requires updating at least
   one existing page to link to it. Enforced in the wiki_write tool.

## Phases

### Phase 0 -- Bootstrap (manual)

Goal: vault exists, Obsidian works, 3-5 seed pages live.

Tasks:
- [x] `mkdir ~/Documents/pelops-wiki && cd ~/Documents/pelops-wiki && git init` (done 2026-05-16)
- [ ] `brew install --cask obsidian`; open `~/Documents/pelops-wiki/Alita/` as vault
- [ ] Add `PELOPS_WIKI_DIR=/Users/jaysonsteffens/Documents/pelops-wiki/Alita/` to `.env`
- [ ] Write seed pages by hand:
  - `alita.md` -- what Alita is, current architecture pointers
  - `vstash.md` -- memory layers, how they work
  - `jay.md` -- Jay's role, preferences, current projects
  - `heartbeat.md` -- what the heartbeat does, decision rules
  - `wiki.md` -- this very system, self-referential

Acceptance: Jay can open the vault in Obsidian and see the graph view
with the 5 seed pages cross-linked.

### Phase 1 -- Wiki tools

Goal: Alita can read, write, search, and list wiki pages.

Tasks:
- [ ] `pelops/wiki.py` module: `read(slug)`, `write(slug, content, sources)`,
  `search(query)`, `list_pages()`, `resolve_backlinks(slug)`.
- [ ] LangChain `@tool`-wrapped versions exposed in `pelops/tools.py`:
  `wiki_read`, `wiki_write`, `wiki_search`, `wiki_list`.
- [ ] Settings field: `wiki_dir: Path = Field(default=Path.home()/"Documents"/"pelops-wiki"/"Alita")`.
- [ ] Persona update: explain wiki vs vstash distinction, when to use which.
- [ ] Unit tests for wiki.py (hermetic via tmp_path fixture).

Acceptance: From an interactive chat, "Alita, busca en la wiki lo que
sabes de X" returns the right page. "Alita, agrega a la wiki que ..."
results in a sensible page edit visible in Obsidian.

### Phase 2 -- Heartbeat integration

Goal: heartbeat compiles fresh vstash entries into wiki updates.

Tasks:
- [ ] Replace `mini_consolidate` action in the heartbeat with `wiki_update`.
- [ ] New heartbeat sub-prompt: given recent episodic/research/rss notes,
  decide which wiki pages to touch and propose edits.
- [ ] Enforce anti-orphan rule in `wiki_write`.
- [ ] Log each wiki update as `agent-action` for auditability.

Acceptance: After 48h running, the wiki has 3-5 new auto-generated pages
or page updates, and the graph view shows real connections (not isolated
nodes). Each update is traceable via vstash agent-action.

### Phase 3 -- Health checks

Goal: weekly self-audit catches drift before it rots the wiki.

Tasks:
- [ ] `job_wiki_health` cron, runs Sundays 04:00.
- [ ] Agent scans all pages, flags: contradictions between pages, orphan
  pages (no backlinks), stale claims (older than N days unupdated),
  broken `[[backlinks]]` to nonexistent pages.
- [ ] Output: a `wiki-health-YYYY-MM-DD.md` report. Push summary to Telegram.

Acceptance: First weekly run produces a report. Manual review confirms
the flags are real (not noise).

### Phase 4 -- Read path integration

Goal: when Jay asks "que sabes de X", Alita prefers wiki, cites vstash.

Tasks:
- [ ] Persona update: query rules. Wiki first for "que sabes / que entiendes
  de", vstash for "que dijo X / cuando vimos Y".
- [ ] Optionally: a `pelops_query(topic)` tool that does both in parallel
  and synthesizes -- wiki gives the "what I know", vstash gives citations.
- [ ] Measure: before / after token cost per chat turn.

Acceptance: Two weeks of qualitative use confirms the assistant feels
"smarter about my project" without inventing facts.

## Out of scope (deliberate)

- Backlink graph as a separate store. `[[slug]]` in markdown is the graph.
- Embedding-based wiki search. Filesystem grep + slug lookup is enough
  for a 100-page vault. Revisit at 1000+ pages.
- Multi-user / sharing. Alita is single-user; the wiki is Jay's only.
- Migrating existing `consolidated` rows. Leave them. New work goes to
  wiki; old consolidations decay naturally.

## Open questions

- Do `[[backlinks]]` need to be resolved at write-time (reject writes
  with unresolvable links) or at read-time (links to nonexistent pages
  are warnings, not errors)?  Read-time is more forgiving, recommended.
- How to handle deletions / page renames? Defer until it bites.
- When the wiki has both a page on "deepagents" AND on "langgraph", and
  they contradict subtly, which one wins?  Defer; health check should
  flag, human resolves.

## Anti-patterns to avoid (from research)

- Letting the agent create pages from cold without seed context. Always
  hand-seed first.
- One giant index page that links everything. Use page-per-concept.
- Wiki pages that just repeat the source verbatim. The whole point is
  synthesis -- if the wiki is just a copy of vstash, it has no value.
- Skipping the health check cron. Wikis without audit rot fast.

## References

- [Karpathy llm-wiki gist (original, 2026-04-03)](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
- [LLM Wiki v2 (extensions from agentmemory project)](https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2)
- [Agent Memory Techniques (NirDiamant repo)](https://github.com/NirDiamant/Agent_Memory_Techniques)
- ADR-0003 (vstash memory layers) for the existing taxonomy
- ADR-0004 (chat model decision) for cost context
