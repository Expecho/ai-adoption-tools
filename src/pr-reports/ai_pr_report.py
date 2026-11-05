#!/usr/bin/env python3
"""
AI-assisted PR report for a GitHub organization or GitLab group.

Scans every repository in an org, classifies each pull request (and each of its
commits) as AI-assisted or not, and writes a self-contained HTML report with:
  * AI-assisted PRs per repository
  * % of PRs that are AI-assisted, per month
  * % of committed code (lines added in merged PRs) that is AI-generated, per month
  * a repository x month heatmap

Detected tools: Claude (Claude Code), Cursor, GitHub Copilot.

GitHub (default) needs the GitHub CLI (`gh auth login`). GitLab (`--source gitlab`, with
--org set to a group path such as `my-group` or `my-group/sub`) needs the GitLab CLI
(`glab auth login`, or a GITLAB_TOKEN with read_api); merge requests are treated as PRs.
No other dependencies.

Usage:
    python ai_pr_report.py --org my-org                  # last 12 months
    python ai_pr_report.py --source gitlab --org my-group   # a GitLab group (merge requests)
    python ai_pr_report.py --org my-org --since 2026-01-01           # only PRs created since
    python ai_pr_report.py --classify all              # classify purpose of every PR, not just AI ones
    python ai_pr_report.py --since all                  # everything (slow: rate limits)
    python ai_pr_report.py --repos RepoA RepoB          # subset of repos
    python ai_pr_report.py --out report.html --json prs.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import html
import json
import re
import hashlib
import shutil
import subprocess
import tempfile
import os
import sys
import threading
import time
from urllib.parse import quote
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict

# --------------------------------------------------------------------------- #
# Detection rules (all regexes are case-insensitive)
# --------------------------------------------------------------------------- #
#  author : PR author login           -> PR made by an agent, all its commits count as AI
#  branch : PR head branch name        -> PR made by an agent, all its commits count as AI
#  text   : PR description, commit message, commit author "name <email>"

TOOLS: dict[str, dict[str, list[str]]] = {
    "Claude": {
        "author": [r"^claude(\[bot\])?$"],
        "branch": [r"^claude/"],
        "text": [
            r"generated with \[?claude code",
            r"claude\.(ai|com)/(code|claude-code)",
            r"co-authored-by:\s*claude",
            r"noreply@anthropic\.com",
        ],
    },
    "GitHub Copilot": {
        "author": [r"^copilot(-swe-agent)?(\[bot\])?$"],
        "branch": [r"^copilot/"],
        "text": [
            r"co-authored-by:\s*(github\s*)?copilot",
            r"copilot-swe-agent",
            r"\d+\+copilot@users\.noreply\.github\.com",
        ],
    },
    "Cursor": {
        "author": [r"^cursor(agent)?(\[bot\])?$"],
        "branch": [r"^cursor/"],
        "text": [
            r"co-authored-by:\s*cursor",
            r"cursoragent@cursor\.com",
            r"made with cursor",
            r"cursor\.com/(agents|background-agent)",
        ],
    },
}

# Automation accounts whose PRs aren't developer work (dependency bumps etc.). Their PRs are
# excluded unless --include-bots is given; AI agents (Claude/Copilot/Cursor) are never excluded.
NON_AI_BOT_RE = re.compile(r"\[bot\]$|_bot_[0-9a-f]+$|^(dependabot|renovate|github-actions|snyk-bot|azure-pipelines)$", re.I)

COMPILED = {
    tool: {kind: [re.compile(p, re.I) for p in pats] for kind, pats in rules.items()}
    for tool, rules in TOOLS.items()
}


@dataclass
class PR:
    repo: str
    number: int
    title: str
    url: str
    state: str
    author: str
    created_at: str
    merged_at: str | None
    tools: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    lines_added: int = 0      # lines added by the PR's non-merge commits
    lines_added_ai: int = 0   # ...of which in AI-generated commits
    commits_truncated: bool = False
    outlier: bool = False     # too large to count towards the code share (see --max-pr-lines)
    branch: str = ""
    body_excerpt: str = ""
    commit_subjects: list[str] = field(default_factory=list)
    category: str | None = None  # kind of work, set by classify_purposes() (headless Claude)

    @property
    def ai(self) -> bool:
        return bool(self.tools)


def _match(rules: list[re.Pattern], text: str) -> bool:
    return any(r.search(text) for r in rules)


def classify(node: dict, repo: str) -> PR:
    author = (node.get("author") or {}).get("login") or "ghost"
    branch = node.get("headRefName") or ""
    body = node.get("body") or ""
    commits_conn = node.get("commits") or {}
    commits = [c["commit"] for c in commits_conn.get("nodes", [])]

    pr = PR(
        repo=repo,
        number=node["number"],
        title=node.get("title") or "",
        url=node["url"],
        state=node["state"],
        author=author,
        created_at=node["createdAt"],
        merged_at=node.get("mergedAt"),
        commits_truncated=(commits_conn.get("totalCount") or 0) > len(commits),
        branch=branch,
        body_excerpt=body[:800],
        commit_subjects=[(c.get("message") or "").split("\n", 1)[0][:120] for c in commits[:15]],
    )

    tools: set[str] = set()
    signals: list[str] = []
    agent_pr = False  # whole PR produced by an agent
    for tool, rules in COMPILED.items():
        if _match(rules["author"], author):
            tools.add(tool); agent_pr = True; signals.append(f"{tool}: PR author '{author}'")
        if _match(rules["branch"], branch):
            tools.add(tool); agent_pr = True; signals.append(f"{tool}: branch '{branch}'")
        if _match(rules["text"], body):
            tools.add(tool); signals.append(f"{tool}: marker in PR description")

    for c in commits:
        if (c.get("parents") or {}).get("totalCount", 1) > 1:
            continue  # skip merge commits (e.g. merging main into the branch)
        added = c.get("additions") or 0
        pr.lines_added += added
        msg = c.get("message") or ""
        a = c.get("author") or {}
        authors = [f'{a.get("name") or ""} <{a.get("email") or ""}>']
        commit_ai = agent_pr
        for tool, rules in COMPILED.items():
            if _match(rules["text"], msg):
                tools.add(tool); commit_ai = True; signals.append(f"{tool}: marker in commit message")
            if any(_match(rules["text"] + rules["author"], a) for a in authors):
                tools.add(tool); commit_ai = True; signals.append(f"{tool}: commit author")
        if commit_ai:
            pr.lines_added_ai += added

    if pr.commits_truncated:
        # Only the first 40 commits were inspected: scale to the PR's total additions.
        total = node.get("additions") or pr.lines_added
        share = (pr.lines_added_ai / pr.lines_added) if pr.lines_added else (1.0 if agent_pr else 0.0)
        pr.lines_added, pr.lines_added_ai = total, round(total * share)

    pr.tools = sorted(tools)
    pr.signals = list(dict.fromkeys(signals))
    return pr


# --------------------------------------------------------------------------- #
# GitHub access via gh CLI
# --------------------------------------------------------------------------- #

_rl_lock = threading.Lock()


def gh(args: list[str]) -> str:
    res = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {res.stderr.strip()}")
    return res.stdout


def wait_for_rate_limit() -> None:
    """Sleep until the GraphQL rate limit resets (only one thread waits/logs at a time)."""
    with _rl_lock:
        try:
            rl = json.loads(gh(["api", "graphql", "-f", "query={rateLimit{remaining resetAt}}"]))["data"]["rateLimit"]
            if rl["remaining"] > 50:
                return
            reset = dt.datetime.fromisoformat(rl["resetAt"].replace("Z", "+00:00"))
        except Exception:
            reset = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
        secs = max(5, (reset - dt.datetime.now(dt.timezone.utc)).total_seconds() + 5)
        print(f"  GraphQL rate limit reached; waiting {secs / 60:.0f} min until {reset:%H:%M} UTC...", file=sys.stderr)
        time.sleep(secs)


def list_repos(org: str, include_archived: bool, include_forks: bool) -> list[str]:
    # REST (not GraphQL) so listing doesn't consume the GraphQL rate-limit budget
    out = gh(["api", "--paginate", f"orgs/{org}/repos?per_page=100&type=all",
              "-q", ".[] | [.name, .archived, .fork] | @tsv"])
    repos = [line.split("	") for line in out.splitlines() if line.strip()]
    return sorted(
        name for name, archived, fork in repos
        if (include_archived or archived != "true") and (include_forks or fork != "true")
    )


PR_QUERY = """
query($owner: String!, $name: String!, $cursor: String, $first: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: $first, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url state createdAt mergedAt headRefName body additions
        author { login }
        commits(first: 40) {
          totalCount
          nodes { commit {
            message additions
            parents { totalCount }
            author { name email }
          } }
        }
      }
    }
  }
}
"""


def fetch_prs(org: str, repo: str, since: dt.datetime | None, cache_dir: str | None, cache_hours: float) -> list[PR]:
    since_key = f"{since:%Y-%m-%d}" if since else "all"
    prefix = "" if SOURCE == "github" else f"{SOURCE}__"
    fname = f"{prefix}{org}__{repo}__{since_key}.json".replace("/", "_")
    path = os.path.join(cache_dir, fname) if cache_dir else None
    if path and os.path.exists(path) and time.time() - os.path.getmtime(path) < cache_hours * 3600:
        with open(path, encoding="utf-8") as f:
            nodes = json.load(f)
    else:
        nodes = fetch_gitlab_nodes(org, repo) if SOURCE == "gitlab" else fetch_pr_nodes(org, repo, since)
        if path:
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(nodes, f)
    return [classify(n, repo) for n in nodes]


def fetch_pr_nodes(org: str, repo: str, since: dt.datetime | None) -> list[dict]:
    prs: list[dict] = []
    cursor = None
    page = 25
    while True:
        args = ["api", "graphql", "-f", f"query={PR_QUERY}", "-F", f"owner={org}", "-F", f"name={repo}"]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        for attempt in range(1000):
            try:
                conn = json.loads(gh(args + ["-F", f"first={page}"]))["data"]["repository"]["pullRequests"]
                break
            except RuntimeError as e:
                if re.search(r"rate limit", str(e), re.I):
                    wait_for_rate_limit()
                    continue
                # 502/504 = query too heavy or GitHub hiccup: retry with a smaller page
                if attempt >= 5 or not re.search(r"50[234]|timeout|timed out", str(e), re.I):
                    raise
                page = max(2, page // 2)
                time.sleep(2 * (attempt + 1))
        for node in conn["nodes"]:
            created = dt.datetime.fromisoformat(node["createdAt"].replace("Z", "+00:00"))
            if since and created < since:
                return prs
            prs.append(node)
        if not conn["pageInfo"]["hasNextPage"]:
            return prs
        cursor = conn["pageInfo"]["endCursor"]
        page = min(25, page + 5)  # recover gradually after a reduction


# --------------------------------------------------------------------------- #
# GitLab access via glab CLI (merge requests are normalised to the GitHub node shape)
# --------------------------------------------------------------------------- #

SOURCE = "github"  # set from --source
WEB_BASE = {"github": "https://github.com", "gitlab": "https://gitlab.com"}
_gl_projects: dict[str, list[dict]] = {}  # project path (relative to the group) -> merge requests


def glab(args: list[str], retries: int = 6) -> str:
    for attempt in range(retries):
        res = subprocess.run(["glab", *args], capture_output=True, text=True, encoding="utf-8")
        if res.returncode == 0:
            return res.stdout
        err = (res.stdout + res.stderr).strip()
        if attempt < retries - 1 and re.search(r"429|rate limit|50[234]|timed? ?out", err, re.I):
            time.sleep(5 * (attempt + 1))
            continue
        raise RuntimeError(f"glab {' '.join(args[:2])} failed: {err[:300]}")
    raise RuntimeError("unreachable")


def _json_values(text: str) -> list:
    """`glab api --paginate` prints one JSON array per page; merge them."""
    dec, i, out = json.JSONDecoder(), 0, []
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return out
        obj, i = dec.raw_decode(text, i)
        out.extend(obj) if isinstance(obj, list) else out.append(obj)


def load_gitlab_group(group: str, since: dt.datetime | None, include_archived: bool) -> list[str]:
    """List the group's merge requests in one call; this also tells us which projects have any."""
    query = (f"groups/{quote(group, safe='')}/merge_requests?per_page=100&state=all&scope=all"
             f"&order_by=created_at&sort=desc&non_archived={'false' if include_archived else 'true'}")
    if since:
        query += f"&created_after={since:%Y-%m-%dT%H:%M:%SZ}"
    prefix = group.lower() + "/"
    _gl_projects.clear()
    for mr in _json_values(glab(["api", "--paginate", query])):
        full = mr["references"]["full"].rsplit("!", 1)[0]
        rel = full[len(prefix):] if full.lower().startswith(prefix) else full
        _gl_projects.setdefault(rel, []).append(mr)
    return sorted(_gl_projects, key=str.lower)


