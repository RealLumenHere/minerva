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


def test_uncaught_exception_in_recent_episodes_overwrites_stale_ok(tmp_path, monkeypatch):
    """wq#2596: the regression wq#2593 reproduced against b3259ca. A prior run
    left status:ok; this run dies in a helper before reaching any of _run()'s
    deliberate exits. Pre-fix the stale 'ok' survived, cron_deadman saw a
    healthy unit, and the only remaining signal was 14-day stamp age."""
    lr.write_run_state('ok', 'previous run: 3 actionable')
    monkeypatch.setattr(lr, '_load_key', lambda: 'test-key')

    def boom(n=lr.N_EPISODES):
        raise RuntimeError('database is locked')
    monkeypatch.setattr(lr, 'recent_episodes', boom)
    monkeypatch.setattr(lr, 'alert', lambda msg: None)

    with pytest.raises(RuntimeError, match='database is locked'):
        lr.main()

    assert _state(tmp_path)['status'] == 'error'
    assert 'uncaught RuntimeError: database is locked' == _state(tmp_path)['detail']


def test_uncaught_exception_alerts_and_reraises(tmp_path, monkeypatch):
    """The crash must stay loud: ntfy fires like every other failure path, and
    the exception propagates so systemd still sees a non-zero exit plus the
    traceback in the journal."""
    monkeypatch.setattr(lr, '_load_key', lambda: 'test-key')
    monkeypatch.setattr(lr, 'call_llm', lambda prompt, key: (_ for _ in ()).throw(KeyError('choices')))
    sent = []
    monkeypatch.setattr(lr, 'alert', sent.append)

    with pytest.raises(KeyError):
        lr.main()

    assert len(sent) == 1
    assert 'uncaught KeyError' in sent[0]
    assert _state(tmp_path)['status'] == 'error'


def test_deliberate_sys_exit_state_is_not_relabelled(tmp_path, monkeypatch):
    """SystemExit must pass through main()'s guard untouched: the no-key path
    writes its own specific detail, and the guard must not overwrite it with a
    generic 'uncaught SystemExit'."""
    monkeypatch.setattr(lr, '_load_key', lambda: '')
    monkeypatch.setattr(lr, 'alert', lambda msg: None)
    with pytest.raises(SystemExit) as exc:
        lr.main()
    assert exc.value.code == 2
    assert _state(tmp_path)['detail'] == 'no LITELLM_MASTER_KEY'


def test_alert_failure_does_not_raise(monkeypatch, capsys):
    class BoomRequests:
        @staticmethod
        def post(*a, **k):
            raise OSError('ntfy unreachable')
    monkeypatch.setitem(sys.modules, 'requests', BoomRequests)
    lr.alert('test message')
    assert 'ntfy unreachable' in capsys.readouterr().err
