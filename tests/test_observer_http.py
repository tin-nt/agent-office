"""Real HTTP regressions for the observer's browser boundary and startup burst."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]


class Observer:
    def __init__(self, directory, *, origins=(), environment_origins=None):
        self.directory = directory
        data = directory / "pixel-office"
        data.mkdir(parents=True)
        (data / "events.jsonl").write_text(json.dumps({
            "event": "session_start", "session_id": "synthetic-observer",
            "platform": "codex", "ts": time.time(),
        }) + "\n")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        environment = {**os.environ, "HERMES_HOME": str(directory)}
        environment.pop("AGENT_OFFICE_DEMO", None)
        environment.pop("AGENT_OFFICE_OBSERVER_ORIGINS", None)
        if environment_origins is not None:
            environment["AGENT_OFFICE_OBSERVER_ORIGINS"] = environment_origins
        command = [sys.executable, str(ROOT / "run.py"), "--port", str(self.port)]
        for origin in origins:
            command.extend(["--observer-origin", origin])
        self.process = subprocess.Popen(command, env=environment,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    if self.request("GET", "/state")[0] == 200:
                        return
                except OSError:
                    if self.process.poll() is not None:
                        raise AssertionError("observer exited: " + self.process.stderr.read().decode())
                    time.sleep(0.02)
            raise AssertionError("observer did not start")
        except BaseException:
            self.close()
            raise

    def request(self, method, route, *, headers=(), payload=None):
        headers = list(headers.items()) if isinstance(headers, dict) else list(headers)
        if not any(name.lower() == "host" for name, _ in headers):
            headers.append(("Host", f"127.0.0.1:{self.port}"))
        body = None if payload is None else json.dumps(payload).encode()
        if body is not None:
            headers.extend([("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.putrequest(method, route, skip_host=True)
            for name, value in headers:
                connection.putheader(name, value)
            connection.endheaders(body)
            response = connection.getresponse()
            data = response.read()
            if response.getheader("Content-Type", "").startswith("application/json"):
                data = json.loads(data)
            return response.status, data, dict(response.getheaders())
        finally:
            connection.close()

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.process.stderr.close()


@pytest.fixture
def observer(tmp_path):
    server = Observer(tmp_path)
    try:
        yield server
    finally:
        server.close()


def test_large_frontend_asset_is_delivered_in_full(observer):
    """A single sendall() over 64 KiB can stall Python 3.15 on Windows."""
    path = ROOT / "web" / "js" / "office.js"
    expected = path.read_bytes()
    assert len(expected) > 65536
    status, body, headers = observer.request("GET", "/js/office.js")
    assert status == 200
    assert headers["Content-Length"] == str(len(expected))
    assert body == expected


@pytest.mark.parametrize("route", ["/", "/js/scene.js", "/state", "/history", "/settings", "/assets-manifest", "/user/missing.svg"])
def test_rebound_hostname_cannot_read_observer_data(observer, route):
    host = f"audit.invalid:{observer.port}"
    status, body, headers = observer.request("GET", route, headers={
        "Host": host, "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin",
    })
    assert status == 403 and body["error"]
    assert "Access-Control-Allow-Origin" not in headers


@pytest.mark.parametrize("method,route", [("POST", "/settings"), ("DELETE", "/history"), ("DELETE", "/state")])
def test_rebound_hostname_cannot_change_observer_data(observer, method, route):
    _, before, revision = observer.request("GET", "/settings")
    host = f"audit.invalid:{observer.port}"
    status, body, _ = observer.request(method, route, headers={
        "Host": host, "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin",
        "If-Match": revision["ETag"],
    }, payload={"room_name": "Untrusted edit"} if method == "POST" else None)
    assert status == 403 and body["error"]
    assert observer.request("GET", "/settings")[1] == before
    assert len(observer.request("GET", "/history")[1]["events"]) == 1


@pytest.mark.parametrize("headers", [
    [("Host", "localhost:1")],
    [("Host", "127.0.0.1")],
    [("Host", "audit.invalid")],
    [("Origin", "null")],
    [("Origin", "https://attacker.example")],
    [("Origin", "http://127.0.0.1:1")],
    [("Origin", "http://localhost:1")],
    [("Origin", "null"), ("Origin", "null")],
    [("Sec-Fetch-Site", "cross-site")],
    [("Sec-Fetch-Site", "same-site")],
    [("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin")],
    [("Sec-Fetch-Site", "same-origin, cross-site")],
    [("Sec-Fetch-Site", "unexpected")],
], ids=["wrong-port", "missing-port", "foreign-host", "null-origin", "foreign-origin",
        "origin-wrong-port", "localhost-wrong-port", "duplicate-origin", "cross-site", "same-site",
        "duplicate-fetch-site", "combined-fetch-site", "unknown-fetch-site"])
def test_private_reads_and_mutations_reject_untrusted_browser_headers(observer, headers):
    _, _, revision = observer.request("GET", "/settings")
    for method, route, payload in [("GET", "/state", None), ("GET", "/history", None),
                                   ("GET", "/settings", None), ("DELETE", "/state", None),
                                   ("DELETE", "/history", None), ("POST", "/settings", {"sound": True})]:
        status, body, _ = observer.request(method, route,
            headers=[*headers, ("If-Match", revision["ETag"])], payload=payload)
        assert status == 403 and body["error"], (method, route, status)


def test_duplicate_or_missing_host_is_rejected(observer):
    host = f"127.0.0.1:{observer.port}"
    for values in [[("Host", host), ("Host", host)], [("Host", "")]]:
        status, body, _ = observer.request("GET", "/state", headers=values)
        assert status == 403 and body["error"]
    with socket.create_connection(("127.0.0.1", observer.port), timeout=3) as client:
        client.sendall(b"GET /state HTTP/1.0\r\n\r\n")
        response = http.client.HTTPResponse(client)
        response.begin()
        assert response.status == 403 and json.loads(response.read())["error"]


def test_even_trusted_loopback_aliases_must_match_the_browser_origin(observer):
    assert observer.request("GET", "/state", headers={
        "Host": f"127.0.0.1:{observer.port}", "Origin": f"http://localhost:{observer.port}",
        "Sec-Fetch-Site": "same-origin",
    })[0] == 403


@pytest.mark.parametrize("hostname", ["127.0.0.1", "localhost", "[::1]"])
def test_exact_local_origin_preserves_reads_conditional_writes_and_reset(observer, hostname):
    host = f"{hostname}:{observer.port}"
    headers = {"Host": host, "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"}
    assert observer.request("GET", "/state", headers=headers)[0] == 200
    status, _, revision = observer.request("GET", "/settings", headers=headers)
    assert status == 200
    status, saved, _ = observer.request("POST", "/settings", headers={**headers, "If-Match": revision["ETag"]},
                                      payload={"room_name": "Trusted edit"})
    assert status == 200 and saved["room_name"] == "Trusted edit"
    assert observer.request("DELETE", "/history", headers=headers)[0] == 200
    assert observer.request("DELETE", "/state", headers=headers)[0] == 200


def test_explicit_forwarded_origin_supports_preserved_and_rewritten_host_without_enabling_runner(tmp_path):
    origin = "https://office.forward.example:9443"
    server = Observer(tmp_path, origins=[origin])
    try:
        for host in ["office.forward.example:9443", f"127.0.0.1:{server.port}"]:
            headers = {"Host": host, "Origin": origin, "Sec-Fetch-Site": "same-origin"}
            status, _, revision = server.request("GET", "/settings", headers=headers)
            assert status == 200
            status, saved, _ = server.request("POST", "/settings", headers={**headers, "If-Match": revision["ETag"]},
                                             payload={"room_name": "Forwarded view"})
            assert status == 200 and saved["room_name"] == "Forwarded view"
            assert server.request("GET", "/task-runs", headers=headers)[0] == 403
        for headers in [
            {"Host": "office.forward.example:9444", "Origin": origin},
            {"Host": "office.forward.example:9443", "Origin": "https://office.forward.example:9444"},
            {"Host": f"127.0.0.1:{server.port}", "Origin": "https://attacker.example", "X-Forwarded-Host": "office.forward.example:9443"},
        ]:
            assert server.request("GET", "/state", headers=headers)[0] == 403
    finally:
        server.close()


def test_environment_origins_and_startup_option_are_combined_with_browser_default_port_serialization(tmp_path):
    server = Observer(tmp_path, origins=["https://other.forward.example:443"],
                      environment_origins="https://office.forward.example,http://office.other.example:8080")
    try:
        for origin in ["https://other.forward.example", "https://office.forward.example", "http://office.other.example:8080"]:
            assert server.request("GET", "/state", headers={"Host": origin.split("://")[1], "Origin": origin})[0] == 200
    finally:
        server.close()


@pytest.mark.parametrize("origin", ["*", "https://*.example", "null", "file:///office", "https://office.example/",
    "https://office.example?", "https://office.example#", "https://user:private@example.test", "https://office.example:0",
    "https://office.example:65536", "https://office.example:invalid", " https://office.example"])
def test_invalid_forwarding_configuration_fails_closed_without_echoing_private_values(tmp_path, origin):
    environment = {**os.environ, "HERMES_HOME": str(tmp_path), "AGENT_OFFICE_OBSERVER_ORIGINS": origin}
    result = subprocess.run([sys.executable, str(ROOT / "run.py"), "--port", "8113"], cwd=ROOT,
                            env=environment, capture_output=True, timeout=5)
    assert result.returncode == 1
    assert b"observer origins must be explicit HTTP(S) origins" in result.stderr
    assert b"private" not in result.stderr and b"Traceback" not in result.stderr


def test_startup_accepts_a_burst_of_frontend_connections_before_serving(tmp_path):
    """A cold browser can queue many assets while the server thread is descheduled."""
    ready, release = tmp_path / "ready", tmp_path / "release"
    source = """