def _gitlab_commit(project_id: int, c: dict) -> dict:
    merge = len(c.get("parent_ids") or []) > 1
    added = 0
    if not merge:  # merge commits are skipped by classify(); don't spend a call on them
        try:
            info = json.loads(glab(["api", f"projects/{project_id}/repository/commits/{c['id']}"]))
            added = (info.get("stats") or {}).get("additions") or 0
        except Exception:
            pass  # commit no longer reachable (squashed / branch deleted): count as 0 lines
    return {"commit": {
        "message": c.get("message") or c.get("title") or "",
        "additions": added,
        "parents": {"totalCount": 2 if merge else 1},
        "author": {"name": c.get("author_name"), "email": c.get("author_email")},
    }}


def _gitlab_node(mr: dict) -> dict:
    pid, iid = mr["project_id"], mr["iid"]
    commits = _json_values(glab(["api", f"projects/{pid}/merge_requests/{iid}/commits?per_page=100"]))
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        nodes = list(pool.map(lambda c: _gitlab_commit(pid, c), commits[:40]))
    state = {"opened": "OPEN", "merged": "MERGED"}.get(mr["state"], "CLOSED")
    merged_at = None
    if state == "MERGED":
        merged_at = mr.get("merged_at") or mr.get("closed_at") or mr.get("updated_at")
    return {
        "number": iid, "title": mr.get("title"), "url": mr["web_url"], "state": state,
        "createdAt": mr["created_at"], "mergedAt": merged_at, "headRefName": mr.get("source_branch"),
        "body": mr.get("description"),
        "additions": sum(n["commit"]["additions"] for n in nodes),
        "author": {"login": (mr.get("author") or {}).get("username")},
        "commits": {"totalCount": len(commits), "nodes": nodes},
    }


