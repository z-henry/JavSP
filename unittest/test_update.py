"""Startup version checks must also support an uninstalled source checkout."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import javsp.func as func


@pytest.mark.parametrize('allow_check,auto_update', [(True, False), (True, True), (False, False)])
def test_source_checkout_skips_update_without_package_metadata(monkeypatch, allow_check, auto_update):
    monkeypatch.delattr(func.sys, 'javsp_version', raising=False)
    monkeypatch.setattr(func.meta, 'version', Mock(side_effect=func.meta.PackageNotFoundError('javsp')))
    request = Mock(side_effect=AssertionError('source checkout must not query updates'))
    download = Mock(side_effect=AssertionError('source checkout must not download updates'))
    monkeypatch.setattr(func, 'request_get', request)
    monkeypatch.setattr(func, 'download_update', download)

    func.check_update(allow_check, auto_update)

    request.assert_not_called()
    download.assert_not_called()


def test_packaged_version_uses_injected_version(monkeypatch, capsys):
    monkeypatch.setattr(func.sys, 'javsp_version', 'v1.2.3', raising=False)
    metadata = Mock(side_effect=AssertionError('injected version needs no distribution metadata'))
    monkeypatch.setattr(func.meta, 'version', metadata)
    func.check_update(False, False)
    assert 'v1.2.3' in capsys.readouterr().out
    metadata.assert_not_called()


def test_installed_package_still_checks_for_updates(monkeypatch, capsys):
    monkeypatch.delattr(func.sys, 'javsp_version', raising=False)
    monkeypatch.setattr(func.meta, 'version', Mock(return_value='1.2.3'))
    request = Mock(return_value=SimpleNamespace(json=lambda: {
        'tag_name': 'v1.2.3', 'published_at': '2026-09-08T00:00:00Z'}))
    monkeypatch.setattr(func, 'request_get', request)
    func.check_update(True, False)
    request.assert_called_once()
    assert '已是最新版' in capsys.readouterr().out
