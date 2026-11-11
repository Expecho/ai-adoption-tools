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
    python ado_story_report.py --config stories.json            # restrict to areas / created date / stories with tasks
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
import story_config
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
    pr_links: list[dict] = field(default_factory=list)   # {url, created, via}: PR/MR links on the story or its child items (via = work item id)
    pr_matched: list[str] = field(default_factory=list)
    ai_prs: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)       # repositories of the matched PRs
    ai_repos: list[str] = field(default_factory=list)    # ...of which have an AI-assisted PR for this story
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


def pr_links(relations: list[dict], via: int | None = None) -> list[dict]:
    out: dict[str, dict] = {}
    for r in relations or []:
        url = r.get("url") or ""
        attrs = r.get("attributes") or {}
        name = attrs.get("name") or ""
        if PR_URL_RE.search(url) or (r.get("rel") == "ArtifactLink" and "pull request" in name.lower()):
            out.setdefault(url, {"url": url, "created": attrs.get("resourceCreatedDate"), "via": via})
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


def build_story(wi: dict, events: list[tuple[dt.datetime, str]], org_url: str, categories: dict[str, str],
                descendants: list[dict] | None = None) -> Story | None:
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
    own_links: dict[str, dict] = {}   # the story's own links first, then its descendants'; the first holder wins
    for holder in [wi, *(descendants or [])]:
        for link in pr_links(holder.get("relations") or [], holder["id"]):
            own_links.setdefault(link["url"], link)
    return Story(
        id=wi["id"], title=f.get("System.Title") or "", url=f"{org_url}/_workitems/edit/{wi['id']}",
        created=created.isoformat(), done=done_at.isoformat(), tags=tags,
        days_in_state={k: round(v, 2) for k, v in per_state.items()},
        phase_days={"created": round(phases.get("created", 0.0), 2), "committed": round(phases.get("committed", 0.0), 2)},
        lead_days=round(days(created, done_at), 2),
        reopened=sum(1 for _, st in events if st == "Done") > 1,
        pr_links=list(own_links.values()),
    )


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def query_ids(ado: Ado, project: str, wtype: str, since: dt.date | None, created_since: str | None = None) -> list[int]:
    esc = lambda s: s.replace("'", "''")
    where = (f"[System.TeamProject] = '{esc(project)}' AND [System.WorkItemType] = '{esc(wtype)}' "
             f"AND [System.State] = 'Done'")
    if since:
        where += f" AND [Microsoft.VSTS.Common.StateChangeDate] >= '{since:%Y-%m-%d}'"
    if created_since:
        where += f" AND [System.CreatedDate] >= '{created_since}'"
    res = ado.request(f"{urllib.parse.quote(project)}/_apis/wit/wiql", {"query": f"SELECT [System.Id] FROM WorkItems WHERE {where} ORDER BY [System.Id]"})
    return [w["id"] for w in res.get("workItems", [])]


def fetch_items(ado: Ado, ids: list[int]) -> list[dict]:
    chunks = [ids[i:i + 200] for i in range(0, len(ids), 200)]
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        parts = pool.map(lambda c: ado.request("_apis/wit/workitemsbatch", {"ids": c, "$expand": "relations"}).get("value", []), chunks)
        return [wi for part in parts for wi in part]


def fetch_descendants(ado: Ado, items: list[dict], depth: int = 3) -> dict[int, list[dict]]:
    """story id -> the work items below it (tasks, and anything under those), with their relations.
    Developers usually link a pull request from the task, not from the story itself."""
    child_ids = lambda wi: [int(r["url"].rsplit("/", 1)[1]) for r in wi.get("relations") or []
                            if r.get("rel") == "System.LinkTypes.Hierarchy-Forward"]
    owner: dict[int, int] = {}
    for wi in items:
        for c in child_ids(wi):
            owner.setdefault(c, wi["id"])
    found: dict[int, list[dict]] = defaultdict(list)
    seen: set[int] = set()
    level = list(owner)
    for _ in range(depth):
        level = [i for i in level if i not in seen]
        if not level:
            break
        seen.update(level)
        nxt = []
        for w in fetch_items(ado, level):
            found[owner[w["id"]]].append(w)
            for c in child_ids(w):
                if c not in owner:
                    owner[c] = owner[w["id"]]
                    nxt.append(c)
        level = nxt
    return found


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


