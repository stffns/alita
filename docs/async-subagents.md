# Async sub-agents in deepagents

`deepagents` ships three flavors of sub-agent. Today Pelops uses only the
synchronous one. This doc explains the difference, when you would migrate to
async, and exactly what changes when you do.

## The three flavors

All declared in the same `subagents=` parameter on `create_deep_agent`. The SDK
routes them by type:

| Type | Where it runs | Blocking? | Setup cost |
|---|---|---|---|
| `SubAgent` (declarative sync) | Same process as the parent | Yes, blocks the loop | Zero. Just a TypedDict |
| `CompiledSubAgent` (sync) | Same process | Yes | A pre-built LangGraph runnable |
| `AsyncSubAgent` | A remote LangGraph / Agent Protocol server | **No**, fire-and-forget | Need to host the server |

Mixing types in the same list is fine -- the middleware routes each by shape.

## How sync sub-agents work today

`pelops/subagents.py` declares the `researcher`:

```python
RESEARCHER: SubAgent = {
    "name": "researcher",
    "description": "...",
    "system_prompt": "...",
    "tools": [research, vstash_remember],
}
```

When the parent calls `task("researcher", ...)`:

```
Pelops (qwen, main loop)         researcher (qwen, isolated context)
   |                                  |
   | task("researcher", brief)        |
   |---BLOCK--------------------------|
   |                                  | research() -> compound
   |                                  | vstash_remember()
   |                                  | <- final markdown
   |<-result--------------------------|
   | (resumes)                        |
```

Key properties:
- **Blocks the parent.** No other tool runs until the sub-agent returns.
- **Isolated context.** The sub-agent does not see the parent's messages, only
  the prompt it was invoked with. This is why sync sub-agents still save tokens
  -- the parent stays small even if the sub-agent burns thousands.
- **Sequential.** If the parent calls `task` three times in one turn, they run
  one after another.

## How async sub-agents work

```python
ASYNC_RESEARCHER: AsyncSubAgent = {
    "name": "researcher",
    "description": "...",
    "graph_id": "research_agent",
    "url": "http://localhost:9000",          # or LangGraph Platform URL
    "headers": {"Authorization": "Bearer ..."},  # optional
}
```

When you register an async sub-agent, `AsyncSubAgentMiddleware` **auto-injects
five tools** into the parent agent:

| Tool | What it does |
|---|---|
| `astart_async_task(description, subagent_type)` | Kicks off a run on the remote server, returns a `task_id` immediately |
| `acheck_async_task(task_id)` | Polls status and pulls partial output |
| `aupdate_async_task(task_id, message)` | Injects an additional user message into a running task |
| `acancel_async_task(task_id)` | Kills a running task |
| `alist_async_tasks()` | Lists all active task_ids in this conversation |

The task IDs are persisted in agent state under the `async_tasks` key, so they
survive context compaction and offloading. The parent can keep working between
checks; nothing blocks.

```
Pelops (local)             researcher (remote LangGraph server)
   |                            |
   | astart_async_task(...)     |
   |-------HTTP POST----------->|
   |<-- task_id={uuid} ---------|  (server keeps running in background)
   |                            |
   | (parent continues chatting,|
   |  uses other tools, etc.)   |
   |                            |
   | acheck_async_task(uuid)    |
   |-------HTTP GET------------>|
   |<-- status=running ---------|
   |  ...                       |
   | acheck_async_task(uuid)    |
   |-------HTTP GET------------>|
   |<-- status=done, result ----|
   | (uses result)              |
```

## When you actually need async

Pick async only when **at least one** of these is true:

1. **The task takes minutes, not seconds.** A 30-second research call inside
   a chat turn is fine sync -- the user is waiting on the next message anyway.
   A 5-minute deep paper analysis is a candidate for async.

2. **You want the parent to keep chatting while the sub-agent works.** For
   example: the user asks for a briefing AND a market summary. Async lets both
   start in parallel and the parent reports back as each finishes.

3. **You want true parallelism across sub-agents.** Three researchers
   investigating three topics simultaneously. Sync would serialize them.

