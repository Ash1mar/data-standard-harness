"""Scan staged Git blobs or a committed tree, never print matched secret values."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rev", help="Scan a committed tree instead of the complete Git index")
    args = parser.parse_args()
    files = (git("ls-tree", "-r", "--name-only", "-z", args.rev) if args.rev
             else git("ls-files", "-z")).decode("utf-8").split("\0")
    known = []
    config = ROOT / "data/llm_config.json"
    if config.exists():
        values = json.loads(config.read_text(encoding="utf-8-sig"))
        known += [str(values[k]) for k in ("api_key", "token", "password") if values.get(k)]
    key_file = ROOT / "data/llm_key.txt"
    if key_file.exists():
        raw = key_file.read_text(encoding="utf-8-sig").strip()
        known += [raw, *re.findall(r"sk-[A-Za-z0-9._-]{16,}", raw)]
    known = [value for value in known if len(value) >= 12]
    patterns = {
        "credential_pattern": r"(?:sk-[A-Za-z0-9._-]{16,}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})",
        "personal_windows_path": r"[A-Za-z]:\\Users\\[A-Za-z0-9_.-]+\\",
        "private_key": r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
    }
    forbidden = ("data/", "deliverables/", ".runtime/", ".private/", ".local/", ".venv", ".tmpcheck/", ".workbuddy/")
    hits, count = [], 0
    for name in filter(None, files):
        if name.startswith(forbidden) or name.endswith((".credentials.yaml", "llm_key.txt", "mcp.local.yml")):
            hits.append({"file": name, "rule": "private_path"})
        blob = git("show", f"{args.rev}:{name}" if args.rev else f":{name}")
        text = blob.decode("utf-8", errors="replace")
        count += 1
        for rule, pattern in patterns.items():
            if re.search(pattern, text):
                hits.append({"file": name, "rule": rule})
        if any(value in text for value in known):
            hits.append({"file": name, "rule": "known_local_credential"})
    print(json.dumps({"files_checked": count, "findings": hits}, ensure_ascii=False))
    return bool(hits)


if __name__ == "__main__":
    raise SystemExit(main())
