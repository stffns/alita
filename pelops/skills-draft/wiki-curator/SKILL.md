---
name: wiki-curator
description: Guide for when and how to read, create, and update wiki pages. The wiki is Alita's compiled-knowledge layer (mutable markdown pages with [[backlinks]]), complementary to vstash (raw retrieval). Use this skill any time the conversation might touch a long-lived topic worth compiling, or when answering "what do you know about X".
---

# Wiki Curator

The wiki is Alita's compiled-knowledge layer. It complements [[vstash]]
the same way a textbook complements your stack of source papers.

  * vstash answers "what did the source say"
  * wiki answers "what do I know about Y"

## Read path: when to query the wiki

Reach for `wiki_read` (or `wiki_search`) FIRST when the user asks:

  * "que sabes de X" / "what do you know about X"
  * "que entiendes sobre Y"
  * "que es Z" (when Z is a recurring topic, not a one-off fact)
  * Anything that asks about Alita's understanding of a topic, not about
    what a specific source said on a specific date.

Reach for `vstash_recall` FIRST when the user asks:

  * "que dijo el paper sobre X"
  * "cuando hablamos de Y"
  * "ayer me dijiste algo de Z"
  * Anything that wants a citation, a date, or a specific source.

Both tools can be combined: wiki for the answer, vstash for the citations.

## Write path: when to update the wiki

Update or create a page when, in this turn, you learned something that
would change the answer to "what does Alita know about <topic>" for
future turns. Concretely:

  * A new piece of research lands and the topic already has a page -> update.
  * A topic surfaces repeatedly across multiple chats and has no page yet
    -> create.
  * A claim on an existing page is contradicted by a new source -> update
    the page AND note the contradiction inline.

Do NOT create a page just because the topic appeared once. Wait for the
second or third encounter -- pages have maintenance cost.

## Rules

1. **One canonical page per concept.** If `deepagents` already has a
   page, do NOT create `deepagents-2026` or `deepagents-update`. EDIT
   the existing page.

2. **Anti-orphan.** When creating a new page, the body MUST link to at
   least one existing page via `[[other-slug]]`. The tool will reject
   orphan pages. Adopt the new page into the graph -- link upward to a
   parent topic, downward to a child detail, or sideways to a related
   concept.

3. **Slugs are kebab-case.** Lowercase letters, digits, hyphens. No
   spaces, no underscores, no caps. Match the filename.

4. **Synthesize, do not paste.** The wiki body is YOUR understanding,
   not a copy of the source. If the page would be 80% verbatim from
   research, you have not done the curation work yet -- distill it.

5. **Cite sources via the `sources` argument.** Pass the vstash document
   titles or URLs you used. This is the audit trail; do not skip it.

6. **Conversational prose in the body.** Headings, bullet lists, and
   tables are fine in the wiki -- this is documentation, not chat. But
   keep it readable: each page is meant to fit on a screen, not be a
   thesis.

## What NOT to do

  * Do not write a wiki page about every chat. Most chats are episodic
    and belong in [[vstash]], not in the wiki.
  * Do not duplicate vstash content verbatim. If the source is already
    in vstash, the wiki page should add a layer of synthesis.
  * Do not invent backlinks. `[[other-slug]]` must point to a real page
    (use `wiki_list` to confirm) or it becomes a broken link.
  * Do not edit the frontmatter directly. The `wiki_write` tool manages
    it; you provide body only.

## Tool reference

  * `wiki_list()` -- enumerate all pages. Cheap; use to discover.
  * `wiki_read(slug)` -- fetch a page including frontmatter.
  * `wiki_search(query, limit=10)` -- substring search across bodies.
  * `wiki_write(slug, body, sources=None)` -- create or update. Body is
    markdown WITHOUT frontmatter. Sources is an optional list of vstash
    titles or URLs.
