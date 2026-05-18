"""Read-only GitHub access via the `gh` CLI.

Deliberately READ-only. No PR creation, no comments, no commits, no
branch pushes -- those expand the agent's blast radius materially and
deserve their own opt-in path (likely a separate module + an explicit
user-fact authorization).

We shell out to `gh` because it is already installed and authenticated
on Jay's laptop. No new secrets to manage, no API token plumbing.
Subprocess calls are scoped: only known-safe `gh` subcommands are
invoked, with strict argument validation upstream in the tool wrappers.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess

_log = logging.getLogger("pelops.github_read")

# Restrict to `owner/repo` form for safety. Without this, a malformed
# argument could escape and run arbitrary `gh` subcommands.
_REPO_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9._-]+$")


class GitHubReadError(RuntimeError):
    """Raised when a `gh` invocation fails or returns malformed data."""


def _gh(*args: str, timeout: float = 30.0) -> str:
    """Run `gh` with arguments and return stdout. Never shell-expands."""
    cmd = ["gh", *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitHubReadError("gh CLI not installed on this machine") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubReadError(f"gh timed out after {timeout}s: {' '.join(args)}") from exc
    if result.returncode != 0:
        # Common case: not authenticated, repo not found, network down.
        # Surface stderr (truncated) so the caller can fix the cause.
        raise GitHubReadError(
            f"gh exited {result.returncode}: {(result.stderr or '').strip()[:300]}"
        )
    return result.stdout


def _validate_repo(repo: str) -> None:
    if not _REPO_RE.match(repo):
        raise GitHubReadError(f"invalid repo identifier: {repo!r} (expected 'owner/name')")


def pr_list(repo: str, state: str = "open", limit: int = 10) -> list[dict]:
    """List PRs in a repo.

    Args:
        repo: 'owner/name' (e.g. 'stffns/alita').
        state: 'open' | 'closed' | 'merged' | 'all'.
        limit: max number of PRs.
    """
    _validate_repo(repo)
    if state not in {"open", "closed", "merged", "all"}:
        raise GitHubReadError(f"invalid state: {state!r}")
    raw = _gh(
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        state,
        "--limit",
        str(int(limit)),
        "--json",
        "number,title,state,createdAt,author,url",
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubReadError(f"gh returned non-JSON: {raw[:200]!r}") from exc


def pr_view(repo: str, number: int) -> dict:
    """Fetch a single PR with body + comments summary + diff stat."""
    _validate_repo(repo)
    raw = _gh(
        "pr",
        "view",
        str(int(number)),
        "--repo",
        repo,
        "--json",
        "number,title,state,body,author,createdAt,mergedAt,additions,deletions,changedFiles,url,comments",
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubReadError(f"gh returned non-JSON: {raw[:200]!r}") from exc


def issue_list(repo: str, state: str = "open", limit: int = 10) -> list[dict]:
    """List issues in a repo."""
    _validate_repo(repo)
    if state not in {"open", "closed", "all"}:
        raise GitHubReadError(f"invalid state: {state!r}")
    raw = _gh(
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        state,
        "--limit",
        str(int(limit)),
        "--json",
        "number,title,state,createdAt,author,url,labels",
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubReadError(f"gh returned non-JSON: {raw[:200]!r}") from exc


def issue_view(repo: str, number: int) -> dict:
    """Fetch a single issue with body + comments summary."""
    _validate_repo(repo)
    raw = _gh(
        "issue",
        "view",
        str(int(number)),
        "--repo",
        repo,
        "--json",
        "number,title,state,body,author,createdAt,url,labels,comments",
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubReadError(f"gh returned non-JSON: {raw[:200]!r}") from exc


def recent_commits(repo: str, branch: str = "main", limit: int = 10) -> list[dict]:
    """List recent commits on a branch via the GitHub API.

    `gh api` is used because `gh repo view` does not expose commit
    history directly. The response is filtered to the fields most
    useful for the agent: sha, message, author, date.
    """
    _validate_repo(repo)
    if not re.match(r"^[a-zA-Z0-9._/-]+$", branch):
        raise GitHubReadError(f"invalid branch name: {branch!r}")
    # Query params go directly in the URL. Using `-f`/`-F` flags makes
    # gh treat them as form data for POST endpoints, which causes a
    # 404 on this read-only GET.
    raw = _gh(
        "api",
        f"repos/{repo}/commits?sha={branch}&per_page={int(limit)}",
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubReadError(f"gh returned non-JSON: {raw[:200]!r}") from exc
    # Defensive: gh api should return a list of commit dicts, but a
    # malformed response or an error body with HTTP 200 could land here.
    # Reject anything that does not match the expected shape.
    if not isinstance(data, list):
        return []
    out = []
    for c in data[:limit]:
        if not isinstance(c, dict):
            continue
        commit = c.get("commit") or {}
        author = commit.get("author") or {}
        # `or "?"` covers the case where the key exists but the value
        # is explicitly None -- without it, downstream str slicing
        # like `c['date'][:10]` would TypeError.
        out.append(
            {
                "sha": (c.get("sha") or "")[:12],
                "message": (commit.get("message") or "").split("\n", 1)[0][:200],
                "author": str(author.get("name") or "?"),
                "date": str(author.get("date") or "?"),
                "url": c.get("html_url") or "",
            }
        )
    return out
