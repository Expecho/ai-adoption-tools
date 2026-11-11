# ai-adoption-tools

Small tools for measuring how much AI coding assistance is used in a software organisation.

## Contents

```
src/pr-reports/ai_pr_report.py      AI-assisted PR/MR report for GitHub orgs and GitLab groups
src/pr-reports/report_config.py     source filters (wildcards) and config validation
src/pr-reports/sources.schema.json  JSON Schema for the config file
src/pr-reports/sources.example.json example config (copy to sources.json, which is git-ignored)
src/pr-reports/test_report_config.py unit tests (python -m unittest, run from src/pr-reports)
src/backlog-reports/ado_story_report.py   Azure DevOps: time in state and AI usage for done user stories
src/backlog-reports/story_config.py        story selection config (area paths, created date, tasks required)
src/backlog-reports/stories.schema.json    JSON Schema for that config
src/backlog-reports/stories.example.json   example (copy to stories.json, which is git-ignored)
src/backlog-reports/test_ado_story_report.py unit tests (python -m unittest, run from src/backlog-reports)
```

### `ai_pr_report.py`

Scans every repository of a GitHub organisation or GitLab group, classifies each pull request and its commits as AI-assisted or not, and writes a self-contained HTML report (optionally also JSON) with:

- AI-assisted PRs per repository
- % of PRs that are AI-assisted, per month
- % of committed code (lines added in merged PRs) that is AI-generated, per month
- a repository x month heatmap
- the kind of work (feature, bug fix, refactor, ...) of AI-assisted PRs, classified with headless Claude (`claude -p`)

Detected tools: Claude (Claude Code), GitHub Copilot and Cursor. Detection is based on the PR author, the branch name, and markers in the PR description, commit messages and commit authors (for example `Co-Authored-By: Claude`). The rules live in the `TOOLS` dict at the top of the script.

#### Requirements

