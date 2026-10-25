import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import harness_mcp as tools


class FakeBackend:
    def __init__(self):
        self.calls = []

    async def stage(self, path, body=None, **kwargs):
        self.calls.append((path, body, kwargs))
        if path == "/api/landscape/approve":
            return {"count": len(set(body["candidate_ids"]))}
        if path == "/api/standard/generate":
            return {"events": [{"event": "std_created", "data": {"standard_id": f"STD-{body['source']}-123456"}}]}
        return {}


def test_original_zero_argument_generation_stays_mode_b(monkeypatch):
    backend = FakeBackend(); monkeypatch.setattr(tools, "backend", backend)
    result = asyncio.run(tools.generate_standard())
    assert result["standard_id"] == "STD-B-123456"
    assert backend.calls[0][0] == "/api/requirement/synthesize"
    assert backend.calls[1][1] == {"source": "B"}


def test_mode_a_requires_explicit_selection_before_any_write(monkeypatch):
    backend = FakeBackend(); monkeypatch.setattr(tools, "backend", backend)
    with pytest.raises(ValueError, match="explicit"):
        asyncio.run(tools.generate_standard(mode="A"))
    assert not backend.calls


def test_mode_a_selection_is_marked_technical_draft(monkeypatch):
    backend = FakeBackend(); monkeypatch.setattr(tools, "backend", backend)
    result = asyncio.run(tools.generate_standard(mode="A", candidate_ids=["a-001"]))
    assert result["standard_id"] == "STD-A-123456" and result["draft_only"]
    assert "NOT human approval" in backend.calls[0][1]["note"]


@pytest.mark.parametrize("mode", ["A", "B"])
def test_evaluation_and_export_follow_exact_standard_mode(monkeypatch, mode):
    backend = FakeBackend(); monkeypatch.setattr(tools, "backend", backend)
    sid = f"STD-{mode}-123456"
    asyncio.run(tools.evaluate_standard(sid)); asyncio.run(tools.export_standard(sid))
    assert backend.calls[0][1]["mode"] == mode
    assert backend.calls[0][1]["standard_id"] == sid
    assert backend.calls[1][2]["params"] == {"mode": mode, "standard_id": sid}


def test_shared_tools_default_to_b(monkeypatch):
    backend = FakeBackend(); monkeypatch.setattr(tools, "backend", backend)
    asyncio.run(tools.parse_documents()); asyncio.run(tools.extract_knowledge()); asyncio.run(tools.build_knowledge_base())
    assert all(body["mode"] == "B" for _, body, _ in backend.calls)


def test_unbound_evaluation_never_claims_draft_accuracy(monkeypatch):
    monkeypatch.setattr(tools, "backend", FakeBackend())
    result = asyncio.run(tools.evaluate_standard("STD-B-123456"))
    assert result["artifact_binding_verified"] is False
    assert "provenance metadata is incomplete" in result["evaluation_scope_note"]
