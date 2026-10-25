"""Run only the disposable synthetic copy. Does not read the original data directory."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
p = argparse.ArgumentParser()
p.add_argument("--port", type=int, default=8124)
p.add_argument("--model", default="gemma4:31b-cloud")
p.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
p.add_argument("--no-llm", action="store_true")
args = p.parse_args()
project = Path(json.loads((root / ".runtime/latest-validation.json").read_text())["project"]).resolve()
if not project.is_relative_to((root / ".runtime/validation").resolve()):
    raise SystemExit("Validation project must be inside .runtime/validation")
env = dict(os.environ, STANDARD_EXECUTION_MODE="live", PYTHONIOENCODING="utf-8",
           LLM_BASE_URL=args.base_url, LLM_MODEL=args.model, LLM_BULK_MODEL=args.model, LLM_API_KEY="local-ollama",
           LLM_TIMEOUT="180", LLM_ENABLED="0" if args.no_llm else "1")
raise SystemExit(subprocess.call([sys.executable, "-m", "uvicorn", "main:app", "--app-dir", str(project / "backend"),
                                 "--host", "127.0.0.1", "--port", str(args.port)], cwd=project, env=env))
