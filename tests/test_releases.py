import importlib.util
from pathlib import Path
import tomllib
import subprocess

import pytest
from semantic_release.cli.config import RawConfig
from semantic_release.commit_parser.conventional import ConventionalCommitParser, ConventionalCommitParserOptions
from semantic_release.enums import LevelBump

ROOT = Path(__file__).parents[1]
CONFIG = tomllib.loads((ROOT / 'pyproject.toml').read_text())['tool']['semantic_release']
PARSER = ConventionalCommitParser(ConventionalCommitParserOptions(**CONFIG['commit_parser_options']))


@pytest.fixture
def coordinator():
    spec = importlib.util.spec_from_file_location('release_coordinator', ROOT / '.github/scripts/release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_configuration_is_valid():
    config = RawConfig.model_validate(CONFIG)
    assert config.assets == ['CHANGELOG.md']


@pytest.mark.parametrize('kind', ['fix', 'perf', 'build', 'chore', 'ci', 'docs', 'style', 'refactor', 'test', 'revert'])
def test_routine_merges_create_patch_releases(kind):
    assert PARSER.parse_message(f'{kind}(bridge): improve behaviour').bump == LevelBump.PATCH


def test_features_and_breaking_changes():
    assert PARSER.parse_message('feat: add settings').bump == LevelBump.MINOR
    assert PARSER.parse_message('fix!: change settings').bump == LevelBump.MAJOR
    assert PARSER.parse_message('fix: change settings\n\nBREAKING CHANGE: remove old setting').bump == LevelBump.MAJOR
    assert PARSER.parse_message('Update settings') is None


def test_release_backlog_preserves_order_and_retries_untagged_commits(coordinator):
    commits = ['a', 'b', 'c']
    assert coordinator.pending_commits(commits, {}) == commits
    assert coordinator.pending_commits(commits, {'a': 'v0.1.0'}) == ['b', 'c']
    assert coordinator.pending_commits(commits, {'a': 'v0.1.0', 'b': 'v0.1.1'}) == ['c']
    assert coordinator.pending_commits(commits, {'c': 'v0.1.2'}) == []


@pytest.mark.parametrize('already_uploaded', [False, True])
def test_retry_repairs_release_without_replacing_existing_attachment(coordinator, monkeypatch, already_uploaded):
    calls = []

    def fake_run(*args):
        calls.append(args)
        return ''

    monkeypatch.setattr(coordinator, 'run', fake_run)
    monkeypatch.setattr(coordinator, 'get_release', lambda tag: {
        'assets': [{'name': 'CHANGELOG.md'}] if already_uploaded else [],
    })
    coordinator.ensure_release('v0.1.0')
    assert not any('--post-to-release-tag' in args for args in calls)
    assert (('semantic-release', 'changelog') in calls) is not already_uploaded
    assert (('gh', 'release', 'upload', 'v0.1.0', 'CHANGELOG.md') in calls) is not already_uploaded


def test_missing_release_is_created_before_upload(coordinator, monkeypatch):
    responses = iter([None, {'assets': []}])
    calls = []
    monkeypatch.setattr(coordinator, 'get_release', lambda tag: next(responses))
    monkeypatch.setattr(coordinator, 'run', lambda *args: calls.append(args))
    coordinator.ensure_release('v0.1.0')
    assert calls == [
        ('semantic-release', 'changelog', '--post-to-release-tag', 'v0.1.0'),
        ('semantic-release', 'changelog'),
        ('gh', 'release', 'upload', 'v0.1.0', 'CHANGELOG.md'),
    ]


@pytest.mark.parametrize('status', [401, 403, 404, 500])
def test_only_confirmed_404_means_release_is_missing(coordinator, monkeypatch, status):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'leonroy/airplay2-dlna-bridge')
    monkeypatch.setattr(coordinator.subprocess, 'run', lambda *args, **kwargs:
        subprocess.CompletedProcess(args[0], 1, f'HTTP/2.0 {status}\n\n{{}}', 'API error'))
    if status == 404:
        assert coordinator.get_release('v0.1.0') is None
    else:
        with pytest.raises(subprocess.CalledProcessError):
            coordinator.get_release('v0.1.0')


def test_release_api_response_is_parsed(coordinator, monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'leonroy/airplay2-dlna-bridge')
    monkeypatch.setattr(coordinator.subprocess, 'run', lambda *args, **kwargs:
        subprocess.CompletedProcess(args[0], 0, 'HTTP/2.0 200\nContent-Type: application/json\n\n{"assets": []}', ''))
    assert coordinator.get_release('v0.1.0') == {'assets': []}


def test_release_refuses_to_run_locally(coordinator, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.setattr(coordinator, 'run', lambda *args: pytest.fail('Must not execute release commands'))
    with pytest.raises(SystemExit, match='only enabled'):
        coordinator.main()


@pytest.mark.parametrize('retry_after_first_tag', [False, True])
def test_multiple_pending_merges_are_tested_and_released_separately(coordinator, monkeypatch, retry_after_first_tag):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_REPOSITORY', 'leonroy/airplay2-dlna-bridge')
    calls, released = [], []
    current = {'sha': None, 'tag': None}
    tag_targets = {'v0.1.0': 'first'} if retry_after_first_tag else {}

    def fake_run(*args):
        calls.append(args)
        if args[:3] == ('git', 'rev-list', '--first-parent'):
            return 'first\nsecond'
        if args == ('git', 'tag', '--list'):
            return 'v0.1.0' if retry_after_first_tag else ''
        if args[:3] == ('git', 'checkout', '-B'):
            current['sha'] = args[-1]
        if args[:3] == ('semantic-release', '--strict', 'version'):
            assert ('python', '-m', 'pytest', '-q') in calls
            assert '--no-commit' in args
            current['tag'] = 'v0.1.0' if current['sha'] == 'first' else 'v0.1.1'
            tag_targets[current['tag']] = current['sha']
        if args == ('semantic-release', 'version', '--print-last-released-tag'):
            return current['tag']
        if args[:4] == ('git', 'rev-list', '-n', '1'):
            return tag_targets[args[-1]]
        return ''

    monkeypatch.setattr(coordinator, 'run', fake_run)

    def existing_release(tag):
        released.append(tag)
        return {'assets': []}

    monkeypatch.setattr(coordinator, 'get_release', existing_release)
    coordinator.main()
    assert released == ['v0.1.0', 'v0.1.1']
    assert not any('--post-to-release-tag' in args for args in calls)
    assert ('gh', 'release', 'upload', 'v0.1.0', 'CHANGELOG.md') in calls
    assert ('gh', 'release', 'upload', 'v0.1.1', 'CHANGELOG.md') in calls
    assert calls.count(('python', '-m', 'pytest', '-q')) == (1 if retry_after_first_tag else 2)
    commands = [args for args in calls if args[:3] == ('semantic-release', '--strict', 'version')]
    if retry_after_first_tag:
        assert '--minor' not in commands[0]
    else:
        assert '--minor' in commands[0]
        assert '--minor' not in commands[1]
