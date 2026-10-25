"""stdio MCP tools for the existing application; no business modules are imported."""
from __future__ import annotations

import sys
import argparse

from mcp.server.fastmcp import FastMCP
from harness_http import BackendClient, BackendError, validate_standard_id, validate_mode, standard_mode

mcp = FastMCP("data-standard-agent", instructions=(
    "Use inspect_project first. Only live computation is accepted. Operate on data selected in the existing web UI. "
    "Outputs are draft artifacts: human decision enforcement and upstream artifact association remain deferred. "
    "Never claim human approval. Do not ingest generated drafts into the standard library. "
    "Always use the exact newly returned standard_id for evaluation and export. "
    "Do not repeat a timed-out generation call without checking the application."
))
backend = BackendClient()


@mcp.tool()
async def parse_documents(mode: str = "B") -> dict:
    """Parse currently selected A documents or B requirement tables. B is the backwards-compatible default."""
    return await backend.stage("/api/parse", {"mode": validate_mode(mode)}, terminal="parse_complete")


@mcp.tool()
async def extract_knowledge(mode: str = "B") -> dict:
    """Extract evidence-backed candidates for A or B using existing business code and configured LLM."""
    return await backend.stage("/api/extract", {"mode": validate_mode(mode)}, terminal="extract_complete")


@mcp.tool()
async def analyze_discovery() -> dict:
    """Mode A: analyze novelty, coverage and priorities; return candidate IDs for explicit draft selection."""
    return await backend.stage("/api/landscape/build", {"force": False}, terminal="landscape_complete")


@mcp.tool()
async def prioritize_discovery(candidate_ids: list[str] | None = None) -> dict:
    """Mode A: rank discovered candidates with existing priority rationale; does not approve anything."""
    result = await backend.stage("/api/landscape/prioritize", {"candidate_ids": candidate_ids})
    return {"count": result.get("count"), "priorities": result.get("priorities", [])[:24]}


@mcp.tool()
async def inspect_project() -> dict:
    """Check backend health, live/replay mode, data slots and configured model without exposing credentials."""
    status = await backend.request("/api/integration/status", method="GET")
    slots = await backend.request("/api/data/upload/status", method="GET")
    return {"status": status, "data_slots": slots,
            "limitations": ["human_decisions_not_yet_enforced", "upstream_artifact_association_not_yet_fixed"]}


@mcp.tool()
async def analyze_requirements() -> dict:
    """Parse the currently selected mode-B requirement files and analyze completeness against D4."""
    parsed = await backend.stage("/api/requirement/parse", {}, terminal="req_complete")
    gaps = await backend.stage("/api/requirement/gap", {}, terminal="gap_complete")
    return {"parsed": parsed, "gaps": gaps}


@mcp.tool()
async def build_knowledge_base(mode: str = "B") -> dict:
    """Build A/B graph from D0 and selected input. Does not run full AI relationship inference. Defaults to B."""
    return await backend.stage("/api/graph/build", {"mode": validate_mode(mode), "with_candidates": True}, terminal="graph_complete")


@mcp.tool()
async def propose_clarifications() -> dict:
    """Generate mode-B clarification suggestions, including real LLM metadata. Does not submit decisions."""
    result = await backend.stage("/api/requirement/clarify", {"decisions": {}})
    return {"questions": result.get("questions", [])[:12], "inferences": result.get("inferences", [])[:12],
            "low_confidence_count": result.get("low_confidence_count"),
            "llm": result.get("llm"), "note": result.get("note"), "draft_only": True}


@mcp.tool()
async def match_standards() -> dict:
    """Match mode-B requirements to the existing standard library and return retrieval summaries."""
    return await backend.stage("/api/requirement/match", {}, terminal="match_complete")