- Python 3.10+, no third-party packages
- For GitHub: the [GitHub CLI](https://cli.github.com/) (`gh`), authenticated with access to the organisation (`gh auth login`)
- For GitLab: the [GitLab CLI](https://gitlab.com/gitlab-org/cli) (`glab`), authenticated via `glab auth login` or a `GITLAB_TOKEN`. Use a classic personal (or group) access token with the `read_api` scope and at least the Reporter role: it enables the fast GraphQL path. Fine-grained tokens are not accepted by GitLab's GraphQL API, so the tool falls back to the much slower REST path (one call per merge request and commit)
- Optional: the `claude` CLI on the PATH, for purpose classification (`--classify none` skips it)

#### Usage

```sh
python src/pr-reports/ai_pr_report.py --org my-org                       # one GitHub org, last 12 months
python src/pr-reports/ai_pr_report.py --source gitlab --org my-group     # one GitLab group
python src/pr-reports/ai_pr_report.py --config sources.json              # orgs, groups and repos from a config file
python src/pr-reports/ai_pr_report.py --config sources.json --dry-run    # list what would be scanned, fetch nothing
python src/pr-reports/ai_pr_report.py --org my-org --since 2026-01-01
python src/pr-reports/ai_pr_report.py --org my-org --classify all        # classify every PR, not just AI ones
python src/pr-reports/ai_pr_report.py --org my-org --repos RepoA 'api-*'
python src/pr-reports/ai_pr_report.py --org my-org --out report.html --json prs.json
```

On GitLab, merge requests are treated as PRs. With GraphQL, a project's merge requests, commits and total added lines come back in a few calls, and per-commit line counts are only fetched for AI commits of partly-AI merge requests. The GitLab code share therefore uses each merge request's net diff, which is slightly lower than the sum over commits used on GitHub and in the REST fallback. Run `--help` for all options. Fetched data is cached per repository in `.cache/` (12 hours by default).

#### Choosing what is scanned (config file)

A config file selects GitHub orgs, GitLab groups and repositories, and combines them in one report. Copy `sources.example.json` to `sources.json` (git-ignored, so your org and group names stay private) and pass it with `--config`. The `$schema` key gives autocompletion in editors.

```json
{
  "$schema": "./sources.schema.json",
  "since": "2026-01-01",
  "github": {
    "orgs":  { "include": ["my-org"] },
    "repos": { "exclude": ["*-archive", "demo-*"] }
  },
  "gitlab": {
    "groups": { "include": ["my-group", "my-group/team-*"], "exclude": ["my-group/sandbox*"] },
    "repos":  { "include": ["app-*", "shared-lib"], "exclude": ["my-group/app-prototype"] }
  },
  "options": { "includeArchived": false, "includeBots": false }
}
```

- `github.orgs` / `gitlab.groups` choose where to look and need at least one `include`. `repos` filters the repositories found there.
- Every filter is a pair of arrays, `include` and `exclude`, of names or wildcards. A name is processed if it matches an `include` pattern (an empty `include` means everything) and no `exclude` pattern, so `exclude` always wins.
- Wildcards: `*` matches within one path segment, `**` also across `/`, `?` matches one character. Matching is case-insensitive and against the whole name.
- Repo patterns without a `/` match the bare repository name in any org or group. Patterns with a `/` match the full path (`org/repo`, `group/sub/project`).
- Literal org and group names are used as-is. Wildcard names are matched against the orgs you belong to (GitHub) or the groups your token can list (GitLab, which needs a token that can read groups).
- Patterns that match nothing are reported on stderr, which usually means a typo.
- Command-line flags override the config: `--source` limits the run to one platform, `--org` replaces that platform's orgs/groups, `--repos` replaces the repo include list, and `--since` and the `--include-*` flags win over the config's `since` and `options`.

#### Privacy

The report and cache contain PR titles, descriptions, commit messages and author emails from your organisation. They are git-ignored here; do not publish them if the repositories are private.

### `ado_story_report.py` (Azure DevOps backlog)

Looks at the user stories (or another work item type) that reached `Done` and reports how long they sat in each state and whether they were solved with AI:

- time per state (New, Approved, Refined, Committed, Testable, ...) with median, mean and P90
- three phases: **created** (time in Proposed-category states), **committed** (InProgress-category states) and **lead time** (creation until done)
- the same figures split into AI-assisted, not AI and no PR data, plus trend charts, a per-month table and a filterable story list
- a per-repository breakdown next to the overall figures: a story belongs to the repositories of its matched PRs (once in each) and is AI in a repository when that repository's PR is AI-assisted

A story counts as AI-assisted when one of its linked pull / merge requests (on the story itself or on its child tasks, since developers usually link from the task) is AI-assisted in the JSON written by `ai_pr_report.py --json`, so run that first. Stories without a linked PR found in that data are shown as "no PR data" rather than "not AI". GitHub PR links created by Azure Boards (which only contain a repository id and a PR number) are matched by PR number plus creation time; GitLab merge request links and plain URLs match directly. Azure Repos pull requests are not matched.

```sh
python src/backlog-reports/ado_story_report.py                          # org/project from `az devops configure`
python src/backlog-reports/ado_story_report.py --org my-org --project MyProject --since 2026-01-01
python src/backlog-reports/ado_story_report.py --prs src/pr-reports/ai-prs.json --out ai-stories.html --json ai-stories.json
```

#### Choosing which stories are used (config file)

By default every story of the type that reached `Done` in the period is used. A config file narrows that. Copy `stories.example.json` to `stories.json` (git-ignored, so your area paths stay private) and pass it with `--config`:

```json
{
  "$schema": "./stories.schema.json",
  "areaPaths": { "include": ["MyProject\Team A", "MyProject\Team B*"], "exclude": ["MyProject\Team A\Operations"] },
  "createdSince": "2025-01-01",
  "requireTasks": true
}
```

- `areaPaths`: `include` / `exclude` lists of area paths. A pattern matches the area and everything below it (like `UNDER` in Azure DevOps). `*` matches within one level, `**` across levels, `?` one character; case-insensitive. `exclude` wins, an empty `include` means every area. Patterns that match no story are reported on stderr.
- `createdSince`: only stories created on or after this date (also available as `--created-since`, which wins over the config).
- `requireTasks`: only stories with at least one child work item. Useful when pull requests are linked from tasks, because a story without tasks usually has no PR to find.

The report header lists the active selection. `--since` still controls the done date.

Authentication uses your Azure CLI login (`az login`). A login can span several Entra tenants and only one backs the organization, so the tool tries each tenant until Azure DevOps accepts one (`--tenant` pins it). Alternatively set a personal access token in `ADO_PAT`. State histories are cached in `.cache/`. The report contains story titles and is git-ignored.
