"""Assess only a validation session created by verify_web_harness.py (WSL).

Decoding uses the installed Node runtime. Raw session content never leaves the
machine or appears on stdout; the report contains counts and integration facts.
"""
import argparse
import json
import re
import subprocess
from pathlib import Path

from verify_harness import assess

PROJECT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["A", "B"], required=True)
    parser.add_argument("--installation", type=Path, default=Path("/opt/data/deepseek-harness"))
    args = parser.parse_args()
    runtime = PROJECT / ".runtime"
    session = json.loads((runtime / f"web-{args.mode}-session.json").read_text())
    sid = session["session_id"]
    if not re.fullmatch(r"session-[a-f0-9-]{36}", sid):
        raise ValueError("Invalid validation session ID")
    files = list((args.installation / "home/sessions").glob(f"*/{sid}/session.*jsonl*"))
    if not files:
        raise RuntimeError("Validation session has no persisted events yet")
    source = max(files, key=lambda p: int(re.search(r"\.v(\d+)\.", p.name)[1]) if ".v" in p.name else 0)
    if source.name.endswith(".zstd"):
        node = args.installation / "runtime/current/bin/node"
        code = "const fs=require('fs'),z=require('zlib');process.stdout.write(z.zstdDecompressSync(fs.readFileSync(process.argv[1])));"
        raw = subprocess.check_output([str(node), "-e", code, str(source)])
    else:
        raw = source.read_bytes()
    (runtime / f"web-{args.mode}-events.jsonl").write_bytes(raw)
    rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    events, payloads = [], {}
    for row in rows:
        data = row.get("data", {})
        if row["type"] == "tool/call":
            arguments = data["arguments"]
            events.append({"type": "tool_call", "callId": data["callId"], "tool": data["name"],
                           "input": json.loads(arguments) if isinstance(arguments, str) else arguments})
        elif row["type"] == "tool/result":
            message = data["message"]
            value = "".join(x.get("text", "") for x in message.get("content", []))
            call_id = message["toolCallId"]
            events.append({"type": "tool_result", "callId": call_id,
                           "status": "error" if message.get("isError") else "completed", "result": value})
            name = next((x["tool"] for x in events if x.get("callId") == call_id and x["type"] == "tool_call"), "")
            try:
                payloads.setdefault(name.removeprefix("mcp__standards__"), []).append(json.loads(value))
            except ValueError:
                pass
    assessment = assess(events, args.mode)
    completed = bool(rows and rows[-1]["type"] == "turn/end" and rows[-1]["data"].get("reason", {}).get("kind") == "completed")
    generated = (payloads.get("generate_standard") or [{}])[-1]
    counts = next((e["data"] for e in generated.get("standard", {}).get("events", []) if e["event"] == "std_created"), {})
    enrichments = payloads.get("enrich_standard", [])
    evaluation = (payloads.get("evaluate_standard") or [{}])[-1]
    evaluation.setdefault("artifact_binding_verified", evaluation.get("standard_id") == assessment["standard_id"] and bool(assessment["standard_id"]))
    extraction = (payloads.get("extract_knowledge") or [{}])[-1]
    candidate_count = next((e["data"].get("candidate_count") for e in extraction.get("events", []) if e["event"] == "extract_complete"), None)
    report = {"status": "RUNNING" if not completed else
              ("PASS" if not assessment["missing_tools"] and not assessment["validation_errors"] else "FAIL"),
              "mode": args.mode, "session_id": sid, "normal_web_instance": True,
              "synthetic_data_only": args.mode == "A", "turn_completed": completed, **assessment,
              "generated_counts": {k: counts[k] for k in ("class_count", "attribute_count") if k in counts},
              "candidate_count": candidate_count,
              "definitions_filled_total": sum(x.get("filled_definition", 0) for x in enrichments),
              "business_llm_batches_total": sum(x.get("batches", 0) for x in enrichments),
              "remaining_gaps": (enrichments[-1] if enrichments else {}).get("usability", {}).get("gaps", {}),
              "evaluation": {k: evaluation[k] for k in ("evaluable", "has_gold", "standard_id", "requested_standard_id",
                             "artifact_binding_verified", "evaluation_scope_note", "metrics", "pred_counts") if k in evaluation},
              "draft_only": True, "deferred": ["human_decisions", "upstream_artifact_association"]}
    (runtime / f"web-{args.mode}-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