@mcp.tool()
async def generate_standard(mode: str = "B", candidate_ids: list[str] | None = None) -> dict:
    """Create a DRAFT standard using existing A/B business code. Defaults to the original mode-B flow.

    Uses existing business rules without fixing approval enforcement. Never means approved or production-ready.
    A requires explicit candidate IDs returned by analyze_discovery. Selection is a technical draft operation,
    not human approval, even though the legacy endpoint stores it under approved-candidates.
    """
    validate_mode(mode)
    if mode == "B":
        if candidate_ids is not None:
            raise ValueError("candidate_ids is only used for mode A")
        model = await backend.stage("/api/requirement/synthesize", {"commit": True, "note": "MCP draft generation"}, terminal="syn_complete")
    else:
        if not candidate_ids or len(candidate_ids) > 50:
            raise ValueError("Mode A requires 1..50 explicit candidate_ids from analyze_discovery")
        model = await backend.stage("/api/landscape/approve", {"candidate_ids": candidate_ids,
                                   "note": "MCP technical draft selection only; NOT human approval"})
        if model.get("count") != len(set(candidate_ids)):
            raise BackendError("Some selected candidate IDs were not found; inspect discovery before retrying")
        # Full evidence and candidates remain in FastAPI artifacts. Keep the MCP
        # response small enough that Harness does not truncate the JSON/ID.
        model = {**{k: model[k] for k in ("call_id", "count", "note", "novelty_dist", "type_dist") if k in model},
                 "selected_candidate_ids": list(dict.fromkeys(candidate_ids))}
    standard = await backend.stage("/api/standard/generate", {"source": mode}, terminal="std_complete")
    created = next(e["data"] for e in standard["events"] if e["event"] == "std_created")
    return {"standard_id": validate_standard_id(created["standard_id"]), "model": model,
            "standard": standard, "draft_only": True,
            "limitations": ["human_decisions_not_yet_enforced", "upstream_artifact_association_not_yet_fixed"]}


@mcp.tool()
async def enrich_standard(standard_id: str, limit: int = 12) -> dict:
    """Use the configured business LLM to fill missing definitions in this exact draft. Limit 1..24."""
    validate_standard_id(standard_id)
    if not 1 <= limit <= 24:
        raise ValueError("limit must be between 1 and 24")
    result = await backend.stage("/api/standard/enrich", {"standard_id": standard_id, "limit": limit, "batch": min(limit, 12)})
    if result.get("requested", 0) and not result.get("batches", 0):
        raise BackendError("No LLM enrichment batch completed; inspect backend model configuration")
    return result


@mcp.tool()
async def evaluate_standard(standard_id: str) -> dict:
    """Request evaluation of this A/B ID against selected D3. Check artifact_binding_verified before attributing scores to the draft."""
    mode = standard_mode(standard_id)
    result = await backend.stage("/api/eval/run", {"mode": mode, "standard_id": standard_id, "pred_source": "standard"})
    bound = result.get("standard_id") == standard_id
    return {"requested_standard_id": standard_id, "artifact_binding_verified": bound,
            "evaluation_scope_note": ("Backend reported the requested standard ID" if bound else
                                      "Backend omitted the standard ID in its response; provenance metadata is incomplete, scores are reference only"),
            **{k: result[k] for k in ["call_id", "mode", "group", "evaluable", "unevaluable_reason", "has_gold",
                                    "overall", "metrics", "notes", "standard_id", "pred_counts", "gold_counts"] if k in result}}


@mcp.tool()
async def export_standard(standard_id: str) -> dict:
    """Export this exact A/B draft to XLSX. Infer mode from ID; does not write to the standard library."""
    mode = standard_mode(standard_id)
    return await backend.stage("/api/standard/export", method="GET", params={"mode": mode, "standard_id": standard_id}, download=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-url", help="Explicit URL also works across WSL/Windows environment boundaries")
    args = parser.parse_args()
    backend = BackendClient(args.backend_url)
    # stdout is exclusively the MCP protocol. Application messages go to stderr.
    print("data-standard-agent MCP ready", file=sys.stderr)
    mcp.run(transport="stdio")
