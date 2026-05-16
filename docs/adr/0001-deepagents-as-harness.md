# 1. deepagents as the agent harness

- Status: Accepted
- Date: 2026-05-14
- Deciders: Jay

## Context

We needed a Python framework that gives us:

- a tool-using LLM loop with planning,
- sub-agent delegation with isolated context,
- skill loading and persona prompting,
- enough escape hatches that we can graft our own memory/scheduling on top.

Candidates: build it from scratch on raw LangGraph, use LangChain agents
directly, CrewAI, AutoGen, or deepagents (which is a thin opinionated
layer over LangGraph).

## Decision

We picked **deepagents 0.6**. The mode-detection persona we want maps
cleanly onto its system_prompt + tools + subagents + skills surface, and
LangGraph underneath gives us hooks (middleware, callbacks) for the
metrics + compression-logging features.

## Consequences

- Built-in primitives: `task` tool for sub-agents, file-based skills with
  progressive disclosure, `write_todos` for planning, etc.
- We inherit deepagents' base middleware stack (TodoListMiddleware,
  PatchToolCallsMiddleware, SkillsMiddleware). Adding our own work
  alongside it via `middleware=[...]` is well-supported.
- We are exposed to deepagents' beta-API rate of change. Specifically:
  the 0.6 docstring claims `SummarizationMiddleware` is in the base
  stack; in practice it is not. We caught this only by inspecting the
  assembled graph. If we move to 0.7 we should re-check.
- We avoid building agent-loop, message-state, and tool-routing logic
  ourselves. That is a lot of code we do not have to own or test.

## Alternatives considered

- **Raw LangGraph**: more flexible, more code. Right answer if we hit
  deepagents limitations later.
- **CrewAI**: more "crew" mental model than companion-agent. Less direct
  fit for one Pelops with sub-agents.
- **AutoGen**: heavier; was last evaluated for multi-agent debate setups
  which we do not need.
