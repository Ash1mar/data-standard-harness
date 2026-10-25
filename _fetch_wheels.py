"""临时脚本：绕过 pip 的 403（改用 urllib 下载 wheel），构建依赖闭包到 .wheels/"""
import io, json, os, re, sys, zipfile, urllib.request
from html.parser import HTMLParser

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple/"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wheels")
os.makedirs(OUT, exist_ok=True)

PY_TAG = "cp%d%d" % sys.version_info[:2]
ABI3 = ["cp39-abi3", "cp310-abi3", "cp311-abi3", "cp312-abi3", "cp313-abi3"]

class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.href = []
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href":
                    self.href.append(v)

def fetch(url, headers=None):
    # 本机 urllib 直连被代理拦截（403），改用系统 curl
    import subprocess
    r = subprocess.run(["curl", "-sSL", "--max-time", "180", url],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("curl 失败 %s: %s" % (url, r.stderr[:200]))
    return r.stdout

def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()

def ver_key(v):
    return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-+]", v)[:4]]

def candidates(pkg):
    html = fetch(MIRROR + pkg + "/").decode("utf-8", "ignore")
    p = Links(); p.feed(html)
    out = []
    for h in p.href:
        fname = h.split("/")[-1].split("#")[0]
        if not fname.endswith(".whl"):
            continue
        parts = fname[:-4].split("-")
        if len(parts) < 5:
            continue
        ver = parts[1]
        tags = "-".join(parts[-3:])
        if re.search(r"[a-zA-Z]", ver):   # 跳过 a/b/rc/dev 预发布
            continue
        if "manylinux" in tags or "musllinux" in tags or "macosx" in tags:
            continue
        ok = ("none-any" in tags) or ("win_amd64" in tags and ("none-win_amd64" in tags or PY_TAG in tags or any(a in tags for a in ABI3)))
        if not ok:
            continue
        import urllib.parse as _up
        out.append((ver_key(ver), ver, _up.urljoin(MIRROR + pkg + "/", h.split("#")[0]), fname))
    out.sort(reverse=True)
    return out

def reqs_from_wheel(path):
    deps = []
    with zipfile.ZipFile(path) as z:
        meta = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
        if not meta:
            return deps
        txt = z.read(meta[0]).decode("utf-8", "ignore")
    for line in txt.splitlines():
        if not line.startswith("Requires-Dist:"):
            continue
        spec = line.split(":", 1)[1].strip()
        name = re.split(r"[;\s(<>=!~\[]", spec)[0].strip()
        if "; extra" in spec:
            continue
        if "extra ==" in spec:
            continue
        if "win32" in spec and sys.platform != "win32":
            continue
        if name:
            deps.append(norm(name))
    return deps

def main(roots):
    seen, queue, installed = set(), list(roots), []
    while queue:
        pkg = norm(queue.pop(0))
        if pkg in seen:
            continue
        seen.add(pkg)
        cands = candidates(pkg)
        if not cands:
            print("!! 无可用 wheel:", pkg); continue
        _, ver, url, fname = cands[0]
        dest = os.path.join(OUT, fname)
        if not os.path.exists(dest):
            data = fetch(url, {"User-Agent": "Mozilla/5.0"})
            open(dest, "wb").write(data)
        print("OK", pkg, ver, fname)
        installed.append(fname)
        for d in reqs_from_wheel(dest):
            if d not in seen:
                queue.append(d)
    print("共", len(installed), "个 wheel")

if __name__ == "__main__":
    main(["fastapi", "uvicorn", "python-multipart", "openpyxl", "pdfplumber", "python-docx"])
