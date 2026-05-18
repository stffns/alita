# Design: sandbox code execution for Alita

Status: design only, not implemented.
Owner: Jay.
Date: 2026-05-18.
Companion to PR feature/code-search-and-github-read (Semble + gh
read tools, the layers BELOW this one).

## Why this exists

The current capability stack for Alita:

  - read code (Semble: `code_search` -- PR feature/code-search-...)
  - read GitHub state (gh CLI: `gh_pr_view`, `gh_issue_view`, etc)
  - read/write the wiki (markdown vault)
  - read/write vstash (memory)

What is still NOT here, by design:

  - execute code (run a script, run a test, run a build)
  - write to GitHub (open PR, comment, push branch)
  - touch the host file system outside the vault

Each of those expands the agent's blast radius materially. This
document is the plan for adding execution safely, when there is a
concrete use case that justifies the infrastructure cost.

## Concrete use cases (when we would actually want execution)

  1. **Validate a code change before proposing it.** Alita drafts a
     small refactor, runs the test suite in a sandbox, reports
     whether tests pass. Without execution, she is guessing.

  2. **Run a one-shot research query that requires code.** "What's
     the latency of this library on my laptop?" needs `pip install`
     and a benchmark. Web search is not enough.

  3. **Validate a generated wiki snippet.** When the agent writes a
     code block into a wiki page, execute it to verify it compiles
     and runs.

  4. **Daily smoke tests on side projects.** Alita has read access
     to all of Jay's repos via Semble + gh. A nightly cron could
     pull a side repo, run its test suite, surface failures.

None of these are urgent today. But the design constraints are clear
enough to plan around them.

## Constraints

  - Single user, single laptop. We are not building a service.
  - Latency budget per execution: ~10s acceptable, 60s tolerable,
    minutes only for explicit research tasks.
  - Cost budget: ~$5/month tolerable on top of the existing LLM cost.
  - Security: a hostile prompt must not be able to exfiltrate the
    host's secrets (Jay's AWS keys, GitHub PAT, vstash data outside
    what the agent already has). Sandbox isolation is mandatory.
  - Auditability: every execution leaves a record (command,
    stdout/stderr summary, exit code, duration) in vstash
    `layer='agent-action'`.

## Compared options

| Option | Isolation | Cost | Latency | Setup |
|---|---|---|---|---|
| **Modal Sandboxes** | Cloud container per task | $0.001-0.01/min | ~5s cold start | Needs Modal account + API key |
| **E2B** | Cloud container per task | similar to Modal | similar | Needs E2B account |
| **Daytona** | Cloud, persistent between calls | cheaper if reused | <1s warm | Needs Daytona account |
| **Local Docker** | Container on Jay's machine | free | <2s | Docker installed + agent has docker socket access |
| **Local subprocess + chroot/firejail** | Process isolation | free | <1s | Linux only; macOS not supported well |
| **VM (e.g., UTM)** | Full VM | free | seconds to start | Heavy; overkill |

Recommendation: **Modal Sandboxes** for the cloud path, OR **local
Docker** for the offline path. Both expose a `run(code: str, timeout:
int) -> result` API behind the scenes. Plumbing the agent through
either is similar in code shape.

For Pelops's single-user laptop setup, local Docker is the simpler
choice. Cloud Modal becomes interesting only if Jay wants execution
when his laptop is asleep (cron jobs continuing) -- which would
require running the bot in the cloud anyway, a bigger move.

Going forward, design assumes **local Docker** for v1 of execution.
Modal/E2B can be added later behind the same tool interface.

## Tool surface (when implemented)

```python
@tool
def code_execute(
    code: str,
    language: str = "python",       # 'python' | 'bash'
    timeout_seconds: int = 30,
    persist_workdir: bool = False,  # if True, a per-session
                                     # workdir survives between calls
) -> str:
    """Run code in a sandboxed Docker container.

    Returns: combined stdout/stderr + exit code + duration.
    Errors: container failures are returned as text, not raised.
    """
```

