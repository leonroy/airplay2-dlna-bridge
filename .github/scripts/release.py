"""Release each main commit in order, including commits missed by queued runs."""
import json
import os
import re
import subprocess

# History before release automation was introduced is not released individually.
BASELINE = "6089a33ae8ffa5020c10a98d30fcaa92e8423eec"
VERSION_TAG = re.compile(r"v\d+\.\d+\.\d+\Z")


def run(*args):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE)
    if result.returncode:
        print(result.stdout, flush=True)
        result.check_returncode()
    return result.stdout.strip()


def pending_commits(commits, tags_by_commit):
    """Resume after the newest tagged commit, preserving first-parent order."""
    last = max((i for i, sha in enumerate(commits) if sha in tags_by_commit), default=-1)
    return commits[last + 1:]


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


def ensure_release(tag):
    release = get_release(tag)
    if release is None:
        # Repair a successful tag push followed by a failed release creation.
        run("semantic-release", "changelog", "--post-to-release-tag", tag)
        release = get_release(tag)
        if release is None:
            raise RuntimeError(f"Release {tag} is still missing after creation")
    # Do not repost existing notes: PSR 10.7.0's updater uses POST instead of PATCH.
    assets = release["assets"]
    if not any(asset["name"] == "CHANGELOG.md" for asset in assets):
        run("semantic-release", "changelog")
        run("gh", "release", "upload", tag, "CHANGELOG.md")


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != "leonroy/airplay2-dlna-bridge":
        raise SystemExit("Releases are only enabled in this fork's GitHub Actions.")
    run("git", "fetch", "origin", "main", "--tags")
    commits = run("git", "rev-list", "--first-parent", "--reverse", f"{BASELINE}..origin/main").splitlines()
    tags = [tag for tag in run("git", "tag", "--list").splitlines() if VERSION_TAG.fullmatch(tag)]
    tags_by_commit = {run("git", "rev-list", "-n", "1", tag): tag for tag in tags}
    tagged = [sha for sha in commits if sha in tags_by_commit]
    if tagged:
        latest = tagged[-1]
        run("git", "checkout", "-B", "main", latest)
        ensure_release(tags_by_commit[latest])
    for sha in pending_commits(commits, tags_by_commit):
        print(f"Testing and releasing {sha}", flush=True)
        run("git", "checkout", "-B", "main", sha)
        run("python", "-m", "pip", "install", "-r", "requirements-dev.txt")
        run("python", "-m", "pytest", "-q")
        run("docker", "build", "--tag", "bridge-release-check", "bridge")
        run("docker", "run", "bridge-release-check", "python3", "-c",
            "import runpy; runpy.run_path('/app/server.py', run_name='smoke_test')")
        args = ["semantic-release", "--strict", "version", "--no-commit", "--skip-build"]
        if not tags:
            args.append("--minor")
        run(*args)
        tag = run("semantic-release", "version", "--print-last-released-tag")
        if run("git", "rev-list", "-n", "1", tag) != sha:
            raise SystemExit(f"No release was created for {sha}; check its commit message")
        ensure_release(tag)
        tags.append(tag)


if __name__ == "__main__":
    main()
