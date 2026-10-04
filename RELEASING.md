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

Only the fork's release job has `contents: write`, `packages: write`, and `pull-requests: read`,
using `GITHUB_TOKEN`. Python Semantic Release creates tags with an explicit bump
derived from the PR; GitHub CLI creates releases without updating existing notes.
No personal token or PyPI publishing is required. Server deployment is a separate action.

## Docker images

The release job publishes `ghcr.io/leonroy/airplay2-dlna-bridge` for `linux/amd64`
and `linux/arm64`, starting after `v0.1.0`. Existing `v0.1.0` is not backfilled.
It checks out each release's exact commit, builds a `sha-<full-commit-sha>`
candidate, and tests bridge import and exact PCM/FLAC round trips on both
architectures. It promotes the tested manifest digest to the version without
`v` (e.g. `0.1.1`), then to `latest` for the newest release.

Retries repair image publishing without creating another release. Existing
version images must match the source revision, version, and platforms; they
are not overwritten. Retrying an older version cannot move `latest` backwards.
Candidate tags are retained. Image references and digests appear in the Actions
job summary. The image is linked to this repository using its source label.

After the first successful publish, open your GitHub profile's **Packages**,
select **airplay2-dlna-bridge**, and open **Package settings → Change visibility →
Public**. GitHub initially creates the package as private. Verify an anonymous
pull using a Docker client with no GHCR credentials. Package write access
remains tied to the repository; public visibility permits downloads, not pushes.

Production Compose pulls the image selected by `BRIDGE_IMAGE_TAG` (default
`latest`). Pin a version in `.env`, run `docker compose pull bridge`, then
`docker compose up -d bridge`. For rollback, select an earlier published version
and repeat those commands. `v0.1.0` has no published image.

For local builds, use the development override described in the
[README quick start](README.md#quick-start).

## Automation authorization

GitHub Actions may create release tags, publish releases, and push Docker images
as configured in explicitly approved workflows. This does not authorize the
agent to commit, push, publish, deploy, or delete files directly.

For local tests, select Python with pyenv, create a `.venv`, install
`requirements-dev.txt`, and run `.venv/bin/python -m pytest -q`. FFmpeg must be
installed for the audio integration test. The release coordinator refuses to
run outside this fork's GitHub Actions.
