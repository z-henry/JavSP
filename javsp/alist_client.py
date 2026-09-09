"""Native AList/OpenList operations; deliberately no file download/link API.

Wire format: OpenList v4.2.5 server/handles/{fsread,fsmanage,fsup,task}.go.
Task states: github.com/OpenListTeam/tache v0.2.2, state.go.
Mutations are submitted once. Callers must journal intent and reconcile ambiguous
network failures before attempting another mutation.
"""

import math
import os
import posixpath
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests
from requests.adapters import HTTPAdapter


class AlistError(RuntimeError):
    """Remote operation failed or its result could not be confirmed."""


class AuthenticationError(AlistError):
    """Authentication or authorization was refused."""


class ConflictError(AlistError):
    """An existing remote object prevents a non-overwriting operation."""


class TaskFailedError(AlistError):
    """A known task is terminal (failed/canceled), and cannot still execute."""


def _path(path: str) -> str:
    if (not isinstance(path, str) or not path.startswith('/') or '\\' in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(part in ('.', '..') for part in path.split('/'))):
        raise AlistError('Remote paths must be absolute POSIX paths without traversal')
    return '/' + '/'.join(part for part in path.split('/') if part)


def _name(name: str) -> str:
    if (not isinstance(name, str) or not name or name in ('.', '..')
            or '/' in name or '\\' in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise AlistError('Invalid remote entry name')
    return name


def _split(path: str) -> tuple[str, str]:
    path = _path(path)
    if path == '/':
        raise AlistError('Cannot mutate the remote root')
    return posixpath.dirname(path), posixpath.basename(path)


class AlistClient:
    """Config is an object with base_url, token and three timeout attributes.

    ``session`` may be a requests-compatible fake. For real Sessions retries are
    disabled, including on injected Sessions. No response body or transport
    exception is included in errors, as either may contain credentials.
    """

    PAGE_SIZE = 200
    _STATES = ('pending', 'running', 'succeeded', 'canceling', 'canceled',
               'errored', 'failing', 'failed', 'waitingretry', 'beforeretry')

    def __init__(self, config: Any, session=None):
        self.base_url = str(config.base_url).rstrip('/')
        try:
            parsed = urlsplit(self.base_url)
            valid = (parsed.scheme in ('http', 'https') and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment)
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise AlistError('Invalid AList base_url; use an HTTP(S) service URL without credentials')
        token = os.environ.get('JAVSP_ALIST_TOKEN')
        if token is None:
            token = config.token
            if hasattr(token, 'get_secret_value'):
                token = token.get_secret_value()
        if not isinstance(token, str) or not token.strip() or any(ord(c) < 32 for c in token):
            raise AuthenticationError('Configure an AList token or JAVSP_ALIST_TOKEN')
        self._token = token.strip()
        for field in ('http_timeout', 'task_timeout', 'poll_interval'):
            try:
                value = float(getattr(config, field))
            except (TypeError, ValueError, AttributeError):
                raise AlistError('Invalid AList timeout configuration') from None
            if not math.isfinite(value) or value <= 0:
                raise AlistError('AList timeouts and poll_interval must be positive and finite')
            setattr(self, field, value)
        self.session = session if session is not None else requests.Session()
        if isinstance(self.session, requests.Session):
            # Also override custom mounts supplied by a caller: never replay writes.
            for prefix in list(self.session.adapters):
                self.session.mount(prefix, HTTPAdapter(max_retries=0))
        self._owns_session = session is None

    def close(self):
        if self._owns_session:
            self.session.close()

    def _request(self, method: str, endpoint: str, *, timeout=None, **kwargs):
        headers = dict(kwargs.pop('headers', {}))
        headers['Authorization'] = self._token
        try:
            response = self.session.request(
                method, self.base_url + '/api/' + endpoint, headers=headers,
                timeout=self.http_timeout if timeout is None else timeout,
                allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise AlistError(f'AList transport failure at {endpoint}; result may be uncertain') from None
        try:
            status = response.status_code
            try:
                payload = response.json()
            except ValueError:
                payload = None
            code = payload.get('code') if isinstance(payload, dict) else None
            # v4.2.5 reports non-overwrite conflicts as 403, not 409.
            message = payload.get('message') if isinstance(payload, dict) else None
            exists = (isinstance(message, str) and
                      re.fullmatch(r'file(?: \[.*\])? exists', message) is not None)
            if (status == 409 or code in (409, '409') or
                    (endpoint in ('fs/put', 'fs/move', 'fs/rename') and exists
                     and (status == 403 or code in (403, '403')))):
                raise ConflictError(f'AList destination already exists at {endpoint}')
            if status in (401, 403) or code in (401, 403, '401', '403'):
                raise AuthenticationError(f'AList authentication/permission denied at {endpoint}')
            if not 200 <= status < 300:
                raise AlistError(f'AList HTTP error {status} at {endpoint}')
            if not isinstance(payload, dict) or code not in (200, '200'):
                raise AlistError(f'AList API error or malformed response at {endpoint}')
            return payload.get('data')
        finally:
            response.close()

    def _pages(self, endpoint: str, *, path=None, refresh=True) -> list[dict]:
        entries, seen = [], set()
        page, expected_total = 1, None
        while True:
            args = {'page': page, 'per_page': self.PAGE_SIZE}
            if path is None:
                data = self._request('GET', endpoint, params=args)
            else:
                args.update(path=path, password='', refresh=refresh)
                data = self._request('POST', endpoint, json=args)
            if not isinstance(data, dict):
                raise AlistError('Malformed AList directory/storage listing')
            total, content = data.get('total'), data.get('content')
            if type(total) is not int or total < 0:
                raise AlistError('Malformed AList listing total')
            if content is None and total == 0:
                content = []
            if not isinstance(content, list):
                raise AlistError('Malformed AList listing content')
            if expected_total is not None and total != expected_total:
                raise AlistError('AList listing changed during pagination; rescan required')
            expected_total = total
            for entry in content:
                if not isinstance(entry, dict):
                    raise AlistError('Malformed AList listing entry')
                if path is not None:
                    identity = _name(entry.get('name'))
                    if (type(entry.get('is_dir')) is not bool
                            or type(entry.get('size')) is not int or entry['size'] < 0
                            or not isinstance(entry.get('modified'), str)):
                        raise AlistError('Malformed AList file metadata')
                else:
                    identity = _path(entry.get('mount_path'))
                if identity in seen:
                    raise AlistError('Duplicate AList listing entry; rescan required')
                seen.add(identity)
                entries.append(entry)
            if len(entries) == total:
                return entries
            if len(entries) > total or not content:
                raise AlistError('Incomplete or inconsistent AList pagination')
            page += 1

    def list_dir(self, path: str, refresh: bool = True) -> list[dict]:
        """Return all raw entries, preserving both upstream hash field formats."""
        return self._pages('fs/list', path=_path(path), refresh=refresh)

    def stat(self, path: str) -> dict | None:
        """Only a successful ancestor listing can establish absence.

        The root is synthetic after verifying that its listing is accessible.
        If listing the parent fails, verify whether the parent itself is absent.
        Authentication failures are not probed further. If ancestor inspection
        cannot prove absence, preserve the original listing error.
        """
        path = _path(path)
        if path == '/':
            self.list_dir('/')
            return {'name': '/', 'size': 0, 'is_dir': True, 'modified': ''}
        parent, name = _split(path)
        try:
            entries = self.list_dir(parent)
        except AuthenticationError:
            raise
        except AlistError:
            # Root has no parent and a failed root listing proves no absence.
            if parent != '/':
                try:
                    if self.stat(parent) is None:
                        return None
                except AlistError:
                    pass
            raise
        return next((entry for entry in entries if entry['name'] == name), None)

    def mkdirs(self, path: str) -> None:
        path = _path(path)
        if path == '/':
            self.stat('/')
            return
        current = ''
        for part in path.strip('/').split('/'):
            current += '/' + part
            entry = self.stat(current)
            if entry is None:
                self._request('POST', 'fs/mkdir', json={'path': current})
                entry = self.stat(current)
                if entry is None:
                    raise AlistError('AList directory creation could not be verified')
            if not entry['is_dir']:
                raise ConflictError('A file occupies the requested directory path')

    def rename(self, path: str, name: str) -> None:
        parent, old_name = _split(path)
        name = _name(name)
        if name == old_name:
            return
        if self.stat(posixpath.join(parent, name)) is not None:
            raise ConflictError('Rename destination already exists')
        self._request('POST', 'fs/rename', json={'path': _path(path), 'name': name, 'overwrite': False})

    def move(self, path: str, dst_dir: str, *, on_task=None) -> None:
        """Perform a native move and wait for any returned move tasks."""
        parent, name = _split(path)
        dst_dir = _path(dst_dir)
        if parent == dst_dir:
            return
        self._same_storage(path, dst_dir)
        if self.stat(posixpath.join(dst_dir, name)) is not None:
            raise ConflictError('Move destination already exists')
        data = self._request('POST', 'fs/move', json={
            'src_dir': parent, 'dst_dir': dst_dir, 'names': [name],
            'overwrite': False, 'skip_existing': False, 'merge': False})
        if data is None:
            return
        if not isinstance(data, dict):
            raise AlistError('Malformed AList move result')
        tasks = data.get('tasks', [])
        if not isinstance(tasks, list):
            raise AlistError('Malformed AList move tasks')
        for task in tasks:
            task_id = self._task_id(task)
            if on_task is not None:
                on_task(task_id)
            self.wait_move(task_id)

    def remove(self, path: str) -> None:
        parent, name = _split(path)
        self._request('POST', 'fs/remove', json={'dir': parent, 'names': [name]})

    @staticmethod
    def _task_id(task) -> str:
        if not isinstance(task, dict) or not isinstance(task.get('id'), str) or not task['id']:
            raise AlistError('Malformed AList task identifier')
        return task['id']

    def upload(self, local_path, remote_path: str) -> str | None:
        """Stream one caller-selected metadata file; return its task ID or None."""
        _split(remote_path)
        remote_path = _path(remote_path)
        if self.stat(remote_path) is not None:
            raise ConflictError('Upload destination already exists')
        try:
            with Path(local_path).open('rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                data = self._request('PUT', 'fs/put', data=stream, headers={
                    'File-Path': quote(remote_path, safe='/'), 'As-Task': 'true',
                    'Overwrite': 'false', 'Content-Type': 'application/octet-stream',
                    'Content-Length': str(size)})
        except OSError:
            raise AlistError('Cannot read the local metadata upload file') from None
        if data is None:
            return None
        if not isinstance(data, dict) or 'task' not in data:
            raise AlistError('Malformed AList upload result; reconcile before retrying')
        return self._task_id(data['task'])

    def wait_upload(self, task_id: str) -> None:
        self._wait_task('upload', task_id)

    def wait_move(self, task_id: str) -> None:
        self._wait_task('move', task_id)

    def _wait_task(self, kind: str, task_id: str) -> None:
        self._task_id({'id': task_id})
        deadline = time.monotonic() + self.task_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AlistError(f'AList {kind} task timed out; task may still be running')
            data = self._request('POST', f'admin/task/{kind}/info', params={'tid': task_id},
                                 timeout=min(self.http_timeout, remaining))
            if not isinstance(data, dict) or data.get('id') != task_id:
                raise AlistError('Malformed or mismatched AList task info')
            state = data.get('state')
            if isinstance(state, str):
                state = state.lower().replace('_', '').replace(' ', '')
                if state.startswith('state'):
                    state = state[5:]
                if state in tuple(str(i) for i in range(len(self._STATES))):
                    state = self._STATES[int(state)]
            elif type(state) is int and 0 <= state < len(self._STATES):
                state = self._STATES[state]
            else:
                raise AlistError('Unknown AList task state')
            if state not in self._STATES:
                raise AlistError('Unknown AList task state')
            if state == 'succeeded':
                if data.get('error'):
                    raise AlistError(f'AList {kind} task reported an error despite success state')
                return
            if state in ('canceled', 'failed'):
                raise TaskFailedError(f'AList {kind} task {state}')
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(self.poll_interval, remaining))

    def ensure_same_storage(self, source: str, video: str) -> dict:
        """Require the same longest matching mount and known native 115 support.

        Storage responses can contain access/refresh tokens; never expose them.
        Resolve afresh on each call so a cached mount cannot authorize a move.
        """
        storage = self._same_storage(source, video)
        if storage.get('driver') != '115 Open':
            raise AlistError('Native video moves require the verified 115 Open driver in v1')
        return storage

    def storage_identity(self, path: str) -> dict:
        return self._same_storage(path, path)

    def _same_storage(self, source: str, video: str) -> dict:
        source, video = _path(source), _path(video)
        storages = self._pages('admin/storage/list')

        def resolve(path):
            matches = [s for s in storages if s['mount_path'].rstrip('/') == path
                       or path.startswith(s['mount_path'].rstrip('/') + '/')]
            if not matches:
                raise AlistError('No AList storage mount matches the requested path')
            return max(matches, key=lambda s: len(s['mount_path'].rstrip('/')))

        src, dst = resolve(source), resolve(video)
        if src is not dst:
            raise AlistError('Cross-storage video moves are not supported')
        if src.get('disabled'):
            raise AlistError('The AList video storage is disabled')
        return {key: src.get(key) for key in ('id', 'mount_path', 'driver')}
