import sys
import asyncio
from pathlib import Path

import pytest
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from harness_http import BackendClient, BackendError, parse_sse, validate_standard_id


def test_sse_named_events_crlf_multiline_and_unterminated_last_block():
    text = ': keepalive\r\nevent: std_created\r\ndata: {"standard_id":\r\ndata: "STD-B-123456"}\r\n\r\nevent: std_complete\r\ndata: {}'
    events = parse_sse(text)
    assert events == [{"event": "std_created", "data": {"standard_id": "STD-B-123456"}},
                      {"event": "std_complete", "data": {}}]


def test_bad_sse_json_fails_visibly():
    with pytest.raises(BackendError):
        parse_sse('event: std_complete\ndata: {broken}\n\n')


@pytest.mark.parametrize("url", ["https://example.com", "http://192.0.2.1:8123", "http://u:p@localhost", "http://localhost/private"])
def test_backend_is_loopback_only(url):
    with pytest.raises(ValueError):
        BackendClient(url)


@pytest.mark.parametrize("sid", ["../secret", "STD-A-123456", "STD-B-../secret", ""])
def test_only_exact_mode_b_standard_ids_are_accepted(sid):
    with pytest.raises(ValueError):
        validate_standard_id(sid)


def test_standard_id_matches_existing_business_format():
    assert validate_standard_id("STD-B-123456") == "STD-B-123456"


def test_http_200_with_failure_event_is_not_success(tmp_path):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                    text='event: std_failed\ndata: {"error":"missing input"}\n\n'))
    client = BackendClient(audit_dir=tmp_path, transport=transport)
    with pytest.raises(BackendError, match="stage failed"):
        asyncio.run(client.request("/api/standard/generate", {"source": "B"}, terminal="std_complete"))


def test_truncated_stream_does_not_pass(tmp_path):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                    text='event: std_created\ndata: {"standard_id":"STD-B-123456"}\n\n'))
    client = BackendClient(audit_dir=tmp_path, transport=transport)
    with pytest.raises(BackendError, match="missing std_complete"):
        asyncio.run(client.request("/api/standard/generate", terminal="std_complete"))


def test_replay_mode_prevents_any_business_call(tmp_path):
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"live_mode": False, "replay_effective": True})
    client = BackendClient(audit_dir=tmp_path, transport=httpx.MockTransport(handler))
    with pytest.raises(BackendError, match="refuses replay"):
        asyncio.run(client.stage("/api/standard/generate", {"source": "B"}))
    assert paths == ["/api/integration/status"]
