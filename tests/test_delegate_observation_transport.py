"""The public supervision beat never becomes a five-second HTTP deadline."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from ouroboros import delegate_custody, delegate_supervision
from ouroboros.gateways import claudexor as gateway_module
from ouroboros.tools import delegate
from tests._delegated_transport_shared import _delegating_ctx


@pytest.mark.serial
@pytest.mark.parametrize("initially_queued", [False, True])
def test_public_wait_reads_response_slower_than_five_seconds(tmp_path, monkeypatch, initially_queued):
    requests = []
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def answer(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            requests.append((self.command, self.path))
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            assert self.path == "/v2/handshake"
            self.answer({"compatible": True, "protocolMajor": 3,
                         "engine": {"version": "3.10.2", "sha": "fixture"}})

        def do_GET(self):
            requests.append((self.command, self.path))
            assert self.path == "/v2/runs/run-slow"
            # Deliberate network-latency reproduction: the previous five-second
            # HTTP timeout fails before this real socket sends its headers.
            first_read = requests.count(("GET", "/v2/runs/run-slow")) == 1
            if first_read:
                threading.Event().wait(5.2)
            if initially_queued and first_read:
                self.answer({"lastSeq": 0, "summary": {"state": "queued", "runDir": str(run_dir)}})
                return
            self.answer({"lastSeq": 1, "summary": {
                "state": "succeeded", "effectiveAccess": "readonly", "runDir": str(run_dir),
            }, "primaryOutput": {"kind": "answer", "text": "complete result", "truncated": False}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway_type = gateway_module.ClaudexorGateway
    ctx = _delegating_ctx(tmp_path, acting=False)
    entry = delegate._RunCustody(task_id=ctx.task_id, route_id="fixture", model="fixture",
                                project_id="fixture", project_owned=False, access="readonly")
    monkeypatch.setitem(delegate_custody._CUSTODY, "run-slow", entry)
    monkeypatch.setattr(gateway_module, "ClaudexorGateway", lambda: gateway_type(
        gateway_module.DaemonEndpoint("127.0.0.1", server.server_port, "fixture-token")))
    try:
        started = time.monotonic()
        result = json.loads(delegate._delegate_wait_entry(ctx, "run-slow"))
        assert time.monotonic() - started >= 5.0
        assert result["status"] == "terminal", result
        assert result["state"] == "succeeded"
        assert requests == [("POST", "/v2/handshake"), ("GET", "/v2/runs/run-slow")] * (2 if initially_queued else 1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_typed_observation_timeout_keeps_same_run_inside_supervision(tmp_path, monkeypatch):
    ctx = _delegating_ctx(tmp_path, acting=False)
    calls = []
    sleeps = []
    monkeypatch.setattr(delegate_supervision.time, "sleep", sleeps.append)

    def wait_once(_ctx, run_id, _window, _seq):
        calls.append(run_id)
        if len(calls) == 1:
            return json.dumps({"status": "observation_pending", "run_id": run_id,
                               "reason": "observation_read_timeout", "waited_sec": 5.0})
        return json.dumps({"status": "terminal", "run_id": run_id, "state": "succeeded"})

    result = json.loads(delegate_supervision.supervised_wait(ctx, "run-existing", wait_once=wait_once))
    assert result["status"] == "terminal"
    assert calls == ["run-existing", "run-existing"]
    assert sleeps == [delegate_supervision._TICK_SEC]


@pytest.mark.parametrize("status", [200, 401, 403])
def test_received_auth_refusal_cannot_be_hidden_by_body_read_timeout(status):
    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadTimeout("fixture read timeout")
            yield b""  # pragma: no cover -- makes this an iterator

    gateway = gateway_module.ClaudexorGateway(gateway_module.DaemonEndpoint("127.0.0.1", 1, "fixture"))
    gateway._client.close()
    gateway._client = httpx.Client(base_url="http://127.0.0.1:1", transport=httpx.MockTransport(
        lambda _request: httpx.Response(status, stream=BrokenBody())))
    try:
        with pytest.raises(gateway_module.ClaudexorUnavailable) as caught:
            gateway.get_run("run-existing")
        assert caught.value.status_code == status
        assert caught.value.observation_timeout is (status == 200)
        assert isinstance(caught.value.__cause__, httpx.ReadTimeout)
    finally:
        gateway.close()


def test_existing_control_returns_without_starting_another_observation(tmp_path, monkeypatch):
    ctx = _delegating_ctx(tmp_path, acting=False)
    calls = []
    monkeypatch.setattr(delegate_supervision, "_control_wakes", lambda _ctx: [{"type": "deadline"}])
    result = json.loads(delegate_supervision.supervised_wait(
        ctx, "run-existing", wait_once=lambda *_args: calls.append(1)))
    assert result["wake_events"] == [{"type": "deadline"}]
    assert calls == []
