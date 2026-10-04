import importlib.util
import json
from pathlib import Path
import runpy
import subprocess
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).parents[1]
SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
CHILD_DIGESTS = {'amd64': 'sha256:' + 'c' * 64, 'arm64': 'sha256:' + 'd' * 64}


@pytest.fixture
def publisher():
    spec = importlib.util.spec_from_file_location('image_publisher', ROOT / '.github/scripts/publish.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def raw_index(publisher):
    return {
        'schemaVersion': 2,
        'mediaType': 'application/vnd.oci.image.index.v1+json',
        'annotations': {publisher.REVISION: SHA, publisher.VERSION: '0.1.1'},
        'manifests': [
            {'platform': {'os': 'linux', 'architecture': arch}, 'digest': CHILD_DIGESTS[arch]}
            for arch in ('amd64', 'arm64')
        ],
    }


def test_inspection_reads_annotations_from_digest_pinned_raw_index(publisher, monkeypatch):
    ref = f'{publisher.IMAGE}:sha-{SHA}'
    calls = []

    def inspect(args, **kwargs):
        calls.append(args)
        if '--format' in args:
            return subprocess.CompletedProcess(args, 0, DIGEST + '\n', '')
        return subprocess.CompletedProcess(args, 0, json.dumps(raw_index(publisher)), '')

    monkeypatch.setattr(publisher.subprocess, 'run', inspect)
    manifest = publisher.inspect_image(ref)
    assert publisher.validate_image(manifest, '0.1.1', SHA) == DIGEST
    assert calls == [
        ['docker', 'buildx', 'imagetools', 'inspect', ref, '--format', '{{.Manifest.Digest}}'],
        ['docker', 'buildx', 'imagetools', 'inspect', f'{ref}@{DIGEST}', '--raw'],
    ]


@pytest.mark.parametrize('error,missing', [
    ('not found', True),
    ('manifest unknown', True),
    ('unauthorized', False),
    ('toomanyrequests', False),
    ('connection timed out', False),
])
def test_inspection_only_treats_confirmed_missing_tags_as_absent(publisher, monkeypatch, error, missing):
    ref = f'{publisher.IMAGE}:0.1.1'
    calls = []

    def inspect(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, '', f'ERROR: {ref}: {error}\n')

    monkeypatch.setattr(publisher.subprocess, 'run', inspect)
    if missing:
        assert publisher.inspect_image(ref) is None
    else:
        with pytest.raises(subprocess.CalledProcessError):
            publisher.inspect_image(ref)
    assert len(calls) == 1


def test_raw_manifest_failure_does_not_become_a_missing_tag(publisher, monkeypatch):
    def inspect(args, **kwargs):
        if '--format' in args:
            return subprocess.CompletedProcess(args, 0, DIGEST, '')
        return subprocess.CompletedProcess(args, 1, '', 'manifest unknown')

    monkeypatch.setattr(publisher.subprocess, 'run', inspect)
    with pytest.raises(subprocess.CalledProcessError):
        publisher.inspect_image(f'{publisher.IMAGE}:0.1.1')


def test_invalid_digest_stops_before_raw_inspection(publisher, monkeypatch):
    calls = []

    def inspect(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, 'invalid digest', '')

    monkeypatch.setattr(publisher.subprocess, 'run', inspect)
    with pytest.raises(ValueError, match='valid image digest'):
        publisher.inspect_image(f'{publisher.IMAGE}:0.1.1')
    assert len(calls) == 1


def test_new_candidate_promotes_raw_index_digest_to_version_and_latest(publisher, monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_REPOSITORY', 'leonroy/airplay2-dlna-bridge')
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    registry = {}
    promotions, smoke_tests = [], []

    def docker(args, **kwargs):
        if args[:4] == ['docker', 'buildx', 'imagetools', 'inspect']:
            ref = args[4]
            if '--raw' in args:
                assert ref.endswith('@' + DIGEST)
                return subprocess.CompletedProcess(args, 0, json.dumps(raw_index(publisher)), '')
            if ref not in registry:
                return subprocess.CompletedProcess(args, 1, '', f'ERROR: {ref}: not found\n')
            return subprocess.CompletedProcess(args, 0, registry[ref], '')
        if args[:3] == ('docker', 'buildx', 'build'):
            registry[args[args.index('--tag') + 1]] = DIGEST
        elif args[:4] == ('docker', 'buildx', 'imagetools', 'create'):
            assert smoke_tests == [
                ('linux/amd64', f'{publisher.IMAGE}@{CHILD_DIGESTS["amd64"]}'),
                ('linux/arm64', f'{publisher.IMAGE}@{CHILD_DIGESTS["arm64"]}'),
            ]
            target = args[args.index('--tag') + 1]
            assert args[-1] == f'{publisher.IMAGE}@{DIGEST}'
            registry[target] = DIGEST
            promotions.append(target)
        elif args[:2] == ['docker', 'run']:
            platform, ref = args[3], args[5]
            assert ref != f'{publisher.IMAGE}@{DIGEST}'
            assert kwargs['input'] == publisher.SMOKE_PROGRAM
            smoke_tests.append((platform, ref))
        else:
            pytest.fail(f'Unexpected command: {args}')
        return subprocess.CompletedProcess(args, 0, '', '')

    monkeypatch.setattr(publisher.subprocess, 'run', docker)
    publisher.publish_image('v0.1.1', SHA, is_latest=True)
    assert promotions == [f'{publisher.IMAGE}:0.1.1', f'{publisher.IMAGE}:latest']


@pytest.mark.parametrize('problem', ['missing', 'invalid', 'duplicate'])
def test_bad_child_manifest_stops_before_running_containers(publisher, monkeypatch, problem):
    manifest = raw_index(publisher)
    if problem == 'missing':
        manifest['manifests'].pop()
    elif problem == 'invalid':
        manifest['manifests'][1]['digest'] = 'invalid'
    else:
        manifest['manifests'].append(manifest['manifests'][1])
    monkeypatch.setattr(publisher.subprocess, 'run', lambda *args, **kwargs: pytest.fail('Must not run containers'))
    with pytest.raises(ValueError, match='child manifest'):
        publisher.smoke_image(manifest)


def test_smoke_failure_prevents_version_and_latest_promotion(publisher, monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_REPOSITORY', 'leonroy/airplay2-dlna-bridge')
    manifest = dict(raw_index(publisher), digest=DIGEST)
    monkeypatch.setattr(publisher, 'inspect_image', lambda ref: None if ref.endswith(':0.1.1') else manifest)
    monkeypatch.setattr(publisher, 'run', lambda *args: pytest.fail('Must not promote a failing image'))
    calls = []

    def docker(args, **kwargs):
        calls.append(args)
        if args[3] == 'linux/arm64':
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(publisher.subprocess, 'run', docker)
    with pytest.raises(subprocess.CalledProcessError):
        publisher.publish_image('v0.1.1', SHA, is_latest=True)
    assert len(calls) == 2


def test_existing_version_retry_smokes_children_and_keeps_index_for_latest(publisher, monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_REPOSITORY', 'leonroy/airplay2-dlna-bridge')
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    manifest = dict(raw_index(publisher), digest=DIGEST)
    manifest['manifests'].append({
        'platform': {'os': 'unknown', 'architecture': 'unknown'},
        'digest': 'sha256:' + 'e' * 64,
    })
    monkeypatch.setattr(publisher, 'inspect_image', lambda ref: manifest)
    calls, latest = [], []
    monkeypatch.setattr(publisher.subprocess, 'run', lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(publisher, 'promote_latest', lambda *args: latest.append(args))
    publisher.publish_image('v0.1.1', SHA, is_latest=True)
    assert [(args[3], args[5]) for args in calls] == [
        ('linux/amd64', f'{publisher.IMAGE}@{CHILD_DIGESTS["amd64"]}'),
        ('linux/arm64', f'{publisher.IMAGE}@{CHILD_DIGESTS["arm64"]}'),
    ]
    assert latest == [('0.1.1', DIGEST)]


@pytest.mark.integration
def test_cached_smoke_program_accepts_legacy_bridge_and_checks_samples(publisher, monkeypatch, capsys):
    program_path = Path(__file__).parents[1] / ".github/scripts/docker_smoke.py"
    program = publisher.SMOKE_PROGRAM  # Cached before switching release source.
    legacy_source = '''import subprocess
def encode():
    subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0",
        "-c:a", "flac", "-compression_level", "0", "-flush_packets", "1",
        "-f", "flac", "pipe:1"])
'''
    legacy_bridge = {}
    exec(compile(legacy_source, "legacy_server.py", "exec"), legacy_bridge)
    monkeypatch.setattr(runpy, "run_path", Mock(return_value=legacy_bridge))
    monkeypatch.setattr(Path, "read_text", Mock(return_value=legacy_source))
    with pytest.raises(SystemExit) as exit_result:
        exec(compile(program, str(program_path), "exec"), {"__file__": str(program_path)})
    assert exit_result.value.code == 0
    assert "Legacy bridge import and exact PCM/FLAC round trip passed" in capsys.readouterr().out
