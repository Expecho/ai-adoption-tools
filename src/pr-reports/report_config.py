"""Source filters and config handling for ai_pr_report.py (standard library only).

A config file selects which GitHub orgs / GitLab groups and which repositories are processed.
Every filter is a pair of arrays, `include` and `exclude`, holding names or wildcards:

    *   matches any characters within one path segment (not '/')
    **  matches any characters including '/'
    ?   matches one character (not '/')

Matching is case-insensitive and always against the whole name. A name is allowed when it matches
at least one `include` pattern (an empty `include` means "everything") and no `exclude` pattern.

Repository patterns without a '/' are matched against the bare repository name; patterns with a
'/' are matched against the full path (`org/repo`, `group/sub/repo`).

The JSON Schema in sources.schema.json describes the same format for editors; validation at
runtime is done here so no third-party package is needed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

PLATFORMS = ("github", "gitlab")
OPTION_TYPES: dict[str, type] = {
    "includeArchived": bool, "includeForks": bool, "includeBots": bool,
    "maxPrLines": int, "heatmapMonths": int, "workers": int,
}
_MIN_ONE = {"heatmapMonths", "workers"}


class ConfigError(ValueError):
    pass


def is_literal(pattern: str) -> bool:
    return "*" not in pattern and "?" not in pattern


def compile_glob(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        out.append("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch))
        i += 1
    return re.compile("".join(out), re.I | re.S)


@dataclass
class Filter:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    hits: dict[str, int] = field(default_factory=dict)  # pattern -> number of names it matched

    def __post_init__(self) -> None:
        self._inc = [(p, compile_glob(p)) for p in self.include]
        self._exc = [(p, compile_glob(p)) for p in self.exclude]
        self.hits = {p: 0 for p in [*self.include, *self.exclude]}

    def _target(self, pattern: str, name: str) -> str:
        return name

    def allows(self, name: str) -> bool:
        inc = [p for p, rx in self._inc if rx.fullmatch(self._target(p, name))]
        exc = [p for p, rx in self._exc if rx.fullmatch(self._target(p, name))]
        for p in inc + exc:
            self.hits[p] += 1
        return not exc and (bool(inc) or not self._inc)

    def literals(self) -> list[str]:
        return [p for p in self.include if is_literal(p)]

    def wildcards(self) -> list[str]:
        return [p for p in self.include if not is_literal(p)]

    def unused(self) -> list[str]:
        return [p for p, n in self.hits.items() if n == 0]


class RepoFilter(Filter):
    """Patterns containing '/' match the full path, others the bare repository name."""

    def _target(self, pattern: str, name: str) -> str:
        return name if "/" in pattern else name.rsplit("/", 1)[-1]


@dataclass
class SourceConfig:
    scopes: Filter = field(default_factory=Filter)        # GitHub orgs / GitLab groups
    repos: RepoFilter = field(default_factory=RepoFilter)


@dataclass
class Config:
    since: str | None = None
    github: SourceConfig | None = None
    gitlab: SourceConfig | None = None
    options: dict = field(default_factory=dict)

    def platforms(self) -> list[str]:
        return [p for p in PLATFORMS if getattr(self, p)]


# --------------------------------------------------------------------------- #
# Parsing / validation
# --------------------------------------------------------------------------- #

def _patterns(value, where: str) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected an array of names or wildcards")
    seen: set[str] = set()
    for p in value:
        if not isinstance(p, str) or not p.strip():
            raise ConfigError(f"{where}: entries must be non-empty strings (got {p!r})")
        if p != p.strip() or re.search(r"\s", p):
            raise ConfigError(f"{where}: {p!r} must not contain whitespace")
        if p.lower() in seen:
            raise ConfigError(f"{where}: duplicate pattern {p!r}")
        seen.add(p.lower())
    return list(value)


def _filter(cls, value, where: str, require_include: bool = False):
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected an object with 'include' and/or 'exclude'")
    unknown = set(value) - {"include", "exclude"}
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}; allowed: include, exclude")
    include = _patterns(value.get("include", []), f"{where}.include")
    exclude = _patterns(value.get("exclude", []), f"{where}.exclude")
    if require_include and not include:
        raise ConfigError(f"{where}.include: list at least one name or wildcard")
    return cls(include=include, exclude=exclude)


def parse(data: object) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    unknown = set(data) - {"$schema", "since", "github", "gitlab", "options"}
    if unknown:
        raise ConfigError(f"unknown key(s) {sorted(unknown)}; allowed: since, github, gitlab, options")
    cfg = Config()
    since = data.get("since")
    if since is not None:
        if not isinstance(since, str) or not (since == "all" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", since)):
            raise ConfigError("since: expected 'YYYY-MM-DD' or 'all'")
        cfg.since = since
    for platform, scope_key in (("github", "orgs"), ("gitlab", "groups")):
        block = data.get(platform)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ConfigError(f"{platform}: expected an object")
        unknown = set(block) - {scope_key, "repos"}
        if unknown:
            raise ConfigError(f"{platform}: unknown key(s) {sorted(unknown)}; allowed: {scope_key}, repos")
        if scope_key not in block:
            raise ConfigError(f"{platform}.{scope_key}: required")
        setattr(cfg, platform, SourceConfig(
            scopes=_filter(Filter, block[scope_key], f"{platform}.{scope_key}", require_include=True),
            repos=_filter(RepoFilter, block.get("repos", {}), f"{platform}.repos"),
        ))
    if not cfg.platforms():
        raise ConfigError("define at least one of 'github' or 'gitlab'")
    options = data.get("options", {})
    if not isinstance(options, dict):
        raise ConfigError("options: expected an object")
    for key, val in options.items():
        want = OPTION_TYPES.get(key)
        if want is None:
            raise ConfigError(f"options.{key}: unknown option; allowed: {', '.join(OPTION_TYPES)}")
        if not isinstance(val, want) or (want is int and isinstance(val, bool)):
            raise ConfigError(f"options.{key}: expected {want.__name__}")
        if want is int and val < (1 if key in _MIN_ONE else 0):
            raise ConfigError(f"options.{key}: must be >= {1 if key in _MIN_ONE else 0}")
    cfg.options = dict(options)
    return cfg


def load(path: str) -> Config:
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


def apply_cli(cfg: Config | None, source: str | None, org: str | None, repos: list[str] | None) -> Config:
    """Overlay command-line selections on a config (or build one when there is no config file).

    --source limits the run to one platform. --org replaces that platform's scope list
    (platform defaults to github). --repos replaces the include list of the repo filter.
    """
    cfg = cfg or Config()
    if org:
        platform = source or "github"
        existing = getattr(cfg, platform)
        setattr(cfg, platform, SourceConfig(
            scopes=Filter(include=[org]),
            repos=RepoFilter(list(existing.repos.include), list(existing.repos.exclude)) if existing else RepoFilter(),
        ))
        source = platform
    if source:
        if not getattr(cfg, source):
            raise ConfigError(f"--source {source}: no '{source}' section in the config (use --org or add one)")
        for p in PLATFORMS:
            if p != source:
                setattr(cfg, p, None)
    if repos:
        for p in cfg.platforms():
            sc = getattr(cfg, p)
            sc.repos = RepoFilter(list(repos), list(sc.repos.exclude))
    if not cfg.platforms():
        raise ConfigError("nothing to scan: pass --org, or --config with a github/gitlab section")
    return cfg
