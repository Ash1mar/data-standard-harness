"""Run as WSL root: back up and extend the existing Web profile and runtime skills."""
import argparse
import json
import os
import pwd
import shutil
import subprocess
import time
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parent.parent
MARKER = "# managed: data-standard-harness MCP\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--installation", type=Path, default=Path("/opt/data/deepseek-harness"))
    args = parser.parse_args()
    install = args.installation
    profile = install / "home/profiles/web/cordis.patch.yml"
    original = profile.read_text()
    backup = install / "backups" / ("standards-" + str(time.time_ns()))
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(profile, backup / "web-profile.yml")
    overlay = json.loads((PROJECT / ".dsh/mcp.local.yml").read_text())
    profile.write_text(original.split(MARKER)[0].rstrip() + "\n\n" + MARKER + yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False))
    account = pwd.getpwuid((install / "home").stat().st_uid)
    os.chown(profile, account.pw_uid, account.pw_gid)
    for name in ("data-standard-mode-b", "data-standard-mode-a"):
        source = PROJECT / ".dsh/skills" / name
        target = install / "home/skills" / name
        if target.exists():
            shutil.copytree(target, backup / name)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / "SKILL.md", target / "SKILL.md")
        os.chown(target, account.pw_uid, account.pw_gid)
        os.chown(target / "SKILL.md", account.pw_uid, account.pw_gid)
    subprocess.run(["systemctl", "restart", "deepseek-harness.service"], check=True)
    print(json.dumps({"web_mcp_configured": True,
                      "existing_profile_backed_up": True, "credentials_modified": False}))


if __name__ == "__main__":
    main()
