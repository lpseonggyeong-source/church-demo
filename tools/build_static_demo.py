"""온길 시연용 정적 사본 만들기 (GitHub Pages용)

    python3 tools/build_static_demo.py [출력 폴더, 기본: docs]

하는 일
  1) 임시 DB로 온길을 잠깐 실행하고(테스트 헌금 데이터 자동 생성)
  2) 성도(모바일·PC 웹) · 키오스크 · 관리자 화면을 차례로 열어 HTML로 저장한 뒤
  3) 링크를 정적 파일 주소로 바꿔 줍니다.

정적 사본에서는 저장 · 로그인 · 결제 처리가 동작하지 않습니다(화면 보기 전용).
헌금 · 키오스크 흐름은 예시 값(감사헌금 50,000원, 테스트 승인)으로 미리 진행해 둔 화면을 보여 줍니다.
"""
import http.cookiejar
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "docs"
CHURCH = "grace"
G = f"/c/{CHURCH}"
BIBLE_CHAPTERS = {("1CO", "13"), ("PSA", "23"), ("PSA", "121"), ("JHN", "3"), ("GEN", "1"), ("MAT", "5")}  # None = 성경 전체
OKB_URL = "https://github.com/open-korean-bible/open-korean-bible"   # 시연판에 없는 장은 성경 원본 저장소로 연결
NOTICE = "demo-only"          # 시연판에서 지원하지 않는 동작 안내 페이지


# ───────── 1. 임시 서버 ─────────
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seed(db_path):
    db = sqlite3.connect(db_path)
    rnd = random.Random(7)
    names = ["", "", "", "정하늘", "장요한", "최다니엘", "김수아", "송예린", "김은혜", "박지훈", None]
    cats = ["주일헌금", "감사헌금", "십일조", "선교헌금", "기타"]
    base = datetime(2026, 11, 23, 14, 0)
    for i in range(58):
        t = base - timedelta(hours=rnd.randint(1, 24 * 40), minutes=rnd.randint(0, 59))
        st = "PENDING" if i in (0, 1) else ("PAID" if i > 3 else rnd.choice(["FAILED", "CANCELLED"]))
        did = f"demo{i:04d}"
        paid = (t + timedelta(seconds=rnd.randint(20, 90))).strftime("%Y-%m-%d %H:%M:%S") if st == "PAID" else None
        db.execute("INSERT INTO donation(id,church,category,amount,owner,day,lookup,donor_name,paid_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (did, CHURCH, rnd.choice(cats), rnd.choice([10000, 20000, 30000, 50000, 70000, 100000, 300000]), "seed",
                    t.strftime("%Y-%m-%d %H:%M:%S"), did, rnd.choice(names), paid))
        db.execute("INSERT INTO payment(id,request,owner,reference,status) VALUES (?,?,?,?,?)", (did, did, "seed", "seed", st))
    db.execute("UPDATE category SET note='주일 예배 헌금' WHERE church=? AND title='주일헌금'", (CHURCH,))
    for title, body, day, public, tag in [
        ("추수감사주일 연합예배 안내", "11월 22일 추수감사주일에는 1부와 2부 예배를 합쳐 오전 11시에 한 번 드립니다.", "2026-11-20", 1, "행사"),
        ("겨울 성경학교 교사 모집", "겨울 성경학교에서 교사로 섬길 분을 찾습니다.", "2026-11-10", 0, "교육")]:
        db.execute("INSERT INTO notice(church,title,body,day,public,tag) VALUES (?,?,?,?,?,?)", (CHURCH, title, body, day, public, tag))
    db.commit()
    db.close()


class Session:
    def __init__(self, base):
        self.base = base
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, path):
        r = self.op.open(self.base + path, timeout=15)
        return r.geturl()[len(self.base):], r.read().decode()

    def submit(self, html, action, button=None, fields=None):
        """화면의 form(action으로 찾음)을 hidden 값 그대로 제출"""
        for form in re.findall(r'<form[^>]*>.*?</form>', html, re.S):
            m = re.search(r'action="([^"]*)"', form)
            if not m or not unescape(m.group(1)).endswith(action):
                continue
            if button and f'value="{button[1]}"' not in form:
                continue
            data = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form))
            data = {k: unescape(v) for k, v in data.items()}
            data.update(fields or {})
            if button:
                data[button[0]] = button[1]
            r = self.op.open(self.base + unescape(m.group(1)), urllib.parse.urlencode(data).encode(), timeout=15)
            return r.geturl()[len(self.base):], r.read().decode()
        raise RuntimeError(f"form not found: {action}")