ONE tool, not many. This follows the "one run_python tool" pattern
that emerged in 2026 (see the MCP code-execution sandbox writeups).
Tool schema bloat is real and the agent only needs to learn one verb.

Optional second tool:

```python
@tool
def code_session_reset() -> str:
    """Wipe the persistent workdir, kill any running containers."""
```

For the rare case where state leaks across calls and the user wants
a clean slate.

## Security model

  1. **Read-only host mount, write to scratch.** The container gets a
     read-only mount of `~/Documents/pelops-wiki/Alita/` (to read
     the wiki) and writes only to a scratch dir that gets destroyed
     when the container exits.

  2. **No host network access by default.** Container's network is
     disabled. If a use case needs `pip install` or web requests,
     that is opt-in via a second variant (e.g., `code_execute_net`).

  3. **No host credentials in env.** Container is started with a
     stripped `--env` set: no GITHUB_TOKEN, no AWS keys, no GROQ_API_KEY,
     nothing from the agent's `Settings`.

  4. **CPU/memory caps.** `--cpus=2 --memory=1g`. Prevents fork bombs
     and OOMs from killing Jay's laptop.

  5. **Resource cleanup.** A `--rm --stop-timeout 5` so containers
     do not pile up.

## Authorization flow

For v1, execution is gated by an explicit user message: Jay says
"OK, run that" and Alita executes. The agent does NOT decide to
execute autonomously without a human go-ahead -- this is a
deliberate guardrail.

Once the loop is stable for a few weeks, we revisit: a `safe`
allowlist of small commands (run tests, check syntax, validate a
YAML) might be okay to run without asking.

## Implementation outline (when we ship v1)

  1. New module `pelops/sandbox.py` -- wraps `docker run` via
     `subprocess`, parameterized.
  2. New tool `code_execute` in `pelops/tools.py`, gated by an env
     var `ALITA_SANDBOX_ENABLED=true` so it can be toggled off
     without code changes.
  3. Persona update (wiki edit): "you have a `code_execute` tool.
     ALWAYS ask Jay before using it. Echo the command back, wait for
     a yes/no, then run."
  4. Skill: `code-validator` -- when Jay asks Alita to validate a
     code change, the workflow is:
       a. Show the proposed change
       b. Ask permission to execute the test suite
       c. Run via `code_execute`
       d. Report pass/fail + relevant test names
  5. Docker prep: a base image with Python 3.11, ruff, pytest,
     and a few common stdlib things pre-installed -- saves 30s of
     cold-start `pip install`.

## What we ship in this PR

Just the design doc you are reading. The plumbing comes later, when
we hit a use case that demands it. Going forward by writing code
without a use case in mind is the over-engineering trap that this
project has stayed clear of.

## Open questions (defer until v1)

  - Do we let Alita run sandboxed code WITHOUT asking, in the
    heartbeat? Probably not for v1.
  - Do we want a "code playground" mode where Jay can interactively
    pair-program with Alita and ask her to run snippets? Worth
    revisiting once `code_execute` is in.
  - At what point does the cron also need execution? Right now no
    cron requires it.

## References

  - [AI Agent Sandbox 2026 (Firecrawl)](https://www.firecrawl.dev/blog/ai-agent-sandbox)
  - [Modal sandboxes example with LangGraph](https://modal.com/docs/examples/agent)
  - [Code execution with MCP (Red Hat)](https://next.redhat.com/2026/04/23/how-sandboxed-python-reduces-tool-schema-overhead-in-ai-agents/)
  - [LangChain deepagents sandboxes docs](https://docs.langchain.com/oss/python/deepagents/sandboxes)
  - [Best code execution sandbox 2026 (Northflank)](https://northflank.com/blog/best-code-execution-sandbox-for-ai-agents)
