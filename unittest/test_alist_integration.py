"""Opt-in real AList integration against UUID-isolated synthetic fixtures only.

JAVSP_ALIST_INTEGRATION=1 and JAVSP_ALIST_TOKEN enable it. Crawlers and cover
downloads use deterministic fixtures; naming, NFO, poster, HTTP, native moves,
SQLite recovery and deletion use the real implementation.
"""
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest


@pytest.mark.skipif(os.environ.get('JAVSP_ALIST_INTEGRATION') != '1', reason='opt-in remote integration')
def test_real_alist_isolated(tmp_path, monkeypatch):
    from PIL import Image
    from javsp.config import Cfg, AlistConfig
    from javsp.datatype import MovieInfo
    from javsp.alist_client import AlistClient
    from javsp.alist_pipeline import run_alist
    import javsp.__main__ as main
    import javsp.file as scanner

    marker = '.javsp-integration-' + uuid.uuid4().hex
    video_root = '/115/' + marker
    metadata_root = '/local-emby/' + marker
    cfg = Cfg().model_copy(deep=True)
    object.__setattr__(cfg.scanner, 'source', 'alist')
    object.__setattr__(cfg.scanner, 'manual', False)
    object.__setattr__(cfg.scanner, 'minimum_size', 1)
    object.__setattr__(cfg.scanner, 'skip_nfo_dir', False)
    object.__setattr__(cfg.summarizer.path, 'output_folder_pattern', '#整理完成/{actress}/[{num}] {title}')
    object.__setattr__(cfg.summarizer.path, 'basename_pattern', '{num}')
    object.__setattr__(cfg.summarizer.path, 'length_maximum', 250)
    object.__setattr__(cfg.summarizer, 'move_files', True)
    object.__setattr__(cfg.summarizer.path, 'hard_link', False)
    object.__setattr__(cfg.summarizer.cover.crop, 'engine', None)
    object.__setattr__(cfg.summarizer.cover, 'add_label', False)
    object.__setattr__(cfg.summarizer.extra_fanarts, 'enabled', False)
    object.__setattr__(cfg.translator, 'engine', None)
    object.__setattr__(cfg, 'alist', AlistConfig(base_url=os.environ.get('JAVSP_ALIST_TEST_URL', 'http://192.168.100.10:5244'),
                            token='', source_dir=video_root + '/source',
                            video_dir=video_root + '/videos', metadata_dir=metadata_root + '/metadata',
                            work_dir=tmp_path / 'state', http_timeout=60, task_timeout=180, poll_interval=2))
    monkeypatch.setattr(main, 'Cfg', lambda: cfg)
    monkeypatch.setattr(scanner, 'Cfg', lambda: cfg)
    monkeypatch.setattr(main, 'parallel_crawler', lambda *a: {'fixture': True})

    def summary(movie, _):
        info = MovieInfo(movie.dvdid)
        info.title = 'Remote integration fixture'
        info.actress = ['Test Actor']
        info.plot = 'Synthetic fixture; no actual media content.'
        info.cover = 'https://example.invalid/fixture.jpg'
        info.covers, info.big_covers = [info.cover], []
        info.genre, info.uncensored = [], False
        movie.info = info
        return True

    def cover(covers, path, *args):
        Image.new('RGB', (600, 400), '#507080').save(path)
        return covers[0], path

    monkeypatch.setattr(main, 'info_summary', summary)
    monkeypatch.setattr(main, 'download_cover', cover)
    client = AlistClient(cfg.alist)
    # Verify mount support before creating anything.
    client.ensure_same_storage(cfg.alist.source_dir, cfg.alist.video_dir)
    for root in (video_root, metadata_root):
        assert root.endswith('/' + marker) and client.stat(root) is None
    requests_seen = []
    original_request = client._request

    def traced(method, endpoint, **kwargs):
        requests_seen.append((method, endpoint, kwargs.get('headers', {}).get('File-Path', '')))
        return original_request(method, endpoint, **kwargs)

    try:
        client.mkdirs(cfg.alist.source_dir + '/release')
        client.mkdirs(cfg.alist.video_dir)
        client.mkdirs(cfg.alist.metadata_dir)
        # Upload tiny deliberately synthetic files only to provision the fixture.
        for name in ('TEST-001-CD1.mp4', 'TEST-001-CD2.mp4', 'readme.txt'):
            local = tmp_path / name
            local.write_bytes(('JavSP synthetic fixture: ' + name).encode() * 16)
            task = client.upload(local, cfg.alist.source_dir + '/release/' + name)
            if task:
                client.wait_upload(task)
        monkeypatch.setattr(client, '_request', traced)
        assert run_alist(cfg, main.RunNormalMode, client=client) == 0
        relative = '#整理完成/Test Actor/[TEST-001] Remote integration fixture'
        assert client.stat(cfg.alist.source_dir)['is_dir']
        assert client.stat(cfg.alist.source_dir + '/release') is None
        for name in ('TEST-001-CD1.mp4', 'TEST-001-CD2.mp4'):
            assert client.stat(cfg.alist.video_dir + '/' + relative + '/' + name)['size'] > 0
        for name in ('movie.nfo', 'poster.jpg', 'fanart.jpg'):
            assert client.stat(cfg.alist.metadata_dir + '/' + relative + '/' + name)['size'] > 0
        assert not any(endpoint in ('fs/get', 'fs/copy') for _, endpoint, _ in requests_seen)
        assert not any(path.lower().endswith('.mp4') for method, _, path in requests_seen if method == 'PUT')
        # A second run is a no-op on the synthetic videos.
        assert run_alist(cfg, main.RunNormalMode, client=client) == 0
    finally:
        # These exact UUID roots were generated above and confirmed absent before creation.
        for root in (video_root, metadata_root):
            if root in ('/115/' + marker, '/local-emby/' + marker) and client.stat(root) is not None:
                client.remove(root)
                assert client.stat(root) is None
        client.close()