CHART_CSS = r"""
:root { --surface:#fcfcfb; --grid:#e1e0d9; --axis:#c3c2b7; --ink2:#52514e; --muted-ink:#898781;
  --c-ai:#2a78d6; --c-human:#898781; --c-nodata:#c3c2b7; }
@media (prefers-color-scheme: dark) { :root { --surface:#1a1a19; --grid:#2c2c2a; --axis:#383835; --ink2:#c3c2b7;
  --c-ai:#3987e5; --c-human:#898781; --c-nodata:#4a4944; } }
.charts { display:grid; gap:16px; margin-top:8px }
.chart { background:var(--surface); padding:16px 16px 8px }
.chart h3 { font-size:14px; margin:0 } .chart p { margin:2px 0 8px }
.legend { display:flex; gap:16px; flex-wrap:wrap; margin:0 0 6px; font-size:12px; color:var(--ink2) }
.lk { display:inline-flex; align-items:center; gap:6px }
.lk i { width:10px; height:10px; border-radius:2px; display:inline-block }
.host { width:100%; position:relative } .host svg { display:block; overflow:visible }
.host text { font:11px system-ui,-apple-system,"Segoe UI",sans-serif; fill:var(--muted-ink) }
.host text.val { fill:var(--fg); font-weight:600 }
.host .hit { fill:transparent; outline:none } .host .hit:focus-visible { stroke:var(--fg); stroke-width:1; rx:4 }
#tip { position:fixed; z-index:10; pointer-events:none; background:var(--card); color:var(--fg); border:1px solid var(--line);
  border-radius:8px; padding:8px 10px; font-size:12px; box-shadow:0 4px 16px rgba(0,0,0,.15); display:none; max-width:260px }
#tip .t { color:var(--muted); margin-bottom:4px } #tip .r { display:flex; align-items:center; gap:8px; white-space:nowrap }
#tip .k { width:10px; height:3px; border-radius:2px; flex:none } #tip b { font-variant-numeric:tabular-nums }
tr.overall td { background:var(--bg); border-bottom:2px solid var(--axis) }
.tag { color:var(--fg); background:var(--card); border:1px solid var(--line); display:inline-flex; align-items:center; gap:5px; padding:0 8px }
.tag.ai, .tag.human, .tag.nodata { background:var(--card) }
.tag::before { content:""; width:8px; height:8px; border-radius:50%; background:var(--c) }
.tag.ai { --c:var(--c-ai) } .tag.human { --c:var(--c-human) } .tag.nodata { --c:var(--c-nodata) }
"""
CHART_JS = r"""
(function () {
  var D = JSON.parse(document.getElementById('chart-data').textContent);
  var NS = 'http://www.w3.org/2000/svg';
  var SER = [['ai', 'AI-assisted', 'var(--c-ai)'], ['human', 'Not AI (PRs checked)', 'var(--c-human)'], ['nodata', 'No PR data', 'var(--c-nodata)']];
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var tip = document.getElementById('tip');

  function el(tag, attrs, parent) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) { if (k === 'style') n.setAttribute('style', attrs[k]); else n.setAttribute(k, attrs[k]); }
    if (parent) parent.appendChild(n);
    return n;
  }
  function text(parent, x, y, str, anchor, cls) {
    var t = el('text', { x: x, y: y, 'text-anchor': anchor || 'start' }, parent);
    if (cls) t.setAttribute('class', cls);
    t.textContent = str;
    return t;
  }
  function monthLabel(m) { var p = m.split('-'); return MON[+p[1] - 1] + (p[1] === '01' || m === D.months[0].m ? ' ' + p[0].slice(2) : ''); }
  function niceScale(v) {
    if (v <= 0) return { max: 1, step: 1 };
    var raw = v / 4, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
    var step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
    return { max: Math.ceil(v / step) * step, step: step };
  }
  function fmtDays(x) { return x == null ? '–' : (x < 100 ? x.toFixed(1) : Math.round(x)) + ' d'; }

  // tooltip: rows = [{key: cssColor|null, label, value}], all text via textContent
  function showTip(evt, title, rows, anchorEl) {
    tip.textContent = '';
    var t = document.createElement('div'); t.className = 't'; t.textContent = title; tip.appendChild(t);
    rows.forEach(function (r) {
      var d = document.createElement('div'); d.className = 'r';
      if (r.key) { var k = document.createElement('span'); k.className = 'k'; k.style.background = r.key; d.appendChild(k); }
      var b = document.createElement('b'); b.textContent = r.value; d.appendChild(b);
      var l = document.createElement('span'); l.textContent = r.label; l.style.color = 'var(--muted)'; d.appendChild(l);
      tip.appendChild(d);
    });
    tip.style.display = 'block';
    var x, y;
    if (evt && evt.clientX != null) { x = evt.clientX; y = evt.clientY; }
    else { var b2 = anchorEl.getBoundingClientRect(); x = b2.left + b2.width / 2; y = b2.top; }
    var w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, x + 14)) + 'px';
    tip.style.top = Math.max(8, Math.min(window.innerHeight - h - 8, y - h - 10 < 8 ? y + 18 : y - h - 10)) + 'px';
  }
  function hideTip() { tip.style.display = 'none'; }
  function wire(node, title, rows) {
    node.setAttribute('tabindex', '0');
    node.setAttribute('role', 'img');
    node.setAttribute('aria-label', title + ': ' + rows.map(function (r) { return r.value + ' ' + r.label; }).join(', '));
    node.addEventListener('pointermove', function (e) { showTip(e, title, rows, node); });
    node.addEventListener('pointerleave', hideTip);
    node.addEventListener('focus', function () { showTip(null, title, rows, node); });
    node.addEventListener('blur', hideTip);
  }
  function topRounded(x, y, w, h, r) {
    r = Math.max(0, Math.min(r, h, w / 2));
    return 'M' + x + ',' + (y + h) + 'V' + (y + r) + 'Q' + x + ',' + y + ' ' + (x + r) + ',' + y + 'H' + (x + w - r) +
      'Q' + (x + w) + ',' + y + ' ' + (x + w) + ',' + (y + r) + 'V' + (y + h) + 'Z';
  }

  // 1. Stacked columns: stories done per month by AI status
  function columns(host) {
    host.textContent = '';
    var rows = D.months, n = rows.length, W = host.clientWidth || 600, H = 250;
    var m = { l: 36, r: 44, t: 10, b: 26 }, pw = W - m.l - m.r, ph = H - m.t - m.b;
    var sc = niceScale(Math.max.apply(null, rows.map(function (r) { return r.ai + r.human + r.nodata; })));
    var svg = el('svg', { width: W, height: H, viewBox: '0 0 ' + W + ' ' + H }, host);
    var y = function (v) { return m.t + ph - v / sc.max * ph; };
    for (var v = 0; v <= sc.max + 1e-9; v += sc.step) {
      el('line', { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), style: 'stroke:' + (v === 0 ? 'var(--axis)' : 'var(--grid)') + ';stroke-width:1' }, svg);
      text(svg, m.l - 6, y(v) + 4, String(Math.round(v)), 'end');
    }
    var band = pw / n, bw = Math.min(24, band * 0.62), every = Math.ceil(46 / band);
    rows.forEach(function (r, i) {
      var cx = m.l + band * (i + 0.5), x = cx - bw / 2, acc = 0, g = el('g', {}, svg);
      var segs = SER.filter(function (s) { return r[s[0]] > 0; });
      segs.forEach(function (s, j) {
        var v0 = acc, v1 = acc + r[s[0]]; acc = v1;
        var top = y(v1), bottom = y(v0), h = bottom - top - (j > 0 ? 2 : 0);   // 2px surface gap between segments
        if (h <= 0) return;
        var last = j === segs.length - 1;
        el('path', { d: last ? topRounded(x, top, bw, h, 4) : 'M' + x + ',' + top + 'h' + bw + 'v' + h + 'h' + (-bw) + 'Z', style: 'fill:' + s[2] }, g);
      });
      if (i % every === 0) text(svg, cx, H - 8, monthLabel(r.m), 'middle');
      var hit = el('rect', { class: 'hit', x: m.l + band * i, y: m.t, width: band, height: ph }, svg);
      var rowsT = SER.map(function (s) { return { key: s[2], label: s[1], value: String(r[s[0]]) }; });
      rowsT.push({ key: null, label: 'total done', value: String(r.ai + r.human + r.nodata) });
      wire(hit, r.m, rowsT);
      hit.addEventListener('pointerenter', function () { g.style.opacity = 0.8; });
      hit.addEventListener('pointerleave', function () { g.style.opacity = 1; });
      hit.addEventListener('focus', function () { g.style.opacity = 0.8; });
      hit.addEventListener('blur', function () { g.style.opacity = 1; });
    });
  }

  // 2. Line: share of AI-assisted stories among those with PR data
  function line(host) {
    host.textContent = '';
    var rows = D.months, n = rows.length, W = host.clientWidth || 600, H = 210;
    var m = { l: 36, r: 44, t: 12, b: 26 }, pw = W - m.l - m.r, ph = H - m.t - m.b;
    var svg = el('svg', { width: W, height: H, viewBox: '0 0 ' + W + ' ' + H }, host);
    var y = function (v) { return m.t + ph - v / 100 * ph; };
    var band = pw / Math.max(1, n), X = function (i) { return m.l + band * (i + 0.5); };
    [0, 25, 50, 75, 100].forEach(function (v) {
      el('line', { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), style: 'stroke:' + (v === 0 ? 'var(--axis)' : 'var(--grid)') + ';stroke-width:1' }, svg);
      text(svg, m.l - 6, y(v) + 4, v + '%', 'end');
    });
    var every = Math.ceil(46 / band), d = '', pen = false;
    rows.forEach(function (r, i) {
      if (i % every === 0) text(svg, X(i), H - 8, monthLabel(r.m), 'middle');
      if (r.pct == null) { pen = false; return; }
      d += (pen ? 'L' : 'M') + X(i) + ',' + y(r.pct); pen = true;
    });
    el('path', { d: d, style: 'fill:none;stroke:var(--c-ai);stroke-width:2;stroke-linecap:round;stroke-linejoin:round' }, svg);
    var cross = el('line', { y1: m.t, y2: m.t + ph, style: 'stroke:var(--axis);stroke-width:1;display:none' }, svg);
    var lastI = -1;
    rows.forEach(function (r, i) {
      if (r.pct == null) return;
      lastI = i;
      el('circle', { cx: X(i), cy: y(r.pct), r: 4, style: 'fill:var(--c-ai);stroke:var(--surface);stroke-width:2' }, svg);
    });
    if (lastI >= 0) text(svg, X(lastI) + 9, y(rows[lastI].pct) + 4, rows[lastI].pct.toFixed(0) + '%', 'start', 'val');
    rows.forEach(function (r, i) {
      var hit = el('rect', { class: 'hit', x: m.l + band * i, y: m.t, width: band, height: ph }, svg);
      var known = r.ai + r.human;
      wire(hit, r.m, r.pct == null
        ? [{ key: null, label: 'no stories with PR data', value: '–' }]
        : [{ key: 'var(--c-ai)', label: 'AI-assisted', value: r.pct.toFixed(1) + '%' },
           { key: null, label: 'of stories with PR data', value: r.ai + ' / ' + known }]);
      var on = function () { cross.setAttribute('x1', X(i)); cross.setAttribute('x2', X(i)); cross.style.display = ''; };
      var off = function () { cross.style.display = 'none'; };
      hit.addEventListener('pointermove', on); hit.addEventListener('pointerleave', off);
      hit.addEventListener('focus', on); hit.addEventListener('blur', off);
    });
  }

  // 3. Grouped horizontal bars: median days per phase, AI vs not AI
  function bars(host) {
    host.textContent = '';
    var W = host.clientWidth || 600, cats = [['created', 'Created'], ['committed', 'Committed'], ['lead', 'Lead time']];
    var series = [['ai', 'AI-assisted', 'var(--c-ai)'], ['human', 'Not AI (PRs checked)', 'var(--c-human)']];
    var bh = 16, gap = 2, group = bh * 2 + gap, gg = 22, m = { l: 84, r: 70, t: 4, b: 4 };
    var H = m.t + m.b + cats.length * group + (cats.length - 1) * gg;
    var max = 0;
    cats.forEach(function (c) { series.forEach(function (s) { var v = D.phases[s[0]][c[0]]; if (v != null && v > max) max = v; }); });
    var sc = niceScale(max || 1), pw = W - m.l - m.r;
    var svg = el('svg', { width: W, height: H, viewBox: '0 0 ' + W + ' ' + H }, host);
    cats.forEach(function (c, ci) {
      var y0 = m.t + ci * (group + gg);
      text(svg, m.l - 10, y0 + group / 2 + 4, c[1], 'end');
      var tipRows = series.map(function (s) {
        return { key: s[2], label: s[1] + ' (n=' + D.phases[s[0]].n + ')', value: fmtDays(D.phases[s[0]][c[0]]) };
      });
      series.forEach(function (s, si) {
        var v = D.phases[s[0]][c[0]], y = y0 + si * (bh + gap);
        if (v == null) return;
        var w = Math.max(1, v / sc.max * pw), r = Math.min(4, w);
        el('path', { d: 'M' + m.l + ',' + y + 'H' + (m.l + w - r) + 'Q' + (m.l + w) + ',' + y + ' ' + (m.l + w) + ',' + (y + r) +
          'V' + (y + bh - r) + 'Q' + (m.l + w) + ',' + (y + bh) + ' ' + (m.l + w - r) + ',' + (y + bh) + 'H' + m.l + 'Z', style: 'fill:' + s[2] }, svg);
        text(svg, m.l + w + 6, y + bh / 2 + 4, fmtDays(v), 'start', 'val');
      });
      var hit = el('rect', { class: 'hit', x: 0, y: y0 - gg / 2 + 2, width: W, height: group + gg - 4 }, svg);
      wire(hit, 'Median days: ' + c[1], tipRows);
    });
    el('line', { x1: m.l, x2: m.l, y1: m.t, y2: H - m.b, style: 'stroke:var(--axis);stroke-width:1' }, svg);
  }

  // 4. Stacked horizontal bars: stories per repository (top 15), AI-assisted vs not AI
  function repos(host) {
    host.textContent = '';
    var rows = D.repos, W = host.clientWidth || 600, bh = 16, rg = 10, m = { l: 190, r: 52, t: 4, b: 4 };
    var H = m.t + m.b + Math.max(1, rows.length) * (bh + rg) - rg;
    var sc = niceScale(Math.max.apply(null, rows.map(function (r) { return r.ai + r.human; }).concat([1])));
    var pw = W - m.l - m.r, svg = el('svg', { width: W, height: H, viewBox: '0 0 ' + W + ' ' + H }, host);
    el('line', { x1: m.l, x2: m.l, y1: m.t, y2: H - m.b, style: 'stroke:var(--axis);stroke-width:1' }, svg);
    rows.forEach(function (r, i) {
      var y = m.t + i * (bh + rg), total = r.ai + r.human, x = m.l;
      var name = r.name.length > 30 ? r.name.slice(0, 29) + '…' : r.name;
      text(svg, m.l - 10, y + bh / 2 + 4, name, 'end');
      [['ai', 'var(--c-ai)'], ['human', 'var(--c-human)']].forEach(function (s, j, arr) {
        var v = r[s[0]]; if (!v) return;
        var w = v / sc.max * pw - (x > m.l ? 2 : 0), last = (j === arr.length - 1) || !r[arr[j + 1][0]];
        if (w <= 0) return;
        var x0 = x > m.l ? x + 2 : x, rr = last ? Math.min(4, w, bh / 2) : 0;
        el('path', { d: 'M' + x0 + ',' + y + 'H' + (x0 + w - rr) + (rr ? 'Q' + (x0 + w) + ',' + y + ' ' + (x0 + w) + ',' + (y + rr) + 'V' + (y + bh - rr) +
          'Q' + (x0 + w) + ',' + (y + bh) + ' ' + (x0 + w - rr) + ',' + (y + bh) : 'V' + (y + bh)) + 'H' + x0 + 'Z', style: 'fill:' + s[1] }, svg);
        x = x0 + w;
      });
      text(svg, x + 6, y + bh / 2 + 4, String(total), 'start', 'val');
      var hit = el('rect', { class: 'hit', x: 0, y: y - rg / 2, width: W, height: bh + rg }, svg);
      wire(hit, r.full, [{ key: 'var(--c-ai)', label: 'AI-assisted', value: String(r.ai) },
        { key: 'var(--c-human)', label: 'Not AI', value: String(r.human) },
        { key: null, label: 'AI share', value: (100 * r.ai / total).toFixed(0) + '%' }]);
    });
  }

  var hosts = [[document.getElementById('c1'), columns], [document.getElementById('c2'), line], [document.getElementById('c3'), bars], [document.getElementById('c4'), repos]];
  function draw() { hosts.forEach(function (h) { if (h[0]) h[1](h[0]); }); }
  draw();
  if (window.ResizeObserver) { var t; new ResizeObserver(function () { clearTimeout(t); t = setTimeout(draw, 80); }).observe(document.querySelector('main')); }
})();
"""


