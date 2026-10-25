"""Run with WSL python3 against the existing local Harness installation.

Uses an isolated headless home, an existing Ollama route, and the synthetic
backend. Never modifies the user's Harness home or copies its credentials.
"""
from __future__ import annotations

import argparse
import base64
import http.server
import json
import os
import subprocess
import time
import threading
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parent.parent


def assess(events, mode="B"):
    """Validate tool completion and same-ID propagation, not just tool discovery."""
    calls = [e for e in events if e.get("type") == "tool_call"]
    results = {e.get("callId"): e for e in events if e.get("type") == "tool_result"}
    required = ["inspect_project", "analyze_requirements", "build_knowledge_base", "match_standards",
                "generate_standard", "enrich_standard", "evaluate_standard", "export_standard"]
    if mode == "A":
        required = ["inspect_project", "parse_documents", "extract_knowledge", "build_knowledge_base",
                    "analyze_discovery", "prioritize_discovery", "generate_standard", "enrich_standard",
                    "evaluate_standard", "export_standard"]
    names = [e.get("tool") or e.get("name") for e in calls]
    missing = [name for name in required if "mcp__standards__" + name not in names]
    errors, payloads = [], {}
    for call in calls:
        name = call.get("tool") or call.get("name")
        result = results.get(call.get("callId"), {})
        if result.get("status") != "completed":
            errors.append(f"{name}: tool did not complete")
            continue
        if name and name.startswith("mcp__standards__"):
            value = result.get("result")
            try:
                payloads[name.removeprefix("mcp__standards__")] = json.loads(value) if isinstance(value, str) else value
            except (ValueError, TypeError):
                errors.append(f"{name}: invalid result")
    sid = (payloads.get("generate_standard") or {}).get("standard_id")
    for call in calls:
        if call.get("tool") in ["mcp__standards__" + x for x in ("enrich_standard", "evaluate_standard", "export_standard")]:
            if not sid or call.get("input", {}).get("standard_id") != sid:
                errors.append("Different standard IDs used across generation/evaluation/export")
    enrichment = payloads.get("enrich_standard") or {}
    if not enrichment.get("batches") or enrichment.get("errors"):
        errors.append("Business LLM enrichment did not complete")
    evaluation = payloads.get("evaluate_standard") or {}
    if mode == "B" and not evaluation.get("evaluable"):
        errors.append("Synthetic B draft was not evaluable")
    if mode == "A" and evaluation.get("evaluable"):
        errors.append("Synthetic A has no gold and must not report scored accuracy")
    export_bytes = (payloads.get("export_standard") or {}).get("bytes", 0)
    if not export_bytes:
        errors.append("No exported XLSX")
    status = (payloads.get("inspect_project") or {}).get("status", {})
    if not status.get("live_mode") or status.get("replay_effective"):
        errors.append("Backend was not live")
    return {"tool_names": names, "missing_tools": missing, "validation_errors": errors,
            "standard_id": sid, "export_bytes": export_bytes,
            "business_llm_batches": enrichment.get("batches", 0),
            "definitions_filled": enrichment.get("filled_definition", 0)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--installation", type=Path, default=Path("/opt/data/deepseek-harness"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8125")
    parser.add_argument("--mode", choices=["A", "B"], default="B")
    parser.add_argument("--windows-ollama-relay", action="store_true",
                        help="Temporary loopback relay when the existing WSL Docker bridge is unavailable")
    args = parser.parse_args()
    install = args.installation
    home = PROJECT / ".runtime/harness-home"
    env = dict(os.environ, DSH_HOME=str(home), STANDARD_TEST_MODEL_KEY="local-ollama",
               PATH=f"{install}/runtime/current/bin:{install}/app/node_modules/.bin:/usr/bin:/bin",
               DSH_TELEMETRY_MODE="DISABLED")
    binary = str(install / "app/node_modules/.bin/dsh")
    subprocess.run([binary, "--profile", "headless", "--dump-config"], env=env,
                   stdout=subprocess.DEVNULL, check=True)
    config = yaml.safe_load((install / "home/profiles/web/cordis.patch.yml").read_text())
    route = next(r["config"]["providers"]["ollama"] for r in config if r.get("id") == "llm-pi-ai")
    # Only retain non-secret endpoint/capability information for the existing
    # unauthenticated local Ollama bridge. No managed credentials are copied.
    provider = {k: route[k] for k in ["baseURL", "api", "models", "compat"] if k in route}
    provider["apiKeyEnv"] = "STANDARD_TEST_MODEL_KEY"
    relay = None
    if args.windows_ollama_relay:
        windows_python = str(PROJECT / ".venv-local/Scripts/python.exe")
        windows_script = subprocess.check_output(["wslpath", "-w", str(PROJECT / "scripts/ollama_relay_request.py")], text=True).strip()

        class Relay(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                if self.path != "/v1/chat/completions":
                    self.send_error(404); return
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2 * 1024 * 1024:
                    self.send_error(413); return
                response = subprocess.run([windows_python, windows_script], input=self.rfile.read(size),
                                          capture_output=True, timeout=250)
                if response.returncode:
                    self.send_error(502, "Windows model relay failed"); return
                result = json.loads(response.stdout)
                payload = base64.b64decode(result["body"])
                self.send_response(result["status"])
                self.send_header("Content-Type", result["content_type"])
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        relay = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Relay)
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        provider["baseURL"] = f"http://127.0.0.1:{relay.server_port}/v1"
    overlay = json.loads((PROJECT / ".dsh/mcp.local.yml").read_text())
    overlay[0]["insert"][0]["config"]["env"]["STANDARD_BACKEND_URL"] = args.base_url
    overlay[0]["insert"][0]["config"]["args"] = [overlay[0]["insert"][0]["config"]["args"][0], "--backend-url", args.base_url]
    patch = [
        {"id": "agent-default-model", "config": {"provider": "ollama", "model": "gemma4:31b-cloud"}},
        {"id": "llm-pi-ai", "config": {"providers": {"ollama": provider}}},
        *overlay,
    ]
    (home / "profiles/headless/cordis.patch.yml").write_text(json.dumps(patch, indent=2))
    prompt = (
        "Run the mode B integration test on the already selected SYNTHETIC dataset only. "
        "Use ONLY mcp__standards__ tools, no shell, files, web or subagents. "
        "Call inspect_project, analyze_requirements, build_knowledge_base, match_standards, generate_standard in order. "
        "Keep the exact returned standard_id. Call enrich_standard with that ID and limit=2, "
        "then evaluate_standard and export_standard using the SAME ID. "
        "Do not repeat successful calls. Stop and report any tool error. "
        "These are draft artifacts; human decision enforcement and upstream association are deferred. "
        "Finally report the ID, whether evaluable, export size, and those limitations in Chinese."
    )
    if args.mode == "A":
        prompt = (
            "Run the synthetic mode A text integration test. Use only mcp__standards__ tools. "
            "Call inspect_project, parse_documents(mode=A), extract_knowledge(mode=A), build_knowledge_base(mode=A), "
            "analyze_discovery, prioritize_discovery. Then select 2..4 actual candidate_id values returned by analyze_discovery "
            "and call generate_standard(mode=A,candidate_ids=those IDs). This is a technical draft selection, not human approval. "
            "Use the exact returned STD-A ID for enrich_standard(limit=4), evaluate_standard, export_standard. "
            "A has no gold: report not evaluable, not 0% accuracy. Stop on any tool error. "
            "No shell, files, web, subagents or library writes. Final response in Chinese with the ID, export size and limitations."
        )
    started = time.monotonic()
    try:
        result = subprocess.run([binary, "--profile", "headless", "--json", prompt],
                                cwd=PROJECT, env=env, text=True, capture_output=True, timeout=600)
    finally:
        if relay:
            relay.shutdown()
    # Raw local logs may include paths or generated content; never commit them.
    (PROJECT / f".runtime/harness-{args.mode}.stdout.jsonl").write_text(result.stdout)
    (PROJECT / f".runtime/harness-{args.mode}.stderr.log").write_text(result.stderr)
    events = []
    for line in result.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    assessment = assess(events, args.mode)
    failures = [e for e in events if e.get("type") in ("error", "fatal")]
    report = {"status": "PASS" if result.returncode == 0 and not assessment["missing_tools"] and not assessment["validation_errors"] and not failures else "FAIL",
              "real_harness": True, "real_model": "gemma4:31b-cloud", "synthetic_data_only": True,
              "mode": args.mode,
              "temporary_windows_ollama_relay": bool(relay),
              "seconds": round(time.monotonic() - started, 2), "exit_code": result.returncode,
              "tool_call_count": len(assessment["tool_names"]), **assessment,
              "final": [e for e in events if e.get("type") == "final"]}
    (PROJECT / f".runtime/harness-{args.mode}-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if report["status"] != "PASS":
        # Log paths only; raw errors may contain local credentials or URLs.
        print("See .runtime/harness.stderr.log and harness.stdout.jsonl for local diagnostics")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
