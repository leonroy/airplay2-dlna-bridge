"""Publish tested release images; never replace an existing version tag."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

IMAGE = 'ghcr.io/leonroy/airplay2-dlna-bridge'
PLATFORMS = ('linux/amd64', 'linux/arm64')
SOURCE = 'https://github.com/leonroy/airplay2-dlna-bridge'
REVISION = 'org.opencontainers.image.revision'
VERSION = 'org.opencontainers.image.version'
SMOKE_PROGRAM = Path(__file__).with_name('docker_smoke.py').read_text()
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')


def version_tuple(tag):
    if not re.fullmatch(r'v?\d+\.\d+\.\d+', tag):
        raise ValueError(f'Invalid release version: {tag}')
    return tuple(int(n) for n in tag.removeprefix('v').split('.'))


def run(*args):
    subprocess.run(args, check=True, timeout=1800)


def inspect_image(ref):
    result = subprocess.run(
        ['docker', 'buildx', 'imagetools', 'inspect', ref, '--format', '{{.Manifest.Digest}}'],
        text=True, capture_output=True, timeout=120,
    )
    if result.returncode:
        # Fail closed on credentials, rate limits, and network failures.
        missing = re.search(
            rf'(?m)^(?:ERROR: )?{re.escape(ref)}: (?:not found|manifest unknown)\s*$',
            result.stderr,
        )
        if missing:
            return None
        raise subprocess.CalledProcessError(
            result.returncode, result.args, output=result.stdout, stderr=result.stderr,
        )
    digest = result.stdout.strip()
    if not DIGEST.fullmatch(digest):
        raise ValueError('Registry did not return a valid image digest')
    # Read the original index, pinned to the digest resolved above. Formatted
    # summaries do not reliably preserve the index's top-level annotations.
    pinned = f'{ref.split("@", 1)[0]}@{digest}'
    raw = subprocess.run(
        ['docker', 'buildx', 'imagetools', 'inspect', pinned, '--raw'],
        text=True, capture_output=True, timeout=120,
    )
    raw.check_returncode()
    manifest = json.loads(raw.stdout)
    manifest['digest'] = digest
    return manifest


def validate_image(manifest, version, sha):
    annotations = manifest.get('annotations', {})
    if annotations.get(REVISION) != sha or annotations.get(VERSION) != version:
        raise ValueError('Existing image has a different revision or version; refusing to overwrite')
    platforms = {
        f"{item['platform'].get('os')}/{item['platform'].get('architecture')}"
        for item in manifest.get('manifests', []) if 'platform' in item
    } - {'unknown/unknown'}  # BuildKit provenance descriptors are not runnable images.
    if platforms != set(PLATFORMS):
        raise ValueError(f'Unexpected image platforms: {platforms}')
    if not DIGEST.fullmatch(manifest.get('digest', '')):
        raise ValueError('Registry did not return a valid image digest')
    return manifest['digest']


def smoke_image(manifest):
    images = {}
    for item in manifest.get('manifests', []):
        platform_info = item.get('platform', {})
        platform = f"{platform_info.get('os')}/{platform_info.get('architecture')}"
        if platform not in PLATFORMS:
            continue
        digest = item.get('digest', '')
        if not DIGEST.fullmatch(digest) or platform in images:
            raise ValueError(f'Invalid or ambiguous child manifest for {platform}')
        images[platform] = f'{IMAGE}@{digest}'
    if set(images) != set(PLATFORMS):
        raise ValueError('Missing child manifest for a required platform')
    for platform in PLATFORMS:
        # Each architecture has its own digest, avoiding classic Docker's
        # conflict when loading two platforms under the same index digest.
        ref = images[platform]
        print(f'Smoke testing {ref} on {platform}', flush=True)
        subprocess.run(
            ['docker', 'run', '--platform', platform, '-i', ref, 'python3', '-'],
            input=SMOKE_PROGRAM, text=True, check=True, timeout=180,
        )


def check_local_images():
    for platform in PLATFORMS:
        ref = f"bridge-ci-{platform.split('/')[-1]}"
        run('docker', 'buildx', 'build', '--platform', platform, '--load', '--tag', ref, 'bridge')
        subprocess.run(
            ['docker', 'run', '--platform', platform, '-i', ref, 'python3', '-'],
            input=SMOKE_PROGRAM, text=True, check=True, timeout=180,
        )


def promote_latest(version, digest):
    latest = inspect_image(f'{IMAGE}:latest')
    if latest is not None:
        previous = latest.get('annotations', {}).get(VERSION, '')
        if version_tuple(previous) > version_tuple(version):
            return
        if version_tuple(previous) == version_tuple(version):
            if latest.get('digest') != digest:
                raise ValueError('latest conflicts with the version image')
            return
    run('docker', 'buildx', 'imagetools', 'create', '--tag', f'{IMAGE}:latest', f'{IMAGE}@{digest}')
    updated = inspect_image(f'{IMAGE}:latest')
    if updated is None or updated.get('digest') != digest:
        raise ValueError('latest promotion did not preserve the tested image digest')


def publish_image(tag, sha, *, is_latest):
    # v0.1.0 predates publishing; do not backfill it on coordinator retries.
    if version_tuple(tag) <= (0, 1, 0):
        return
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'leonroy/airplay2-dlna-bridge':
        raise RuntimeError('Publishing is only enabled in this fork\'s release workflow')
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('Publishing requires a full Git commit SHA')
    version = tag.removeprefix('v')
    ref = f'{IMAGE}:{version}'
    manifest = inspect_image(ref)
    if manifest is None:
        candidate = f'{IMAGE}:sha-{sha}'
        manifest = inspect_image(candidate)
        if manifest is None:
            run(
                'docker', 'buildx', 'build', '--platform', ','.join(PLATFORMS),
                '--output', 'type=image,push=true,oci-mediatypes=true', '--tag', candidate,
                '--label', f'org.opencontainers.image.source={SOURCE}',
                '--label', 'org.opencontainers.image.licenses=MIT',
                '--label', f'{REVISION}={sha}', '--label', f'{VERSION}={version}',
                '--annotation', f'index:{REVISION}={sha}',
                '--annotation', f'index:{VERSION}={version}', 'bridge',
            )
            manifest = inspect_image(candidate)
        if manifest is None:
            raise RuntimeError('Candidate image is missing after its build')
        digest = validate_image(manifest, version, sha)
        smoke_image(manifest)
        # Check again immediately before promotion; never knowingly replace a version.
        existing = inspect_image(ref)
        if existing is not None:
            if validate_image(existing, version, sha) != digest:
                raise ValueError('Version tag appeared with a conflicting digest')
        else:
            run('docker', 'buildx', 'imagetools', 'create', '--tag', ref, f'{IMAGE}@{digest}')
        published = inspect_image(ref)
        if published is None or validate_image(published, version, sha) != digest:
            raise ValueError('Version promotion did not preserve the tested image digest')
    else:
        digest = validate_image(manifest, version, sha)
        smoke_image(manifest)
    if is_latest:
        promote_latest(version, digest)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as output:
            output.write(f'\n### Docker image {version}\n\n- `{ref}`\n- `{IMAGE}@{digest}`\n- Platforms: {", ".join(PLATFORMS)}\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-local', action='store_true', required=True)
    parser.parse_args()
    check_local_images()
