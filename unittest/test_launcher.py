import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from javsp.launcher import run


@pytest.mark.parametrize('exit_code', [0, 1])
def test_frozen_console_waits_and_preserves_exit_code(monkeypatch, capsys, exit_code):
    entry = Mock(side_effect=SystemExit(exit_code))
    key = Mock(return_value='x')
    monkeypatch.setitem(sys.modules, 'javsp.__main__', SimpleNamespace(entry=entry))
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(getwch=key))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    with pytest.raises(SystemExit) as result:
        run()
    assert result.value.code == exit_code
    key.assert_called_once()
    assert '按任意键退出' in capsys.readouterr().out


def test_unexpected_error_is_printed_before_wait(monkeypatch):
    events = []
    entry = Mock(side_effect=RuntimeError('startup failed'))
    monkeypatch.setitem(sys.modules, 'javsp.__main__', SimpleNamespace(entry=entry))
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(getwch=lambda: events.append('wait')))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(sys, 'excepthook', lambda *args: events.append('error'))
    with pytest.raises(SystemExit) as result:
        run()
    assert result.value.code == 1
    assert events == ['error', 'wait']


@pytest.mark.parametrize('frozen,terminal', [(False, True), (True, False)])
def test_noninteractive_or_source_run_does_not_wait(monkeypatch, frozen, terminal):
    key = Mock()
    monkeypatch.setitem(sys.modules, 'javsp.__main__', SimpleNamespace(entry=Mock()))
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(getwch=key))
    monkeypatch.setattr(sys, 'frozen', frozen, raising=False)
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(isatty=lambda: terminal))
    run()
    key.assert_not_called()