import os, pathlib, time
import __init__ as office
os.environ['AGENT_OFFICE_PORT'] = '0'
def ready(port):
    pathlib.Path(os.environ['READY']).write_text(str(port))
    while not pathlib.Path(os.environ['RELEASE']).exists():
        time.sleep(.01)
office._serve(on_ready=ready)
"""
    environment = {**os.environ, "HERMES_HOME": str(tmp_path / "home"),
                   "READY": str(ready), "RELEASE": str(release)}
    environment.pop("AGENT_OFFICE_OBSERVER_ORIGINS", None)
    process = subprocess.Popen([sys.executable, "-c", source], cwd=ROOT, env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    clients = []
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(.02)
        assert ready.exists(), "observer never bound its listening socket"
        port = int(ready.read_text())
        for _ in range(32):
            try:
                client = socket.create_connection(("127.0.0.1", port), timeout=.25)
                clients.append(client)
                client.sendall(f"GET /js/scene.js HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
            except OSError as exc:
                raise AssertionError("a queued frontend startup connection was dropped") from exc
        release.touch()
        for client in clients:
            client.settimeout(5)
            response = http.client.HTTPResponse(client)
            response.begin()
            assert response.status == 200
            assert b"OfficeScene" in response.read()
    finally:
        release.touch()
        for client in clients:
            client.close()
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
