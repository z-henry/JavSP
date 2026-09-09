"""Contract tests using fake HTTP Sessions; no live AList credentials required."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

import pytest
import requests
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from javsp.alist_client import AlistClient, AlistError, AuthenticationError, ConflictError


def entry(name, is_dir=False, **extra):
    return dict(name=name, size=0 if is_dir else 123, is_dir=is_dir,
                modified='2026-09-08T00:00:00Z', **extra)


def listing(*entries, total=None):
    return {'content': list(entries), 'total': len(entries) if total is None else total}


def storage(mount='/115', driver='115 Open', **extra):
    return dict(id=1, mount_path=mount, driver=driver, **extra)


class Response:
    def __init__(self, data=None, code=200, status=200, message='success', invalid=False):
        self.status_code = status
        self.payload = {'code': code, 'message': message, 'data': data}
        self.invalid = invalid
        self.closed = False

    def json(self):
        if self.invalid:
            raise ValueError('sensitive response body')
        return self.payload

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.returned = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url.split('/api/')[-1], kwargs))
        assert '/fs/get' not in url and '/fs/link' not in url and '/fs/copy' not in url
        assert kwargs['allow_redirects'] is False
        assert kwargs['timeout'] > 0
        if 'data' in kwargs:
            kwargs['uploaded_bytes'] = kwargs['data'].read()
        assert self.responses, 'Unexpected HTTP request'
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if not isinstance(response, Response):
            response = Response(response)
        self.returned.append(response)
        return response


@pytest.fixture(autouse=True)
def clean_token_environment(monkeypatch):
    monkeypatch.delenv('JAVSP_ALIST_TOKEN', raising=False)


def client(*responses, **overrides):
    config = dict(base_url='https://alist.example/subpath/', token=SecretStr('private-token'),
                  http_timeout=10, task_timeout=30, poll_interval=0.01)
    config.update(overrides)
    session = Session(*responses)
    return AlistClient(SimpleNamespace(**config), session=session), session


def test_token_and_transport_settings(monkeypatch):
    monkeypatch.setenv('JAVSP_ALIST_TOKEN', 'environment-token')
    api, session = client(listing())
    assert api.list_dir('/115') == []
    assert session.calls[0][2]['headers']['Authorization'] == 'environment-token'
    assert session.calls[0][2]['timeout'] == 10
    assert session.returned[0].closed


def test_secretstr_token():
    api, session = client(listing())
    api.list_dir('/115')
    assert session.calls[0][2]['headers']['Authorization'] == 'private-token'


@pytest.mark.parametrize('override', [dict(token=SecretStr('')), dict(http_timeout=0),
                         dict(task_timeout=float('nan')), dict(poll_interval=-1),
                         dict(base_url='https://user:secret@alist.example'),
                         dict(base_url='https://alist.example?token=secret')])
def test_bad_config(override):
    with pytest.raises(AlistError):
        client(**override)


def test_empty_environment_token_does_not_fall_back(monkeypatch):
    monkeypatch.setenv('JAVSP_ALIST_TOKEN', '')
    with pytest.raises(AuthenticationError):
        client()


def test_real_injected_session_retries_disabled():
    real = requests.Session()
    real.mount('https://alist.example', requests.adapters.HTTPAdapter(max_retries=5))
    try:
        config = SimpleNamespace(base_url='https://alist.example', token=SecretStr('secret'),
                                 http_timeout=10, task_timeout=30, poll_interval=1)
        AlistClient(config, session=real)
        assert all(adapter.max_retries.total == 0 for adapter in real.adapters.values())
    finally:
        real.close()


def test_paginated_list_preserves_raw_hash_fields_and_unicode():
    first = entry('中文字幕 ' + '长' * 150 + '.mp4', hash_info={'sha1': 'abcd'},
                  hashinfo='{"sha1":"abcd"}', custom='untouched')
    second = entry('folder', True)
    api, session = client(listing(first, total=2), listing(second, total=2))
    assert api.list_dir('/115/中文目录', refresh=False) == [first, second]
    for index, (_, endpoint, args) in enumerate(session.calls, 1):
        assert endpoint == 'fs/list'
        assert args['json'] == dict(path='/115/中文目录', password='', page=index,
                                    per_page=200, refresh=False)


@pytest.mark.parametrize('pages', [
    [listing(entry('a'), total=2), listing(entry('a'), total=2)],
    [listing(entry('a'), total=2), listing(total=2)],
    [listing(entry('a'), total=2), listing(entry('b'), total=3)],
    [{'content': None, 'total': 2}], [{'content': [], 'total': '0'}],
    [{'content': [entry('../escape')], 'total': 1}],
    [{'content': [{'name': 'bad'}], 'total': 1}],
])
def test_inconsistent_listing_is_never_partial_success(pages):
    api, _ = client(*pages)
    with pytest.raises(AlistError):
        api.list_dir('/115')


def test_empty_null_listing():
    api, _ = client({'content': None, 'total': 0})
    assert api.list_dir('/115') == []


def test_stat_only_uses_complete_parent_listing():
    wanted = entry('video.mp4', hash_info={'sha1': 'abc'})
    api, session = client(listing(entry('other'), total=2), listing(wanted, total=2), listing())
    assert api.stat('/115/video.mp4') is wanted
    assert api.stat('/115/missing') is None
    assert all(call[1] == 'fs/list' for call in session.calls)


@pytest.mark.parametrize('response,error', [
    (Response(code=401), AuthenticationError), (Response(status=403), AuthenticationError),
    (Response(code=403), AuthenticationError), (Response(status=404), AlistError),
    (Response(code=404), AlistError), (Response(code=500), AlistError),
    (Response(status=503), AlistError), (Response(invalid=True), AlistError),
    (Response(status=307), AlistError),
])
def test_stat_errors_are_not_absence_or_secret_leaks(response, error):
    response.payload['message'] = 'secret raw_url=https://private-token.example'
    api, session = client(response, listing(entry('115', True)))
    with pytest.raises(error) as caught:
        api.stat('/115/file')
    assert 'private-token' not in str(caught.value)
    assert 'raw_url' not in str(caught.value)
    assert len(session.calls) == (1 if error is AuthenticationError else 2)
    assert response.closed


def test_stat_absent_ancestor_proven_by_successful_listing():
    api, session = client(Response(code=500), Response(code=404), listing(entry('existing', True)))
    assert api.stat('/A/new/folder/movie.nfo') is None
    assert [call[2]['json']['path'] for call in session.calls] == [
        '/A/new/folder', '/A/new', '/A']
    assert all(call[1] == 'fs/list' for call in session.calls)


def test_stat_absent_top_level_ancestor():
    api, session = client(Response(code=500), listing())
    assert api.stat('/new/movie.nfo') is None
    assert [call[2]['json']['path'] for call in session.calls] == ['/new', '/']


@pytest.mark.parametrize('is_dir', [False, True])
def test_stat_existing_parent_preserves_original_error(is_dir):
    api, session = client(Response(status=502), listing(entry('folder', is_dir)))
    with pytest.raises(AlistError, match='HTTP error 502'):
        api.stat('/A/folder/movie.nfo')
    assert len(session.calls) == 2


@pytest.mark.parametrize('ancestor_error', [Response(code=403), Response(code=401),
                                          Response(status=503)])
def test_stat_inaccessible_ancestor_preserves_original_error(ancestor_error):
    api, session = client(Response(status=502), ancestor_error)
    with pytest.raises(AlistError, match='HTTP error 502'):
        api.stat('/folder/movie.nfo')
    assert len(session.calls) == 2


def test_stat_direct_permission_error_does_not_probe_ancestors():
    api, session = client(Response(code=403))
    with pytest.raises(AuthenticationError):
        api.stat('/A/new/folder/movie.nfo')
    assert len(session.calls) == 1


def test_stat_root_listing_failure_is_not_retried():
    api, session = client(Response(status=502))
    with pytest.raises(AlistError, match='HTTP error 502'):
        api.stat('/movie.nfo')
    assert len(session.calls) == 1


def test_stat_ancestor_absence_requires_all_listing_pages():
    api, session = client(Response(status=502), listing(entry('other'), total=2),
                          listing(entry('folder', True), total=2))
    with pytest.raises(AlistError, match='HTTP error 502'):
        api.stat('/A/folder/movie.nfo')
    assert [call[2]['json']['page'] for call in session.calls] == [1, 1, 2]


def test_mkdirs_verifies_each_creation():
    api, session = client(listing(entry('115', True)), listing(), None,
                          listing(entry('new', True)), listing(), None,
                          listing(entry('nested', True)))
    api.mkdirs('/115/new/nested')
    mutations = [args['json'] for method, endpoint, args in session.calls if endpoint == 'fs/mkdir']
    assert mutations == [{'path': '/115/new'}, {'path': '/115/new/nested'}]


def test_mkdirs_file_conflict_and_unverified_creation():
    api, _ = client(listing(entry('occupied')))
    with pytest.raises(ConflictError):
        api.mkdirs('/occupied/deeper')
    api, _ = client(listing(), None, listing())
    with pytest.raises(AlistError, match='verified'):
        api.mkdirs('/new')


def test_rename_and_remove_payloads():
    api, session = client(listing(entry('old.mp4')), None, None)
    api.rename('/115/old.mp4', '新 CD1.mp4')
    api.remove('/115/completed')
    assert session.calls[1][1:] == ('fs/rename', dict(
        headers={'Authorization': 'private-token'}, timeout=10, allow_redirects=False,
        json={'path': '/115/old.mp4', 'name': '新 CD1.mp4', 'overwrite': False}))
    assert session.calls[2][2]['json'] == {'dir': '/115', 'names': ['completed']}


def test_conflict_preflight_prevents_mutation():
    api, session = client(listing(entry('new')))
    with pytest.raises(ConflictError):
        api.rename('/115/old', 'new')
    assert len(session.calls) == 1


@pytest.mark.parametrize('path', ['/', '/a/../b', 'relative', '/a\\b', '/a\x00b'])
def test_invalid_mutation_path(path):
    api, session = client()
    with pytest.raises(AlistError):
        api.remove(path)
    assert not session.calls


@pytest.mark.parametrize('name', ['', '..', 'a/b', 'a\\b', 'a\nb'])
def test_invalid_rename_name(name):
    api, session = client()
    with pytest.raises(AlistError):
        api.rename('/115/old', name)
    assert not session.calls


def test_mutation_transport_error_is_not_retried_or_leaked():
    api, session = client(requests.ConnectionError('private-token raw_url=secret'))
    with pytest.raises(AlistError) as caught:
        api.remove('/115/completed')
    assert len(session.calls) == 1
    assert 'private-token' not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize('message', ['file exists', 'file [poster.jpg] exists'])
def test_upstream_403_conflict_is_distinct_from_auth(message):
    api, _ = client(listing(), Response(code=403, message=message))
    with pytest.raises(ConflictError):
        api.rename('/115/old', 'poster.jpg')


def test_upload_streams_metadata_with_encoded_path(tmp_path):
    local = tmp_path / 'poster.jpg'
    local.write_bytes(b'metadata-image')
    remote = '/local/A/.javsp-staging/job/#整理完成/中 文+%.jpg'
    api, session = client(listing(), {'task': {'id': 'upload-1', 'state': 0}})
    assert api.upload(local, remote) == 'upload-1'
    method, endpoint, args = session.calls[-1]
    assert (method, endpoint) == ('PUT', 'fs/put')
    assert args['uploaded_bytes'] == b'metadata-image'
    assert args['data'].closed
    assert args['headers']['File-Path'] == quote(remote, safe='/')
    assert args['headers']['As-Task'] == 'true'
    assert args['headers']['Overwrite'] == 'false'
    assert args['headers']['Content-Length'] == '14'


def test_upload_synchronous_and_ambiguous_results(tmp_path):
    local = tmp_path / 'movie.nfo'
    local.write_bytes(b'')
    api, _ = client(listing(), None)
    assert api.upload(local, '/A/movie.nfo') is None
    for result in ({}, {'task': {}}, {'task': None}, {'task': {'id': ''}}):
        api, session = client(listing(), result)
        with pytest.raises(AlistError):
            api.upload(local, '/A/movie.nfo')
        assert len(session.calls) == 2


def test_upload_existing_destination_is_not_opened():
    api, session = client(listing(entry('movie.nfo')))
    with pytest.raises(ConflictError):
        api.upload('does-not-exist', '/A/movie.nfo')
    assert len(session.calls) == 1


def test_upload_lost_response_is_unresolved_and_stream_closed(tmp_path):
    local = tmp_path / 'movie.nfo'
    local.write_bytes(b'metadata')
    api, session = client(listing(), requests.Timeout('secret transport detail'))
    with pytest.raises(AlistError, match='uncertain'):
        api.upload(local, '/A/stage/movie.nfo')
    assert len(session.calls) == 2
    assert session.calls[-1][2]['data'].closed


def test_task_success_with_error_is_rejected():
    api, _ = client({'id': 'task', 'state': 2, 'error': 'private-token'})
    with pytest.raises(AlistError) as caught:
        api.wait_upload('task')
    assert 'private-token' not in str(caught.value)


@pytest.mark.parametrize('state', [2, '2', 'Succeeded', 'StateSucceeded', 'state_succeeded'])
def test_task_success_state(state):
    api, session = client({'id': 'task', 'state': state, 'error': ''})
    api.wait_upload('task')
    assert session.calls[0][0:2] == ('POST', 'admin/task/upload/info')
    assert session.calls[0][2]['params'] == {'tid': 'task'}


@pytest.mark.parametrize('state', [4, 7, 'Canceled', 'Failed', 10, -1, True, 2.0, None, 'Done', 'Success'])
def test_task_failed_or_unknown_state_never_succeeds(state):
    api, _ = client({'id': 'task', 'state': state, 'progress': 100, 'error': 'secret'})
    with pytest.raises(AlistError) as caught:
        api.wait_upload('task')
    assert 'secret' not in str(caught.value)


def test_task_retry_states_are_polled_without_retrying_mutation():
    states = [0, 1, 3, 5, 6, 8, 9, 'WaitingRetry', 2]
    api, session = client(*[{'id': 'task', 'state': state} for state in states])
    with patch('javsp.alist_client.time.sleep'):
        api.wait_upload('task')
    assert len(session.calls) == len(states)
    assert all(call[1] == 'admin/task/upload/info' for call in session.calls)


def test_task_timeout_and_mismatched_id():
    api, session = client({'id': 'task', 'state': 1}, task_timeout=1)
    with patch('javsp.alist_client.time.monotonic', side_effect=[0, 0, 1, 1]):
        with pytest.raises(AlistError, match='timed out'):
            api.wait_upload('task')
    assert len(session.calls) == 1
    assert session.calls[0][2]['timeout'] == 1
    api, _ = client({'id': 'different', 'state': 2})
    with pytest.raises(AlistError, match='mismatched'):
        api.wait_upload('task')


def test_storage_pagination_returns_only_safe_identity():
    secret_storage = storage(addition='secret-refresh-token')
    api, session = client(listing(storage('/other'), total=2), listing(secret_storage, total=2))
    assert api.ensure_same_storage('/115/source', '/115/video') == {
        'id': 1, 'mount_path': '/115', 'driver': '115 Open'}
    assert [call[2]['params']['page'] for call in session.calls] == [1, 2]


@pytest.mark.parametrize('storages,source,destination', [
    ([storage()], '/115/source', '/1150/target'),
    ([storage(), storage('/115/video')], '/115/source', '/115/video/subdir'),
    ([storage('/115/a'), storage('/115/b')], '/115/a/src', '/115/b/dst'),
    ([storage(driver='Local')], '/115/source', '/115/video'),
    ([storage(disabled=True)], '/115/source', '/115/video'),
])
def test_storage_rejects_cross_mount_unknown_driver_and_disabled(storages, source, destination):
    api, _ = client(listing(*storages))
    with pytest.raises(AlistError):
        api.ensure_same_storage(source, destination)


def test_longest_mount_and_root_mount():
    api, _ = client(listing(storage('/', driver='Local'), storage('/115')))
    assert api.ensure_same_storage('/115/a', '/115/b')['mount_path'] == '/115'


def test_move_metadata_within_local_mount_waits_for_task():
    api, session = client(listing(storage('/A', driver='Local')), listing(),
                          {'tasks': [{'id': 'move-1', 'state': 0}]},
                          {'id': 'move-1', 'state': 2})
    api.move('/A/.javsp-staging/job/movie.nfo', '/A/final')
    assert session.calls[2][2]['json'] == {
        'src_dir': '/A/.javsp-staging/job', 'dst_dir': '/A/final', 'names': ['movie.nfo'],
        'overwrite': False, 'skip_existing': False, 'merge': False}
    assert session.calls[3][1] == 'admin/task/move/info'


def test_move_cross_storage_never_submitted():
    api, session = client(listing(storage('/source'), storage('/destination')))
    with pytest.raises(AlistError, match='Cross-storage'):
        api.move('/source/movie.mp4', '/destination')
    assert len(session.calls) == 1


def test_move_existing_target_never_submitted():
    api, session = client(listing(storage()), listing(entry('movie.mp4')))
    with pytest.raises(ConflictError):
        api.move('/115/source/movie.mp4', '/115/final')
    assert all(call[1] != 'fs/move' for call in session.calls)


def test_move_failure_and_noop():
    api, session = client(listing(storage()), listing(), {'tasks': [{'id': 'task'}]},
                          {'id': 'task', 'state': 7})
    with pytest.raises(AlistError, match='failed'):
        api.move('/115/source/movie.mp4', '/115/final')
    assert sum(call[1] == 'fs/move' for call in session.calls) == 1
    api, session = client()
    api.move('/115/file', '/115')
    api.rename('/115/file', 'file')
    assert not session.calls


def test_move_synchronous_result():
    api, session = client(listing(storage()), listing(),
                          {'message': 'Move operations completed immediately'})
    api.move('/115/source/movie.mp4', '/115/final')
    assert len(session.calls) == 3


def test_root_stat_requires_access_and_is_directory():
    api, session = client(listing())
    assert api.stat('/')['is_dir'] is True
    assert session.calls[0][2]['json']['path'] == '/'
    api, _ = client(Response(code=403))
    with pytest.raises(AuthenticationError):
        api.stat('/')


def test_move_task_is_journaled_before_wait_failure():
    from javsp.alist_client import TaskFailedError
    api, session = client(listing(storage()), listing(), {'tasks': [{'id': 'move-task'}]},
                          {'id': 'move-task', 'state': 7})
    remembered = []

    def save(task_id):
        assert session.calls[-1][1] == 'fs/move'
        remembered.append(task_id)

    with pytest.raises(TaskFailedError):
        api.move('/115/source/movie.mp4', '/115/final', on_task=save)
    assert remembered == ['move-task']


def test_metadata_storage_identity_is_independent_of_video_driver():
    api, _ = client(listing(storage('/A', driver='Local')))
    assert api.storage_identity('/A/library')['driver'] == 'Local'
