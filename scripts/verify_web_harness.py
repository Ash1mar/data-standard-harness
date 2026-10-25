"""Create an observable validation session in the existing authenticated Harness Web instance.

Run with WSL python. Launch tokens and raw business results stay local.
"""
import argparse
import http.cookiejar
import json
import re
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["A", "B"], required=True)
    parser.add_argument("--installation", type=Path, default=Path("/opt/data/deepseek-harness"))
    parser.add_argument("--probe", action="store_true", help="Check authenticated Web/workspace API only; no session or model call")
    args = parser.parse_args()
    install = args.installation
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    # A service restart returns before Web has bound its port. Read the newest
    # launch token on each retry, without ever printing it.
    for attempt in range(30):
        urls = re.findall(r"dsh web: (http[^\s]+)", (install / "logs/web.log").read_text())
        try:
            if not urls:
                raise ConnectionError("Web not ready")
            client.open(urls[-1], timeout=5).close()
            break
        except (urllib.error.URLError, ConnectionError):
            if attempt == 29:
                raise RuntimeError("Harness Web did not become ready") from None
            time.sleep(1)

    def rpc(endpoint, values):
        body = {"type": "client-request", "rpcId": uuid.uuid4().hex, "method": endpoint,
                "payload": {"args": values}}
        request = urllib.request.Request("http://127.0.0.1:3080/api/" + endpoint,
                                         data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with client.open(request, timeout=60) as response:
            result = json.load(response)["result"]
        if not result.get("ok"):
            raise RuntimeError(json.dumps(result.get("error"), ensure_ascii=False))
        return result.get("value")

    workspace = rpc("workspace/create", {"request": {"path": str(PROJECT)}})
    workspace_view = workspace.get("workspace", workspace)
    workspace_id = workspace_view.get("workspaceId") or workspace_view.get("id")
    if not workspace_id:
        raise RuntimeError("Workspace API returned no identity")
    if args.probe:
        print(json.dumps({"web_authenticated": True, "workspace_api": "PASS"}))
        return
    created = rpc("session/create", {"request": {"workspaceId": workspace_id}})
    sid = created["sessionId"]
    rpc("session/rename", {"request": {"sessionId": sid, "title": f"MCP 模式 {args.mode} 真实验证"}})
    if args.mode == "B":
        prompt = (
            "验证现有型钢数据的模式 B。只用 mcp__standards__ 工具，不用 shell、文件、网页或子代理。"
            "依次调用 inspect_project、parse_documents(mode=B)、extract_knowledge(mode=B)、"
            "analyze_requirements、build_knowledge_base(mode=B)、propose_clarifications、match_standards、generate_standard(mode=B)。"
            "不要提交人工决策，所有产物仅为草案。保存本次 standard_id，使用同一 ID 调用 enrich_standard(limit=24)，"
            "如仍有 missing_definition 则继续补写，每次 limit=24，最多12次，无进展即停止并如实报告。"
            "随后用同一 ID 调用 evaluate_standard 和 export_standard。不要重复成功生成。"
            "任何工具失败即停止并报告。最终仅报告 ID、对象类数、属性数、实际评测指标、导出大小及未修复限制。"
            "人工决策执行和上游产物关联尚未修复，不得声称人工批准或生产验收。"
            "若评测 artifact_binding_verified=false，应说明来源元数据不完整，指标仅供参考，不是验收结论。"
        )
    else:
        prompt = (
            "验证已选合成文本样例的模式 A。只用 mcp__standards__ 工具，不用 shell、文件、网页或子代理。"
            "依次调用 inspect_project、parse_documents(mode=A)、extract_knowledge(mode=A)、build_knowledge_base(mode=A)、"
            "analyze_discovery、prioritize_discovery。根据 analyze_discovery 返回的真实 candidate_id，明确选择2到4个候选，"
            "调用 generate_standard(mode=A,candidate_ids=所选ID)。这只是技术草案选择，不代表人工审批。"
            "保存本次 STD-A 标准 ID，用同一 ID 调用 enrich_standard(limit=4)、evaluate_standard、export_standard。"
            "无模式 A 答案时如实说明不可评测，不要把 B 答案用于 A。任何工具失败即停止。"
            "最终报告真实抽取候选数、标准ID、输出完整性问题、可否评测与导出大小，以及暂缓的人工决策/产物关联问题。"
        )
    rpc("session/prompt", {"request": {"requestId": uuid.uuid4().hex, "sessionId": sid, "mode": "queue",
         "content": [{"type": "text", "text": prompt}], "clientTimeZone": "Asia/Shanghai"}})
    folder = PROJECT / ".runtime"; folder.mkdir(exist_ok=True)
    report = {"session_id": sid, "workspace_id": workspace_id, "mode": args.mode, "web_instance": True,
              "status": "STARTED", "time": time.time()}
    (folder / f"web-{args.mode}-session.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
