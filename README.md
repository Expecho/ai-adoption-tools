# ai-adoption-tools

Small tools for measuring how much AI coding assistance is used in a software organisation.

## Contents

```
src/pr-reports/ai_pr_report.py   AI-assisted pull request report for a GitHub organisation
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
- For GitLab: the [GitLab CLI](https://gitlab.com/gitlab-org/cli) (`glab`), authenticated via `glab auth login` or a `GITLAB_TOKEN`. A classic token needs `read_api`. A fine-grained token needs read access to merge requests and repository commits
- Optional: the `claude` CLI on the PATH, for purpose classification (`--classify none` skips it)

#### Usage

```sh
python src/pr-reports/ai_pr_report.py --org my-org                    # last 12 months
python src/pr-reports/ai_pr_report.py --org my-org --since 2026-01-01
python src/pr-reports/ai_pr_report.py --source gitlab --org my-group  # GitLab group (subgroups: my-group/sub)
python src/pr-reports/ai_pr_report.py --org my-org --classify all     # classify every PR, not just AI ones
python src/pr-reports/ai_pr_report.py --org my-org --repos RepoA RepoB
python src/pr-reports/ai_pr_report.py --org my-org --out report.html --json prs.json
```

On GitLab, merge requests are treated as PRs and the group's projects are discovered from its merge requests. Run `--help` for all options. Fetched data is cached per repository in `.cache/` (12 hours by default).

#### Privacy

The report and cache contain PR titles, descriptions, commit messages and author emails from your organisation. They are git-ignored here; do not publish them if the repositories are private.
