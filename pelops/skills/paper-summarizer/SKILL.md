---
name: paper-summarizer
description: Read an arXiv abstract or paper URL and produce a structured summary aimed at an ML practitioner. Use when Jay shares an arxiv link or asks "resume este paper".
---

# Paper Summarizer

## Goal

Turn a paper URL or abstract into a one-screen technical summary that an ML
practitioner can read in 30 seconds and decide whether to read the full paper.

## Workflow

1. **Fetch the paper**. Call the `researcher` sub-agent via the `task` tool with
   a query like *"Visit <url> and return the abstract, key contributions, and
   evaluation setup, verbatim where possible"*. The researcher will use Compound's
   visit_website tool.

2. **Extract these fields** from the returned material:
   - **Problem**: what is the paper trying to solve, in one sentence
   - **Key idea**: the core technique or insight, in one sentence
   - **Method**: 2-3 bullets on the technical approach
   - **Evaluation**: what benchmarks, what baselines, headline numbers
   - **Limitations**: what the authors themselves acknowledge as weak
   - **So-what**: one sentence on what would change in practice if the idea works

3. **Write the summary** in this shape:

   ```
   # <Title> (<authors>, <year>)

   - **Problem**: ...
   - **Key idea**: ...
   - **Method**:
     - ...
     - ...
   - **Evaluation**: <benchmark> -- <headline numbers> vs <baseline>
   - **Limitations**: ...
   - **So-what**: ...

   Link: <url>
   ```

4. **Save** it with `vstash_remember(content=summary, title='paper_<slug>', tags='paper,summary')`.

## Anti-patterns

- Do not regurgitate the abstract. The summary is your synthesis.
- Do not invent numbers. If the source did not give a number, write "N/A".
- Do not include the math. Leave equations to the paper itself.
- Cap each bullet at one line. If you need more, the idea is not crisp yet.
