"""Event subscription / webhook layer — all offline over a temp DB.

Covers the acceptance criteria from the design/request:
  * running->done publishes a harness.task.status_changed outbox event
  * subscription filters (executor/requester/new_status/terminal)
  * fake HTTP receiver gets the webhook, 2xx marks delivered
  * HMAC signature header verifies with the configured secret
  * 500/timeout retry with backoff, exhausted attempts -> dead_letter
  * permanent 4xx -> failed (but 429 retries)
  * a webhook/publish failure never fails the status-update API
  * the read/test REST surface (/v1/events, /v1/webhook-subscriptions, /test)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "gateway"))

import random  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from ai_common_repo.core import constants as C  # noqa: E402
from ai_common_repo.core.config import (  # noqa: E402
    NotificationsConfig, WebhookSubscriptionConfig, load_config,
)
from ai_common_repo.core.ids import shift_iso  # noqa: E402
from ai_common_repo.core.models import (  # noqa: E402
    HarnessClaimRequest, HarnessStatusUpdate, HarnessTaskCreate,
)
from ai_common_repo.events.dispatcher import DeliveryResult, WebhookDispatcher  # noqa: E402
from ai_common_repo.events.signing import sign, verify  # noqa: E402
from ai_common_repo.server.app import create_app  # noqa: E402
from ai_common_repo.service import MemoryService  # noqa: E402


# -- fixtures / helpers -------------------------------------------------------
def _service(subscriptions=None, **notif) -> MemoryService:
    tmp = tempfile.mkdtemp()
    os.environ["AICR_STORAGE__DB_PATH"] = os.path.join(tmp, "memory.db")
    os.environ["AICR_MODELS__DEFAULT_BACKEND"] = "none"  # offline hash embedder
    cfg = load_config()
    notif.setdefault("enabled", True)
    notif.setdefault("jitter_ratio", 0.0)  # deterministic backoff in tests
    cfg = cfg.model_copy(update={"notifications": NotificationsConfig(
        subscriptions=subscriptions or [], **notif)})
    return MemoryService(cfg)


def _sub(**kw) -> WebhookSubscriptionConfig:
    defaults = dict(id="s1", name="test", url="http://127.0.0.1:1/hook",
                    event_types=[C.EVENT_HARNESS_TASK_STATUS_CHANGED])
    defaults.update(kw)
    return WebhookSubscriptionConfig(**defaults)


def _run_to_done(svc, **task_kw) -> str:
    """Drive a task pending->claimed->running->done and return its id."""
    defaults = dict(requester="hermes", executor="claudecode", prompt="do it")
    defaults.update(task_kw)
    task_id = svc.harness.create(HarnessTaskCreate(**defaults)).id
    svc.harness.claim(HarnessClaimRequest(executor=defaults["executor"],
                                          worker_id="w1"))
    svc.harness.update_status(task_id, HarnessStatusUpdate(
        status="running", worker_id="w1"))
    svc.harness.update_status(task_id, HarnessStatusUpdate(
        status="done", worker_id="w1",
        result={"summary": "all green", "exit_code": 0,
                "response_md": "/tmp/response.md", "log": "/tmp/x.log",
                "tests": {"exit_code": 0, "tail": "38 passed"}}))
    return task_id


class _Receiver:
    """A throwaway localhost HTTP server that records requests and replies with a
    configurable status. Used to prove real (httpx) delivery + signing."""

    def __init__(self, status=200):
        self.status = status
        self.requests: list[dict] = []
        handler = self._make_handler()
        self.httpd = HTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                outer.requests.append({
                    "headers": {k: v for k, v in self.headers.items()},
                    "body": body,
                })
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *args):  # silence
                pass

        return Handler

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _FakeTransport:
    """Deterministic transport: returns queued responses or raises to simulate a
    network timeout. Records calls."""

    def __init__(self, *responses):
        # each item: int status, or an Exception instance to raise
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": body})
        item = self._responses.pop(0) if self._responses else 200
        if isinstance(item, Exception):
            raise item
        return DeliveryResult(status_code=item)


# -- 1. publication -----------------------------------------------------------
def test_running_to_done_creates_status_changed_outbox_event():
    svc = _service(subscriptions=[_sub(filters={"new_status": ["done", "failed"]})])
    task_id = _run_to_done(svc)

    rows = svc.event_outbox.list(aggregate_id=task_id)
    assert len(rows) == 1
    ev = rows[0]
    assert ev.event_type == C.EVENT_HARNESS_TASK_STATUS_CHANGED
    assert ev.status == C.OUTBOX_STATUS_PENDING
    p = ev.payload["payload"]
    assert p["task_id"] == task_id
    assert p["old_status"] == "running" and p["new_status"] == "done"
    assert p["terminal"] is True
    assert p["executor"] == "claudecode" and p["requester"] == "hermes"
    assert p["result_summary"] == "all green"
    assert p["response_md"] == "/tmp/response.md"
    assert p["tests"]["exit_code"] == 0
    # the envelope carries id/type/aggregate
    assert ev.payload["aggregate"] == {"type": "harness_task", "id": task_id}


def test_non_matching_status_is_filtered_out():
    # filter only wants failed/cancelled — a running or done event must not enqueue
    svc = _service(subscriptions=[_sub(filters={"new_status": ["failed", "cancelled"]})])
    task_id = _run_to_done(svc)
    assert svc.event_outbox.list(aggregate_id=task_id) == []


# -- 2. filters ---------------------------------------------------------------
def test_filters_match_executor_requester_and_status():
    subs = [
        _sub(id="match-exec", filters={"executor": ["claudecode"]}),
        _sub(id="miss-exec", filters={"executor": ["openclaw"]}),
        _sub(id="match-req", filters={"requester": ["hermes"]}),
        _sub(id="match-terminal", filters={"terminal": True}),
        _sub(id="miss-status", filters={"new_status": ["failed"]}),
    ]
    svc = _service(subscriptions=subs)
    task_id = _run_to_done(svc)
    got = {r.subscription_id for r in svc.event_outbox.list(aggregate_id=task_id)}
    assert got == {"match-exec", "match-req", "match-terminal"}


# -- 3./4./7. real delivery + 2xx delivered + HMAC ----------------------------
def test_fake_receiver_delivers_and_signature_verifies():
    os.environ["TEST_WEBHOOK_SECRET"] = "topsecret-value"
    rcv = _Receiver(status=200)
    try:
        svc = _service(subscriptions=[_sub(
            url=rcv.url, secret_env="TEST_WEBHOOK_SECRET",
            filters={"new_status": ["done"]})])
        _run_to_done(svc)
        attempted = svc.event_dispatcher.run_pending()
        assert attempted == 1
        assert len(rcv.requests) == 1

        req = rcv.requests[0]
        body = req["body"]
        headers = req["headers"]
        payload = json.loads(body)
        assert payload["event_type"] == C.EVENT_HARNESS_TASK_STATUS_CHANGED
        assert payload["payload"]["task_id"]
        # signature header verifies with the configured secret over ts.body
        ts = headers[C.HDR_TIMESTAMP]
        assert verify("topsecret-value", ts, body, headers[C.HDR_SIGNATURE])
        assert headers[C.HDR_SIGNATURE] == sign("topsecret-value", ts, body)
        assert headers[C.HDR_EVENT_TYPE] == C.EVENT_HARNESS_TASK_STATUS_CHANGED
        assert headers[C.HDR_DELIVERY_ID].startswith("dlv_")

        rows = svc.event_outbox.list()
        assert len(rows) == 1 and rows[0].status == C.OUTBOX_STATUS_DELIVERED
        assert rows[0].http_status == 200
    finally:
        rcv.close()
        os.environ.pop("TEST_WEBHOOK_SECRET", None)


# -- 5./6. retry + dead-letter ------------------------------------------------
def _dispatcher(svc, sub, transport, **cfg_kw):
    cfg = svc.cfg.notifications.model_copy(update=cfg_kw)
    return WebhookDispatcher(svc.event_outbox, cfg, subscriptions=[sub],
                             transport=transport, secret_provider=lambda k: "s",
                             rng=random.Random(0))


def test_500_then_timeout_retry_keeps_pending_with_backoff():
    sub = _sub(url="http://127.0.0.1:9/hook", max_attempts=8,
               filters={"new_status": ["done"]})
    svc = _service(subscriptions=[sub])
    _run_to_done(svc)
    transport = _FakeTransport(500, TimeoutError("timed out"))
    disp = _dispatcher(svc, sub, transport)

    disp.run_pending(now="2999-01-01T00:00:00Z")           # 500 -> retry
    row = svc.event_outbox.list()[0]
    assert row.status == C.OUTBOX_STATUS_PENDING
    assert row.attempts == 1 and row.http_status == 500
    assert row.next_attempt_at > "2999-01-01T00:00:00Z"     # backed off

    disp.run_pending(now="2999-01-01T01:00:00Z")           # timeout -> retry
    row = svc.event_outbox.list()[0]
    assert row.status == C.OUTBOX_STATUS_PENDING
    assert row.attempts == 2 and "transport error" in row.last_error


def test_exhausted_attempts_go_to_dead_letter():
    sub = _sub(url="http://127.0.0.1:9/hook", max_attempts=3,
               filters={"new_status": ["done"]})
    svc = _service(subscriptions=[sub])
    _run_to_done(svc)
    transport = _FakeTransport(500, 500, 500)  # always fails
    disp = _dispatcher(svc, sub, transport)

    now = "2999-01-01T00:00:00Z"
    for _ in range(3):
        disp.run_pending(now=now)
        now = shift_iso(now, 3600)  # jump past the backoff each round
    row = svc.event_outbox.list()[0]
    assert row.status == C.OUTBOX_STATUS_DEAD_LETTER
    assert row.attempts == 3
    assert len(transport.calls) == 3


def test_permanent_4xx_fails_but_429_retries():
    sub = _sub(url="http://127.0.0.1:9/hook", max_attempts=5,
               filters={"new_status": ["done"]})
    # 404 permanent
    svc = _service(subscriptions=[sub])
    _run_to_done(svc)
    _dispatcher(svc, sub, _FakeTransport(404)).run_pending(now="2999-01-01T00:00:00Z")
    row = svc.event_outbox.list()[0]
    assert row.status == C.OUTBOX_STATUS_FAILED and row.http_status == 404

    # 429 is retryable (destination asking to slow down)
    svc2 = _service(subscriptions=[sub])
    _run_to_done(svc2)
    _dispatcher(svc2, sub, _FakeTransport(429)).run_pending(now="2999-01-01T00:00:00Z")
    row2 = svc2.event_outbox.list()[0]
    assert row2.status == C.OUTBOX_STATUS_PENDING and row2.attempts == 1


# -- 8. webhook/publish failure must not fail the status update ---------------
def test_publish_failure_does_not_fail_status_update():
    svc = _service(subscriptions=[_sub(filters={"new_status": ["done"]})])

    def boom(*a, **k):
        raise RuntimeError("outbox exploded")

    svc.harness._events.publish_status_changed = boom  # type: ignore[attr-defined]
    task_id = _run_to_done(svc)  # would raise if the failure propagated
    assert svc.harness.get(task_id).status == "done"
    # and the failure was audited, not lost
    rows = svc._db.conn.execute(
        "SELECT action FROM audit_log WHERE action='event_enqueue_failed'").fetchall()
    assert rows


def test_signing_roundtrip_and_tamper_detection():
    body = b'{"hello":"world"}'
    sig = sign("secret", "2026-07-06T00:00:00Z", body)
    assert sig.startswith("v1=")
    assert verify("secret", "2026-07-06T00:00:00Z", body, sig)
    assert not verify("secret", "2026-07-06T00:00:00Z", body + b"x", sig)
    assert not verify("wrong", "2026-07-06T00:00:00Z", body, sig)


# -- REST surface (read + test endpoints) -------------------------------------
def test_rest_events_and_subscription_endpoints():
    os.environ["TEST_WEBHOOK_SECRET"] = "s3cr3t"
    rcv = _Receiver(status=200)
    try:
        svc = _service(subscriptions=[_sub(
            id="hermes-term", url=rcv.url, secret_env="TEST_WEBHOOK_SECRET",
            filters={"new_status": ["done", "failed", "cancelled"]})])
        app = create_app(svc.cfg)
        # Reuse the same service instance so the temp DB is shared. Drive the
        # client WITHOUT the lifespan context so no background delivery thread
        # races the foreground run_pending() over the single sqlite connection.
        app.state.service = svc
        client = TestClient(app)
        _run_to_done(svc)
        svc.event_dispatcher.run_pending()

        r = client.get("/v1/events")
        assert r.status_code == 200
        body = r.json()
        assert body["counts"]["delivered"] == 1
        assert body["events"][0]["event_type"] == C.EVENT_HARNESS_TASK_STATUS_CHANGED
        event_id = body["events"][0]["event_id"]

        r = client.get(f"/v1/events/{event_id}")
        assert r.status_code == 200
        assert r.json()["deliveries"][0]["subscription_id"] == "hermes-term"
        assert client.get("/v1/events/evt_missing").status_code == 404

        # subscriptions are redacted: env var NAME only, never a secret value
        r = client.get("/v1/webhook-subscriptions")
        subs = r.json()["subscriptions"]
        assert subs[0]["id"] == "hermes-term"
        assert subs[0]["secret_env"] == "TEST_WEBHOOK_SECRET"
        assert subs[0]["signed"] is True
        assert "s3cr3t" not in r.text

        # /test delivers a synthetic webhook.test event to the receiver
        before = len(rcv.requests)
        r = client.post("/v1/webhook-subscriptions/hermes-term/test")
        assert r.status_code == 200
        assert r.json()["delivery"]["status"] == C.OUTBOX_STATUS_DELIVERED
        assert len(rcv.requests) == before + 1
        assert client.post(
            "/v1/webhook-subscriptions/nope/test").status_code == 404
    finally:
        rcv.close()
        os.environ.pop("TEST_WEBHOOK_SECRET", None)


if __name__ == "__main__":
    test_running_to_done_creates_status_changed_outbox_event()
    print("EVENTS WEBHOOK OK")
