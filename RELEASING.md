# Releases

Each squash merge into `main` creates a version tag and GitHub release after tests
and a Docker build/import smoke test pass. The first release is `v0.1.0`.
`CHANGELOG.md` is generated and attached to each release; it is not committed.
Release notes are generated from Conventional Commit messages.

Use PR titles such as `fix: restore artwork` or `feat: add renderer settings`.
`feat` increments the minor version. `!` or a `BREAKING CHANGE:` footer increments
the major version, including from version zero. All other accepted types (`fix`,
`perf`, `build`, `chore`, `ci`, `docs`, `style`, `refactor`, `test`, `revert`)
increment the patch version.

Repository settings must allow **squash merging only**, use the **PR title** as
the squash commit title and **PR description** as its body. Require these checks:
`Conventional PR title`, `Tests (Python 3.12)`, `Tests (Python 3.13)`, and
`Docker smoke test`. Block direct pushes and force pushes to main. These settings
must be configured separately; workflow files cannot enforce them alone.

Release runs are serialized and drain untagged first-parent commits in order,
starting after the upstream baseline recorded in `.github/scripts/release.py`.
This prevents closely spaced merges from being combined into one release.
Failure stops the backlog; rerun the Release workflow after fixing the cause.
If a tag was pushed but its release or attachment failed, rerunning repairs it.
Tags point directly at tested commits; the bot never commits or pushes main.

Only the fork's release job has `contents: write`, using `GITHUB_TOKEN`.
No personal token, PyPI publishing, container publishing, or server deployment
is required in this first PR. Docker image publishing can follow in another PR.

For local tests, select Python with pyenv, create a `.venv`, install
`requirements-dev.txt`, and run `.venv/bin/python -m pytest -q`. FFmpeg must be
installed for the audio integration test. The release coordinator refuses to
run outside this fork's GitHub Actions.
