"""Generate a secret-free, workstation-specific MCP overlay for Windows or WSL Harness."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser()
parser.add_argument("--wsl", action="store_true")
parser.add_argument("--base-url", default="http://127.0.0.1:8123")
args = parser.parse_args()
python = ROOT / ".venv-local/Scripts/python.exe"
command = str(python)
if args.wsl:
    command = "/mnt/" + python.drive[0].lower() + python.as_posix()[2:]
patch = [{"insert": [{"id": "mcp-data-standards", "name": "@deepseek-ai/dsh-mcp-client", "config": {
    "serverName": "standards", "transport": "stdio", "command": command,
    "args": [str(ROOT / "backend/harness_mcp.py"), "--backend-url", args.base_url], "toolCallTimeoutMs": 360000,
    "env": {"STANDARD_BACKEND_URL": args.base_url, "PYTHONIOENCODING": "utf-8"},
    "failOnStartupError": True,
}}]}]
target = ROOT / ".dsh/mcp.local.yml"
target.write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")
print(str(target))
