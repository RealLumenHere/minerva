"""wq#2573: lumen_reflector must record run status on every exit path so
cron_deadman.py can page on a failure immediately, independent of the
ExecStartPost-only `.stamp.<unit>` (which never fires when a Type=oneshot's
ExecStart exits non-zero)."""
import json
import sys

import pytest

import lumen_reflector as lr


@pytest.fixture(autouse=True)
def isolate_stamp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, 'STAMP_DIR', tmp_path)
    monkeypatch.setattr(lr, 'RUN_STATE', tmp_path / '.state.lumen-reflector')
    monkeypatch.setattr(lr, 'RUN_STAMP', tmp_path / '.stamp.lumen-reflector')
    monkeypatch.setattr(lr, 'OUT_DIR', tmp_path / 'reflections')
    monkeypatch.setattr(lr, 'recent_episodes', lambda n=lr.N_EPISODES: [])
    monkeypatch.setattr(lr, 'latest_dream', lambda: '')
    monkeypatch.setattr(lr, 'growth', lambda: (0, 0))
    return tmp_path


def _state(tmp_path):
    return json.loads((tmp_path / '.state.lumen-reflector').read_text())


def test_write_run_state_ok_then_error_overwrites(tmp_path):
    lr.write_run_state('ok', '3 actionable')
    assert _state(tmp_path) == {'status': 'ok', 'detail': '3 actionable',
                                 'ts': _state(tmp_path)['ts']}
    assert (tmp_path / '.stamp.lumen-reflector').exists()

    lr.write_run_state('error', 'http_401: bad key')
    assert _state(tmp_path)['status'] == 'error'
    assert _state(tmp_path)['detail'] == 'http_401: bad key'


def test_main_no_key_writes_error_state_and_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, '_load_key', lambda: '')
    with pytest.raises(SystemExit) as exc:
        lr.main()
    assert exc.value.code == 2
    assert _state(tmp_path) == {'status': 'error', 'detail': 'no LITELLM_MASTER_KEY',
                                 'ts': _state(tmp_path)['ts']}


def test_main_llm_error_writes_error_state_and_exits_1(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, '_load_key', lambda: 'test-key')
    monkeypatch.setattr(lr, 'call_llm', lambda prompt, key: (None, 'http_401: invalid key'))
    with pytest.raises(SystemExit) as exc:
        lr.main()
    assert exc.value.code == 1
    st = _state(tmp_path)
    assert st['status'] == 'error'
    assert st['detail'] == 'http_401: invalid key'


def test_main_success_writes_ok_state(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, '_load_key', lambda: 'test-key')
    monkeypatch.setattr(lr, 'call_llm', lambda prompt, key: ({'summary': 'x', 'proposals': []}, None))
    lr.main()
    assert _state(tmp_path)['status'] == 'ok'
    assert (tmp_path / '.stamp.lumen-reflector').exists()


def test_alert_failure_does_not_raise(monkeypatch, capsys):
    class BoomRequests:
        @staticmethod
        def post(*a, **k):
            raise OSError('ntfy unreachable')
    monkeypatch.setitem(sys.modules, 'requests', BoomRequests)
    lr.alert('test message')
    assert 'ntfy unreachable' in capsys.readouterr().err