# ───────── 2. 주소 → 정적 파일 ─────────
def canonical(url):
    """앱 주소를 시연판 페이지 키로. 저장하지 않는 주소는 None."""
    parts = urllib.parse.urlsplit(unescape(url))
    path = parts.path.rstrip("/")
    q = dict(urllib.parse.parse_qsl(parts.query))
    if path == "":
        return "churches"
    if path == "/admin":
        return "admin"
    if not path.startswith(G):
        return None
    page = path[len(G):].strip("/") or "index"
    if page in {"index", "give", "site", "review", "checkout", "result"}:
        return f"c/{CHURCH}/{page}"
    if page == "news":
        return f"c/{CHURCH}/news" + (f"?id={q['id']}" if q.get("id", "").isdigit() else "")
    if page == "bible":
        b, ch = q.get("b"), q.get("ch")
        if not b:
            return f"c/{CHURCH}/bible" + (f"?section={q['section']}" if q.get("section") in {"old", "new"} else "")
        if not ch:
            return f"c/{CHURCH}/bible?b={b}"
        if BIBLE_CHAPTERS is not None and (b, ch) not in BIBLE_CHAPTERS:
            return None
        return f"c/{CHURCH}/bible?b={b}&ch={ch}" + ("&v=1" if q.get("v") else "")
    if page == "kiosk":
        step = q.get("step", "")
        if step == "input":
            cat = q.get("cat", "")
            if not cat:
                return f"c/{CHURCH}/kiosk?step=input"
            return f"c/{CHURCH}/kiosk?step=input&cat={cat}&amt=50000"
        if step == "confirm":
            return f"c/{CHURCH}/kiosk?step=confirm"
        if step in {"pay", "done"}:
            return f"c/{CHURCH}/kiosk?step={step}"
        return f"c/{CHURCH}/kiosk"
    if page == "admin":
        if q.get("tab") == "settings":
            sec = q.get("sec", "info")
            extra = ""
            if sec == "notices" and q.get("edit"):
                extra = f"&edit={q['edit']}"
            elif sec == "categories" and q.get("add") == "1":
                extra = "&add=1"
            return f"c/{CHURCH}/admin?tab=settings&sec={sec}{extra}"
        p = q.get("page", "1")
        return f"c/{CHURCH}/admin" + (f"?page={p}" if p.isdigit() and p != "1" else "")
    return None


def fetch_url(key):
    """페이지 키 → 실제로 열 앱 주소"""
    path, _, query = key.partition("?")
    path = "/" if path == "churches" else "/" + path
    if path.endswith("/index"):
        path = path[: -len("index")]
    if "/bible" in path and query:
        query = "t=okb&" + query
    if "/kiosk" in path and "cat=" in query and "amt=" in query:
        query = query.replace("amt=50000", "amt=50000")
    return path + ("?" + query if query else "")


def filename(key):
    path, _, query = key.partition("?")
    if query:
        path += "__" + re.sub(r"[^A-Za-z0-9]+", "-", query).strip("-")
    return path + ".html"


