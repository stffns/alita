---
name: wiki-page-creator
description: Create a new wiki page about a topic, end to end -- research it, write the canonical page, link from a parent (outbound anti-orphan), adopt by editing another page to link in (inbound anti-orphan), and report. Use when the user says "crea una pagina wiki sobre X", "agrega X a la wiki", "documenta X", or when you decide to promote a recurring thought into a wiki page. Handles the full lifecycle without burning recursion budget on improvised step order.
---

# Wiki page creator

When you receive a request to create a new wiki page on a topic,
follow the steps below IN ORDER. Do NOT improvise the order or
collapse steps -- the structure exists so the whole flow fits well
under the graph recursion cap. Improvised chains hit recursion limit
50 and leave the page half-created.

## Step 1: Check if a page already exists

Slug the topic to lowercase kebab-case (e.g. "LangGraph" -> `langgraph`).
Call `wiki_search(<topic-keywords>)` and `wiki_read(<slug>)`. If a page
already exists, EDIT it via `wiki_write` to add the new material -- do
NOT create a parallel page. Exit after the edit.

## Step 2: Research the topic

Call `research(query=<topic>, deep=False)`. Take the 3-5 most useful
sentences from the result. Save the full synthesis to vstash:

    vstash_remember(layer='research', title='research_<slug>',
                    content=<the synthesis>)

You will cite this title as a `sources` entry in step 4.

## Step 3: Pick a parent page for outbound link

Call `wiki_list()` to see existing slugs. Pick the one most topically
related to the new topic. This becomes the `[[parent-slug]]` reference
inside the new page's body, which satisfies the anti-orphan OUTBOUND
rule (`wiki_write` rejects pages with no outbound link).

## Step 4: Write the new page

Build a body that:
  - Opens with a 1-2 sentence definition of the topic.
  - Has 2-3 short sections on the most important aspects, in prose.
  - Includes the `[[parent-slug]]` from step 3 inline, at a natural
    spot (not jammed at the end).
  - Uses prose, NOT bullet lists -- unless the topic is genuinely
    list-shaped (e.g. "the 5 main features of X").
  - Stays under ~2KB. Detail goes in vstash; the wiki page is the
    canonical synthesis.

Call:

    wiki_write(<slug>, body, sources=['research_<slug>'])

## Step 5: Adopt the new page (inbound link)

Pick the SAME parent from step 3 OR a different topically related
page from `wiki_list()`. Read its current body with `wiki_read`.
Decide where adding a `[[<new-slug>]]` reference is natural in
that body -- inside an existing paragraph that already touches the
topic, not crammed at the end. Build the updated body. Call:

    wiki_write(<adopter-slug>, updated_body,
               sources=['adopted_<new-slug>'])

This satisfies the INBOUND anti-orphan rule (no graph orphans).

## Step 6: Reply

Tell the user, in ONE conversational paragraph:
  - The slug you created.
  - The parent slug used for outbound link.
  - The adopter slug now containing the inbound link.

Do NOT ask permission at any point. Execute every step and report
the result. Asking "do you want me to..." mid-flow is the failure
mode this skill exists to prevent.

## Step counting (budget)

This workflow expects ~7-8 graph node iterations end to end:
  1. wiki_search / wiki_read (existence check)
  2. research()
  3. vstash_remember()
  4. wiki_list() (parent picking)
  5. wiki_write() (create)
  6. wiki_read() (adopter)
  7. wiki_write() (adoption)
  8. final composition

Within recursion_limit=50. If you find yourself making more than
~10 tool calls, you are improvising -- stop and pick one path.

## Recovery: when research is empty

If `research()` returns nothing useful (the topic is genuinely
unknown or paywalled), DO NOT create a page from imagination. Two
options, in order of preference:

  (a) Call `vstash_recall(query=<topic>)` to find any prior mention.
      If there is past chat or research touching the topic, summarize
      from that. Continue from step 3.

  (b) If vstash is empty too, create a minimal stub page that
      acknowledges the gap:

          "Topic requested but no source material available yet.
           Will revisit when next research or RSS surfaces something."

      Include the `[[parent-slug]]` for anti-orphan. Skip step 5
      (adoption can wait until the page has real content).

## Anti-patterns

  - Creating a page by paraphrasing the user's request without any
    research call. The wiki is YOUR synthesis, not theirs.
  - Combining steps 4 and 5 into one tool call -- impossible, but
    some models try. Two `wiki_write` calls. No shortcuts.
  - Asking "donde quieres que lo enlace?" mid-flow. You decide.
  - Treating step 5 as optional. Without inbound adoption, the new
    page is a graph orphan from birth and the wiki rots.
