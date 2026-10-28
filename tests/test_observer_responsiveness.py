"""Slow ingestion must not stop the listener accepting bootstrap requests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.client
import http.server
import logging
import threading

import pytest

import __init__ as office


@contextmanager
def running_observer(monkeypatch, release):
    """Run the production handler with an ephemeral loopback listener."""
    servers = []
    ready = threading.Event()
    base = http.server.ThreadingHTTPServer

    class CapturedServer(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append(self)

    monkeypatch.setattr(http.server, 'ThreadingHTTPServer', CapturedServer)
    monkeypatch.setattr(office, '_resolve_port', lambda: 0)
    monkeypatch.setattr(office, '_port', office._port)
    worker = threading.Thread(target=office._serve,
                              kwargs={'on_ready': lambda _port: ready.set()}, daemon=True)
    worker.start()
    try:
        assert ready.wait(3), 'production listener must start'
        yield servers[0], worker
    finally:
        # Always unblock the old implementation too; failing tests must not leak
        # a server or leave a monkeypatched builder running after teardown.
        release.set()
        if servers:
            servers[0].shutdown()
        worker.join(4)
        assert not worker.is_alive(), 'observer stopped'


def get(server, route):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=1)
    try:
        connection.request('GET', route)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_stalled_maintenance_keeps_bootstrap_and_capabilities_responsive(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked_state():
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(10), 'test released ingestion'
        return {}

    monkeypatch.setattr(office, 'build_state', blocked_state)
    with running_observer(monkeypatch, release) as (server, _):
        assert entered.wait(3), 'maintenance entered the stalled ingestion'
        paths = ['/', '/js/office.js', '/js/task-runner.js', '/js/agents.js', '/js/task-desk.js', '/task-runs', '/agents']
        with ThreadPoolExecutor(max_workers=len(paths)) as pool:
            replies = list(pool.map(lambda route: get(server, route), paths))
        assert all(status == 200 and body for status, body in replies)
        for _ in range(20):
            server.service_actions()
        assert len(calls) == 1, 'listener cycles must not spawn overlapping maintenance'
        # The background worker must not expand the observer trust boundary.
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=1)
        try:
            connection.request('GET', '/', headers={'Host': 'untrusted.example'})
            response = connection.getresponse()
            assert response.status == 403
            response.read()
        finally:
            connection.close()


def test_listener_can_stop_while_ingestion_is_stalled(monkeypatch):
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def blocked_state():
        entered.set()
        assert release.wait(10), 'test released ingestion'
        return {}

    monkeypatch.setattr(office, 'build_state', blocked_state)
    with running_observer(monkeypatch, release) as (server, worker):
        assert entered.wait(3)
        def stop():
            server.shutdown()
            worker.join(2)
            if not worker.is_alive():
                stopped.set()
        stopper = threading.Thread(target=stop, daemon=True)
        stopper.start()
        try:
            assert stopped.wait(3), 'stalled disk ingestion must not prevent listener shutdown'
        finally:
            release.set()
            stopper.join(3)
        maintenance = getattr(server, '_maintenance_thread', None)
        if maintenance is not None:
            maintenance.join(2)
            assert not maintenance.is_alive(), 'stopped worker exits after its current read'


def test_ingestion_failure_is_redacted_and_does_not_kill_maintenance(monkeypatch, caplog):
    release, recovered = threading.Event(), threading.Event()
    calls = []
    def flaky_state():
        calls.append(1)
        if len(calls) == 1:
            raise OSError('sensitive-path secret-token private-event-body')
        recovered.set()
        return {}
    monkeypatch.setattr(office, 'build_state', flaky_state)
    caplog.set_level(logging.WARNING, logger=office.logger.name)
    with running_observer(monkeypatch, release) as (server, _):
        assert recovered.wait(4), 'next maintenance pass recovers without a page request'
        assert get(server, '/js/office.js')[0] == 200
    assert 'OSError' in caplog.text
    assert not any(value in caplog.text for value in ['sensitive-path', 'secret-token', 'private-event-body'])


def test_broken_logging_destination_does_not_end_maintenance(monkeypatch):
    release, recovered = threading.Event(), threading.Event()
    calls = []

    def flaky_state():
        calls.append(1)
        if len(calls) == 1:
            raise OSError('synthetic ingestion failure')
        recovered.set()
        return {}

    def broken_logger(*args, **kwargs):
        raise RuntimeError('synthetic log destination failure')

    monkeypatch.setattr(office, 'build_state', flaky_state)
    monkeypatch.setattr(office.logger, 'warning', broken_logger)
    with running_observer(monkeypatch, release) as (server, _):
        assert recovered.wait(4), 'logging failure must not disable future ingestion'
        assert get(server, '/js/office.js')[0] == 200


# These failures happen before the worker body, so ingestion's own exception
# handler cannot keep the listener alive. Exercise the real server lifecycle.


@pytest.mark.parametrize('failure_stage', ['construct', 'start'])
def test_maintenance_startup_failure_keeps_listener_alive_and_recovers(monkeypatch, caplog, failure_stage):
    release, failed, recovered = threading.Event(), threading.Event(), threading.Event()
    original_thread = threading.Thread
    attempts = []

    class FailingOnceThread(original_thread):
        def __init__(self, *args, **kwargs):
            maintenance = kwargs.get('name') == 'agent-office-maintenance'
            if maintenance:
                attempts.append(1)
                if failure_stage == 'construct' and len(attempts) == 1:
                    failed.set()
                    raise RuntimeError('private-project secret-token')
            super().__init__(*args, **kwargs)

        def start(self):
            if self.name == 'agent-office-maintenance' and failure_stage == 'start' and len(attempts) == 1:
                failed.set()
                raise RuntimeError('private-project secret-token')
            return super().start()

    monkeypatch.setattr(office.threading, 'Thread', FailingOnceThread)
    monkeypatch.setattr(office, 'build_state', lambda: recovered.set() or {})
    caplog.set_level(logging.WARNING, logger=office.logger.name)
    with running_observer(monkeypatch, release) as (server, _):
        assert failed.wait(3), 'injected maintenance startup failure occurred'
        assert get(server, '/js/office.js')[0] == 200, 'startup failure must not close HTTP'
        assert recovered.wait(4), 'a later pass retries maintenance startup'
        assert len(attempts) == 2, 'only one replacement worker starts'
        assert get(server, '/task-runs')[0] == 200
    assert 'RuntimeError' in caplog.text
    assert 'private-project' not in caplog.text and 'secret-token' not in caplog.text


def test_persistent_worker_start_failure_is_bounded_even_with_broken_logger(monkeypatch):
    release, failed = threading.Event(), threading.Event()
    original_start = threading.Thread.start
    attempts = []

    def fail_maintenance_start(thread):
        if thread.name == 'agent-office-maintenance':
            attempts.append(1)
            failed.set()
            raise RuntimeError('synthetic thread exhaustion')
        return original_start(thread)

    def broken_logger(*args, **kwargs):
        raise OSError('synthetic logging failure')

    monkeypatch.setattr(threading.Thread, 'start', fail_maintenance_start)
    monkeypatch.setattr(office.logger, 'warning', broken_logger)
    with running_observer(monkeypatch, release) as (server, worker):
        assert failed.wait(3)
        assert get(server, '/js/office.js')[0] == 200
        # Use a fixed monotonic instant: request traffic must not bypass the
        # retry deadline or create a burst of failed worker allocations.
        now = office.time.monotonic()
        with monkeypatch.context() as clock:
            clock.setattr(office.time, 'monotonic', lambda: now)
            before = len(attempts)
            for _ in range(100):
                server.service_actions()
            assert len(attempts) <= before + 1
        assert worker.is_alive()
        assert get(server, '/task-runs')[0] == 200
    assert not worker.is_alive(), 'shutdown must not join a never-started thread'


@pytest.fixture
def publishing_server(tmp_path, monkeypatch):
    """Real inbox publication with a controlled, not-yet-bound server worker."""
    workers = []
    probes = []
    release = threading.Event()
    bound = [False]
    original_thread = threading.Thread

    class TrackedThread(original_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if self.name == 'pixel-office-http':
                workers.append(self)

    monkeypatch.setattr(office, '_office_dir', lambda: tmp_path)
    monkeypatch.setattr(office, '_server_thread', None, raising=False)
    monkeypatch.setattr(office, '_serve', lambda: release.wait())
    monkeypatch.setattr(office, '_server_bound', lambda: probes.append(1) or bound[0])
    monkeypatch.setattr(threading, 'Thread', TrackedThread)
    try:
        yield workers, release, bound, probes, tmp_path
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(3)
                assert not worker.is_alive(), 'controlled listener stopped'


def test_pending_listener_start_is_shared_without_connecting_on_every_event(publishing_server):
    workers, _, _, probes, directory = publishing_server
    for index in range(12):
        office._publish({'event': 'tool_start', 'call_id': str(index)})
    assert len(list((directory / 'inbox').glob('*.json'))) == 12
    assert len(workers) == 1, 'a worker awaiting bind still owns listener startup'
    assert workers[0].is_alive()
    assert probes == [], 'own live worker needs no extra accepted socket per event'


def test_concurrent_publishers_share_one_pending_listener(publishing_server):
    workers, _, _, probes, directory = publishing_server
    with ThreadPoolExecutor(max_workers=4) as pool:
        for batch in range(3):
            list(pool.map(lambda index: office._publish({
                'event': 'tool_start', 'call_id': str(batch * 4 + index),
            }), range(4)))
    assert len(list((directory / 'inbox').glob('*.json'))) == 12
    assert len(workers) == 1
    assert probes == []


@pytest.mark.parametrize('failure_stage', ['construct', 'start'])
def test_listener_worker_failure_preserves_events_and_retries_next_event(
    publishing_server, monkeypatch, caplog, failure_stage,
):
    workers, _, _, _, directory = publishing_server
    original_thread = threading.Thread
    attempts = []

    class FailingOnceThread(original_thread):
        def __init__(self, *args, **kwargs):
            attempts.append(1)
            if failure_stage == 'construct' and len(attempts) == 1:
                raise RuntimeError('private/path secret-token')
            super().__init__(*args, **kwargs)

        def start(self):
            if failure_stage == 'start' and len(attempts) == 1:
                raise RuntimeError('private/path secret-token')
            return super().start()

    monkeypatch.setattr(threading, 'Thread', FailingOnceThread)
    monkeypatch.setattr(office, '_publish_warning_at', None)
    monkeypatch.setattr(office, '_publish_warnings_suppressed', 0)
    caplog.set_level(logging.WARNING, logger=office.logger.name)
    office._publish({'event': 'session_start', 'session_id': 'first'})
    office._publish({'event': 'tool_start', 'session_id': 'first'})
    assert len(list((directory / 'inbox').glob('*.json'))) == 2
    assert len(attempts) == 2, 'the next event must retry without a stale started flag'
    assert sum(worker.is_alive() for worker in workers) == 1
    assert 'RuntimeError' in caplog.text
    assert 'private/path' not in caplog.text and 'secret-token' not in caplog.text


def test_successful_publication_and_startup_do_not_wait_for_ingestion_lock(publishing_server):
    workers, _, _, _, directory = publishing_server
    entered, returned = threading.Event(), threading.Event()

    def publish():
        entered.set()
        office._publish({'event': 'session_start', 'session_id': 'not-blocked'})
        returned.set()

    publisher = threading.Thread(target=publish, daemon=True)
    try:
        with office._lock:
            publisher.start()
            assert entered.wait(2)
            completed_while_locked = returned.wait(0.5)
    finally:
        publisher.join(3)
    assert not publisher.is_alive()
    assert len(list((directory / 'inbox').glob('*.json'))) == 1
    assert completed_while_locked, 'listener coordination must not wait behind state ingestion'
    assert len(workers) == 1


def test_completed_listener_attempt_respects_existing_server_then_retries_when_gone(publishing_server):
    workers, release, bound, probes, directory = publishing_server
    office._publish({'event': 'session_start', 'session_id': 'first'})
    release.set()
    workers[0].join(3)
    assert not workers[0].is_alive()
    bound[0] = True
    for _ in range(3):
        office._publish({'event': 'tool_start'})
    assert len(workers) == 1, 'an existing listener keeps this process feed-only'
    assert len(probes) == 3
    bound[0] = False
    office._publish({'event': 'tool_end'})
    assert len(workers) == 2, 'a dead attempt must not permanently prevent replacement'
    assert len(list((directory / 'inbox').glob('*.json'))) == 5


@pytest.mark.skipif(not hasattr(office.os, 'fork'), reason='POSIX fork lifecycle')
def test_forked_publisher_does_not_inherit_listener_ownership_or_locked_startup(publishing_server, monkeypatch):
    import os
    import select
    import signal
    import warnings

    workers, _, _, _, directory = publishing_server
    office._publish({'event': 'session_start', 'session_id': 'parent'})
    parent_worker = workers[0]
    monkeypatch.setattr(office, '_serve', lambda: None)
    reader, writer = os.pipe()
    response = b''
    try:
        # Fork deliberately while another thread's coordination state would
        # otherwise be inherited forever. No child runs an HTTP server here.
        with office._server_start_lock:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)
                child = os.fork()
            if child == 0:
                try:
                    os.close(reader)
                    office._publish({'event': 'session_start', 'session_id': 'child'})
                    os.write(writer, b'1' if office._server_thread is not parent_worker else b'0')
                finally:
                    os._exit(0)
            os.close(writer)
            writer = None
            try:
                readable, _, _ = select.select([reader], [], [], 2)
                if readable:
                    response = os.read(reader, 1)
            finally:
                if not response:
                    os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
        assert response == b'1', 'child publication must not wait on a parent-owned startup lock'
        assert office._server_thread is parent_worker and parent_worker.is_alive()
        assert len(list((directory / 'inbox').glob('*.json'))) == 2
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)