def chart_data(stories: list[Story]) -> dict:
    months = sorted({s.done[:7] for s in stories})
    if months:  # fill gaps so the time axis is continuous
        y, m = map(int, months[0].split("-"))
        ly, lm = map(int, months[-1].split("-"))
        months = []
        while (y, m) <= (ly, lm):
            months.append(f"{y:04d}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    rows = []
    for mo in months:
        g = [s for s in stories if s.done[:7] == mo]
        ai, human = sum(s.ai == "ai" for s in g), sum(s.ai == "human" for s in g)
        rows.append({"m": mo, "ai": ai, "human": human, "nodata": len(g) - ai - human,
                     "pct": round(100.0 * ai / (ai + human), 2) if ai + human else None})
    phases = {}
    for k in ("ai", "human"):
        g = [s for s in stories if s.ai == k]
        r = lambda v: None if v is None else round(v, 2)
        phases[k] = {"n": len(g), "created": r(med([s.phase_days["created"] for s in g])),
                     "committed": r(med([s.phase_days["committed"] for s in g])), "lead": r(med([s.lead_days for s in g]))}
    rs = repo_stats(stories)[:15]
    labels = repo_labels(r["repo"] for r in rs)
    repos = [{"name": labels[r["repo"]], "full": r["repo"], "ai": r["ai"], "human": r["human"]} for r in rs]
    return {"months": rows, "phases": phases, "repos": repos}


def repo_labels(repos) -> dict[str, str]:
    """Short names: drop the org / group prefix when every repository shares it."""
    repos = list(repos)
    heads = {r.split("/", 1)[0] for r in repos}
    return {r: (r.split("/", 1)[1] if len(heads) == 1 and "/" in r else r) for r in repos}


def summarize(g: list[Story], ai: list[Story], human: list[Story]) -> dict:
    return {"n": len(g), "ai": len(ai), "human": len(human),
            "pct": 100.0 * len(ai) / len(g) if g else None,
            "created": med([s.phase_days["created"] for s in g]), "committed": med([s.phase_days["committed"] for s in g]),
            "lead": med([s.lead_days for s in g]),
            "lead_ai": med([s.lead_days for s in ai]), "lead_human": med([s.lead_days for s in human])}


def repo_stats(stories: list[Story]) -> list[dict]:
    """One row per repository: stories whose matched PRs touch it. A story with PRs in several
    repositories counts once in each, and is AI in a repository when that repository's PR is AI-assisted."""
    out = []
    for r in sorted({r for s in stories for r in s.repos}):
        g = [s for s in stories if r in s.repos]
        out.append({"repo": r, **summarize(g, [s for s in g if r in s.ai_repos], [s for s in g if r not in s.ai_repos])})
    return sorted(out, key=lambda x: (-x["n"], x["repo"].lower()))


LABEL = {"ai": "AI-assisted", "human": "Not AI (PRs checked)", "nodata": "No PR data"}


def render(org: str, project: str, wtype: str, stories: list[Story], states: list[str], since: dt.date | None,
           prs_path: str, pr_count: int, filter_note: str = "") -> str:
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

    def repo_rows() -> str:
        rs = repo_stats(stories)
        labels = repo_labels(r["repo"] for r in rs)
        known_stories = [s for s in stories if s.repos]
        overall = summarize(known_stories, [s for s in known_stories if s.ai == "ai"], [s for s in known_stories if s.ai == "human"])
        norepo = [s for s in stories if not s.repos]
        pc = lambda v: "–" if v is None else f"{v:.1f}%"
        cells = lambda x: (f"<td class='num'>{x['n']:,}</td><td class='num'>{x['ai']:,}</td><td class='num'>{x['human']:,}</td>"
                           f"<td class='num'><b>{pc(x['pct'])}</b></td>"
                           f"<td class='num'>{fd(x['created'])}</td><td class='num'>{fd(x['committed'])}</td>"
                           f"<td class='num'>{fd(x['lead'])}</td><td class='num'>{fd(x['lead_ai'])}</td><td class='num'>{fd(x['lead_human'])}</td>")
        out = [f"<tr class='overall'><td><b>Overall</b> <span class='muted'>(stories with PR data)</span></td>{cells(overall)}</tr>"]
        for x in rs:
            out.append(f"<tr data-q='{esc(x['repo'].lower())}'><td title='{esc(x['repo'])}'>{esc(labels[x['repo']])}</td>{cells(x)}</tr>")
        if norepo:
            g = summarize(norepo, [], [])
            out.append(f"<tr class='muted'><td>No repository <span class='muted'>(no PR data)</span></td><td class='num'>{g['n']:,}</td>"
                       f"<td class='num'>–</td><td class='num'>–</td><td class='num'>–</td><td class='num'>{fd(g['created'])}</td>"
                       f"<td class='num'>{fd(g['committed'])}</td><td class='num'>{fd(g['lead'])}</td><td class='num'>–</td><td class='num'>–</td></tr>")
        return "".join(out)

    def story_rows() -> str:
        out = []
        for s in sorted(stories, key=lambda s: s.done, reverse=True):
            tag = {"ai": "AI", "human": "no AI", "nodata": "no PR data"}[s.ai]
            out.append(
                f"<tr data-q='{esc((str(s.id) + ' ' + s.title + ' ' + ' '.join(s.repos)).lower())}'>"
                f"<td><a href='{esc(s.url)}' target='_blank' rel='noopener'>#{s.id}</a> {esc(s.title)}"
                f"{' <span class=muted>(reopened)</span>' if s.reopened else ''}</td>"
                f"<td>{s.done[:10]}</td><td class='num'>{fd(s.phase_days['created'])}</td>"
                f"<td class='num'>{fd(s.phase_days['committed'])}</td><td class='num'>{fd(s.lead_days)}</td>"
                f"<td><span class='tag {s.ai}'>{tag}</span></td>"
                f"<td class='num'>{len(s.pr_matched)}/{len(s.pr_links)}</td>"
                f"<td class='muted'>{esc(', '.join(repo_labels(s.repos).values()))}</td></tr>")
        return "".join(out)

    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    plural = wtype[:-1] + "ies" if wtype.lower().endswith("y") else wtype + "s"
    chart_json = json.dumps(chart_data(stories)).replace("</", "<\\/")
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
{CHART_CSS}</style></head><body><main>
<h1>Done {esc(plural)} · {esc(project)}</h1>
<div class="muted">{esc(org)} · done {"since " + str(since) if since else "all time"}{" · " + esc(filter_note) if filter_note else ""} · AI status from {pr_count:,} PRs in {esc(os.path.basename(prs_path)) or "n/a"} · generated {generated}</div>
<div class="cards">{cards()}</div>

<h2>Trends</h2>
<div class="charts">
  <section class="card chart"><h3>Stories done per month</h3><p class="muted">By whether a linked PR was AI-assisted</p>
    <div class="legend"><span class="lk"><i style="background:var(--c-ai)"></i>AI-assisted</span><span class="lk"><i style="background:var(--c-human)"></i>Not AI (PRs checked)</span><span class="lk"><i style="background:var(--c-nodata)"></i>No PR data</span></div>
    <div id="c1" class="host"></div></section>
  <section class="card chart"><h3>Share of AI-assisted stories per month</h3><p class="muted">Of the stories done that month with a linked PR found in the PR report</p>
    <div id="c2" class="host"></div></section>
  <section class="card chart"><h3>Median days per phase</h3><p class="muted">AI-assisted vs not AI; stories without PR data are left out</p>
    <div class="legend"><span class="lk"><i style="background:var(--c-ai)"></i>AI-assisted</span><span class="lk"><i style="background:var(--c-human)"></i>Not AI (PRs checked)</span></div>
    <div id="c3" class="host"></div></section>
  <section class="card chart"><h3>Stories per repository</h3><p class="muted">Top 15 repositories by stories with a matched PR; the table below lists them all</p>
    <div class="legend"><span class="lk"><i style="background:var(--c-ai)"></i>AI-assisted</span><span class="lk"><i style="background:var(--c-human)"></i>Not AI (PRs checked)</span></div>
    <div id="c4" class="host"></div></section>
</div>
<p class="muted">Hover or focus a column, point or bar for its values. The same numbers are in the tables below.</p>
<div id="tip" role="tooltip"></div>
<script type="application/json" id="chart-data">{chart_json}</script>

<h2>AI-assisted vs not (median days)</h2>
<div class="wrap"><table><tr><th>Group</th><th class="num">Stories</th><th class="num">Share</th><th class="num">Created</th><th class="num">Committed</th><th class="num">Lead time</th><th class="num">Mean lead</th></tr>{cmp_rows()}</table></div>
<p class="muted">Created = time in Proposed-category states, committed = time in InProgress-category states, lead time = creation until done. "No PR data" means no linked PR was found in the PR report (no link, or a repository it did not scan); these stories may or may not be AI-assisted.</p>

<h2>Time per state (days)</h2>
<div class="wrap"><table><tr><th rowspan="2">State</th><th class="num" colspan="2">All stories</th><th class="num" rowspan="2">Mean</th><th class="num" rowspan="2">P90</th><th class="num" colspan="2">AI-assisted</th><th class="num" colspan="2">Not AI</th></tr>
<tr><th class="num">n</th><th class="num">median</th><th class="num">n</th><th class="num">median</th><th class="num">n</th><th class="num">median</th></tr>{state_rows()}</table></div>
<p class="muted">Counts only completed stays; a story that moved back and forth adds up its time per state. Done itself has no duration because every story ends there.</p>

<h2>Per month (by done date)</h2>
<div class="wrap"><table><tr><th>Month</th><th class="num">Done</th><th class="num">AI</th><th class="num">With PR data</th><th class="num">% AI</th><th class="num">Created d</th><th class="num">Committed d</th><th class="num">Lead d</th></tr>{month_rows()}</table></div>

<h2>Per repository</h2>
<p><input id="rq" placeholder="Filter repositories"></p>
<div class="wrap"><table id="rt"><tr><th rowspan="2">Repository</th><th class="num" colspan="3">Stories</th><th class="num" rowspan="2">% AI</th><th class="num" colspan="3">Median days</th><th class="num" colspan="2">Lead time</th></tr>
<tr><th class="num">total</th><th class="num">AI</th><th class="num">not AI</th><th class="num">created</th><th class="num">committed</th><th class="num">lead</th><th class="num">AI</th><th class="num">not AI</th></tr>{repo_rows()}</table></div>
<p class="muted">A story counts once in every repository its linked PRs touch, so the rows can add up to more than the overall row. A story is AI in a repository when that repository's PR is AI-assisted. Stories without a matched PR have no repository.</p>

<h2>Stories</h2>
<p><input id="q" placeholder="Filter by id or title"></p>
<div class="wrap"><table id="t"><tr><th>Story</th><th>Done</th><th class="num">Created d</th><th class="num">Committed d</th><th class="num">Lead d</th><th>AI</th><th class="num">PRs found/linked</th><th>Repositories</th></tr>{story_rows()}</table></div>
<script>
document.getElementById('q').addEventListener('input', e => {{
  const q = e.target.value.toLowerCase();
  document.querySelectorAll('#t tr[data-q]').forEach(r => r.hidden = !r.dataset.q.includes(q));
}});
document.getElementById('rq').addEventListener('input', e => {{
  const q = e.target.value.toLowerCase();
  document.querySelectorAll('#rt tr[data-q]').forEach(r => r.hidden = !r.dataset.q.includes(q));
}});
</script>
</main><script>{CHART_JS}</script></body></html>"""


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
    ap.add_argument("--config", help="JSON file narrowing which stories are used (areaPaths, createdSince, requireTasks); see stories.example.json")
    ap.add_argument("--created-since", default=None, help="Only stories created on/after this date (YYYY-MM-DD); overrides the config")
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

    try:
        scfg = story_config.load(args.config) if args.config else story_config.StoryConfig()
        if args.created_since:
            scfg.created_since = story_config.parse_date(args.created_since, "--created-since")
    except story_config.ConfigError as e:
        ap.error(str(e))

    ado = Ado(args.org, args.tenant)
    org_url = ado.base
    ids = query_ids(ado, args.project, args.type, since, scfg.created_since)
    if args.limit:
        ids = ids[-args.limit:]
    print(f"{len(ids)} {args.type} work items in state Done since {args.since} in {args.org}/{args.project}", file=sys.stderr)
    if not ids:
        return 1

    cats_raw = ado.request(f"{urllib.parse.quote(args.project)}/_apis/wit/workitemtypes/{urllib.parse.quote(args.type)}/states").get("value", [])
    categories = {s["name"]: s.get("category") or "" for s in cats_raw}
    items = fetch_items(ado, ids)
    if scfg.areas.active() or scfg.require_tasks:
        before = len(items)
        items = story_config.select(items, scfg)
        print(f"Story selection kept {len(items)} of {before} ({scfg.describe()})", file=sys.stderr)
        for p in scfg.areas.unused():
            print(f"  warning: area pattern '{p}' matched no story (typo?)", file=sys.stderr)
        if not items:
            return 1
    descendants = fetch_descendants(ado, items)
    print(f"Fetched {len(items)} work items and {sum(len(v) for v in descendants.values())} child items; reading state history...", file=sys.stderr)

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
                st = build_story(wi, fut.result(), org_url, categories, descendants.get(wi["id"]))
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
        per_repo: dict[str, bool] = {}
        for u in s.pr_matched:
            pr = prs[norm_url(u)]
            per_repo[pr["repo"]] = per_repo.get(pr["repo"], False) or bool(pr.get("ai"))
        s.repos = sorted(per_repo)
        s.ai_repos = sorted(r for r, is_ai in per_repo.items() if is_ai)

    states = [n for n, c in sorted(categories.items(), key=lambda kv: list(categories).index(kv[0])) if n != "Done" and c != "Removed"]
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(render(args.org, args.project, args.type, stories, states, since, args.prs, len(prs), scfg.describe()))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([asdict(s) for s in stories], f, indent=2)
    counts = {k: sum(1 for s in stories if s.ai == k) for k in LABEL}
    print(f"Done: {len(stories)} stories ({counts['ai']} AI, {counts['human']} not AI, {counts['nodata']} no PR data). "
          f"Report written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
