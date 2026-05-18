# Experiment: gbrain-style entity tools for wiki

Date: 2026-05-18
Branch: `feature/wiki-entity-tools`
Status: design (not yet run)

## Motivation

GBrain (Garry Tan, April 2026) demonstrates that an LLM agent gets a
non-trivial recall and reasoning boost from a self-wiring entity graph
over its markdown notes -- without the per-write LLM cost of fancier
extraction. The graph lets the agent answer "which pages mention X" or
"what does the wiki think about X across all references", which a
single-page `wiki_read(X)` cannot.

Pelops already has the substrate: pages with `[[slug]]` wikilinks. We
have a `backlinks()` function but it has never been exposed to the
agent. This experiment validates whether exposing it (plus typed links
and a graph summary) measurably improves the agent's recall on
multi-page topics.

## Hypothesis

Given queries that span multiple wiki pages, the agent will produce
more accurate and dense answers when `wiki_backlinks` is available
than when only `wiki_read` and `wiki_search` exist.

Specifically:
  H1. The agent will choose `wiki_backlinks(X)` over enumerating every
      page when asked "what does the wiki say about X".
  H2. Answer coverage (number of pages cited in the response) will
      go up.
  H3. Token cost per query will NOT increase materially, because
      `wiki_backlinks` is a single regex scan vs N page reads.

## Setup

Seed pages currently in `~/Documents/pelops-wiki/Alita/`:

| slug | inbound (today) |
|---|---|
| alita | 9 |
| vstash | 7 |
| wiki | 7 |
| heartbeat | 5 |
| jay | 1 |
| persona | 0 (orphan) |

The vault is small. The experiment validates the MECHANIC, not the
quality at scale. Scale validation comes when the wiki grows to ~50+
pages.

## Test queries

Three categories, run BOTH against control (no `wiki_backlinks`) and
treatment (with `wiki_backlinks` available).

### A. Multi-page synthesis

A1. "Que pages mencionan a alita en la wiki?" -- expects a list, not
    a long-form essay.
A2. "Resume todo lo que la wiki dice sobre vstash, no solo la pagina
    canonica." -- expects 2-3 paragraphs synthesizing alita.md,
    heartbeat.md, wiki.md, jay.md.
A3. "Que conexiones existen entre heartbeat y wiki?" -- expects the
    agent to compare both pages and note the explicit
    `[[heartbeat]] -> [[wiki]]` and reverse links.

### B. Discovery / orphan detection

B1. "Hay paginas wiki sin nadie que las enlace?" -- expects `persona`
    to be flagged.
B2. "Cual es la pagina mas referenciada de la wiki?" -- expects
    `alita` (9 inbound).

### C. Anti-orphan workflow

C1. Ask the agent to create a NEW wiki page on "compound" without
    giving it instructions on linking. Watch whether it uses
    `wiki_backlinks` or `wiki_list` first to find a parent. Treatment
    should have a higher rate of `wiki_backlinks` use because the
    tool now exists in its toolset.

## Metrics

For each query x condition combination:

| Metric | How to measure |
|---|---|
| Pages cited in response | Count distinct `[[slug]]` or slug-name mentions in the agent's reply |
| Tool call sequence | From LangSmith trace: ordered list of tools called |
| Total prompt tokens | LangSmith run `prompt_tokens` summed across the chain |
| Total completion tokens | LangSmith run `completion_tokens` summed |
| Wall-clock latency | LangSmith trace `start_time` to last child `end_time` |
| Qualitative coherence | Manual 1-5 score: does the answer feel like it synthesizes the wiki vs paraphrase one page? |

## Success criteria

The experiment "succeeds" (treatment is better than control) when:
- H1 is observed: agent picks `wiki_backlinks` over a chain of
  `wiki_read` calls for query A1 in >=66% of treatment trials.
- H2 holds: average pages cited in A2 treatment > A2 control.
- H3 holds: average total tokens per query within ~20% of control
  (no big regression).
- C1: in treatment, the agent uses `wiki_backlinks` or `wiki_list`
  before `wiki_write` on the new page in >=80% of trials.

The experiment "fails" (treatment NOT better) when none of those hold,
OR the treatment increases tokens by >50% with no quality gain.

## Sample size and method

Per query, run 3 trials in control and 3 in treatment (6 per query *
6 queries = 36 trials total). LangSmith captures all traces. Manual
qualitative score by Jay after reading all 36 responses, blind to
condition.

To avoid the lru_cache caching the agent across conditions, restart
the bot between control and treatment phases.

For control, temporarily comment `wiki_backlinks` and
`wiki_graph_stats` out of `WIKI_TOOLS` before restart.

## Limitations / threats to validity

- **Vault is tiny.** Six pages limits the entity graph's expressivity.
  A bigger wiki would amplify any effect. We MAY see no difference here
  just because there is not enough graph to exploit.
- **Single user.** Effects observable to Jay only; cannot generalize
  to other users with different mental models.
- **Model drift.** deepseek-v4-flash routing decisions can vary;
  3 trials per condition is the floor for noise.
- **No automated eval.** Qualitative coherence is judged by Jay
  manually. Subject to anchoring on the first answer seen.

## After the experiment

If the entity tools win: keep them in `CHAT_TOOLS`, update persona to
explicitly recommend them for "which pages mention X" queries.

If they lose: keep the module (the code is small and serves the
internal `entity_graph()`) but drop them from `CHAT_TOOLS` to reduce
the schema surface fed to the model.

If results are mixed: keep `wiki_backlinks` (clear use case), drop
`wiki_graph_stats` (rarely needed at this scale).
