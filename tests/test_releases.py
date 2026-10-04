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


def make_pr(number=1, sha='first', title='fix: restore artwork', body='Details'):
    return {'number': number, 'merge_commit_sha': sha, 'title': title, 'body': body,
            'merged_at': '2026-10-04T00:00:00Z', 'base': {'ref': 'main'},
            'html_url': f'https://github.com/leonroy/airplay2-dlna-bridge/pull/{number}'}


@pytest.fixture
def coordinator(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('release_coordinator', ROOT / '.github/scripts/release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'pyproject.toml').write_text((ROOT / 'pyproject.toml').read_text())
    return module


def test_release_configuration_is_valid():
    config = RawConfig.model_validate(CONFIG)
    assert config.assets == []
    assert config.branches['main'].match == '^main$'


@pytest.mark.parametrize('kind', ['fix', 'perf', 'build', 'chore', 'ci', 'docs', 'style', 'refactor', 'test', 'revert'])
def test_routine_merges_create_patch_releases(kind):
    assert PARSER.parse_message(f'{kind}(bridge): improve behaviour').bump == LevelBump.PATCH


def test_features_and_breaking_changes():
    assert PARSER.parse_message('feat: add settings').bump == LevelBump.MINOR
    assert PARSER.parse_message('fix!: change settings').bump == LevelBump.MAJOR
    assert PARSER.parse_message('fix: change settings\n\nBREAKING CHANGE: remove old setting').bump == LevelBump.MAJOR
    assert PARSER.parse_message('Update settings') is None


def test_pull_requests_follow_git_order_not_api_or_timestamp_order(coordinator):
    first, second = make_pr(), make_pr(2, 'second')
    old = make_pr(3, 'before-baseline')
    closed = make_pr(4, 'unmerged')
    closed['merged_at'] = None
    other_branch = make_pr(5, 'other')
    other_branch['base']['ref'] = 'develop'
    assert coordinator.ordered_pull_requests(
        ['wip', 'first', 'direct-push', 'second'],
        [second, old, closed, other_branch, first],
    ) == [first, second]


@pytest.mark.parametrize('title,body,bump', [
    ('feat: add discovery', '', 'minor'),
    ('fix: improve reconnects', '', 'patch'),
    ('fix!: change settings', '', 'major'),
    ('fix: change settings', 'BREAKING CHANGE: remove old setting', 'major'),
])
def test_version_bump_comes_from_pr_metadata(coordinator, title, body, bump):
    assert coordinator.bump_for_pr(make_pr(title=title, body=body)) == bump


def test_invalid_pr_title_stops_release(coordinator):
    with pytest.raises(ValueError, match='Conventional Commit title'):
        coordinator.bump_for_pr(make_pr(title='WIP'))


def test_pull_request_discovery_reads_every_api_page(coordinator, monkeypatch):
    import json
    monkeypatch.setenv('GITHUB_REPOSITORY', 'leonroy/airplay2-dlna-bridge')
    calls = []
    closed = make_pr(3, 'closed')
    closed['merged_at'] = None
    def fake_run(*args):
        calls.append(args)
        return json.dumps([[make_pr(), closed], [make_pr(2, 'second')]])
    monkeypatch.setattr(coordinator, 'run', fake_run)
    assert len(coordinator.merged_pull_requests()) == 2
    assert '--paginate' in calls[0] and '--slurp' in calls[0]


def test_changelog_contains_one_entry_per_pr_in_reverse_order(coordinator):
    entries = [('v0.1.0', make_pr()), ('v0.2.0', make_pr(2, 'second', 'feat: add discovery'))]
    text = coordinator.changelog(entries)
    assert text.index('## v0.2.0') < text.index('## v0.1.0')
    assert text.count('Pull request:') == 2


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
    pr = make_pr()
    coordinator.ensure_release('v0.1.0', pr, [('v0.1.0', pr)])
    assert not any(args[:3] == ('gh', 'release', 'create') for args in calls)
    assert not any(args[0] == 'semantic-release' for args in calls)
    assert (('gh', 'release', 'upload', 'v0.1.0', 'CHANGELOG.md') in calls) is not already_uploaded


def test_missing_release_is_created_before_upload(coordinator, monkeypatch):
    responses = iter([None, {'assets': []}])
    calls = []
    monkeypatch.setattr(coordinator, 'get_release', lambda tag: next(responses))
    monkeypatch.setattr(coordinator, 'run', lambda *args: calls.append(args))
    pr = make_pr()
    coordinator.ensure_release('v0.1.0', pr, [('v0.1.0', pr)])
    assert calls == [
        ('gh', 'release', 'create', 'v0.1.0', '--verify-tag', '--title', 'v0.1.0',
         '--notes-file', '.release-preview/RELEASE_NOTES.md'),
        ('gh', 'release', 'upload', 'v0.1.0', 'CHANGELOG.md'),
    ]
    assert Path('.release-preview/RELEASE_NOTES.md').read_text() == coordinator.release_notes(pr)
    assert '## v0.1.0' in Path('CHANGELOG.md').read_text()


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
@pytest.mark.parametrize('merge_method', ['squash', 'merge', 'rebase'])
def test_multiple_pending_prs_are_released_once_for_any_merge_method(coordinator, monkeypatch, retry_after_first_tag, merge_method):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_REPOSITORY', 'leonroy/airplay2-dlna-bridge')
    calls, released = [], []
    current = {'sha': None, 'tag': None}
    tag_targets = {'v0.1.0': 'first'} if retry_after_first_tag else {}
    monkeypatch.setattr(coordinator, 'merged_pull_requests', lambda: [make_pr(2, 'second'), make_pr()])

    def fake_run(*args):
        calls.append(args)
        if args[:3] == ('git', 'rev-list', '--first-parent'):
            return 'wip1\nfirst\nwip2\nsecond' if merge_method == 'rebase' else 'first\nsecond'
        if args == ('git', 'tag', '--list'):
            return 'v0.1.0' if retry_after_first_tag else ''
        if args[:3] == ('git', 'checkout', '-B'):
            current['sha'] = args[-1]
        if args[:3] == ('semantic-release', '--strict', 'version'):
            assert ('python', '-m', 'pytest', '-q') in calls
            assert '--no-commit' in args
            assert '--no-changelog' in args and '--no-vcs-release' in args
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
    checkouts = [args[-1] for args in calls if args[:3] == ('git', 'checkout', '-B')]
    assert checkouts == (['second'] if retry_after_first_tag else ['first', 'second'])
    assert not any(args[:2] == ('semantic-release', 'changelog') for args in calls)
    assert ('gh', 'release', 'upload', 'v0.1.0', 'CHANGELOG.md') in calls
    assert ('gh', 'release', 'upload', 'v0.1.1', 'CHANGELOG.md') in calls
    assert calls.count(('python', '-m', 'pytest', '-q')) == (1 if retry_after_first_tag else 2)
    commands = [args for args in calls if args[:3] == ('semantic-release', '--strict', 'version')]
    if retry_after_first_tag:
        assert '--patch' in commands[0]
    else:
        assert '--minor' in commands[0]
        assert '--patch' in commands[1]