def fetch_gitlab_nodes(group: str, project: str) -> list[dict]:
    return [_gitlab_node(mr) for mr in _gl_projects.get(project, [])]


# --------------------------------------------------------------------------- #
# Purpose classification with headless Claude (`claude -p`)
# --------------------------------------------------------------------------- #

CATEGORIES: dict[str, tuple[str, str]] = {  # key: (label, description for the model)
    "feature":      ("New feature",      "adds new functionality, screens, endpoints or capabilities"),
    "enhancement":  ("Enhancement",      "improves or extends existing functionality, UX, or performance"),
    "bugfix":       ("Bug fix",          "fixes incorrect behaviour, errors, crashes, or regressions"),
    "refactor":     ("Refactoring",      "restructures or cleans up code without changing behaviour"),
    "tests":        ("Tests",            "adds or changes automated tests only"),
    "docs":         ("Documentation",    "documentation, READMEs, specs, ADRs, comments, agent instructions"),
    "infra":        ("Build / CI / infra", "pipelines, build config, packaging, deployment, infrastructure-as-code"),
    "dependencies": ("Dependencies",     "dependency or framework version bumps"),
    "chore":        ("Chore / other",    "releases, reverts, merges, config tweaks, anything else"),
}
CATEGORY_COLORS = {
    "feature": "#2f7ed8", "enhancement": "#8b5cf6", "bugfix": "#e5484d", "refactor": "#e0a100",
    "tests": "#10a37f", "docs": "#0ea5b7", "infra": "#6b7280", "dependencies": "#c2410c", "chore": "#a8a29e",
}
CLASSIFY_VERSION = "v1"  # bump to invalidate cached classifications after changing the prompt/categories
CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "category": {"type": "string", "enum": list(CATEGORIES)}},
        "required": ["id", "category"]}}},
    "required": ["items"],
}


def _classify_prompt(batch: list[PR]) -> str:
    cats = "\n".join(f"- {k}: {d}" for k, (_, d) in CATEGORIES.items())
    def one(i: int, p: PR) -> str:
        desc = re.sub(r"\s+", " ", p.body_excerpt)[:500] or "(none)"
        commits = " ; ".join(p.commit_subjects[:10]) or "(none)"
        return f"[{i}] repo: {p.repo} | branch: {p.branch}\ntitle: {p.title}\ndescription: {desc}\ncommits: {commits}"
    prs = "\n\n".join(one(i, p) for i, p in enumerate(batch))
    return (
        "Classify each pull/merge request below by the main kind of work it contains. "
        "Text may be in Dutch or English. Pick exactly one category per PR; when a PR mixes kinds, "
        "pick the dominant one.\n\nCategories:\n" + cats +
        "\n\nReturn one item per PR, using the number in brackets as id.\n\n" + prs
    )


