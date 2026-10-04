import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


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
            {'platform': {'os': 'linux', 'architecture': arch}}
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
            assert smoke_tests == [f'{publisher.IMAGE}@{DIGEST}']
            target = args[args.index('--tag') + 1]
            assert args[-1] == f'{publisher.IMAGE}@{DIGEST}'
            registry[target] = DIGEST
            promotions.append(target)
        else:
            pytest.fail(f'Unexpected command: {args}')
        return subprocess.CompletedProcess(args, 0, '', '')

    monkeypatch.setattr(publisher.subprocess, 'run', docker)
    monkeypatch.setattr(publisher, 'smoke_image', smoke_tests.append)
    publisher.publish_image('v0.1.1', SHA, is_latest=True)
    assert promotions == [f'{publisher.IMAGE}:0.1.1', f'{publisher.IMAGE}:latest']
