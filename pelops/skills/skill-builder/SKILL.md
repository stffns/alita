---
name: skill-builder
description: Author a new skill or refine an existing one. Use when you (Alita) notice a recurring task that would benefit from being encoded as a skill, OR when Jay says "crea un skill para X", "guarda esto como skill", "haz que esto sea reutilizable", or asks you to modify an existing skill. Skills are markdown files with YAML frontmatter that deepagents' SkillsMiddleware auto-loads when a user query matches the skill's description.
---

# Skill builder

A "skill" is a reusable mini-playbook for a recurring kind of task.
It lives in `<wiki_dir>/skills/<slug>/SKILL.md` (when authored by
you) or `pelops/skills/<slug>/SKILL.md` (when shipped in the code
repo). Both locations are scanned at agent build time. You can
ONLY write to the vault location -- the code-shipped skills are
the floor, immutable from your side.

## When to author a new skill

  - You notice you have done the SAME multi-step workflow more than
    twice (research-summarize-save, list-filter-act, etc).
  - Jay asks to make a one-off task reusable.
  - A recurring chat pattern would benefit from a known recipe (e.g.
    "every time Jay asks for X, I should do Y, Z, then W").

## When to refine an existing skill

  - The skill's trigger description keeps producing false positives
    (loading when it should not) or false negatives (not loading
    when it should).
  - A step in the workflow is consistently wrong or wasteful.
  - You discover a better tool for one of the steps.

## Workflow: AUTHOR a new skill

### Step 1: Check if a similar skill already exists

Call `skill_list()`. Read the result. If a slug or description
looks close to what you would write, `skill_read(<close-slug>)` to
check if you should EDIT that one instead of creating a new
parallel skill. Same anti-parallel rule as wiki pages.

### Step 2: Read 1-2 existing skills as templates

`skill_read('wiki-page-creator')` or `skill_read('wiki-curator')`
to see the standard structure: YAML frontmatter at the top,
numbered Steps, Anti-patterns section, Recovery section. Match
that shape.

### Step 3: Draft the body

The body MUST start with YAML frontmatter:

    ---
    name: <slug>
    description: <single paragraph: WHAT the skill does + WHEN to load>
    ---

`description` is what SkillsMiddleware shows the agent when
deciding whether to load the skill. Make it CONCRETE -- list the
trigger phrases (in Spanish AND English) and the user intent in
plain language. Vague descriptions don't trigger reliably.

Then a markdown body. Recommended sections (you may omit any that
do not apply):

  - `# <Title>` -- the slug as a heading.
  - Short intro paragraph: one sentence on the problem the skill
    solves.
  - `## When to <X>` -- one or two short paragraphs on the trigger
    conditions, to mirror the description with more detail.
  - `## Workflow:` or `## Step 1: ...`, `## Step 2: ...` -- explicit
    numbered steps. Each step says which tool to call and what to do
    with the result.
  - `## Step counting (budget)` -- estimate iterations end to end so
    the model can self-check. Helps when graph recursion is a
    concern.
  - `## Anti-patterns` -- explicit failure modes to avoid. Be blunt.
  - `## Recovery` -- what to do when a step fails or returns empty.

### Step 4: Save via skill_write

    skill_write('<slug>', body)

Returns the path. The skill is auto-loaded on the next `ask()` turn
because the persona-mtime invalidator clears the build_agent cache
when ANY tracked file changes -- including new skills under
`<wiki_dir>/skills/`. No manual restart required.

### Step 5: Confirm in one sentence

Tell Jay what slug you created, the trigger phrases you put in the
description, and that the skill is live now. Do NOT ask permission
-- skill creation is housekeeping like wiki page authoring.

## Workflow: REFINE an existing skill

### Step 1: Read it

`skill_read('<slug>')`. Note whether it lives in `code` or `vault`.

### Step 2: Decide the change

  - If the change is a TRIGGER refinement (description): the goal is
    to make the skill load on the right queries.
  - If the change is a STEP refinement: a specific step is wrong or
    has a better tool sequence.
  - If REMOVAL: the skill is no longer useful. Note: you can only
    delete `vault`-sourced skills (the code-shipped ones are the
    floor).

### Step 3: Write the updated body

Take the existing body, apply the diff in your head, and pass the
WHOLE new body to `skill_write('<slug>', new_body)`. If you forget
the frontmatter, the tool will reject and tell you. There is no
in-place edit; it is always full rewrites.

### Step 4: Confirm

One sentence to Jay: "I refined the <slug> skill -- <one-line
description of the diff>." Do NOT ask permission for refinements.
Removal is the only case where you should pause and confirm with
Jay first.

## Step counting (budget)

AUTHORING:
  1. skill_list                       -- 1 call
  2. skill_read (template)            -- 1 call
  3. (deliberation, no tool call)
  4. skill_write                      -- 1 call
  5. final reply composition          -- 1 model call

REFINING:
  1. skill_read                       -- 1 call
  2. (deliberation, no tool call)
  3. skill_write                      -- 1 call
  4. final reply composition          -- 1 model call

Both fit very comfortably under recursion_limit=50. If you find
yourself burning more than 8 iterations on a skill operation, you
are over-thinking it.

## Anti-patterns

  - Creating a skill that does what one of the existing skills
    already does, with a different slug. Same rule as wiki pages:
    one canonical workflow per task.
  - Writing the skill body WITHOUT first reading an existing skill
    as template. Style drift makes skills harder to navigate.
  - Vague descriptions: "for various tasks" or "useful for many
    things". SkillsMiddleware needs concrete intent matches.
  - Including code blocks with executable Python in the skill body.
    Skills are PLAYBOOKS for the agent, not executable code.
  - Storing secrets, API keys, or PII in skill bodies. The vault is
    in git; everything you write is version-controlled and
    inspectable by Jay.

## Recovery

  - If `skill_write` returns a validation error, READ the error
    carefully. Most common cases:
      * Missing frontmatter -- add `--- name: ... description: ... ---`
        at the very top.
      * Invalid slug -- must be lowercase kebab-case, start with
        letter/digit.
  - If you tried to skill_read a slug that does not exist and you
    are sure it should -- check `skill_list` for the exact name.
    Slug typos are common.
  - If the new skill does not load on the next turn -- check that
    `<wiki_dir>/skills/<slug>/SKILL.md` exists and that the
    frontmatter is valid YAML. The agent rebuild only sees properly-
    structured skill files.