# ───────── 3. 화면 모으기 ─────────
def collect(base):
    pages = {}          # key → html
    post_map = {}       # (key, action 끝부분) → 이동할 key
    guest = Session(base)
    _, pages["admin"] = guest.get("/admin")                      # 로그인 화면 (로그인 전)

    s = Session(base)
    # 관리자 로그인 (이후 관리자 화면도 같은 세션으로)
    _, login = s.get("/admin")
    s.submit(login, "/login", fields={"role": "admin", "password": os.environ.get("ONGIL_ADMIN_PASSWORD", "demo-admin")})

    # 헌금 흐름 (예시: 감사헌금 50,000원 → 테스트 승인)
    _, give = s.get(f"{G}/give")
    cat = re.search(r'name="category" value="(\d+)"[^>]*>(?:</?[^>]+>)*\s*감사헌금', give)
    cat = cat.group(1) if cat else "2"
    _, review = s.submit(give, f"{G}/review", fields={"category": cat, "amount": "50000", "name": ""})
    pages[f"c/{CHURCH}/review"] = review
    _, checkout = s.submit(review, f"{G}/start")
    pages[f"c/{CHURCH}/checkout"] = checkout
    finish_btn = re.search(r'<button[^>]*name="([^"]+)"[^>]*value="(PAID)"', checkout)
    _, result = s.submit(checkout, "/finish", button=(finish_btn.group(1), "PAID") if finish_btn else None)
    pages[f"c/{CHURCH}/result"] = result
    post_map.update({("c/grace/give", "/review"): "c/grace/review", ("c/grace/review", "/start"): "c/grace/checkout",
                     ("c/grace/review", "/edit"): "c/grace/give", ("c/grace/checkout", "/finish"): "c/grace/result"})

    # 키오스크 흐름 (감사헌금 50,000원 → 테스트 승인)
    _, kconfirm = s.get(f"{G}/kiosk?step=confirm&cat={cat}&amt=50000")
    pages[f"c/{CHURCH}/kiosk?step=confirm"] = kconfirm
    _, kpay = s.submit(kconfirm, "/kiosk-pay")
    pages[f"c/{CHURCH}/kiosk?step=pay"] = kpay
    kbtn = re.search(r'<button[^>]*name="([^"]+)"[^>]*value="(PAID)"', kpay)
    _, kdone = s.submit(kpay, "/finish", button=(kbtn.group(1), "PAID") if kbtn else None)
    pages[f"c/{CHURCH}/kiosk?step=done"] = kdone
    post_map.update({("c/grace/kiosk?step=confirm", "/kiosk-pay"): "c/grace/kiosk?step=pay",
                     ("c/grace/kiosk?step=pay", "/finish"): "c/grace/kiosk?step=done"})

    # 나머지: 링크를 따라 모으기
    seeds = ["churches", f"c/{CHURCH}/index", f"c/{CHURCH}/kiosk", f"c/{CHURCH}/kiosk?step=input",
             f"c/{CHURCH}/kiosk?step=input&cat={cat}&amt=50000", f"c/{CHURCH}/admin",
             f"c/{CHURCH}/admin?tab=settings&sec=categories&add=1", f"c/{CHURCH}/admin?tab=settings&sec=notices&edit=new"]
    queue = deque(k for k in seeds if k not in pages)
    seen = set(pages) | set(queue)
    while queue:
        key = queue.popleft()
        _, html = s.get(fetch_url(key))
        pages[key] = html
        for href in re.findall(r'href="([^"#]*)', html):
            k = canonical(href) if href.startswith("/") else None
            if k and k not in seen:
                seen.add(k)
                queue.append(k)
        if len(pages) > 3000:
            raise RuntimeError("페이지가 너무 많습니다 — canonical 규칙을 확인하세요")
    return pages, post_map


# ───────── 4. 정적 링크로 바꿔 저장 ─────────
def rel(from_key, target_file):
    here = os.path.dirname(filename(from_key))
    return os.path.relpath(target_file, here or ".")


def rewrite(key, html, pages, post_map):
    def link(url):
        url = unescape(url)
        frag = ""
        if "#" in url:
            url, frag = url.split("#", 1)
            frag = "#" + frag
        if url == "":
            return frag
        if url.startswith(("/assets/", "/style.css", "/give.js")):
            return rel(key, url.lstrip("/"))
        if not url.startswith("/"):
            return url + frag
        k = canonical(url)
        if k is None and urllib.parse.urlsplit(url).path.endswith("/bible"):
            return OKB_URL
        return rel(key, filename(k if k in pages else NOTICE)) + frag

    def form(m):
        tag, body = m.group(1), m.group(2)
        action = unescape(re.search(r'action="([^"]*)"', tag).group(1))
        if 'method="get"' in tag:
            target = link(action)
        else:
            dest = next((v for (src, act), v in post_map.items() if src == key and action.endswith(act)), None)
            if action.endswith("/login"):
                dest = f"c/{CHURCH}/admin"
            elif action.endswith("/logout"):
                dest = "admin"
            target = rel(key, filename(dest if dest in pages else NOTICE))
        body = re.sub(r'<input type="hidden"[^>]*>', "", body)
        tag = re.sub(r'\s(method|action)="[^"]*"', "", tag)      # class 등 다른 속성은 그대로 둠
        return f'<form method="get" action="{target}"{tag}>{body}</form>'

    html = re.sub(r'<form([^>]*)>(.*?)</form>', form, html, flags=re.S)
    html = re.sub(r'(href|src)="([^"]*)"', lambda m: f'{m.group(1)}="{link(m.group(2))}"', html)
    html = re.sub(r'(content="\d+;\s*url=)([^"]*)"', lambda m: f'{m.group(1)}{link(m.group(2))}"', html)
    html = html.replace(" data-autosave", "")
    if "/bible__" in filename(key) and "-v-1" in filename(key):
        # 장마다 한 페이지 — 선택한 절은 주소의 #verse-N 으로 강조 (CSS :target)
        html = html.replace(' aria-current="location"', "")
        html = re.sub(r"<span>\d+절 선택됨 · 앞 절은 위로 스크롤</span>", "<span>선택한 절이 강조됩니다</span>", html)
    return html


