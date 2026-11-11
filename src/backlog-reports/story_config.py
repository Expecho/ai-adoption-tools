"""Story selection config for ado_story_report.py (standard library only).

A JSON config file narrows which done stories are analysed:

    {
      "areaPaths": { "include": ["Ibis\\\\Cashcows*"], "exclude": ["Ibis\\\\Ibis Cloud"] },
      "createdSince": "2025-01-01",
      "requireTasks": true
    }

areaPaths    include/exclude lists of area paths. Matching is case-insensitive. `*` matches within one
             path level (not '\\'), `**` also across levels, `?` one character. A pattern matches the
             area itself and everything below it, like Azure DevOps' UNDER. An empty `include` means
             every area; `exclude` always wins.
createdSince only stories created on or after this date (YYYY-MM-DD).
requireTasks only stories that have at least one child work item (the tasks developers usually link
             their pull requests from).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SEP = "\\"
KEYS = {"$schema", "areaPaths", "createdSince", "requireTasks"}


class ConfigError(ValueError):
    pass


def compile_area(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        ch = pattern[i]
        out.append(r"[^\\]*" if ch == "*" else r"[^\\]" if ch == "?" else re.escape(ch))
        i += 1
    # the pattern itself, or anything below it
    return re.compile("".join(out) + r"(?:\\.*)?", re.I | re.S)


@dataclass
class AreaFilter:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    hits: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._inc = [(p, compile_area(p)) for p in self.include]
        self._exc = [(p, compile_area(p)) for p in self.exclude]
        self.hits = {p: 0 for p in [*self.include, *self.exclude]}

    def allows(self, area: str) -> bool:
        inc = [p for p, rx in self._inc if rx.fullmatch(area)]
        exc = [p for p, rx in self._exc if rx.fullmatch(area)]
        for p in inc + exc:
            self.hits[p] += 1
        return not exc and (bool(inc) or not self._inc)

    def unused(self) -> list[str]:
        return [p for p, n in self.hits.items() if n == 0]

    def active(self) -> bool:
        return bool(self.include or self.exclude)


@dataclass
class StoryConfig:
    areas: AreaFilter = field(default_factory=AreaFilter)
    created_since: str | None = None
    require_tasks: bool = False

    def describe(self) -> str:
        parts = []
        if self.areas.include:
            parts.append("areas " + ", ".join(self.areas.include))
        if self.areas.exclude:
            parts.append("excluding areas " + ", ".join(self.areas.exclude))
        if self.created_since:
            parts.append(f"created since {self.created_since}")
        if self.require_tasks:
            parts.append("with child tasks only")
        return "; ".join(parts)


def _patterns(value, where: str) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected an array of area paths or wildcards")
    seen: set[str] = set()
    for p in value:
        if not isinstance(p, str) or not p.strip():
            raise ConfigError(f"{where}: entries must be non-empty strings (got {p!r})")
        if p.lower() in seen:
            raise ConfigError(f"{where}: duplicate pattern {p!r}")
        seen.add(p.lower())
    return list(value)


def parse_date(value, where: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ConfigError(f"{where}: expected 'YYYY-MM-DD'")
    return value


def parse(data: object) -> StoryConfig:
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    unknown = set(data) - KEYS
    if unknown:
        raise ConfigError(f"unknown key(s) {sorted(unknown)}; allowed: {', '.join(sorted(KEYS - {'$schema'}))}")
    cfg = StoryConfig()
    areas = data.get("areaPaths")
    if areas is not None:
        if not isinstance(areas, dict):
            raise ConfigError("areaPaths: expected an object with 'include' and/or 'exclude'")
        bad = set(areas) - {"include", "exclude"}
        if bad:
            raise ConfigError(f"areaPaths: unknown key(s) {sorted(bad)}; allowed: include, exclude")
        cfg.areas = AreaFilter(include=_patterns(areas.get("include", []), "areaPaths.include"),
                               exclude=_patterns(areas.get("exclude", []), "areaPaths.exclude"))
    if data.get("createdSince") is not None:
        cfg.created_since = parse_date(data["createdSince"], "createdSince")
    if "requireTasks" in data:
        if not isinstance(data["requireTasks"], bool):
            raise ConfigError("requireTasks: expected true or false")
        cfg.require_tasks = data["requireTasks"]
    return cfg


def load(path: str) -> StoryConfig:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}") from e
    try:
        return parse(data)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e


def has_children(work_item: dict) -> bool:
    return any(r.get("rel") == "System.LinkTypes.Hierarchy-Forward" for r in work_item.get("relations") or [])


def select(items: list[dict], cfg: StoryConfig) -> list[dict]:
    """Apply the area and task filters to fetched work items (createdSince is done in the query)."""
    out = []
    for wi in items:
        if cfg.areas.active() and not cfg.areas.allows((wi.get("fields") or {}).get("System.AreaPath") or ""):
            continue
        if cfg.require_tasks and not has_children(wi):
            continue
        out.append(wi)
    return out