4. **Pelops runs on a server with a non-chat front-end.** A Telegram bot or
   cron consumer that fires a long task and pings the user later when it is
   done. Async fits that model naturally; sync would hold the bot's reply
   handler open.

5. **You want the work to survive a parent crash.** Async runs live on the
   remote server with their own persistence, so killing the chat process does
   not kill the task. Sync sub-agents die with the parent.

## When you do NOT need async

For Pelops as it stands today: **none of the above applies.** The chat is
turn-based, the user waits for the reply, sub-agents finish in 5-20s, and the
whole thing runs in a single laptop process. Adding async right now would buy
you nothing and cost you a server.

Specifically, do not migrate if:
- All sub-agents finish in under a minute.
- The UI is request-response (Chainlit, REST endpoint).
- You only ever invoke one sub-agent per turn.
- You do not have anywhere to host a LangGraph server.

## What it costs to add async

Three pieces of infra you do not have today:

1. **A server speaking Agent Protocol.** Two paths:
   - `langgraph dev --port 9000` running on your laptop -- zero dollars,
     dies when the laptop sleeps. Good for prototyping.
   - LangGraph Platform (managed by LangChain) or self-hosted FastAPI
     implementing the [Agent Protocol spec](https://github.com/langchain-ai/agent-protocol) --
     real money or real ops work.

2. **A standalone graph definition** for each async sub-agent. You take what is
   currently inside `RESEARCHER["system_prompt"] + tools` and turn it into a
   `langgraph.json` config plus a `graph.py` that exposes the runnable.

3. **Auth config.** Env vars (`LANGGRAPH_API_KEY` for the managed platform) or
   custom headers for self-hosted.

## Migration recipe (when the day comes)

Step 1 -- extract the researcher into a standalone graph file:

```
pelops/
├── async_graphs/
│   ├── langgraph.json
│   └── researcher_graph.py        # builds the same agent, exports `graph`
```

Step 2 -- run it locally:

```
cd pelops/async_graphs
langgraph dev --port 9000
```

Step 3 -- swap the declaration in `pelops/subagents.py`:

```python
# Before
RESEARCHER: SubAgent = {
    "name": "researcher",
    "description": "...",
    "system_prompt": RESEARCHER_PROMPT,
    "tools": [research, vstash_remember],
}

# After
RESEARCHER: AsyncSubAgent = {
    "name": "researcher",
    "description": "...",
    "graph_id": "researcher",
    "url": "http://localhost:9000",
}
```

Step 4 -- update the persona prompt so Pelops knows to use the new tool names
(`astart_async_task` instead of `task`). The SDK adds the tools automatically;
you only need to teach Pelops that they exist:

```
- For deep research, call `astart_async_task("researcher", <question>)`.
  Save the returned task_id. While it runs, you can keep chatting.
  Poll with `acheck_async_task(task_id)` when the user asks for status
  or after enough time has passed.
```

Step 5 -- everything else (vstash, scheduler, Chainlit UI) stays unchanged.
The contract at the agent level is the same; only the transport changes.

## Bonus: cheap parallelism without async

If all you want is **3 research calls at once** without the infra, skip async
entirely and add a sync tool that uses `asyncio.gather` internally:

```python
@tool
async def research_parallel(queries: list[str], deep: bool = False) -> str:
    """Run multiple research queries against Compound in parallel.
    Returns one consolidated markdown brief.
    """
    import asyncio
    results = await asyncio.gather(*[research_async(q, deep) for q in queries])
    return "\n\n---\n\n".join(results)
```

That gives you `N x` speedup against Compound on the wall clock, runs entirely
in-process, and needs zero new infra. It does not give you the "fire-and-forget,
parent keeps chatting" property -- the parent still blocks until all queries
finish. But for the most common "parallelize this" use case, it is enough.

## TL;DR

- `AsyncSubAgent` is a real, native deepagents feature -- not something you
  build from scratch.
- The client side (the parent agent and the 5 task tools) is ready out of the
  box.
- The server side (somewhere for the sub-agent to actually run) is on you.
- For Pelops today: stay sync. Revisit when sub-agent runtimes exceed ~60s or
  when the UI stops being turn-based.
