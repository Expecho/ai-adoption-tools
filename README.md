# ai-adoption-tools

Small tools for measuring how much AI coding assistance is used in a software organisation.

## Contents

```
src/pr-reports/ai_pr_report.py      AI-assisted PR/MR report for GitHub orgs and GitLab groups
src/pr-reports/report_config.py     source filters (wildcards) and config validation
src/pr-reports/sources.schema.json  JSON Schema for the config file
src/pr-reports/sources.example.json example config (copy to sources.json, which is git-ignored)
src/pr-reports/test_report_config.py unit tests (python -m unittest, run from src/pr-reports)
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
