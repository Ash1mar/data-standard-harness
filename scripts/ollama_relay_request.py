"""One request from WSL test relay to Windows loopback Ollama, via WSL interop.

stdin/stdout are private IPC, not a listening network service. No model keys are
read; Windows Ollama owns its existing cloud login.
"""
import base64
import json
import sys
import urllib.error
import urllib.request

body = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
if len(body) > 2 * 1024 * 1024:
    raise SystemExit("Request exceeds validation limit")
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
request = urllib.request.Request("http://127.0.0.1:11434/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
try:
    with client.open(request, timeout=240) as response:
        result = {"status": response.status, "content_type": response.headers.get("content-type", "application/json"),
                  "body": base64.b64encode(response.read()).decode("ascii")}
except urllib.error.HTTPError as error:
    result = {"status": error.code, "content_type": "application/json",
              "body": base64.b64encode(error.read()).decode("ascii")}
sys.stdout.buffer.write(json.dumps(result).encode("utf-8"))
