#!/usr/bin/env python3
"""
Azure DevOps user story report: how long did done stories sit in each state, and were they
solved with AI?

For every work item of the chosen type that reached `Done` in the period, the tool reads the
state history and computes the time spent in every state and in three phases:
  * created   -> time in "Proposed"-category states (New, Approved, Refined, ...) before work started
  * committed -> time in "InProgress"-category states (Committed, Testable, Acceptable, ...)
  * lead time -> creation until the story reached Done for the last time
A story counts as solved with AI when one of its linked pull / merge requests is AI-assisted
according to the JSON written by pr-reports/ai_pr_report.py (--json). Stories without a linked
PR found in that data are reported separately as "no PR data", not as "not AI".

Authentication: your Azure CLI login (`az login`; the token is requested for Azure DevOps), or a
personal access token in the ADO_PAT environment variable. No other dependencies.

Usage:
    python ado_story_report.py                                  # org/project from `az devops configure`
    python ado_story_report.py --org ibissoftware --project Ibis --since 2026-01-01
    python ado_story_report.py --prs ../pr-reports/ai-prs.json --out ai-stories.html --json ai-stories.json
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field, asdict

ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"  # Azure DevOps application id for Entra tokens
API = "api-version=7.1"
PHASE_NAMES = {"Proposed": "created", "InProgress": "committed"}
HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
# Azure DevOps access (REST, bearer token from the az CLI or a PAT)
# --------------------------------------------------------------------------- #

def _az(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["az.cmd" if os.name == "nt" else "az", *args], capture_output=True, text=True, encoding="utf-8")


class Ado:
    def __init__(self, org: str, tenant: str | None = None) -> None:
        self.base = f"https://dev.azure.com/{org}"
        self._lock = threading.Lock()
        self._auth = ""
        self._auth_time = 0.0
        self._tenant = tenant  # chosen on first use: the tenant that backs the organization

    def _token(self, tenant: str | None) -> str:
        args = ["account", "get-access-token", "--resource", ADO_RESOURCE, "--query", "accessToken", "-o", "tsv"]
        res = _az(*args, *(["--tenant", tenant] if tenant else []))
        return res.stdout.strip() if res.returncode == 0 else ""

    def _accepts(self, auth: str) -> bool:
        req = urllib.request.Request(f"{self.base}/_apis/projects?$top=1&{API}", headers={"Authorization": auth})
        try:
            with urllib.request.urlopen(req, timeout=60):
                return True
        except urllib.error.URLError:
            return False

    def _bearer(self) -> str:
        """Entra token for Azure DevOps. A login can span several tenants and only one backs the
        organization, so try the pinned tenant, else every tenant `az` knows (default first)."""
        if self._tenant:
            candidates = [self._tenant]
        else:
            res = _az("account", "list", "--query", "[].[tenantId,isDefault]", "-o", "tsv")
            rows = [l.split("	") for l in res.stdout.splitlines() if l.strip()]
            candidates = list(dict.fromkeys([t for t, d in rows if d.strip().lower() == "true"] + [t for t, _ in rows]))
            if not candidates:
                candidates = [None]
        for tenant in candidates:
            token = self._token(tenant)
            if token and self._accepts("Bearer " + token):
                self._tenant = tenant
                return "Bearer " + token
        raise SystemExit("Cannot get an Azure DevOps token that this organization accepts. Run `az login` "
                         "(with an account that has access), pass --tenant, or set ADO_PAT.")

    def _header(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._auth or time.time() - self._auth_time > 40 * 60:
                pat = os.environ.get("ADO_PAT")
                self._auth = ("Basic " + base64.b64encode(f":{pat}".encode()).decode()) if pat else self._bearer()
                self._auth_time = time.time()
            return self._auth

    def request(self, path: str, body: dict | None = None) -> dict:
        url = path if path.startswith("http") else f"{self.base}/{path}"
        url += ("&" if "?" in url else "?") + API
        data = json.dumps(body).encode() if body is not None else None
        refreshed = False
        for attempt in range(6):
            req = urllib.request.Request(url, data=data, headers={
                "Authorization": self._header(), "Accept": "application/json",
                **({"Content-Type": "application/json"} if data else {})})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")[:300]
                if e.code == 401 and not refreshed:
                    self._header(force=True)
                    refreshed = True
                    continue
                if e.code in (429, 502, 503, 504) and attempt < 5:
                    time.sleep(float(e.headers.get("Retry-After") or 2 * (attempt + 1)))
                    continue
                raise RuntimeError(f"HTTP {e.code} for {urllib.parse.urlsplit(url).path}: {text}") from e
            except urllib.error.URLError as e:
                if attempt < 5:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"{e} for {url}") from e
        raise RuntimeError("unreachable")


def az_defaults() -> dict[str, str]:
    """organization / project from `az devops configure -l`, if set."""
    try:
        out = _az("devops", "configure", "-l").stdout
    except OSError:
        return {}
    found = dict(re.findall(r"^(organization|project)\s*=\s*(.+?)\s*$", out, re.M))
    if "organization" in found:
        found["organization"] = found["organization"].rstrip("/").rsplit("/", 1)[-1]
    return found


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

@dataclass
class Story:
    id: int
    title: str
    url: str
    created: str
    done: str                      # when it last reached Done
    tags: list[str] = field(default_factory=list)
    days_in_state: dict[str, float] = field(default_factory=dict)   # state -> days (closed intervals)
    phase_days: dict[str, float] = field(default_factory=dict)      # created / committed -> days
    lead_days: float = 0.0
    reopened: bool = False
    pr_links: list[dict] = field(default_factory=list)   # {url, created}: PR/MR links on the story
    pr_matched: list[str] = field(default_factory=list)
    ai_prs: list[str] = field(default_factory=list)
    ai: str = "nodata"             # "ai" | "human" | "nodata"


def parse_ts(s: str) -> dt.datetime:
    s = s.replace("Z", "+00:00")
    if "." in s:  # trim fractional seconds to microseconds
        head, rest = s.split(".", 1)
        m = re.match(r"(\d+)(.*)", rest)
        s = f"{head}.{m.group(1)[:6].ljust(6, '0')}{m.group(2)}"
    return dt.datetime.fromisoformat(s)


def days(a: dt.datetime, b: dt.datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 86400)


def norm_url(u: str) -> str:
    return re.sub(r"[#?].*$", "", u.strip()).rstrip("/").lower()


PR_URL_RE = re.compile(r"https?://[^\s\"']+?(/pull/\d+|/-/merge_requests/\d+|/pullrequest/\d+)", re.I)
GH_PR_RE = re.compile(r"^vstfs:///GitHub/PullRequest/([0-9a-f-]+)%2f(\d+)$", re.I)


def pr_links(relations: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for r in relations or []:
        url = r.get("url") or ""
        attrs = r.get("attributes") or {}
        name = attrs.get("name") or ""
        if PR_URL_RE.search(url) or (r.get("rel") == "ArtifactLink" and "pull request" in name.lower()):
            out.setdefault(url, {"url": url, "created": attrs.get("resourceCreatedDate")})
    return list(out.values())


def resolve_links(stories: list["Story"], prs: dict[str, dict], tolerance_min: float = 10.0) -> None:
    """Match story PR links to PRs from the PR report.

    Plain web URLs (GitLab merge requests, GitHub URLs) match directly. Azure Boards' GitHub
    integration stores `vstfs:///GitHub/PullRequest/<repo guid>%2f<number>`, which names no repository,
    so: (1) a link matches the PR with that number created within a few minutes of the link's
    resourceCreatedDate, if only one repo qualifies, and the guid -> repo pairs found that way are
    remembered; (2) remaining links with a known guid look for that number in that repo."""
    by_number: dict[int, list[dict]] = defaultdict(list)
    for p in prs.values():
        if p.get("source", "github") == "github":
            by_number[p["number"]].append(p)
    votes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    pending: list[tuple[Story, str, str, int]] = []
    for s in stories:
        s.pr_matched = []
        for link in s.pr_links:
            if norm_url(link["url"]) in prs:
                s.pr_matched.append(link["url"])
                continue
            m = GH_PR_RE.match(link["url"])
            if not m:
                continue
            guid, number = m.group(1).lower(), int(m.group(2))
            cands = by_number.get(number, [])
            if link.get("created"):
                t = parse_ts(link["created"])
                near = [p for p in cands if abs((parse_ts(p["created_at"]) - t).total_seconds()) <= tolerance_min * 60]
                if len({p["repo"] for p in near}) == 1:
                    votes[guid][near[0]["repo"]] += 1
                    s.pr_matched.append(near[0]["url"])
                    continue
            pending.append((s, link["url"], guid, number))
    repo_of = {g: max(v, key=v.get) for g, v in votes.items()}
    for s, url, guid, number in pending:
        repo = repo_of.get(guid)
        hit = next((p for p in by_number.get(number, []) if p["repo"] == repo), None) if repo else None
        if hit:
            s.pr_matched.append(hit["url"])


def state_timeline(created: str, updates: list[dict]) -> list[tuple[dt.datetime, str]]:
    """(timestamp, state) in order, one entry per state change, starting at creation."""
    events: list[tuple[dt.datetime, str]] = []
    for u in sorted(updates, key=lambda u: u.get("rev", 0)):
        f = u.get("fields") or {}
        st = (f.get("System.State") or {}).get("newValue")
        if not st:
            continue
        when = (f.get("System.ChangedDate") or {}).get("newValue") or u.get("revisedDate") or created
        events.append((parse_ts(when), st))
    if events:
        events[0] = (min(events[0][0], parse_ts(created)), events[0][1])
    return events


def build_story(wi: dict, events: list[tuple[dt.datetime, str]], org_url: str, categories: dict[str, str]) -> Story | None:
    f = wi["fields"]
    if not events or events[-1][1] != "Done":
        return None
    done_at = events[-1][0]
    created = parse_ts(f["System.CreatedDate"])
    per_state: dict[str, float] = defaultdict(float)
    for (t0, st), (t1, _) in zip(events, events[1:]):
        per_state[st] += days(t0, t1)
    phases: dict[str, float] = defaultdict(float)
    for st, d in per_state.items():
        phase = PHASE_NAMES.get(categories.get(st, ""))
        if phase:
            phases[phase] += d
    tags = [t.strip() for t in (f.get("System.Tags") or "").split(";") if t.strip()]
    return Story(
        id=wi["id"], title=f.get("System.Title") or "", url=f"{org_url}/_workitems/edit/{wi['id']}",
        created=created.isoformat(), done=done_at.isoformat(), tags=tags,
        days_in_state={k: round(v, 2) for k, v in per_state.items()},
        phase_days={"created": round(phases.get("created", 0.0), 2), "committed": round(phases.get("committed", 0.0), 2)},
        lead_days=round(days(created, done_at), 2),
        reopened=sum(1 for _, st in events if st == "Done") > 1,
        pr_links=pr_links(wi.get("relations") or []),
    )


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def query_ids(ado: Ado, project: str, wtype: str, since: dt.date | None) -> list[int]:
    esc = lambda s: s.replace("'", "''")
    where = (f"[System.TeamProject] = '{esc(project)}' AND [System.WorkItemType] = '{esc(wtype)}' "
             f"AND [System.State] = 'Done'")
    if since:
        where += f" AND [Microsoft.VSTS.Common.StateChangeDate] >= '{since:%Y-%m-%d}'"
    res = ado.request(f"{urllib.parse.quote(project)}/_apis/wit/wiql", {"query": f"SELECT [System.Id] FROM WorkItems WHERE {where} ORDER BY [System.Id]"})
    return [w["id"] for w in res.get("workItems", [])]


def fetch_items(ado: Ado, ids: list[int]) -> list[dict]:
    chunks = [ids[i:i + 200] for i in range(0, len(ids), 200)]
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        parts = pool.map(lambda c: ado.request("_apis/wit/workitemsbatch", {"ids": c, "$expand": "relations"}).get("value", []), chunks)
        return [wi for part in parts for wi in part]


def fetch_updates(ado: Ado, wid: int) -> list[dict]:
    out, skip = [], 0
    while True:
        res = ado.request(f"_apis/wit/workItems/{wid}/updates?$top=200&$skip={skip}").get("value", [])
        out += res
        if len(res) < 200:
            return out
        skip += 200


def load_ai_prs(path: str) -> dict[str, dict]:
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return {norm_url(p["url"]): p for p in json.load(f)}


# --------------------------------------------------------------------------- #
# Statistics & HTML
# --------------------------------------------------------------------------- #

esc = lambda s: html.escape(str(s), quote=True)


def med(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def p90(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]


def fd(x: float | None) -> str:
    return "–" if x is None else (f"{x:.1f}" if x < 100 else f"{x:.0f}")


def group_stats(stories: list[Story]) -> dict:
    return {
        "n": len(stories),
        "created": med([s.phase_days["created"] for s in stories]),
        "committed": med([s.phase_days["committed"] for s in stories]),
        "lead": med([s.lead_days for s in stories]),
        "lead_mean": statistics.fmean([s.lead_days for s in stories]) if stories else None,
    }


LABEL = {"ai": "AI-assisted", "human": "Not AI (PRs checked)", "nodata": "No PR data"}


def render(org: str, project: str, wtype: str, stories: list[Story], states: list[str], since: dt.date | None,
           prs_path: str, pr_count: int) -> str:
    total = len(stories)
    by = {k: [s for s in stories if s.ai == k] for k in LABEL}
    known = len(by["ai"]) + len(by["human"])
    pct = lambda a, b: f"{100.0 * a / b:.1f}%" if b else "–"
    allstats = group_stats(stories)

    def cards() -> str:
        items = [
            ("Done stories", f"{total:,}", f"{wtype} reached Done" + (f" since {since}" if since else "")),
            ("With PR data", f"{known:,}", f"{pct(known, total)} of done stories had a linked PR found in the PR report"),
            ("AI-assisted", f"{len(by['ai']):,}", f"{pct(len(by['ai']), known)} of stories with PR data"),
            ("Median lead time", f"{fd(allstats['lead'])} d", "creation → done"),
            ("Median created", f"{fd(allstats['created'])} d", "in New / Approved / Refined…"),
            ("Median committed", f"{fd(allstats['committed'])} d", "in Committed / Testable / Acceptable…"),
        ]
        return "".join(f'<div class="card"><div class="l">{esc(a)}</div><div class="v">{esc(b)}</div><div class="muted">{esc(c)}</div></div>' for a, b, c in items)

    def cmp_rows() -> str:
        rows = []
        for k, label in LABEL.items():
            g = group_stats(by[k])
            rows.append(f"<tr><td>{esc(label)}</td><td class='num'>{g['n']:,}</td><td class='num'>{pct(g['n'], total)}</td>"
                        f"<td class='num'>{fd(g['created'])}</td><td class='num'>{fd(g['committed'])}</td>"
                        f"<td class='num'><b>{fd(g['lead'])}</b></td><td class='num'>{fd(g['lead_mean'])}</td></tr>")
        return "".join(rows)

    def state_rows() -> str:
        rows = []
        for st in states:
            cells = []
            for group in (stories, by["ai"], by["human"]):
                xs = [s.days_in_state[st] for s in group if st in s.days_in_state]
                cells.append(f"<td class='num'>{len(xs):,}</td><td class='num'>{fd(med(xs))}</td>")
            allx = [s.days_in_state[st] for s in stories if st in s.days_in_state]
            rows.append(f"<tr><td>{esc(st)}</td>{cells[0]}<td class='num'>{fd(statistics.fmean(allx) if allx else None)}</td>"
                        f"<td class='num'>{fd(p90(allx))}</td>{cells[1]}{cells[2]}</tr>")
        return "".join(rows)

    def month_rows() -> str:
        buckets: dict[str, list[Story]] = defaultdict(list)
        for s in stories:
            buckets[s.done[:7]].append(s)
        rows = []
        for m in sorted(buckets, reverse=True):
            g = buckets[m]
            a = sum(1 for s in g if s.ai == "ai")
            k = sum(1 for s in g if s.ai != "nodata")
            gs = group_stats(g)
            rows.append(f"<tr><td>{m}</td><td class='num'>{len(g)}</td><td class='num'>{a}</td><td class='num'>{k}</td>"
                        f"<td class='num'><b>{pct(a, k)}</b></td><td class='num'>{fd(gs['created'])}</td>"
                        f"<td class='num'>{fd(gs['committed'])}</td><td class='num'>{fd(gs['lead'])}</td></tr>")
        return "".join(rows)

    def story_rows() -> str:
        out = []
        for s in sorted(stories, key=lambda s: s.done, reverse=True):
            tag = {"ai": "AI", "human": "no AI", "nodata": "no PR data"}[s.ai]
            out.append(
                f"<tr data-q='{esc((str(s.id) + ' ' + s.title).lower())}'>"
                f"<td><a href='{esc(s.url)}' target='_blank' rel='noopener'>#{s.id}</a> {esc(s.title)}"
                f"{' <span class=muted>(reopened)</span>' if s.reopened else ''}</td>"
                f"<td>{s.done[:10]}</td><td class='num'>{fd(s.phase_days['created'])}</td>"
                f"<td class='num'>{fd(s.phase_days['committed'])}</td><td class='num'>{fd(s.lead_days)}</td>"
                f"<td><span class='tag {s.ai}'>{tag}</span></td>"
                f"<td class='num'>{len(s.pr_matched)}/{len(s.pr_links)}</td></tr>")
        return "".join(out)

    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Done stories · {esc(project)}</title>
<style>
:root {{ --bg:#f7f7f5; --card:#fff; --fg:#1d1d1b; --muted:#6b6b66; --line:#e4e4df; --ai:#d97757; --human:#2f7ed8; --nodata:#a8a29e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161615; --card:#1f1f1d; --fg:#ecece8; --muted:#9a9a93; --line:#2f2f2c; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif }}
main {{ max-width:1200px; margin:0 auto; padding:32px 16px 64px }}
h1 {{ font-size:26px; margin:0 0 4px }} h2 {{ font-size:16px; margin:36px 0 12px }}
.muted {{ color:var(--muted); font-size:12px }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-top:20px }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px }}
.card .v {{ font-size:26px; font-weight:650; font-variant-numeric:tabular-nums }} .card .l {{ color:var(--muted); font-size:12px }}
table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden }}
th,td {{ padding:7px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top }}
th {{ font-size:12px; color:var(--muted); font-weight:600 }} td.num,th.num {{ text-align:right; font-variant-numeric:tabular-nums }}
.wrap {{ overflow-x:auto }} a {{ color:inherit }}
.tag {{ font-size:11px; padding:1px 7px; border-radius:99px; color:#fff }}
.tag.ai {{ background:var(--ai) }} .tag.human {{ background:var(--human) }} .tag.nodata {{ background:var(--nodata) }}
input {{ padding:6px 10px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg); width:260px; max-width:100% }}
</style></head><body><main>
<h1>Done {esc(wtype)}s · {esc(project)}</h1>
<div class="muted">{esc(org)} · done {"since " + str(since) if since else "all time"} · AI status from {pr_count:,} PRs in {esc(os.path.basename(prs_path)) or "n/a"} · generated {generated}</div>
<div class="cards">{cards()}</div>

<h2>AI-assisted vs not (median days)</h2>
<div class="wrap"><table><tr><th>Group</th><th class="num">Stories</th><th class="num">Share</th><th class="num">Created</th><th class="num">Committed</th><th class="num">Lead time</th><th class="num">Mean lead</th></tr>{cmp_rows()}</table></div>
<p class="muted">Created = time in Proposed-category states, committed = time in InProgress-category states, lead time = creation until done. "No PR data" means no linked PR was found in the PR report (no link, or a repository it did not scan); these stories may or may not be AI-assisted.</p>

<h2>Time per state (days)</h2>
<div class="wrap"><table><tr><th rowspan="2">State</th><th class="num" colspan="2">All stories</th><th class="num" rowspan="2">Mean</th><th class="num" rowspan="2">P90</th><th class="num" colspan="2">AI-assisted</th><th class="num" colspan="2">Not AI</th></tr>
<tr><th class="num">n</th><th class="num">median</th><th class="num">n</th><th class="num">median</th><th class="num">n</th><th class="num">median</th></tr>{state_rows()}</table></div>
<p class="muted">Counts only completed stays; a story that moved back and forth adds up its time per state. Done itself has no duration because every story ends there.</p>

<h2>Per month (by done date)</h2>
<div class="wrap"><table><tr><th>Month</th><th class="num">Done</th><th class="num">AI</th><th class="num">With PR data</th><th class="num">% AI</th><th class="num">Created d</th><th class="num">Committed d</th><th class="num">Lead d</th></tr>{month_rows()}</table></div>

<h2>Stories</h2>
<p><input id="q" placeholder="Filter by id or title"></p>
<div class="wrap"><table id="t"><tr><th>Story</th><th>Done</th><th class="num">Created d</th><th class="num">Committed d</th><th class="num">Lead d</th><th>AI</th><th class="num">PRs found/linked</th></tr>{story_rows()}</table></div>
<script>
document.getElementById('q').addEventListener('input', e => {{
  const q = e.target.value.toLowerCase();
  document.querySelectorAll('#t tr[data-q]').forEach(r => r.hidden = !r.dataset.q.includes(q));
}});
</script>
</main></body></html>"""


