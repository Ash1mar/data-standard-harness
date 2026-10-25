"""Exercise the real stdio MCP server against the synthetic backend, without a Harness model."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def verify(base_url, enrich):
    env = dict(os.environ, STANDARD_BACKEND_URL=base_url, PYTHONIOENCODING="utf-8")
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "backend/harness_mcp.py")], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            results = {}

            async def call(name, args=None):
                result = await session.call_tool(name, args or {})
                if result.isError:
                    raise RuntimeError(f"{name} failed: {result.content}")
                value = result.structuredContent
                if value is None:
                    value = json.loads(next(c.text for c in result.content if c.type == "text"))
                results[name] = value
                print(json.dumps({"tool": name, "status": "PASS"}), flush=True)
                return value

            await call("inspect_project")
            await call("analyze_requirements")
            await call("build_knowledge_base")
            await call("match_standards")
            draft = await call("generate_standard")
            sid = draft["standard_id"]
            if enrich:
                result = await call("enrich_standard", {"standard_id": sid, "limit": 2})
                if result.get("batches", 0) < 1 or result.get("errors"):
                    raise RuntimeError("Business LLM enrichment did not complete")
            evaluation = await call("evaluate_standard", {"standard_id": sid})
            if not evaluation.get("evaluable"):
                raise RuntimeError("Synthetic gold should be evaluable")
            export = await call("export_standard", {"standard_id": sid})
            report = {"status": "PASS", "transport": "real_stdio_mcp", "harness_used": False,
                      "business_llm_used": enrich, "tools": tools, "standard_id": sid,
                      "evaluation": evaluation, "export_bytes": export["bytes"]}
            folder = ROOT / ".runtime"; folder.mkdir(exist_ok=True)
            (folder / "mcp-verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8124")
    parser.add_argument("--enrich", action="store_true")
    args = parser.parse_args()
    asyncio.run(verify(args.base_url, args.enrich))