LANDING = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>온길 시연</title><link rel="stylesheet" href="{fonts}"><link rel="stylesheet" href="style.css"></head>
<body class="demo-landing"><main class="demo-main">
<div class="heading"><p class="eyebrow" lang="en">Ongil · Demo</p><h1>온길 화면 시연</h1></div>
<p class="lead">교회 헌금 서비스 ‘온길’의 화면을 둘러볼 수 있는 시연용 사본입니다. 저장 · 로그인 · 결제는 동작하지 않으며,
헌금 흐름은 예시 값(감사헌금 50,000원, 테스트 승인)으로 진행됩니다. 이름 · 금액은 모두 가상의 테스트 데이터입니다.</p>
<div class="demo-links">
<a href="c/{c}/index.html"><b>모바일 · PC 웹</b><small>성도 화면 — 창을 좁히면 모바일, 1000px 이상이면 PC 레이아웃</small></a>
<a href="c/{c}/kiosk.html"><b>키오스크</b><small>세로 1080×1920 — 개발자 도구에서 1080×1920으로 보세요</small></a>
<a href="c/{c}/admin.html"><b>관리자</b><small>헌금 내역 · 교회 설정 (PC 1024px 이상 / 모바일)</small></a>
<a href="admin.html"><b>관리자 로그인 화면</b><small>로그인 버튼을 누르면 헌금 내역으로 이동합니다</small></a>
</div>
<p class="muted">만든 시각 {stamp} · 성경은 대표 장(고린도전서 13장, 시편 23·121편 등)만 담았고, 나머지 장은 <a href="https://github.com/open-korean-bible/open-korean-bible">Open Korean Bible 원본</a>으로 연결됩니다</p>
</main></body></html>"""

NOTICE_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>시연판 안내 | 온길</title><link rel="stylesheet" href="{fonts}"><link rel="stylesheet" href="style.css"></head>
<body class="demo-landing"><main class="demo-main"><div class="heading"><p class="eyebrow" lang="en">Demo only</p><h1>시연판에서는 지원하지 않는 동작이에요</h1></div>
<p class="lead">이 사본은 화면을 보기 위한 정적 파일이라 저장 · 로그인 · 결제는 동작하지 않습니다. 실제 동작은 앱을 실행해서 확인해 주세요.</p>
<div class="pair"><a class="primary" href="javascript:history.back()">이전 화면으로</a><a class="ghost" href="index.html">시연 첫 화면</a></div></main></body></html>"""

DEMO_CSS = """
/* 시연판 첫 화면 */
.demo-landing{padding-bottom:0}.demo-main{max-width:640px;padding:56px 20px}
.demo-links{display:grid;gap:12px;margin:24px 0}
.demo-links a{display:grid;gap:4px;padding:20px 24px;border:1px solid var(--line-2);border-radius:var(--r4);background:var(--surface);color:var(--ink);text-decoration:none}
.verses li:target{background:var(--oxblood-tint);padding:8px}
.verses li:target > b{align-self:start;justify-self:center;display:grid;place-items:center;width:22px;height:22px;margin-top:6px;padding:0;border-radius:var(--r1);background:var(--oxblood);color:var(--on-oxblood);font-weight:600}
.passage-flow .v:target{background:var(--oxblood-tint)}
.demo-links a:hover{border-color:var(--oxblood)}.demo-links b{font:600 19px/1.4 var(--serif)}.demo-links small{font-size:13px;color:var(--ink-2)}
"""


def main():
    sys.path.insert(0, str(ROOT))
    port = free_port()
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "demo.sqlite3"
    env = dict(os.environ, PORT=str(port), PAYMENT_MODE="mock", ONGIL_DB=str(db_path), ONGIL_KIOSK_TEST="1")
    server = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        seed(db_path)
        pages, post_map = collect(f"http://127.0.0.1:{port}")
    finally:
        server.terminate()
        server.wait()
        shutil.rmtree(tmp, ignore_errors=True)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    shutil.copytree(ROOT / "assets", OUT / "assets")
    css = (ROOT / "style.css").read_text(encoding="utf-8").replace("url(/assets/", "url(assets/")
    (OUT / "style.css").write_text(css + DEMO_CSS, encoding="utf-8")
    shutil.copy(ROOT / "give.js", OUT / "give.js")
    fonts = re.search(r'FONTS = "([^"]+)"', (ROOT / "app.py").read_text(encoding="utf-8")).group(1).replace("&", "&amp;")
    for key, html in pages.items():
        target = OUT / filename(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rewrite(key, html, pages, post_map), encoding="utf-8")
    (OUT / "index.html").write_text(LANDING.replace("{fonts}", fonts).replace("{c}", CHURCH)
                                    .replace("{stamp}", datetime.now().strftime("%Y-%m-%d %H:%M")), encoding="utf-8")
    (OUT / f"{NOTICE}.html").write_text(NOTICE_HTML.replace("{fonts}", fonts), encoding="utf-8")
    (OUT / ".nojekyll").write_text("")
    print(f"시연판 {len(pages) + 2}개 화면 → {OUT}")


if __name__ == "__main__":
    main()
