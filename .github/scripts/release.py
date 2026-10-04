"""Release each merged PR once, independently of its Git merge method."""
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib

from semantic_release.commit_parser.conventional import ConventionalCommitParser, ConventionalCommitParserOptions
from semantic_release.enums import LevelBump

# History before release automation was introduced is not released individually.
BASELINE = "6089a33ae8ffa5020c10a98d30fcaa92e8423eec"
VERSION_TAG = re.compile(r"v\d+\.\d+\.\d+\Z")


def run(*args):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE)
    if result.returncode:
        print(result.stdout, flush=True)
        result.check_returncode()
    return result.stdout.strip()


def merged_pull_requests():
    endpoint = f"repos/{os.environ['GITHUB_REPOSITORY']}/pulls?state=closed&base=main&per_page=100"
    pages = json.loads(run("gh", "api", "--paginate", "--slurp", endpoint))
    return [pr for page in pages for pr in page if pr.get("merged_at")]


def ordered_pull_requests(commits, pull_requests):
    """GitHub's merge_commit_sha is the resulting main tip for all merge methods."""
    positions = {sha: i for i, sha in enumerate(commits)}
    eligible = []
    for pr in pull_requests:
        if not pr.get("merged_at") or pr["base"]["ref"] != "main":
            continue
        if not pr.get("merge_commit_sha"):
            raise ValueError(f"Merged PR #{pr['number']} has no final commit yet; retry later")
        if pr["merge_commit_sha"] in positions:
            eligible.append(pr)
    return sorted(eligible, key=lambda pr: positions[pr["merge_commit_sha"]])


def bump_for_pr(pr):
    config = tomllib.loads(Path("pyproject.toml").read_text())["tool"]["semantic_release"]
    parser = ConventionalCommitParser(ConventionalCommitParserOptions(**config["commit_parser_options"]))
    title = parser.parse_message(pr["title"])
    if title is None or title.bump == LevelBump.NO_RELEASE:
        raise ValueError(f"PR #{pr['number']} needs a Conventional Commit title")
    parsed = parser.parse_message(pr["title"] + "\n\n" + (pr.get("body") or ""))
    return {LevelBump.PATCH: "patch", LevelBump.MINOR: "minor", LevelBump.MAJOR: "major"}[parsed.bump]


def release_notes(pr):
    body = (pr.get("body") or "").strip()
    return f"## {pr['title']}\n\nPull request: {pr['html_url']}\n\n{body}\n"


def changelog(entries):
    return "# Changelog\n\n" + "\n".join(
        f"## {tag}\n\n{release_notes(pr)}" for tag, pr in reversed(entries)
    )


def get_release(tag):
    endpoint = f"repos/{os.environ['GITHUB_REPOSITORY']}/releases/tags/{tag}"
    result = subprocess.run(
        ["gh", "api", "--include", endpoint], text=True, capture_output=True,
    )
    # Only a confirmed 404 means missing; authentication/network failures must stop.
    if result.returncode and re.match(r"HTTP/\S+ 404\b", result.stdout):
        return None
    if result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, output=result.stdout, stderr=result.stderr,
        )
    _, separator, body = result.stdout.partition("\n\n")
    if not separator:
        raise ValueError("GitHub API response is missing its header separator")
    return json.loads(body)


def ensure_release(tag, pr, entries):
    release = get_release(tag)
    if release is not None and any(asset["name"] == "CHANGELOG.md" for asset in release["assets"]):
        return
    Path(".release-preview").mkdir(exist_ok=True)
    notes_path = Path(".release-preview/RELEASE_NOTES.md")
    notes_path.write_text(release_notes(pr))
    Path("CHANGELOG.md").write_text(changelog(entries))
    if release is None:
        # Repair a successful tag push followed by a failed release creation.
        run("gh", "release", "create", tag, "--verify-tag", "--title", tag,
            "--notes-file", str(notes_path))
        release = get_release(tag)
        if release is None:
            raise RuntimeError(f"Release {tag} is still missing after creation")
    # Existing release notes and attachments are never replaced on retry.
    assets = release["assets"]
    if not any(asset["name"] == "CHANGELOG.md" for asset in assets):
        run("gh", "release", "upload", tag, "CHANGELOG.md")


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != "leonroy/airplay2-dlna-bridge":
        raise SystemExit("Releases are only enabled in this fork's GitHub Actions.")
    run("git", "fetch", "origin", "main", "--tags")
    commits = run("git", "rev-list", "--first-parent", "--reverse", f"{BASELINE}..origin/main").splitlines()
    prs = ordered_pull_requests(commits, merged_pull_requests())
    tags = sorted(
        (tag for tag in run("git", "tag", "--list").splitlines() if VERSION_TAG.fullmatch(tag)),
        key=lambda tag: tuple(int(n) for n in tag[1:].split(".")),
    )
    tags_by_commit = {run("git", "rev-list", "-n", "1", tag): tag for tag in tags}
    last_tagged = max((i for i, pr in enumerate(prs) if pr["merge_commit_sha"] in tags_by_commit), default=-1)
    entries = []
    for i, pr in enumerate(prs):
        sha = pr["merge_commit_sha"]
        if sha in tags_by_commit:
            tag = tags_by_commit[sha]
            entries.append((tag, pr))
            if i == last_tagged:
                ensure_release(tag, pr, entries)
            continue
        if i < last_tagged:
            raise ValueError(f"PR #{pr['number']} is untagged before a later release; repair release history first")
        bump = bump_for_pr(pr)
        print(f"Testing and releasing PR #{pr['number']} at {sha}", flush=True)
        run("git", "checkout", "-B", "main", sha)
        run("python", "-m", "pip", "install", "-r", "requirements-dev.txt")
        run("python", "-m", "pytest", "-q")
        run("docker", "build", "--tag", "bridge-release-check", "bridge")
        run("docker", "run", "bridge-release-check", "python3", "-c",
            "import runpy; runpy.run_path('/app/server.py', run_name='smoke_test')")
        # Force the PR's bump: commit messages inside the PR are irrelevant.
        if not tags and bump != "major":
            bump = "minor"
        args = ["semantic-release", "--strict", "version", "--no-commit", "--skip-build",
                "--no-changelog", "--no-vcs-release", f"--{bump}"]
        run(*args)
        tag = run("semantic-release", "version", "--print-last-released-tag")
        if run("git", "rev-list", "-n", "1", tag) != sha:
            raise SystemExit(f"Release tag {tag} does not point to PR #{pr['number']}'s final commit")
        entries.append((tag, pr))
        ensure_release(tag, pr, entries)
        tags.append(tag)


if __name__ == "__main__":
    main()
