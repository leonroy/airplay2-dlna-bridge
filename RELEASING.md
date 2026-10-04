# Releases

Each PR merged into `main` creates one version tag and GitHub release after tests
and a Docker build/import smoke test pass. Squash, merge, and rebase are supported.
The first release is `v0.1.0` (or `v1.0.0` for a breaking change).
`CHANGELOG.md` is generated and attached to each release; it is not committed.
Release notes use the PR title and description. The attachment contains the
cumulative changelog through that PR. Individual commit messages do not affect
versioning or appear in the generated notes.

Use PR titles such as `fix: restore artwork` or `feat: add renderer settings`.
`feat` increments the minor version. `!` in the title or a `BREAKING CHANGE:` footer
in the PR description increments
the major version, including from version zero. All other accepted types (`fix`,
`perf`, `build`, `chore`, `ci`, `docs`, `style`, `refactor`, `test`, `revert`)
increment the patch version.

All three GitHub merge methods can remain enabled. Require these checks:
`Conventional PR title`, `Tests (Python 3.12)`, `Tests (Python 3.13)`, and
`Docker smoke test`. Block direct pushes and force pushes to main. These settings
must be configured separately; workflow files cannot enforce them alone.

Release runs are serialized and discover merged PRs through the paginated GitHub
API. They process each PR's final main commit in first-parent history order,
starting after the upstream baseline recorded in `.github/scripts/release.py`.
This prevents closely spaced merges from being combined into one release.
Failure stops the backlog; rerun the Release workflow after fixing the cause.
If a tag was pushed but its release or attachment failed, rerunning repairs it.
Tags point directly at tested commits; the bot never commits or pushes main.
Direct pushes do not receive a separate release. If a PR title is invalid,
the backlog stops with an error; correct the title and rerun the workflow.

Only the fork's release job has `contents: write` and `pull-requests: read`,
using `GITHUB_TOKEN`. Python Semantic Release creates tags with an explicit bump
derived from the PR; GitHub CLI creates releases without updating existing notes.
No personal token, PyPI publishing, container publishing, or server deployment
is required in this first PR. Docker image publishing can follow in another PR.

For local tests, select Python with pyenv, create a `.venv`, install
`requirements-dev.txt`, and run `.venv/bin/python -m pytest -q`. FFmpeg must be
installed for the audio integration test. The release coordinator refuses to
run outside this fork's GitHub Actions.