# --------------------------------------------------------------------------- #

def main() -> int:
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    defaults = az_defaults()
    ap = argparse.ArgumentParser(description="Azure DevOps: time in state and AI usage for stories that reached Done.")
    ap.add_argument("--org", default=defaults.get("organization"), help="Azure DevOps organization (default: az devops configure)")
    ap.add_argument("--project", default=defaults.get("project"), help="Project name (default: az devops configure)")
    ap.add_argument("--tenant", default=None, help="Entra tenant id that backs the organization (default: detected from your az login)")
    ap.add_argument("--type", default="User Story", help="Work item type (default 'User Story')")
    ap.add_argument("--since", default=None, help="Only stories done on/after this date (YYYY-MM-DD), or 'all'. Default: 12 months ago")
    ap.add_argument("--prs", default=os.path.join(HERE, "..", "pr-reports", "ai-prs.json"),
                    help="JSON written by ai_pr_report.py --json, used to decide which PRs are AI-assisted")
    ap.add_argument("--cache-dir", default=".cache", help="Cache of state histories (default .cache; '' disables)")
    ap.add_argument("--workers", type=int, default=8, help="Parallel history requests (default 8)")
    ap.add_argument("--limit", type=int, default=0, help="Only process the first N stories (for testing)")
    ap.add_argument("--out", default="ai-stories.html")
    ap.add_argument("--json", help="Also write the per-story data to this JSON file")
    args = ap.parse_args()
    if not args.org or not args.project:
        ap.error("--org and --project are required (or set them with `az devops configure -d organization=... project=...`)")

    if args.since is None:
        today = dt.date.today()
        args.since = f"{today.year - 1:04d}-{today.month:02d}-01"
    since = None if args.since == "all" else dt.date.fromisoformat(args.since)

    ado = Ado(args.org, args.tenant)
    org_url = ado.base
    ids = query_ids(ado, args.project, args.type, since)
    if args.limit:
        ids = ids[-args.limit:]
    print(f"{len(ids)} {args.type} work items in state Done since {args.since} in {args.org}/{args.project}", file=sys.stderr)
    if not ids:
        return 1

    cats_raw = ado.request(f"{urllib.parse.quote(args.project)}/_apis/wit/workitemtypes/{urllib.parse.quote(args.type)}/states").get("value", [])
    categories = {s["name"]: s.get("category") or "" for s in cats_raw}
    items = fetch_items(ado, ids)
    print(f"Fetched {len(items)} work items; reading state history...", file=sys.stderr)

    cache_path = os.path.join(args.cache_dir, f"ado_{args.org}_{args.project}_updates.json") if args.cache_dir else None
    cache: dict[str, dict] = {}
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)

    def events_for(wi: dict) -> list[tuple[dt.datetime, str]]:
        hit = cache.get(str(wi["id"]))
        if hit and hit.get("rev") == wi.get("rev"):
            return [(parse_ts(t), st) for t, st in hit["events"]]
        ev = state_timeline(wi["fields"]["System.CreatedDate"], fetch_updates(ado, wi["id"]))
        cache[str(wi["id"])] = {"rev": wi.get("rev"), "events": [[t.isoformat(), st] for t, st in ev]}
        return ev

    stories: list[Story] = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(events_for, wi): wi for wi in items}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            wi = futs[fut]
            try:
                st = build_story(wi, fut.result(), org_url, categories)
            except Exception as e:
                print(f"  #{wi['id']}: ERROR {e}", file=sys.stderr)
                continue
            if st and (since is None or parse_ts(st.done).date() >= since):
                stories.append(st)
            if i % 200 == 0:
                print(f"  {i}/{len(items)}", file=sys.stderr)
    if cache_path:
        os.makedirs(args.cache_dir, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)

    prs = load_ai_prs(args.prs)
    if not prs:
        print(f"Note: no PR data at {args.prs}; AI status will be 'no PR data' for every story.", file=sys.stderr)
    resolve_links(stories, prs)
    for s in stories:
        s.ai_prs = [u for u in s.pr_matched if prs[norm_url(u)].get("ai")]
        s.ai = "ai" if s.ai_prs else ("human" if s.pr_matched else "nodata")

    states = [n for n, c in sorted(categories.items(), key=lambda kv: list(categories).index(kv[0])) if n != "Done" and c != "Removed"]
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(render(args.org, args.project, args.type, stories, states, since, args.prs, len(prs)))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([asdict(s) for s in stories], f, indent=2)
    counts = {k: sum(1 for s in stories if s.ai == k) for k in LABEL}
    print(f"Done: {len(stories)} stories ({counts['ai']} AI, {counts['human']} not AI, {counts['nodata']} no PR data). "
          f"Report written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