def _run_claude(prompt: str, model: str) -> tuple[dict[int, str], float]:
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("`claude` CLI not found on PATH")
    res = subprocess.run(
        [exe, "-p", "--model", model, "--output-format", "json", "--no-session-persistence",
         "--tools", "", "--json-schema", json.dumps(CLASSIFY_SCHEMA)],
        input=prompt, capture_output=True, text=True, encoding="utf-8",
        cwd=tempfile.gettempdir(),  # neutral dir: don't pick up a project's CLAUDE.md
        timeout=600,
    )
    if res.returncode != 0:
        raise RuntimeError(f"claude -p failed: {(res.stderr or res.stdout).strip()[:300]}")
    out = json.loads(res.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude -p error: {str(out.get('result'))[:300]}")
    items = (out.get("structured_output") or {}).get("items", [])
    return {int(it["id"]): it["category"] for it in items if it.get("category") in CATEGORIES}, out.get("total_cost_usd") or 0.0


def classify_purposes(prs: list[PR], mode: str, model: str, cache_dir: str | None,
                      workers: int, batch_size: int = 40) -> None:
    """Set PR.category using headless Claude. mode: 'ai' (AI-assisted PRs only), 'all', or 'none'."""
    if mode == "none":
        return
    targets = [p for p in prs if mode == "all" or p.ai]
    key = lambda p: f"{p.repo}#{p.number}"
    fingerprint = lambda p: hashlib.sha1(
        f"{CLASSIFY_VERSION}|{model}|{p.title}|{p.body_excerpt}|{p.commit_subjects}".encode()).hexdigest()[:16]

    path = os.path.join(cache_dir, "classifications.json") if cache_dir else None
    cache: dict[str, dict] = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    todo = []
    for p in targets:
        hit = cache.get(key(p))
        if hit and hit.get("fp") == fingerprint(p):
            p.category = hit["category"]
        else:
            todo.append(p)
    print(f"Classifying PR purpose with claude -p ({model}): {len(targets) - len(todo)} cached, {len(todo)} to do...",
          file=sys.stderr)
    if not todo:
        return

    lock = threading.Lock()
    cost = 0.0
    batches = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]

    def work(batch: list[PR]) -> int:
        nonlocal cost
        pending = batch
        for _ in range(3):  # retry PRs the model skipped / failed batches
            if not pending:
                break
            try:
                got, c = _run_claude(_classify_prompt(pending), model)
            except Exception as e:
                print(f"  classify batch failed, retrying: {e}", file=sys.stderr)
                time.sleep(5)
                continue
            with lock:
                cost += c
                for i, p in enumerate(pending):
                    if i in got:
                        p.category = got[i]
                        cache[key(p)] = {"category": got[i], "fp": fingerprint(p)}
            pending = [p for p in pending if p.category is None]
        with lock:
            if path:
                os.makedirs(cache_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
        return len(batch) - len(pending)

    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for i, n in enumerate(pool.map(work, batches), 1):
            done += n
            print(f"  [{i}/{len(batches)}] classified {done}/{len(todo)} (cost so far ${cost:.2f})", file=sys.stderr)
    print(f"Classification done: {done}/{len(todo)} PRs, ${cost:.2f}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #

@dataclass
class Bucket:
    prs: int = 0
    ai_prs: int = 0
    lines: int = 0      # lines added in PRs merged this month
    ai_lines: int = 0

    def pr_pct(self) -> float | None:
        return 100.0 * self.ai_prs / self.prs if self.prs else None

    def code_pct(self) -> float | None:
        return 100.0 * self.ai_lines / self.lines if self.lines else None


def monthly(prs: list[PR]) -> dict[str, Bucket]:
    """PR share is bucketed by creation month; code share by merge month (merged PRs only)."""
    out: dict[str, Bucket] = defaultdict(Bucket)
    for p in prs:
        b = out[p.created_at[:7]]
        b.prs += 1
        b.ai_prs += p.ai
        if p.merged_at and not p.outlier:
            m = out[p.merged_at[:7]]
            m.lines += p.lines_added
            m.ai_lines += p.lines_added_ai
    return out


def month_range(first: str, last: str) -> list[str]:
    y, m = map(int, first.split("-"))
    ly, lm = map(int, last.split("-"))
    res = []
    while (y, m) <= (ly, lm):
        res.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return res


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #

TOOL_COLORS = {"Claude": "#d97757", "GitHub Copilot": "#2f7ed8", "Cursor": "#10a37f"}
PR_COLOR, CODE_COLOR = "#d97757", "#2f7ed8"


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def fmt_pct(v: float | None) -> str:
    return "–" if v is None else f"{v:.1f}%"


def pct(a: float, b: float) -> float:
    return 100.0 * a / b if b else 0.0


def line_chart(months: list[str], data: dict[str, Bucket]) -> str:
    """Two-series % line chart: AI-assisted PR share and AI-generated code share."""
    if not months:
        return '<span class="muted">No data</span>'
    w, h, l, r, t, b = 960, 280, 40, 12, 12, 28
    pw, ph = w - l - r, h - t - b
    step = pw / max(len(months) - 1, 1)
    x = lambda i: l + i * step if len(months) > 1 else l + pw / 2
    y = lambda v: t + ph * (1 - v / 100)

    parts = []
    for g in (0, 25, 50, 75, 100):
        parts.append(f'<line x1="{l}" x2="{w - r}" y1="{y(g):.1f}" y2="{y(g):.1f}" class="grid"/>'
                     f'<text x="{l - 6}" y="{y(g) + 3:.1f}" class="axis" text-anchor="end">{g}%</text>')
    label_every = max(1, len(months) // 12)
    for i, m in enumerate(months):
        if i % label_every == 0 or i == len(months) - 1:
            parts.append(f'<text x="{x(i):.1f}" y="{h - 8}" class="axis" text-anchor="middle">{m[2:]}</text>')

    for key, color, label in (("pr_pct", PR_COLOR, "AI-assisted PRs"), ("code_pct", CODE_COLOR, "AI-generated code")):
        pts = [(i, getattr(data[m], key)()) for i, m in enumerate(months)]
        segs, cur = [], []
        for i, v in pts:  # break the line on months without data
            if v is None:
                if cur: segs.append(cur); cur = []
            else:
                cur.append(f"{x(i):.1f},{y(v):.1f}")
        if cur: segs.append(cur)
        for s in segs:
            parts.append(f'<polyline points="{" ".join(s)}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round"/>')
        for i, v in pts:
            if v is not None:
                d = data[months[i]]
                tip = (f"{months[i]} · {label}: {v:.1f}% "
                       + (f"({d.ai_prs} of {d.prs} PRs)" if key == "pr_pct" else f"({d.ai_lines:,} of {d.lines:,} lines)"))
                parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="4" fill="{color}" class="pt"><title>{esc(tip)}</title></circle>')
    return f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="AI share per month">{"".join(parts)}</svg>'


def heat_cell(v: float | None, n: int) -> str:
    if v is None:
        return '<td class="hm empty"></td>'
    alpha = 0.08 + 0.92 * v / 100
    fg = "#fff" if v >= 55 else "inherit"
    return (f'<td class="hm" style="background:rgba(217,119,87,{alpha:.2f});color:{fg}" '
            f'title="{v:.0f}% of {n} PRs">{v:.0f}</td>')


def cat_badge(c: str | None) -> str:
    if not c:
        return ""
    return (f'<span class="badge" style="border-color:{CATEGORY_COLORS[c]};color:{CATEGORY_COLORS[c]}">'
            f'{esc(CATEGORIES[c][0])}</span> ')


def stacked_month_chart(months: list[str], counts: dict[str, Counter]) -> str:
    """Stacked bars: AI-assisted PRs per month, split by category."""
    if not months:
        return '<span class="muted">No data</span>'
    w, h, l, r, t, b = 960, 260, 40, 12, 12, 28
    pw, ph = w - l - r, h - t - b
    mx = max((sum(counts[m].values()) for m in months), default=0) or 1
    bw = pw / len(months)
    parts = []
    for g in range(5):
        v = mx * g / 4
        y = t + ph * (1 - g / 4)
        parts.append(f'<line x1="{l}" x2="{w - r}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                     f'<text x="{l - 6}" y="{y + 3:.1f}" class="axis" text-anchor="end">{v:.0f}</text>')
    label_every = max(1, len(months) // 12)
    for i, m in enumerate(months):
        x = l + i * bw
        y = t + ph
        tot = sum(counts[m].values())
        for c in CATEGORIES:
            n = counts[m].get(c, 0)
            if not n:
                continue
            hh = ph * n / mx
            y -= hh
            parts.append(f'<rect x="{x + 3:.1f}" y="{y:.1f}" width="{bw - 6:.1f}" height="{hh:.1f}" fill="{CATEGORY_COLORS[c]}">'
                         f'<title>{m} · {esc(CATEGORIES[c][0])}: {n} of {tot} AI-assisted PRs ({pct(n, tot):.0f}%)</title></rect>')
        if i % label_every == 0 or i == len(months) - 1:
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{h - 8}" class="axis" text-anchor="middle">{m[2:]}</text>')
    return f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="AI-assisted PRs per month by purpose">{"".join(parts)}</svg>'


def render_purpose(prs: list[PR], max_pr_lines: int) -> tuple[str, str]:
    """Returns (purpose table section, purpose-per-month chart section)."""
    ai = [p for p in prs if p.ai and p.category]
    if not ai:
        return "", ""
    human = [p for p in prs if not p.ai and p.category]
    ai_c, hu_c = Counter(p.category for p in ai), Counter(p.category for p in human)
    all_c = Counter(p.category for p in prs if p.category)
    ai_lines = Counter()
    for p in ai:
        if p.merged_at and not p.outlier:
            ai_lines[p.category] += p.lines_added_ai
    tot_lines = sum(ai_lines.values())

    rows = []
    for c in sorted(CATEGORIES, key=lambda c: -ai_c[c]):
        share = pct(ai_c[c], len(ai))
        rows.append(
            f'<tr><td><span class="chip"><i style="background:{CATEGORY_COLORS[c]}"></i>{esc(CATEGORIES[c][0])}</span></td>'
            f'<td class="num">{ai_c[c]:,}</td>'
            f'<td><div class="hbar"><span style="width:{share:.2f}%;background:{CATEGORY_COLORS[c]}"></span></div></td>'
            f'<td class="num"><b>{share:.1f}%</b></td>'
            + (f'<td class="num">{pct(hu_c[c], len(human)):.1f}%</td>'
               f'<td class="num">{fmt_pct(pct(ai_c[c], all_c[c]) if all_c[c] else None)}</td>' if human else "")
            + f'<td class="num">{ai_lines[c]:,}</td><td class="num">{pct(ai_lines[c], tot_lines):.1f}%</td></tr>'
        )
    human_head = ('<th class="num">% of non-AI PRs</th><th class="num" title="Share of all PRs of this kind that are AI-assisted">Share of this kind of work done with AI</th>'
                  if human else "")

    by_month: dict[str, Counter] = defaultdict(Counter)
    for p in ai:
        by_month[p.created_at[:7]][p.category] += 1
    keys = sorted(by_month)
    months = month_range(keys[0], keys[-1]) if keys else []
    legend = " ".join(f'<span class="chip"><i style="background:{CATEGORY_COLORS[c]}"></i>{esc(l)}</span>'
                      for c, (l, _) in CATEGORIES.items())
    note = ("" if human else
            '<p class="muted">Only AI-assisted PRs were classified. Run with <code>--classify all</code> '
            'to compare against PRs without AI assistance.</p>')
    table = f"""
<h2>What AI is used for <span class="muted" style="font-weight:400">— purpose of each PR, classified by Claude</span></h2>
<div class="panel">
  <table>
    <thead><tr><th>Purpose</th><th class="num">AI PRs</th><th style="width:30%"></th><th class="num">% of AI PRs</th>{human_head}
    <th class="num">AI lines added</th><th class="num">% of AI lines</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>{note}
</div>"""
    chart = f"""
<h2>AI-assisted PRs per month by purpose</h2>
<div class="panel">
  <div style="margin-bottom:6px">{legend}</div>
  {stacked_month_chart(months, by_month)}
</div>"""
    return table, chart


def render_html(org: str, prs: list[PR], repos: list[str], since: dt.datetime | None,
                errors: dict[str, str], heat_months: int, max_pr_lines: int, include_bots: bool = False) -> str:
    by_repo: dict[str, list[PR]] = defaultdict(list)
    for p in prs:
        by_repo[p.repo].append(p)

    tool_totals = Counter(t for p in prs for t in p.tools)
    purpose_table, purpose_chart = render_purpose(prs, max_pr_lines)
    total, ai_total = len(prs), sum(p.ai for p in prs)
    merged = [p for p in prs if p.merged_at and not p.outlier]
    outliers = [p for p in prs if p.merged_at and p.outlier]
    lines, ai_lines = sum(p.lines_added for p in merged), sum(p.lines_added_ai for p in merged)
    active_repos = sum(1 for r in repos if by_repo.get(r))
    ai_repos = sum(1 for r in repos if any(p.ai for p in by_repo.get(r, [])))

    org_month = monthly(prs)
    keys = sorted(org_month)
    months = month_range(keys[0], keys[-1]) if keys else []
    if since:
        months = [m for m in months if m >= f"{since:%Y-%m}"]
    for m in months:
        org_month[m]  # materialise empty buckets

    # Monthly table (newest first)
    month_rows = "".join(
        f'<tr><td>{m}</td><td class="num">{d.ai_prs:,}</td><td class="num">{d.prs:,}</td>'
        f'<td class="num"><b>{fmt_pct(d.pr_pct())}</b></td>'
        f'<td class="num">{d.ai_lines:,}</td><td class="num">{d.lines:,}</td>'
        f'<td class="num"><b>{fmt_pct(d.code_pct())}</b></td></tr>'
        for m in reversed(months) for d in [org_month[m]]
    )

    # Heatmap: repo x month, AI-assisted PR share
    hm_months = months[-heat_months:]
    repo_month = {r: monthly(by_repo[r]) for r in repos if by_repo.get(r)}
    hm_rows = []
    for r in sorted(repo_month, key=lambda r: (-sum(p.ai for p in by_repo[r]), r.lower())):
        rm = repo_month[r]
        if not any(rm.get(m, Bucket()).prs for m in hm_months):
            continue
        cells = "".join(heat_cell(rm[m].pr_pct() if m in rm else None, rm[m].prs if m in rm else 0) for m in hm_months)
        hm_rows.append(f'<tr><th class="rh">{esc(r)}</th>{cells}</tr>')
    hm_head = "".join(f'<th class="mh">{m[2:]}</th>' for m in hm_months)

    # Repo rows
    rows = []
    for r in sorted(repos, key=lambda r: (-sum(p.ai for p in by_repo.get(r, [])), r.lower())):
        rp = by_repo.get(r, [])
        n, a = len(rp), sum(p.ai for p in rp)
        rl = sum(p.lines_added for p in rp if p.merged_at and not p.outlier)
        ral = sum(p.lines_added_ai for p in rp if p.merged_at and not p.outlier)
        tc = Counter(t for p in rp for t in p.tools)
        stack = "".join(
            f'<span style="width:{pct(c, n):.2f}%;background:{TOOL_COLORS[t]}" title="{esc(t)}: {c}"></span>'
            for t, c in tc.most_common()
        )
        chips = " ".join(
            f'<span class="chip"><i style="background:{TOOL_COLORS[t]}"></i>{esc(t)} {c}</span>' for t, c in tc.most_common()
        )
        rc = Counter(p.category for p in rp if p.ai and p.category)
        nc = sum(rc.values())  # classified AI PRs: shares below add up to 100%
        ordered = sorted(rc.items(), key=lambda kv: (-kv[1], list(CATEGORIES).index(kv[0])))
        full = " · ".join(f"{CATEGORIES[c][0]} {pct(c2, nc):.0f}% ({c2})" for c, c2 in ordered)
        uses = (
            f'<div title="{esc(full)}"><div class="stack">'
            + "".join(f'<span style="width:{pct(c2, nc):.2f}%;background:{CATEGORY_COLORS[c]}" '
                      f'title="{esc(CATEGORIES[c][0])}: {pct(c2, nc):.0f}% ({c2} AI PRs)"></span>' for c, c2 in ordered)
            + '</div><div class="chips">'
            + " ".join(f'<span class="chip"><i style="background:{CATEGORY_COLORS[c]}"></i>{esc(CATEGORIES[c][0])} {pct(c2, nc):.0f}%</span>'
                       for c, c2 in ordered)
            + "</div></div>"
        ) if nc else ""
        details = ""
        if a:
            items = "".join(
                f'<li>{cat_badge(p.category)}<a href="{esc(p.url)}" target="_blank" rel="noopener">#{p.number} {esc(p.title)}</a>'
                f' <span class="muted">· {esc(p.author)} · {p.created_at[:10]} · {"merged" if p.merged_at else p.state.lower()}'
                f' · {p.lines_added_ai:,}/{p.lines_added:,} lines AI</span>'
                f'<div class="sig">{esc("; ".join(p.signals))}</div></li>'
                for p in sorted(rp, key=lambda p: p.created_at, reverse=True) if p.ai
            )
            details = f'<tr class="details" hidden><td colspan="7"><ul>{items}</ul></td></tr>'
        err = f' <span class="err" title="{esc(errors[r])}">⚠ fetch failed</span>' if r in errors else ""
        code_pct = pct(ral, rl)
        rows.append(
            f'<tr class="repo{" has-ai" if a else ""}" data-repo="{esc(r.lower())}" data-total="{n}" data-ai="{a}" '
            f'data-pct="{pct(a, n):.4f}" data-code="{code_pct:.4f}">'
            f'<td><span class="caret">{"▸" if a else ""}</span><a href="{WEB_BASE[SOURCE]}/{esc(org)}/{esc(r)}" target="_blank" rel="noopener">{esc(r)}</a>{err}</td>'
            f'<td class="num">{a}</td><td class="num">{n}</td><td class="num">{pct(a, n):.1f}%</td>'
            f'<td class="num" title="{ral:,} of {rl:,} lines added">{fmt_pct(code_pct if rl else None)}</td>'
            f'<td><div class="stack">{stack}</div><div class="chips">{chips}</div></td><td>{uses}</td></tr>{details}'
        )

    legend = " ".join(
        f'<span class="chip"><i style="background:{TOOL_COLORS[t]}"></i>{esc(t)} <b>{c:,}</b> PRs</span>'
        for t, c in tool_totals.most_common()
    ) or '<span class="muted">No AI-assisted PRs detected.</span>'

    period = f"PRs created since {since:%Y-%m-%d}" if since else "All PRs (all time)"
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    rules_html = "".join(
        f"<li><b>{esc(t)}</b> — PR author: <code>{esc(', '.join(r['author']))}</code>; branch: <code>{esc(', '.join(r['branch']))}</code>; "
        f"description / commit message / commit author: <code>{esc(' | '.join(r['text']))}</code></li>"
        for t, r in TOOLS.items()
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI-assisted PRs · {esc(org)}</title>
<style>
:root {{ --bg:#f7f7f5; --card:#fff; --fg:#1d1d1b; --muted:#6b6b66; --line:#e4e4df; --track:#ececE8; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161615; --card:#1f1f1d; --fg:#ecece8; --muted:#9a9a93; --line:#2f2f2c; --track:#2a2a27; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif }}
main {{ max-width:1200px; margin:0 auto; padding:32px 16px 64px }}
h1 {{ font-size:26px; margin:0 0 4px }} h2 {{ font-size:16px; margin:36px 0 12px }}
.muted {{ color:var(--muted) }} code {{ font-size:11px }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin-top:20px }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px }}
.card .v {{ font-size:26px; font-weight:650; font-variant-numeric:tabular-nums }}
.card .l {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em }}
.panel {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; overflow-x:auto }}
.chart {{ width:100%; min-width:560px; height:auto; display:block }}
.grid {{ stroke:var(--line) }} .axis {{ fill:var(--muted); font-size:11px }} .pt {{ cursor:default }}
.key {{ display:flex; gap:18px; margin-bottom:8px; font-size:13px }}
.key i {{ display:inline-block; width:14px; height:3px; border-radius:2px; vertical-align:middle; margin-right:6px }}
.chip {{ display:inline-flex; align-items:center; gap:5px; font-size:12px; margin:2px 8px 2px 0; white-space:nowrap }}
.chip i {{ width:9px; height:9px; border-radius:2px; display:inline-block }}
.toolbar {{ display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin-bottom:10px }}
input[type=search] {{ padding:7px 10px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg); min-width:240px }}
table {{ width:100%; border-collapse:collapse }}
th {{ text-align:left; font-weight:600; color:var(--muted); font-size:12px; padding:8px; border-bottom:1px solid var(--line); white-space:nowrap }}
#t th[data-k] {{ cursor:pointer; user-select:none }}
th.sorted::after {{ content:" ▾" }} th.sorted.asc::after {{ content:" ▴" }}
td {{ padding:8px; border-bottom:1px solid var(--line); vertical-align:top }}
td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap }}
tr.has-ai {{ cursor:pointer }} tr.has-ai:hover {{ background:color-mix(in srgb,#d97757 6%,transparent) }}
.caret {{ display:inline-block; width:14px; color:var(--muted) }}
tr.open .caret {{ transform:rotate(90deg) }}
a {{ color:inherit }} td a {{ text-decoration:none }} td a:hover {{ text-decoration:underline }}
.stack {{ display:flex; height:8px; background:var(--track); border-radius:4px; overflow:hidden; min-width:140px }}
.stack span {{ height:100% }}
.chips {{ margin-top:4px }}
tr.details td {{ background:color-mix(in srgb,var(--track) 40%,transparent) }}
tr.details ul {{ margin:0; padding-left:18px }} tr.details li {{ margin:6px 0 }}
.sig {{ font-size:12px; color:var(--muted) }}
.badge {{ display:inline-block; font-size:11px; border:1px solid; border-radius:10px; padding:0 7px; margin-right:4px; white-space:nowrap }}
.hbar {{ height:10px; background:var(--track); border-radius:5px; overflow:hidden }} .hbar span {{ display:block; height:100% }}
.err {{ color:#e5484d; font-size:12px }}
.heat {{ width:auto; border-collapse:separate; border-spacing:2px }}
.heat th.rh {{ font-weight:500; color:var(--fg); padding:2px 10px 2px 0; border:0; max-width:280px; overflow:hidden; text-overflow:ellipsis }}
.heat th.mh {{ border:0; padding:2px; text-align:center; font-weight:500 }}
td.hm {{ width:38px; min-width:38px; text-align:center; font-size:11px; padding:4px 2px; border:0; border-radius:3px; font-variant-numeric:tabular-nums }}
td.hm.empty {{ background:var(--track); opacity:.5 }}
.scroll {{ max-height:420px; overflow:auto }}
details {{ margin-top:32px }} details li {{ font-size:12px; margin:6px 0; word-break:break-word }}
</style></head>
<body><main>
<h1>AI-assisted pull requests</h1>
<div class="muted">{esc(org)} · {period} · {"incl." if include_bots else "excl."} PRs by automation bots (Dependabot etc.) · generated {generated}</div>

<div class="cards">
  <div class="card"><div class="l">AI-assisted PRs</div><div class="v">{pct(ai_total, total):.1f}%</div><div class="muted">{ai_total:,} of {total:,} PRs</div></div>
  <div class="card"><div class="l">AI-generated code</div><div class="v">{pct(ai_lines, lines):.1f}%</div><div class="muted">{ai_lines:,} of {lines:,} lines added (merged{f", excl. {len(outliers)} PRs over {max_pr_lines:,} lines" if outliers else ""})</div></div>
  <div class="card"><div class="l">Repos using AI</div><div class="v">{ai_repos}</div><div class="muted">of {active_repos} repos with PRs</div></div>
  <div class="card"><div class="l">Repos scanned</div><div class="v">{len(repos)}</div><div class="muted">{len(errors)} failed</div></div>
</div>

<h2>By tool</h2>
<div class="panel">{legend}</div>

<h2>AI share per month</h2>
<div class="panel">
  <div class="key"><span><i style="background:{PR_COLOR}"></i>% of PRs AI-assisted (by created month)</span>
  <span><i style="background:{CODE_COLOR}"></i>% of committed code AI-generated (lines added, by merge month)</span></div>
  {line_chart(months, org_month)}
</div>

{purpose_table}

<h2>Monthly figures</h2>
<div class="panel scroll">
  <table>
    <thead><tr><th>Month</th><th class="num">AI PRs</th><th class="num">All PRs</th><th class="num">% PRs AI</th>
    <th class="num">AI lines</th><th class="num">All lines</th><th class="num">% code AI</th></tr></thead>
    <tbody>{month_rows}</tbody>
  </table>
</div>

{purpose_chart}

<h2>% of PRs AI-assisted per repository per month <span class="muted" style="font-weight:400">— last {len(hm_months)} months, hover for counts</span></h2>
<div class="panel"><table class="heat"><thead><tr><th></th>{hm_head}</tr></thead><tbody>{''.join(hm_rows)}</tbody></table></div>

<h2>Per repository</h2>
<div class="panel">
  <div class="toolbar">
    <input type="search" id="q" placeholder="Filter repositories">
    <label><input type="checkbox" id="onlyAi"> only repos with AI-assisted PRs</label>
    <span class="muted">Click a row to see its AI-assisted PRs.</span>
  </div>
  <table id="t">
    <thead><tr><th data-k="repo">Repository</th><th data-k="ai" class="num sorted">AI PRs</th><th data-k="total" class="num">All PRs</th>
    <th data-k="pct" class="num">% PRs AI</th><th data-k="code" class="num">% code AI</th><th>Tools</th><th>AI use by purpose <span style="font-weight:400">(% of the repo's AI PRs)</span></th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>

<details><summary class="muted">How this is measured</summary>
<ul>
<li><b>AI-assisted PR</b>: the PR author, branch name, description, or any commit message / commit author matches one of the patterns below. PRs are counted in the month they were <i>created</i>.</li>
<li><b>AI-generated code</b>: lines added by individual commits of <i>merged</i> PRs, counted in the month the PR was merged. A commit counts as AI-generated when its message or author carries an AI marker, or when the whole PR was opened by an agent (agent author or <code>claude/</code>, <code>copilot/</code>, <code>cursor/</code> branch). Merge commits are excluded; merged PRs adding more than {max_pr_lines:,} lines ({len(outliers)} PRs, {sum(p.lines_added for p in outliers):,} lines — typically generated, vendored or re-encoded files) are left out of the code share; only the first 40 commits of a PR are inspected (larger PRs are extrapolated to their total additions); lines added directly to a branch outside a PR are not included.</li>
<li><b>Purpose</b>: each PR's title, branch, description excerpt and commit subjects are sent to headless Claude (<code>claude -p</code>), which picks one category: {esc(", ".join(f"{l} ({d})" for l, d in CATEGORIES.values()))}.</li>
<li>AI help that leaves no trace (e.g. inline completions, or tools configured not to add co-author trailers) can't be detected, so all numbers are a lower bound.</li>
{rules_html}
</ul></details>
</main>
<script>
const tbody = document.querySelector('#t tbody');
const pairs = () => [...tbody.querySelectorAll('tr.repo')].map(r => [r, r.nextElementSibling && r.nextElementSibling.classList.contains('details') ? r.nextElementSibling : null]);
tbody.addEventListener('click', e => {{
  if (e.target.closest('a')) return;
  const row = e.target.closest('tr.has-ai'); if (!row) return;
  row.classList.toggle('open'); row.nextElementSibling.hidden = !row.classList.contains('open');
}});
let sortKey = 'ai', asc = false;
document.querySelectorAll('#t th[data-k]').forEach(th => th.addEventListener('click', () => {{
  const k = th.dataset.k; asc = (k === sortKey) ? !asc : k === 'repo'; sortKey = k;
  document.querySelectorAll('#t th').forEach(x => x.classList.remove('sorted', 'asc'));
  th.classList.add('sorted'); if (asc) th.classList.add('asc');
  const val = r => k === 'repo' ? r.dataset.repo : parseFloat(r.dataset[k]);
  pairs().sort((a, b) => {{ const x = val(a[0]), y = val(b[0]); return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1); }})
    .forEach(([r, d]) => {{ tbody.appendChild(r); if (d) tbody.appendChild(d); }});
}}));
const filter = () => {{
  const q = document.getElementById('q').value.toLowerCase(), only = document.getElementById('onlyAi').checked;
  pairs().forEach(([r, d]) => {{
    const show = r.dataset.repo.includes(q) && (!only || r.dataset.ai > 0);
    r.hidden = !show; if (d) d.hidden = !show || !r.classList.contains('open');
  }});
}};
document.getElementById('q').addEventListener('input', filter);
document.getElementById('onlyAi').addEventListener('change', filter);
</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Report AI-assisted PRs per repository of a GitHub org or GitLab group.")
    ap.add_argument("--source", choices=["github", "gitlab"], default="github",
                    help="Where to read from (default github). For gitlab, merge requests are treated as PRs")
    ap.add_argument("--org", required=True, help="GitHub organization, or GitLab group path (e.g. my-group/sub)")
    ap.add_argument("--since", default=None,
                    help="Only PRs created on/after this date (YYYY-MM-DD), or 'all'. Default: 12 months ago")
    ap.add_argument("--cache-dir", default=".cache", help="Per-repo cache of fetched PRs (default .cache; '' disables)")
    ap.add_argument("--cache-hours", type=float, default=12, help="Reuse cached repo data younger than this (default 12)")
    ap.add_argument("--repos", nargs="*", help="Limit to these repositories")
    ap.add_argument("--include-archived", action="store_true")
    ap.add_argument("--include-forks", action="store_true")
    ap.add_argument("--include-bots", action="store_true",
                    help="Keep PRs opened by automation bots such as Dependabot/Renovate (excluded by default)")
    ap.add_argument("--max-pr-lines", type=int, default=10000,
                    help="Leave merged PRs adding more lines than this out of the code share (default 10000; 0 = no limit)")
    ap.add_argument("--heatmap-months", type=int, default=12, help="Months shown in the repo heatmap (default 12)")
    ap.add_argument("--workers", type=int, default=4, help="Parallel repo fetches / claude calls (default 4)")
    ap.add_argument("--classify", choices=["ai", "all", "none"], default="ai",
                    help="Classify PR purpose with headless Claude: AI-assisted PRs only (default), all PRs "
                         "(adds a comparison with non-AI PRs), or none")
    ap.add_argument("--classify-model", default="haiku", help="Model for classification (default haiku)")
    ap.add_argument("--out", default="ai-prs.html", help="HTML output path (default ai-prs.html)")
    ap.add_argument("--json", help="Also write the classified PR data to this JSON file")
    args = ap.parse_args()

    if args.since is None:
        today = dt.date.today()
        args.since = f"{today.year - 1:04d}-{today.month:02d}-01"
    since = None if args.since == "all" else dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.timezone.utc)
    global SOURCE
    SOURCE = args.source
    if SOURCE == "gitlab":
        repos = load_gitlab_group(args.org, since, args.include_archived)
        if args.repos:
            repos = [r for r in repos if r in args.repos or r.rsplit("/", 1)[-1] in args.repos]
    else:
        repos = args.repos or list_repos(args.org, args.include_archived, args.include_forks)
    print(f"Scanning {len(repos)} repositories in {args.org}...", file=sys.stderr)

    prs: list[PR] = []
    errors: dict[str, str] = {}
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(fetch_prs, args.org, r, since, args.cache_dir or None, args.cache_hours): r for r in repos}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            r = futs[fut]
            try:
                got = fut.result()
                prs.extend(got)
                print(f"  [{i}/{len(repos)}] {r}: {sum(p.ai for p in got)}/{len(got)} AI-assisted", file=sys.stderr)
            except Exception as e:  # keep going; the failure is shown in the HTML
                errors[r] = str(e)
                print(f"  [{i}/{len(repos)}] {r}: ERROR {e}", file=sys.stderr)

    if not args.include_bots:
        before = len(prs)
        prs = [p for p in prs if p.ai or not NON_AI_BOT_RE.search(p.author)]
        print(f"Excluded {before - len(prs)} PRs opened by automation bots (use --include-bots to keep them)", file=sys.stderr)
    for p in prs:
        p.outlier = bool(args.max_pr_lines) and p.lines_added > args.max_pr_lines
    classify_purposes(prs, args.classify, args.classify_model, args.cache_dir or None, args.workers)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(render_html(args.org, prs, repos, since, errors, args.heatmap_months, args.max_pr_lines, args.include_bots))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([asdict(p) | {"ai": p.ai} for p in prs], f, indent=2)

    ai = sum(p.ai for p in prs)
    print(f"Done: {ai}/{len(prs)} PRs AI-assisted. Report written to {args.out}", file=sys.stderr)
    return 1 if errors and not prs else 0


if __name__ == "__main__":
    sys.exit(main())
