import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from import_bible import CODES, NAMES, CHAPTERS
from payments import LinkPaymentTestProvider, MockPaymentProvider, configured_provider
from okb import index_books

ROOT = Path(__file__).resolve().parent
DB = sqlite3.connect(os.getenv("ONGIL_DB", str(ROOT / "ongil.sqlite3")))
DB.row_factory = sqlite3.Row
PG = configured_provider()
# 키오스크는 카드 단말기 연동 전이라 전용 가짜 결제를 씁니다. 실제 운영 전에는 ONGIL_KIOSK_TEST=0 으로 끄세요.
KIOSK_TEST = os.getenv("ONGIL_KIOSK_TEST", "1") == "1"
KIOSK_PG = MockPaymentProvider()
KIOSK_IDLE = 120  # 입력 없으면 처음 화면으로 (초)
LINKPAYMENT_TEST = isinstance(PG, LinkPaymentTestProvider)
PG_MODE_LABEL = "링크페이먼트 카드 테스트" if LINKPAYMENT_TEST else "Mock 결제"
# HTTPS 리버스 프록시 뒤에서 배포할 때 ONGIL_COOKIE_SECURE=1로 설정하면 쿠키에 Secure를 붙입니다.
COOKIE_SECURE = os.getenv("ONGIL_COOKIE_SECURE", "0") == "1"
REVIEW_KEY = secrets.token_bytes(32)
KST = timezone(timedelta(hours=9))
DOW = "월화수목금토일"
STATUSES = {"READY":"준비", "PENDING":"처리 중", "PAID":"테스트 성공", "FAILED":"테스트 실패", "CANCELLED":"테스트 취소"}
# 성도 화면의 상태 표시 (시안 문구)
MEMBER_STATUSES = {"READY":"처리 중", "PENDING":"처리 중", "PAID":"완료", "FAILED":"실패", "CANCELLED":"취소"}
# 결과 화면 문구 (영문 머리글, 제목, 안내 상자) — 처리 중에는 '완료'로 오해할 표현을 쓰지 않습니다.
RESULTS = {
    "PAID": ("Thank you", "헌금이 완료되었습니다", ""),
    "PENDING": ("Processing", "결제 승인을 확인하고 있습니다",
                "카드사 승인 결과가 아직 도착하지 않았습니다. 승인이 확인되기 전까지는 ‘처리 중’으로 기록됩니다. <b>중복 결제를 막기 위해 다시 결제하지 마시고</b> 아래 버튼으로 상태를 확인해 주세요. 이 화면을 닫은 뒤 확인이 필요하면 교회에 문의해 주세요."),
    "FAILED": ("Payment failed", "결제가 완료되지 않았습니다",
               "카드사에서 승인을 거절했습니다(한도 초과, 정보 불일치 등). 금액은 청구되지 않았습니다. 다른 카드로 다시 시도해 주세요."),
    "CANCELLED": ("Cancelled", "결제를 취소하셨습니다", "결제창을 닫아 결제가 진행되지 않았습니다. 금액은 청구되지 않았습니다."),
}
RESULTS["READY"] = RESULTS["PENDING"]
NAV = [("bible","성경"),("news","교회소식"),("give","헌금"),("site","교회사이트")]  # 성도용 헌금내역은 제공하지 않음 (2026-11-23 결정)
GIVE_STEPS = ["입력","확인","결제","결과"]
# 관리자 (Figma 02): 교회 설정 탭, 헌금 내역 기간 선택
ADMIN_SECTIONS = [("info","교회 정보"),("logo","로고"),("site","홈페이지 주소"),("categories","헌금 항목"),("notices","공지 관리")]
ADMIN_PERIODS = [("","전체 기간"),("today","오늘"),("7d","최근 7일"),("month","이번 달"),("30d","최근 30일")]
NOTICE_TAGS = ["공지","행사","교육","헌금","소식"]
# 교회 첫 화면에 쓰는 선택 정보 (관리자 설정에서 입력, 비어 있으면 화면에 표시하지 않음)
CHURCH_PROFILE = [("tagline","영문 소개 문구"),("meta","교단 · 지역"),("cover","대표 그림 주소 (https://)"),("cover_caption","대표 그림 설명"),
                  ("verse","이번 주 말씀"),("verse_ref","말씀 출처 (예: 시편 121:1–2)"),("services","예배 안내 (한 줄에 하나, 예: 주일 1부 예배 | 오전 9:00)"),
                  ("address","주소"),("contact","담임목사 · 연락처")]
FONTS = "https://fonts.googleapis.com/css2?family=EB+Garamond:ital@1&family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=Noto+Serif+KR:wght@400;600;700&display=swap"
ASSET_TYPES = {".svg":"image/svg+xml", ".png":"image/png"}
TRANSLATIONS = json.loads((ROOT / "translations.json").read_text())
METADATA = {t["id"]: t for t in TRANSLATIONS}
DATA = {}
for t in TRANSLATIONS:
    path = ROOT / t["file"]
    document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    DATA[t["id"]] = index_books(document) if t["id"] == "okb" and document else document
SAMPLE = json.loads((ROOT / "okb-sample.json").read_text())
DB.executescript("""
CREATE TABLE IF NOT EXISTS church (id TEXT PRIMARY KEY, name TEXT, site TEXT DEFAULT '', logo TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS category (id INTEGER PRIMARY KEY, church TEXT, title TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS notice (id INTEGER PRIMARY KEY, church TEXT, title TEXT, body TEXT, day TEXT, public INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS session (id TEXT PRIMARY KEY, role TEXT DEFAULT '', csrf TEXT);
CREATE TABLE IF NOT EXISTS donation (id TEXT PRIMARY KEY, church TEXT, category TEXT, amount INTEGER, owner TEXT, day TEXT, lookup TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS payment (id TEXT PRIMARY KEY, request TEXT, owner TEXT, reference TEXT, status TEXT, UNIQUE(owner,request));
CREATE TABLE IF NOT EXISTS translation (id TEXT PRIMARY KEY, name TEXT, license TEXT, source TEXT);
""")
with DB:
    payment_columns = {column["name"] for column in DB.execute("PRAGMA table_info(payment)")}
    if "sdk_started" not in payment_columns:
        DB.execute("ALTER TABLE payment ADD COLUMN sdk_started INTEGER NOT NULL DEFAULT 0")
    donation_columns = {column["name"] for column in DB.execute("PRAGMA table_info(donation)")}
    if "donor_name" not in donation_columns:
        DB.execute("ALTER TABLE donation ADD COLUMN donor_name TEXT")
    if "paid_at" not in donation_columns:
        DB.execute("ALTER TABLE donation ADD COLUMN paid_at TEXT")
    notice_columns = {column["name"] for column in DB.execute("PRAGMA table_info(notice)")}
    if "tag" not in notice_columns:
        DB.execute("ALTER TABLE notice ADD COLUMN tag TEXT NOT NULL DEFAULT ''")
    category_columns = {column["name"] for column in DB.execute("PRAGMA table_info(category)")}
    if "sort" not in category_columns:
        DB.execute("ALTER TABLE category ADD COLUMN sort INTEGER NOT NULL DEFAULT 0")
    if "note" not in category_columns:
        DB.execute("ALTER TABLE category ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    church_columns = {column["name"] for column in DB.execute("PRAGMA table_info(church)")}
    for key, _label in CHURCH_PROFILE:
        if key not in church_columns:
            DB.execute(f"ALTER TABLE church ADD COLUMN {key} TEXT NOT NULL DEFAULT ''")
    for key, name in [("grace","여의도순복음교회"),("spring","새봄교회")]:
        inserted = DB.execute("INSERT OR IGNORE INTO church(id,name) VALUES (?,?)", (key,name))
        if inserted.rowcount:
            DB.executemany("INSERT INTO category(church,title) VALUES (?,?)",
                           [(key,item) for item in ["주일헌금","감사헌금","십일조","선교헌금","기타"]])
            DB.execute("INSERT INTO notice(church,title,body,day) VALUES (?,?,?,?)",
                       (key,"주일예배 안내","주일 오전 11시 본당에서 예배를 드립니다.","2026-11-18"))
    DB.execute("UPDATE category SET sort=id WHERE sort=0")  # 순서가 없던 항목은 만든 순서대로
    # Figma 시안 반영(2026-11-23): 테스트 교회 이름 · 대표 사진. 관리자가 직접 바꾼 값은 건드리지 않습니다.
    DB.execute("UPDATE church SET name='여의도순복음교회' WHERE id='grace' AND name='은혜마을교회'")
    DB.execute("UPDATE church SET cover='/assets/cover-church.png', cover_caption='' WHERE id='grace' AND cover='/assets/cover-grace.png'")
    # 여의도순복음교회 공식 홈페이지 (2026-11-23 요청) — 관리자가 이미 넣은 주소는 덮어쓰지 않습니다.
    DB.execute("UPDATE church SET site='https://www.fgtv.com/' WHERE id='grace' AND site=''")
    # 시안의 예시 내용 — 테스트 교회(grace)에만, 아직 비어 있을 때 한 번 채웁니다.
    DB.execute("""UPDATE church SET tagline=?,meta=?,cover=?,cover_caption=?,verse=?,verse_ref=?,services=?,address=?,contact=?
                  WHERE id='grace' AND tagline='' AND meta='' AND verse=''""",
               ("Grace Village Church · Since 1987","대한예수교장로회 · 경기 양평 · 가상의 교회","/assets/cover-church.png","",
                "내가 산을 향하여 눈을 들리라 나의 도움이 어디서 올꼬 나의 도움이 천지를 지으신 여호와에게서로다","시편 121:1–2",
                "주일 1부 예배 | 오전 9:00\n주일 2부 예배 | 오전 11:00\n수요 기도회 | 오후 7:30\n새벽 기도회 (화–토) | 오전 5:30",
                "경기도 양평군 은혜마을길 12","담임목사 한요셉 · 031-000-0000"))
    for t in TRANSLATIONS:
        DB.execute("INSERT OR IGNORE INTO translation VALUES (?,?,?,?)",(t["id"],t["name"],t["license"],t["source"]))
    DB.execute("UPDATE translation SET name=? WHERE id=? AND name=?",
               ("Open Korean Bible · 원문", "okb", "Open Korean Bible · 현대어"))


def e(value):
    return escape(str(value), quote=True)


def query(sql, *params):
    return DB.execute(sql, params).fetchall()


def one(sql, *params):
    return DB.execute(sql, params).fetchone()


def now():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")



def review_signature(owner, church, category, total, request, donor_name):
    payload = json.dumps([owner, church, category, total, request, donor_name]).encode()
    return hmac.new(REVIEW_KEY, payload, hashlib.sha256).hexdigest()


def amount(value):
    return f"{value:,}원"


def korean_amount(value):
    """50000 → '오만 원' (give.js의 같은 함수와 결과가 같아야 합니다)"""
    if not value or value <= 0:
        return ""
    digits = "영일이삼사오육칠팔구"
    groups = []
    for unit in ["", "만", "억", "조"]:
        part, value = value % 10000, value // 10000
        if part:
            text = ""
            for place, name in [(1000,"천"),(100,"백"),(10,"십"),(1,"")]:
                d = part // place % 10
                if d:
                    text += ("" if d == 1 and name else digits[d]) + name
            groups.append(text + unit)
        if not value:
            break
    return " ".join(reversed(groups)) + " 원"


def fmt_day(day):
    """'2026-11-18' → '2026.11.18 (수)'"""
    try:
        d = datetime.strptime(day[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return e(day or "")
    return f"{d:%Y.%m.%d} ({DOW[d.weekday()]})"


def ico(name, extra=""):
    """assets 폴더의 SVG 아이콘 (색은 글자색을 따름)"""
    return f'<i class="ico ico-{name}{" " + extra if extra else ""}" aria-hidden="true"></i>'


def fmt_time(stamp):
    return e((stamp or "").replace("-", "."))


def web_url(value):
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("https://로 시작하는 주소를 입력해 주세요.")
    return value


def card(body, extra=""):
    return f'<section class="card{" " + extra if extra else ""}">{body}</section>'


def field(label, key, value="", required=False, multiline=False, kind="text", placeholder=""):
    req = " required" if required else ""
    hint = f' placeholder="{e(placeholder)}"' if placeholder else ""
    control = (f'<textarea name="{key}" maxlength="4000"{req}>{e(value)}</textarea>' if multiline
               else f'<input type="{kind}" name="{key}" value="{e(value)}" maxlength="240"{hint}{req}>')
    return f'<label>{label}{control}</label>'


def hidden(key, value):
    return f'<input type="hidden" name="{key}" value="{e(value)}">'


def status_badge(status, member=False):
    label = (MEMBER_STATUSES if member else STATUSES).get(status, status)
    return f'<span class="status status-{e(status.lower())}">{e(label)}</span>'


def receipt_rows(rows):
    return '<dl class="receipt-list">' + "".join(f'<div><dt>{label}</dt><dd>{value}</dd></div>' for label, value in rows) + '</dl>'


def donor_label(name):
    if name is None:
        return "이름 기록 없음"
    return e(name) if name else "익명"


class App(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send(self, body, status=200, mime="text/html; charset=utf-8", location=None, cache=False):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        # 보안 정책(CSP): 우리 서버 파일 + Google Fonts 글꼴만 허용. 결제창 화면에서만 링크페이먼트 주소를 추가로 허용합니다.
        styles = "'self' https://fonts.googleapis.com"
        scripts = "'self'"
        extra = ""
        if getattr(self, "pg_checkout_page", False):
            styles += " 'unsafe-inline'"
            scripts += " https://code.jquery.com https://api.linkpayment.co.kr"
            extra = "; connect-src 'self' https://api.linkpayment.co.kr; frame-src https://api.linkpayment.co.kr"
        policy = (f"default-src 'none'; style-src {styles}; font-src https://fonts.gstatic.com; script-src {scripts}; "
                  f"img-src 'self' https:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'{extra}")
        self.send_header("Content-Security-Policy", policy)
        if location:
            self.send_header("Location", location)
        if getattr(self,"cookie",None):
            secure = "; Secure" if COOKIE_SECURE else ""
            self.send_header("Set-Cookie", f"ongil={self.cookie}; Path=/; HttpOnly; SameSite=Strict{secure}")
        data = body if isinstance(body, bytes) else body.encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, link):
        self.send("",303,location=link)

    def setup_request(self):
        cookies = SimpleCookie(self.headers.get("Cookie",""))
        supplied = cookies.get("ongil")
        self.cookie = None
        self.session = one("SELECT * FROM session WHERE id=?", supplied.value) if supplied else None
        if not self.session:
            self.rotate("")
        parsed = urlsplit(self.path)
        self.args = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        parts = parsed.path.strip("/").split("/")
        self.church = None
        if len(parts) >= 2 and parts[0] == "c":
            self.church = one("SELECT * FROM church WHERE id=?", parts[1])
            if self.church:
                self.base = "/c/" + self.church["id"]
                return parts[2] if len(parts) == 3 else "home" if len(parts) == 2 else "invalid"
            return "invalid"
        self.base = ""
        self.asset_name = parts[1] if len(parts) == 2 and parts[0] == "assets" else ""
        return parts[0] or "home"

    def rotate(self, role):
        sid = secrets.token_urlsafe(32)
        previous = self.session["id"] if self.session else None
        if previous:
            DB.execute("DELETE FROM session WHERE id=?", (previous,))
            for table in ("donation","payment"):
                DB.execute(f"UPDATE {table} SET owner=? WHERE owner=?", (sid, previous))
        DB.execute("INSERT INTO session VALUES (?,?,?)", (sid,role,secrets.token_urlsafe(24)))
        self.cookie = sid
        self.session = one("SELECT * FROM session WHERE id=?", sid)

    def href(self, page, **args):
        return self.base + "/" + page + ("?" + urlencode(args) if args else "")

    def form(self, action, body):
        return f'<form method="post" action="{e(self.href(action))}">{hidden("csrf",self.session["csrf"])}{body}</form>'

    def logo(self):
        church = self.church
        return f'<img src="{e(church["logo"])}" alt="" referrerpolicy="no-referrer">' if church and church["logo"] else "<span>✦</span>"

    @staticmethod
    def badge(church, extra=""):
        """교회 이름 첫 글자 배지 (로고가 등록돼 있으면 로고)"""
        inner = f'<img src="{e(church["logo"])}" alt="" referrerpolicy="no-referrer">' if church["logo"] else e(church["name"][:1])
        return f'<span class="badge{" " + extra if extra else ""}" aria-hidden="true">{inner}</span>'

    @staticmethod
    def stepper(current):
        """헌금 진행 단계: 현재 단계만 글자와 함께 강조 (1 입력 — 2 확인 — 3 결제 — 4 결과)"""
        parts = []
        for number, name in enumerate(GIVE_STEPS, 1):
            if number == current:
                parts.append(f'<b aria-current="step">{number} {name}</b>')
            else:
                parts.append(f'<span>{number}{" " + name if current == 1 else ""}</span>')
        return f'<div class="stepper" aria-label="헌금 {len(GIVE_STEPS)}단계 중 {current}단계">' + '<i aria-hidden="true"></i>'.join(parts) + '</div>'

    def layout(self, title, body, active="", status=200, admin=False, side="", step=0, eyebrow="", actions="", heading=True, top="", back="", pre="", width="", chrome=True):
        church = self.church
        member = bool(church) and not admin
        nav = ""
        if member:
            nav_links = []
            for key, name in NAV:
                current = ' aria-current="page"' if key == active else ""
                nav_links.append(f'<a href="{self.href(key)}"{current}><i class="ico ico-{key}" aria-hidden="true"></i>{name}</a>')
            nav = '<nav class="tabbar" aria-label="주 메뉴"><div class="wrap">' + "".join(nav_links) + '</div></nav>'
        scripts = '<script defer src="/give.js"></script>'
        if getattr(self,"pg_checkout_page",False):
            scripts += (
                '<script defer src="https://code.jquery.com/jquery-3.6.0.js"></script>'
                '<script defer src="https://api.linkpayment.co.kr/js/clientside.js"></script>'
                '<script defer src="/linkpayment-checkout.js"></script>'
            )
        if side or not chrome:
            header = ""
        elif admin:
            label = church["name"] if church else "교회 관리자"
            header = (f'<header class="adm-top"><a class="adm-brand" href="/admin"><b>온길</b><small>{e(label)}</small></a>'
                      f'<a class="change" href="/">교회 선택</a></header>')
        elif top:
            header = top
        elif church:
            # PC 웹(넓은 화면)에서만 보이는 위쪽 가로 메뉴 — 휴대폰은 하단 메뉴
            here = ' aria-current="page"'
            topnav = '<nav class="topnav" aria-label="주 메뉴">' + "".join(
                f'<a href="{self.href(key)}"{here if key == active else ""}>{name}</a>' for key, name in NAV) + '</nav>'
            header = (f'<header class="top"><div class="wrap"><a class="church-link" href="{e(self.base)}/">{self.badge(church)}<b>{e(church["name"])}</b></a>'
                      f'{topnav if not admin else ""}<a class="change" href="/">교회 변경</a></div></header>')
        else:
            header = '<header class="top"><div class="wrap"><a class="brand" href="/"><b>온길</b><i>Ongil</i></a><span class="tag">교회 성경·헌금</span></div></header>'
        head = ""
        if heading:
            steps = self.stepper(step) if step else ""
            if back:
                steps = f'<div class="step-row">{back}{steps}</div>'
            head = (f'{pre}{steps}<div class="heading">' + (f'<p class="eyebrow" lang="en">{e(eyebrow)}</p>' if eyebrow else "")
                    + f'<h1>{e(title)}</h1></div>')
        main = f'<main id="main">{head}{body}</main>'
        if actions:
            main += f'<div class="actionbar"><div class="wrap">{actions}</div></div>'
        if side:
            main = f'<div class="admin-shell">{side}{main}</div>'
        self.send(f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="theme-color" content="#faf6ef">
<title>{e(title)} | 온길</title><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link rel="stylesheet" href="{FONTS}"><link rel="stylesheet" href="/style.css"></head>
<body class="{'admin-page' if admin else ''}{' w-' + width if width else ''}"><a class="skip" href="#main">본문으로</a>{header}
{main}{nav}{scripts}</body></html>""",status)

    def do_GET(self):
        self.run_request(False)

    def do_POST(self):
        self.run_request(True)

    def run_request(self, post):
        with DB:
            page = self.setup_request()
        try:
            if post:
                length = int(self.headers.get("Content-Length","0"))
                if not 0 < length <= 32000:
                    raise ValueError("입력 용량을 확인해 주세요.")
                values = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
                self.form_data = {k:v[0].strip() for k,v in values.items()}
                if not hmac.compare_digest(self.form_data.get("csrf","").encode(), self.session["csrf"].encode()):
                    return self.layout("다시 시도해 주세요",card("페이지를 새로 열어 주세요."),status=403)
                with DB:
                    return self.post(page)
            if page == "style.css" and not self.base:
                return self.send((ROOT / "style.css").read_text(),mime="text/css; charset=utf-8")
            if page in {"linkpayment-checkout.js","give.js"} and not self.base:
                return self.send((ROOT / page).read_text(encoding="utf-8"),mime="text/javascript; charset=utf-8")
            if page == "assets" and not self.base:
                # 디자인 그림 파일: assets 폴더 안의 정해진 형식 파일만 전달 (다른 경로 접근 차단)
                name = self.asset_name
                path = ROOT / "assets" / name
                if re.fullmatch(r"[a-z0-9-]+\.(svg|png)", name) and path.is_file():
                    return self.send(path.read_bytes(), mime=ASSET_TYPES[path.suffix], cache=True)
                return self.send("not found", 404, mime="text/plain; charset=utf-8")
            if page in {"admin","login"}:
                return self.admin()
            if page == "home" and not self.church:
                return self.choose_church()
            if self.church and page in {key for key,_ in NAV} | {"home","checkout","result","kiosk"}:
                return getattr(self, "church_home" if page == "home" else page)()
            self.layout("페이지를 찾을 수 없어요",card('<a href="/">교회 선택으로 돌아가기</a>'),status=404)
        except (ValueError, KeyError, OverflowError) as err:
            self.layout("입력 내용을 확인해 주세요",card(f'<p role="alert">{e(err.args[0] if isinstance(err, ValueError) and err.args else "잘못된 요청입니다.")}</p><a href="{self.href("give" if page in {"review","start","finish"} else "admin" if page=="admin-save" else "bible")}">돌아가기</a>'),status=400)

    def choose_church(self):
        """성도 / 진입 / 교회 선택 — 이름 검색(서버에서 걸러냄, 자바스크립트 불필요)"""
        q = self.args.get("q","").strip()[:40]
        churches = query("SELECT * FROM church ORDER BY name")
        if q:
            churches = [c for c in churches if q in c["name"]]
        rows = "".join(
            f'<a class="row" href="/c/{e(c["id"])}/">{self.badge(c, "lg muted")}<span><b>{e(c["name"])}</b>'
            + (f'<small>{e(c["meta"])}</small>' if c["meta"] else "") + '</span>{ico("chev-r", "chev")}</a>'
            for c in churches) or f'<p class="muted">‘{e(q)}’ 이름의 교회를 찾지 못했습니다.</p>'
        body = ('<p class="lead">성경 읽기와 온라인 헌금은 선택한 교회 기준으로 열립니다.</p>'
                f'<form method="get" action="/" role="search"><label class="search">교회 이름 검색<input type="search" name="q" value="{e(q)}" placeholder="예: 은혜마을" autocomplete="off"></label></form>'
                f'<div class="rows spaced">{rows}</div>'
                '<p class="note spaced">교회에서 받은 전용 링크로 들어오면 이 단계 없이 바로 교회 첫 화면이 열립니다.</p>'
                '<a class="admin-entry" href="/admin">교회 관리자 로그인</a>')
        self.layout("다니시는 교회를 선택해 주세요", body, eyebrow="Choose your church")

    def verse_link(self):
        """'시편 121:1–2' 같은 출처를 성경 화면 링크로 바꿉니다. 해석이 안 되면 성경 첫 화면으로."""
        match = re.match(r"\s*(\S+)\s+(\d+):(\d+)", self.church["verse_ref"] or "")
        if match and match.group(1) in NAMES:
            index = NAMES.index(match.group(1))
            section = "new" if index >= 39 else "old"
            return self.href("bible", t=next(iter(METADATA)), b=CODES[index], ch=match.group(2), v=match.group(3), section=section) + "#verse-" + match.group(3)
        return self.href("bible")

    def church_home(self):
        """성도 / 진입 / 교회 첫 화면 — 관리자 설정에 입력된 항목만 보여 줍니다. (PC: 왼쪽 소개 · 오른쪽 사진)"""
        c = self.church
        top = ('<div class="home-intro">' + (f'<p class="eyebrow" lang="en">{e(c["tagline"])}</p>' if c["tagline"] else "")
               + f'<h1>{e(c["name"])}</h1>' + (f'<p class="meta">{e(c["meta"])}</p>' if c["meta"] else "") + '</div>')
        if c["cover"]:
            caption = f'<figcaption><span>Pl. I</span>{e(c["cover_caption"])}</figcaption>' if c["cover_caption"] else ""
            top += f'<figure class="plate"><div class="frame"><img src="{e(c["cover"])}" alt="{e(c["cover_caption"] or c["name"])}" referrerpolicy="no-referrer"></div>{caption}</figure>'
        if c["verse"]:
            top += ('<section class="home-sec home-verse"><div class="sec-head"><h2>이번 주 말씀</h2><i lang="en">Verse of the week</i></div>'
                    f'<blockquote class="verse">“{e(c["verse"])}”</blockquote>'
                    f'<div class="verse-foot"><span>{e(c["verse_ref"])}</span><a href="{e(self.verse_link())}">본문 전체 읽기</a></div></section>')
        top += (f'<div class="home-actions pair"><a class="primary" href="{e(self.href("give"))}">헌금하기</a>'
                f'<a class="ghost" href="{e(self.href("bible"))}">성경 읽기</a></div>')
        body = f'<div class="home-top">{top}</div>'
        lower = ""
        notices = query("SELECT * FROM notice WHERE church=? AND public=1 ORDER BY day DESC,id DESC LIMIT 3", c["id"])
        if notices:
            lower += (f'<section class="home-sec"><div class="sec-head"><h2>교회소식</h2><a href="{e(self.href("news"))}">전체 보기</a></div><div class="rows flush">'
                      + "".join(f'<a class="row plain" href="{e(self.href("news",id=n["id"]))}"><span><small>{fmt_day(n["day"])}</small><b>{e(n["title"])}</b></span>{ico("chev-r", "chev")}</a>' for n in notices)
                      + '</div></section>')
        lines = [line.split("|",1) for line in (c["services"] or "").splitlines() if line.strip()]
        if lines:
            lower += ('<section class="home-sec"><div class="sec-head"><h2>예배 안내</h2><i lang="en">Services</i></div><table class="services"><tbody>'
                      + "".join(f'<tr><td>{e(p[0].strip())}</td><td>{e(p[1].strip()) if len(p) > 1 else ""}</td></tr>' for p in lines)
                      + '</tbody></table></section>')
        if lower:
            body += f'<div class="home-lower">{lower}</div>'
        if c["address"] or c["contact"]:
            body += '<footer class="home-foot">' + "".join(f'<p>{e(v)}</p>' for v in (c["address"], c["contact"]) if v) + '</footer>'
        self.layout(c["name"], body, heading=False, width="wide")

    def bible(self):
        """성도 / 성경 — 책(구약·신약 탭) → 장 → 절 → 장 전체 본문(선택한 절 강조)"""
        editions = [one("SELECT * FROM translation WHERE id=?", tid) for tid in METADATA]
        edition = next((t for t in editions if t["id"] == self.args.get("t")), editions[0])
        tid = edition["id"]
        bible = DATA.get(tid,{})
        preview = not bible and tid == "okb"
        source = SAMPLE if preview else bible
        book = self.args.get("b","")
        if book not in CODES:
            book = ""
        testament = "new" if (book and CODES.index(book) >= 39) or (not book and self.args.get("section") == "new") else "old"
        chapters = source.get(book,{})
        chapter = self.args.get("ch","")
        available = sorted(chapters,key=int) if bible else [str(n) for n in range(1, CHAPTERS[CODES.index(book)]+1)] if book else []
        if chapter not in available:
            chapter = ""
        verses = chapters.get(chapter,{})
        verse = self.args.get("v","")
        verse_keys = sorted(verses,key=int) or (["1"] if preview else [])
        if verse not in verse_keys:
            verse = ""

        def bible_url(**params):
            url = self.href("bible",t=tid,**params)
            if params.get("v"):
                url += "#verse-" + params["v"]
            return e(url)

        name = NAMES[CODES.index(book)] if book else ""
        testament_label = "신약" if testament == "new" else "구약"
        metadata = METADATA[tid]
        author = f' · {e(metadata["creator"])}' if metadata.get("creator") else ""
        license_label = e(edition["license"])
        if metadata.get("license_url"):
            license_label = f'<a href="{e(metadata["license_url"])}">{license_label}</a>'
        source_name = "Open Korean Bible" if tid == "okb" else edition["name"]
        source_info = (f'<div class="source-box"><p><b>본문 출처</b> <a href="{e(edition["source"])}">{e(source_name)}</a>{author}</p>'
                       f'<p><b>라이선스</b> {license_label} · 원 저장소의 라이선스와 저작권 표시를 그대로 따릅니다.</p></div>')
        demo = '<p class="note">미리보기에는 창세기 1:1을 제공합니다. 전권을 읽으려면 README의 성경 데이터 설치 단계를 진행하세요.</p>' if preview else ""
        versions = ""
        if len(editions) > 1:
            versions = '<div class="chips">' + "".join(
                f'<a class="pill {"selected" if t["id"]==tid else ""}" href="{e(self.href("bible",t=t["id"],b=book,ch=chapter,v=verse))}">{e(t["name"])}</a>' for t in editions) + '</div>'

        if verse:
            # 장 전체 본문: 1절부터 이어 읽고, 선택한 절로 이동해 강조합니다.
            idx = available.index(chapter)
            prev_ch = available[idx-1] if idx > 0 else ""
            next_ch = available[idx+1] if idx+1 < len(available) else ""
            prev_link = (f'<a href="{bible_url(b=book,ch=prev_ch,v="1")}" aria-label="{e(name)} {prev_ch}장">{ico("chev-l")}</a>' if prev_ch else f'<span>{ico("chev-l")}</span>')
            next_link = (f'<a href="{bible_url(b=book,ch=next_ch,v="1")}" aria-label="{e(name)} {next_ch}장">{ico("chev-r")}</a>' if next_ch else f'<span>{ico("chev-r")}</span>')
            bar = (f'<div class="reader-bar">{prev_link}<h1>{e(name)} {e(chapter)}장</h1>{next_link}</div>'
                   f'<div class="reader-tools"><a href="{bible_url(b=book,ch=chapter)}">절 선택 {ico("chev-d", "sm")}</a>'
                   f'<span>{e(verse)}절 선택됨 · 앞 절은 위로 스크롤</span><a href="#verse-1">1절로 {ico("up", "sm")}</a></div>')
            here = ' aria-current="page"'
            side = (f'<aside class="bible-side" aria-label="장 목록"><nav class="crumbs"><a href="{bible_url(section=testament)}">{testament_label}</a> {ico("chev-r", "sm")} <b>{e(name)}</b></nav>'
                    f'<div class="heading"><p class="eyebrow" lang="en">{"New Testament" if testament == "new" else "Old Testament"}</p><h2 class="side-title">{e(name)}</h2></div>'
                    f'<p class="muted">모두 {len(available)}장</p><div class="tiles cols-4">'
                    + "".join(f'<a class="tile{" selected" if n == chapter else ""}" href="{bible_url(b=book,ch=n,v="1")}"{here if n == chapter else ""}>{n}</a>' for n in available)
                    + '</div></aside>')
            body = (f'<div class="bible-layout">{side}<div class="reader">' + bar + versions + self.chapter_text(verses, verse_keys, verse) + demo + source_info + '</div></div>')
            return self.layout(f"{name} {chapter}장", body, "bible", heading=False, width="wide")

        crumbs = ""
        if book:
            parts = [f'<a href="{bible_url(section=testament)}">{testament_label}</a>']
            parts.append(f'<a href="{bible_url(b=book)}">{e(name)}</a>' if chapter else f'<b>{e(name)}</b>')
            if chapter:
                parts.append(f'<b>{e(chapter)}장</b>')
            crumbs = '<nav class="crumbs" aria-label="성경 위치">' + f' {ico("chev-r", "sm")} '.join(parts) + '</nav>'
        if chapter:
            grid = "".join(f'<a class="tile" href="{bible_url(b=book,ch=chapter,v=n)}">{n}</a>' for n in verse_keys) or '<p>사용 가능한 절 데이터가 없습니다.</p>'
            body = (f'<p class="lead">모두 {len(verse_keys)}절 · 절을 고르면 장 전체 본문에서 그 위치로 이동합니다</p><div class="tiles cols-5">{grid}</div>'
                    f'<a class="ghost block spaced" href="{bible_url(b=book,ch=chapter,v=verse_keys[0] if verse_keys else "1")}">1절부터 장 전체 읽기</a>')
            title, eyebrow = f"{name} {chapter}장", f"Chapter {chapter}"
        elif book:
            grid = "".join(f'<a class="tile" href="{bible_url(b=book,ch=n)}">{n}</a>' for n in available)
            body = f'<p class="lead">모두 {len(available)}장 · 읽을 장을 선택해 주세요</p><div class="tiles cols-5">{grid}</div>'
            title, eyebrow = name, ("New Testament" if testament == "new" else "Old Testament")
        else:
            indices = range(39,66) if testament == "new" else range(39)
            current = ' aria-current="page"'
            tabs = '<nav class="tabs" aria-label="구약 · 신약">' + "".join(
                f'<a href="{bible_url(section=key)}"{current if key == testament else ""}>{label} <small>{count}권</small></a>'
                for key,label,count in [("old","구약",39),("new","신약",27)]) + '</nav>'
            grid = "".join(f'<a class="tile" href="{bible_url(b=CODES[i])}">{e(NAMES[i])}</a>' for i in indices)
            body = f'<p class="lead">책을 고르면 장과 절을 차례로 선택합니다.</p>{tabs}<div class="tiles cols-3">{grid}</div>'
            title, eyebrow = "성경", "Holy Bible"
        self.layout(title, versions + body + demo, "bible", eyebrow=eyebrow, pre=crumbs)

    @staticmethod
    def chapter_text(verses, verse_keys, selected):
        items = []
        for number in verse_keys:
            passage = e(verses.get(number,"이 구절의 본문은 전권 데이터를 연결한 뒤 표시됩니다."))
            passage = re.sub(r"\{\{([^{}]+)\}\}", r"<strong>\1</strong>", passage)
            current = ' aria-current="location"' if number == selected else ""
            label = '<span class="sr">선택한 절 </span>' if number == selected else ""
            items.append(f'<li id="verse-{e(number)}"{current} tabindex="-1">{label}<b>{e(number)}<span class="sr">절</span></b><span>{passage}</span></li>')
        return f'<ol class="verses" aria-label="장 전체 본문">{"".join(items)}</ol>'

    def notice_meta(self, n):
        tag = f'<span class="pill-tag">{e(n["tag"])}</span>' if n["tag"] else ""
        return f'<p class="notice-meta">{tag}<time datetime="{e(n["day"])}">{fmt_day(n["day"])}</time></p>'

    def news(self):
        """성도 / 교회소식 / 목록 · 상세"""
        rows = query("SELECT * FROM notice WHERE church=? AND public=1 ORDER BY day DESC,id DESC",self.church["id"])
        if self.args.get("id"):
            row = next((n for n in rows if str(n["id"])==self.args["id"]),None)
            if not row:
                return self.layout("공지를 찾을 수 없어요",f'<a class="back" href="{e(self.href("news"))}">{ico("chev-l", "sm")}교회소식 목록</a>',"news",404)
            paragraphs = "".join(f'<p>{e(part.strip())}</p>' for part in re.split(r"\n\s*\n", row["body"]) if part.strip())
            contact = self.church["contact"]
            foot = f'<p class="article-foot">{e(self.church["name"])} · {e(contact)}</p>' if contact else ""
            body = (f'<a class="back" href="{e(self.href("news"))}">{ico("chev-l", "sm")}교회소식 목록</a>'
                    f'<article class="article">{self.notice_meta(row)}<h1>{e(row["title"])}</h1><div class="article-body">{paragraphs}</div>{foot}</article>')
            return self.layout(row["title"], body, "news", heading=False)
        items = "".join(
            f'<a class="notice" href="{e(self.href("news",id=n["id"]))}">{self.notice_meta(n)}<b>{e(n["title"])}</b><span>{e(n["body"])}</span></a>'
            for n in rows)
        self.layout("교회소식", items or '<p class="muted">게시된 소식이 없습니다.</p>', "news", eyebrow="Church news")

    def site(self):
        """성도 / 교회사이트 / 공식 홈페이지 연결"""
        url = self.church["site"]
        if url:
            host = urlsplit(url).hostname or url
            body = (f'<div class="site-card"><div class="site-who">{self.badge(self.church, "lg")}<div><b>{e(self.church["name"])}</b><small>교회가 직접 운영하는 사이트</small></div></div>'
                    f'<p class="site-url">{e(host)}</p>'
                    f'<a class="primary block" href="{e(url)}" rel="noopener noreferrer" target="_blank"><i class="ico ico-site" aria-hidden="true"></i>홈페이지 열기 (새 창)</a></div>'
                    '<p class="note">온길 밖의 사이트로 이동합니다. 온라인 헌금은 홈페이지가 아닌 온길의 ‘헌금’ 메뉴를 이용해 주세요.</p>')
        else:
            body = '<p class="note">이 교회는 아직 홈페이지 주소를 등록하지 않았습니다.</p>'
        self.layout("교회 공식 홈페이지", body, "site", eyebrow="Official website")

    def kiosk_layout(self, title, body, refresh=KIOSK_IDLE, refresh_url=None):
        """키오스크 전용 틀: 큰 머리말, 하단 메뉴 없음, 일정 시간 뒤 처음 화면으로 자동 이동"""
        c = self.church
        go = refresh_url or self.href("kiosk")
        self.send(f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="theme-color" content="#faf6ef">
<meta http-equiv="refresh" content="{int(refresh)};url={e(go)}">
<title>{e(title)} | 온길 키오스크</title><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link rel="stylesheet" href="{FONTS}"><link rel="stylesheet" href="/style.css"></head>
<body class="kiosk"><header class="k-head"><div class="k-brand">{self.badge(c)}<b>{e(c["name"])}</b></div><span>온라인 헌금 키오스크</span></header>
<main id="main" class="k-body">{body}</main></body></html>""")

    @staticmethod
    def k_steps(current):
        names = ["입력","확인","결제","완료"]
        return '<div class="k-steps" aria-label="4단계 중 ' + str(current) + '단계">' + '<i aria-hidden="true"></i>'.join(
            f'<b aria-current="step">{n} {names[n-1]}</b>' if n == current else f'<span>{n} {names[n-1]}</span>' for n in range(1, 5)) + '</div>'

    @staticmethod
    def k_heading(en, ko):
        return f'<div class="k-heading"><p lang="en">{e(en)}</p><h1>{e(ko)}</h1></div>'

    def kiosk(self):
        """성도 / 키오스크 (세로 1080×1920) — 시작 · 입력 · 확인 · 결제 · 완료. 이름·내역·조회 코드 없음(전부 익명)."""
        items = query("SELECT * FROM category WHERE church=? AND active=1 ORDER BY sort,id",self.church["id"])
        step = self.args.get("step","start")
        ids = {str(r["id"]): r for r in items}
        cat = self.args.get("cat","") if self.args.get("cat","") in ids else ""
        try:
            amt = max(0, min(int(self.args.get("amt","0") or 0), 100000000))
        except ValueError:
            amt = 0
        url = lambda **kw: e(self.href("kiosk", **kw))
        amount_box = (f'<div class="k-amount"><p><strong>{amt:,}</strong><span>원</span></p>'
                      f'<small>{korean_amount(amt) or "1,000원부터 100,000,000원까지"}</small></div>')
        if step == "start" or not items:
            img = f'<img class="k-cover" src="{e(self.church["cover"])}" alt="" referrerpolicy="no-referrer">' if self.church["cover"] else ""
            body = (self.k_heading("Offering", "온라인 헌금")
                    + '<p class="k-lead">카드로 간편하게 헌금하실 수 있습니다.<br>이름과 개인 정보는 받지 않습니다.</p>' + img
                    + '<div class="k-fill"></div>'
                    + (f'<a class="k-btn primary" href="{url(step="input")}">헌금 시작하기</a>' if items else '<p class="k-note">현재 선택할 수 있는 헌금 항목이 없습니다.</p>')
                    + '<p class="k-note">화면을 2분 동안 누르지 않으면 처음 화면으로 돌아갑니다.</p>')
            return self.kiosk_layout("키오스크", body)
        if step == "input":
            here = ' aria-current="true"'
            options = "".join(f'<a class="k-option{" selected" if str(r["id"]) == cat else ""}" href="{url(step="input",cat=r["id"],amt=amt)}"{here if str(r["id"]) == cat else ""}>{e(r["title"])}</a>' for r in items)
            keys = []
            for k in ["1","2","3","4","5","6","7","8","9","00","0","지우기"]:
                if k == "지우기":
                    nxt = amt // 10
                else:
                    nxt = amt * (100 if k == "00" else 10) + (0 if k == "00" else int(k))
                    if nxt > 100000000:
                        nxt = amt
                keys.append(f'<a class="k-key" href="{url(step="input",cat=cat,amt=nxt)}">{k}</a>')
            ready = cat and amt >= 1000
            nxt_btn = (f'<a class="k-btn primary" href="{url(step="confirm",cat=cat,amt=amt)}">다음</a>' if ready
                       else '<span class="k-btn primary disabled" aria-disabled="true">다음</span>')
            body = (self.k_steps(1) + self.k_heading("Offering", "헌금 항목과 금액을 선택해 주세요")
                    + f'<nav class="k-options" aria-label="헌금 항목">{options}</nav>' + amount_box
                    + f'<nav class="k-keypad" aria-label="금액 키패드">{"".join(keys)}</nav><div class="k-fill"></div>'
                    + f'<div class="k-row"><a class="k-btn ghost narrow" href="{url()}">처음으로</a>{nxt_btn}</div>')
            return self.kiosk_layout("헌금 입력", body)
        if step == "confirm":
            if not (cat and amt >= 1000):
                return self.redirect(self.href("kiosk", step="input", cat=cat, amt=amt))
            category = ids[cat]
            request = secrets.token_urlsafe(24)
            sig = review_signature(self.session["id"], self.church["id"], category["id"], amt, request, "")
            fields = hidden("category",category["id"]) + hidden("amount",amt) + hidden("request",request) + hidden("signature",sig)
            body = (self.k_steps(2) + self.k_heading("Confirmation", "헌금 내용을 확인해 주세요")
                    + f'<div class="k-summary"><div class="k-sum-amount"><small>헌금 금액</small><p><strong>{amt:,}</strong><span>원</span></p><small>{korean_amount(amt)}</small></div>'
                    + f'<dl><div><dt>교회</dt><dd>{e(self.church["name"])}</dd></div><div><dt>헌금 항목</dt><dd>{e(category["title"])}</dd></div>'
                    + '<div><dt>이름</dt><dd>익명 (키오스크는 이름을 받지 않습니다)</dd></div></dl></div>'
                    + '<p class="k-note">다음 화면에서 화면 아래 카드 단말기로 결제합니다. 카드 정보는 온길에 저장되지 않습니다.</p><div class="k-fill"></div>'
                    + self.form("kiosk-pay", fields + f'<button class="k-btn primary">{amt:,}원 카드로 결제하기</button>')
                    + f'<a class="k-btn ghost" href="{url(step="input",cat=cat,amt=amt)}">금액 · 항목 수정</a>')
            return self.kiosk_layout("헌금 확인", body)
        row = self.order(self.args.get("id"))
        if not row or not row["reference"].startswith("kiosk_mock_"):
            return self.redirect(self.href("kiosk"))
        if step == "pay":
            if row["status"] != "PENDING":
                return self.redirect(self.href("kiosk", step="done", id=row["id"]))
            sim = self.form("finish", hidden("id",row["id"]) + '<p class="k-sim-title">테스트 · 단말기 결과 선택 (실제 단말기 연동 전)</p><div class="k-sim">'
                            + "".join(f'<button name="outcome" value="{v}">{t}</button>' for v, t in [("PAID","승인"),("FAILED","카드 승인 거절")]) + '</div>'
                            + '<button class="k-btn ghost" name="outcome" value="CANCELLED">결제 취소</button>')
            body = (self.k_steps(3) + self.k_heading("Payment", "카드를 단말기에 꽂아 주세요")
                    + '<p class="k-lead">IC칩이 위로 가도록 화면 아래 단말기에 꽂고, 결제가 끝날 때까지 빼지 마세요.</p>'
                    + '<div class="k-zone"><i class="ico ico-lock" aria-hidden="true"></i><b>카드 단말기 연결 지점</b><span>화면 아래 단말기</span></div>'
                    + f'<p class="k-pay-amount"><strong>{row["amount"]:,}</strong><span>원</span></p>'
                    + '<p class="k-wait"><i aria-hidden="true"></i>결제 대기 중</p><div class="k-fill"></div>' + sim)
            return self.kiosk_layout("카드 결제", body)
        status = row["status"] if row["status"] in RESULTS else "PENDING"
        eyebrow, title, note = RESULTS[status]
        kind = "pending" if status in {"PENDING","READY"} else status.lower()
        if status == "PAID":
            note = "익명으로 기록되었습니다. 기부금 영수증이 필요하시면 교회 사무실에 문의해 주세요."
        retry_cat = next((str(r["id"]) for r in items if r["title"] == row["category"]), "")
        again = (f'<a class="k-btn primary" href="{url(step="input",cat=retry_cat,amt=row["amount"])}">다시 시도</a>' if status in {"FAILED","CANCELLED"} else "")
        wait = 10 if status == "PAID" else 30
        body = (self.k_steps(4) + '<div class="k-fill"></div>' + f'<div class="result-mark k-mark mark-{kind}" aria-hidden="true"></div>'
                + self.k_heading(eyebrow, title)
                + f'<div class="k-done-sum"><small>{e(self.church["name"])} · {e(row["category"])}</small><p><strong>{row["amount"]:,}</strong><span>원</span></p></div>'
                + (f'<p class="k-note">{note}</p>' if note else "") + '<div class="k-fill"></div>'
                + f'<p class="k-count">{wait}초 후 처음 화면으로 돌아갑니다</p>' + again
                + f'<a class="k-btn {"ghost" if again else "primary"}" href="{url()}">처음으로</a>')
        return self.kiosk_layout(title, body, refresh=wait)

    def church_line(self, suffix):
        return f'<div class="receipt-to"><span class="mark">{self.logo()}</span><div><b>{e(self.church["name"])}</b><small>{suffix}</small></div></div>'

    def give(self, prefill=None):
        """성도 / 헌금 / 항목·금액 입력 — prefill: 확인 화면의 '수정'이나 실패·취소 후 '돌아가기'에서 이전 입력값"""
        items = query("SELECT * FROM category WHERE church=? AND active=1 ORDER BY sort,id",self.church["id"])
        if not items:
            return self.layout("헌금",'<p class="muted">현재 선택할 수 있는 헌금 항목이 없습니다.</p>',"give",step=1,eyebrow="Offering")
        prefill = dict(prefill or {})
        again = self.order(self.args.get("again"))
        if again:
            match = next((r for r in items if r["title"] == again["category"]), None)
            prefill = {"category": str(match["id"]) if match else "", "amount": str(again["amount"]), "name": again["donor_name"] or ""}
        chosen = str(prefill.get("category",""))
        try:
            total = int(str(prefill.get("amount","")).replace(",",""))
        except ValueError:
            total = 0
        radios = "".join(
            f'<label class="option"><input type="radio" name="category" value="{r["id"]}" required{" checked" if str(r["id"]) == chosen else ""}>{e(r["title"])}</label>'
            for r in items)
        body = self.stepper(1) + '<div class="heading"><p class="eyebrow" lang="en">Offering</p><h1>헌금</h1></div>'
        body += f'<fieldset><legend>헌금 항목</legend><div class="choices">{radios}</div></fieldset>'
        body += ('<label>금액<span class="amount-input"><input name="amount" type="text" inputmode="numeric" autocomplete="off" '
                 f'value="{f"{total:,}" if total else ""}" placeholder="0" pattern="[0-9,]*" required aria-describedby="amount-words" data-amount>'
                 '<span aria-hidden="true">원</span></span>'
                 f'<span class="amount-words" id="amount-words" aria-live="polite" data-amount-words data-empty="1,000원부터 100,000,000원까지">{korean_amount(total) or "1,000원부터 100,000,000원까지"}</span></label>'
                 '<div class="quick" data-quick hidden><button type="button" data-add="10000">+1만</button><button type="button" data-add="50000">+5만</button>'
                 '<button type="button" data-add="100000">+10만</button><button type="button" data-add="0">지우기</button></div>')
        body += (f'<label>이름 <span class="optional">선택</span><input type="text" name="name" value="{e(prefill.get("name",""))}" maxlength="240" autocomplete="name" placeholder="비워 두면 익명" aria-describedby="name-hint">'
                 '<small class="hint" id="name-hint">비워 두면 <b>익명</b>으로 기록되고, 교회 관리자 화면에도 이름 없이 ‘익명’으로 표시됩니다.</small></label>')
        body += '<p class="test-note">테스트 결제 · 실제 청구가 발생하지 않습니다.</p>'
        picked = next((r["title"] for r in items if str(r["id"]) == chosen), "")
        summary = ('<aside class="give-summary" aria-label="헌금 요약"><h2>헌금 요약</h2><dl class="facts ruled">'
                   f'<div><dt>교회</dt><dd>{e(self.church["name"])}</dd></div>'
                   f'<div><dt>헌금 항목</dt><dd data-sum-category>{e(picked) or "—"}</dd></div>'
                   f'<div><dt>이름</dt><dd data-sum-name>{e(prefill.get("name","")) or "익명"}</dd></div></dl>'
                   f'<p class="sum-amount"><strong data-sum-amount>{f"{total:,}" if total else "0"}</strong><span>원</span></p>'
                   '<button class="primary block">다음 · 내용 확인</button>'
                   '<p class="test-note">테스트 결제 · 실제 청구가 발생하지 않습니다.</p></aside>')
        layout = f'<div class="give-layout"><div class="give-main">{body}<div class="actionbar inset"><button class="primary block">다음 · 내용 확인</button></div></div>{summary}</div>'
        self.layout("헌금", self.form("review", layout), "give", heading=False, width="wide")

    def order(self, key):
        if not key:
            return None
        return one("""SELECT d.*,p.status,p.reference,p.sdk_started FROM donation d JOIN payment p USING(id)
                     WHERE d.id=? AND d.church=? AND d.owner=?""",key,self.church["id"],self.session["id"])

    def pay_page(self, row, zone, extra=""):
        """성도 / 헌금 / 결제창 연결 지점 — 외부 결제창으로 넘어가는 화면 (진한 머리말, 하단 메뉴 없음)"""
        top = ('<header class="pay-top"><div class="wrap"><b lang="en">secure payment</b>'
               f'<span>{"링크페이먼트 (외부)" if LINKPAYMENT_TEST else "테스트 결제 (Mock)"}</span></div></header>')
        title = "링크페이먼트 카드 결제창" if LINKPAYMENT_TEST else "테스트 결제창"
        body = ('<div class="pay-head"><span class="pill-tag">외부 연결 지점</span>'
                f'<h1>{title}</h1><p>이 자리에서 결제사 결제창이 열립니다. 온길은 결제 결과만 돌려받습니다.</p></div>')
        body += f'<div class="pay-zone">{zone}</div>'
        body += (f'<dl class="facts ruled"><div><dt>가맹점</dt><dd>{e(self.church["name"])}</dd></div>'
                 f'<div><dt>결제 금액</dt><dd>{amount(row["amount"])}</dd></div></dl>')
        body += extra
        church, self.church = self.church, None   # 하단 메뉴를 숨기기 위해 잠시 비움
        try:
            self.layout(title, body, heading=False, top=top)
        finally:
            self.church = church

    def checkout(self):
        row = self.order(self.args.get("id"))
        if not row:
            return self.layout("결제 요청을 찾을 수 없어요",f'<a href="{e(self.href("give"))}">헌금으로</a>',"give",404)
        if row["status"] != "PENDING":
            return self.redirect(self.href("result",id=row["id"]))
        if row["reference"].startswith("lp_test_"):
            return self.linkpayment_checkout(row)
        if LINKPAYMENT_TEST:
            return self.layout("Mock 거래",'<p>이 거래는 Mock 결제 모드에서 이어서 처리할 수 있습니다.</p>',"give")
        zone = '카드사 선택 · 카드 정보 입력 · 본인 인증<small>외부 결제창 영역 (테스트에서는 아래에서 결과를 고릅니다)</small>'
        options = [("PAID","승인","처리 중 → 승인 확인 후 완료"),("PENDING","승인 결과 지연","처리 중 유지"),
                   ("FAILED","카드 승인 거절","실패"),("CANCELLED","결제창 닫기","취소")]
        sim = ('<p class="sim-title">테스트 · 가상 거래 결과 선택</p>'
               + self.form("finish", hidden("id",row["id"]) + '<div class="sim">'
                           + "".join(f'<button name="outcome" value="{v}">{t}<small>{d}</small></button>' for v,t,d in options) + '</div>'))
        self.pay_page(row, zone, sim)

    def linkpayment_checkout(self, row):
        link = f'<a class="secondary-link" href="{e(self.href("result",id=row["id"]))}">거래 상태 보기</a>'
        if not LINKPAYMENT_TEST or row["sdk_started"]:
            zone = '결제사에서 거래 결과를 확인하고 있습니다.<small>완료 여부는 거래 상태에서 확인해 주세요.</small>' + link
            return self.pay_page(row, zone)
        self.pg_checkout_page = True
        zone = ('카드사 선택 · 카드 정보 입력 · 본인 인증'
                + self.form("pg-open",hidden("id",row["id"])+f'<button class="primary" id="lp-open" disabled>{row["amount"]:,}원 결제창 열기</button>')
                + '<small id="lp-status" role="status">결제 모듈을 불러오는 중입니다.</small>' + link)
        return self.pay_page(row, zone)

    def pg_open(self):
        row = self.order(self.form_data.get("id"))
        if not LINKPAYMENT_TEST or not row or not row["reference"].startswith("lp_test_"):
            return self.send("{}",403,mime="application/json; charset=utf-8")
        updated = DB.execute(
            "UPDATE payment SET sdk_started=1 WHERE id=? AND status='PENDING' AND sdk_started=0",
            (row["id"],),
        )
        if not updated.rowcount:
            return self.send("{}",409,mime="application/json; charset=utf-8")
        return self.send(json.dumps(PG.checkout_payload(row)),mime="application/json; charset=utf-8")

    def result(self):
        """성도 / 헌금 / 결과 · 완료 · 처리 중 · 실패 · 취소"""
        row = self.order(self.args.get("id"))
        if not row:
            return self.layout("내역을 찾을 수 없어요",f'<a href="{e(self.base)}/">교회 첫 화면으로</a>',"give",404)
        status = row["status"] if row["status"] in RESULTS else "PENDING"
        eyebrow, title, note = RESULTS[status]
        kind = "pending" if status in {"PENDING","READY"} else status.lower()
        body = (f'<div class="result-mark mark-{kind}" aria-hidden="true"></div>'
                f'<div class="heading"><p class="eyebrow" lang="en">{eyebrow}</p><h1>{title}</h1></div>'
                f'<div class="result-sum"><small>{e(self.church["name"])}</small><strong>{amount(row["amount"])}</strong></div>')
        when = ("결제 완료", fmt_time(row["paid_at"])) if row["paid_at"] else ("접수 시각", fmt_time(row["day"]))
        facts = [("헌금 항목",e(row["category"])),("이름",donor_label(row["donor_name"])),when,("상태",status_badge(status, member=True))]
        body += '<dl class="facts ruled">' + "".join(f'<div><dt>{k}</dt><dd>{v}</dd></div>' for k,v in facts) + '</dl>'
        if note:
            body += f'<p class="note">{note}</p>'
        if status in {"PENDING","READY"}:
            actions = (f'<a class="primary block" href="{e(self.href("result",id=row["id"]))}">거래 상태 확인</a>'
                       f'<a class="quiet block" href="{e(self.base)}/">교회 첫 화면으로</a>')
        elif status == "PAID":
            actions = (f'<a class="primary block" href="{e(self.base)}/">교회 첫 화면으로</a>'
                       f'<a class="quiet block" href="{e(self.href("give"))}">새 헌금</a>')
        else:
            actions = (f'<a class="primary block" href="{e(self.href("give",again=row["id"]))}">헌금 화면으로 돌아가기</a>'
                       '<small>입력하신 항목과 금액은 그대로 남아 있습니다.</small>')
        self.layout(title, body, "give", heading=False, actions=actions, width="narrow")

    def post(self, page):
        f = self.form_data
        if page in {"login","logout"}:
            if page == "logout":
                self.rotate("")
            else:
                role = f.get("role")
                defaults = {"admin":("ONGIL_ADMIN_PASSWORD","demo-admin"),"operator":("ONGIL_OPERATOR_PASSWORD","demo-operator")}
                if role not in defaults:
                    raise ValueError("계정 유형을 선택해 주세요.")
                key, default = defaults[role]
                if not hmac.compare_digest(f.get("password","").encode(),os.getenv(key,default).encode()):
                    raise ValueError("개발 계정 정보를 확인해 주세요.")
                self.rotate("operator" if role=="operator" else "admin:grace")
                if role == "admin":
                    return self.redirect("/c/grace/admin")
            return self.redirect(self.href("admin"))
        if page == "admin-save":
            return self.admin_save()
        if not self.church:
            raise ValueError("교회를 선택해 주세요.")
        cid = self.church["id"]
        owner = self.session["id"]
        if page == "pg-open":
            return self.pg_open()
        if page == "finish":
            row = self.order(f.get("id"))
            outcome = f.get("outcome")
            if row and row["reference"].startswith("kiosk_mock_"):
                if not KIOSK_TEST or outcome not in {"PAID","FAILED","CANCELLED"}:
                    raise ValueError("결제 결과를 확인할 수 없습니다.")
                if row["status"] == "PENDING":
                    event = {"reference":row["reference"],"amount":row["amount"],"status":outcome}
                    status = KIOSK_PG.verify_event(row,event,KIOSK_PG.sign_event(event))
                    DB.execute("UPDATE payment SET status=? WHERE id=? AND status='PENDING'",(status,row["id"]))
                    if status == "PAID":
                        DB.execute("UPDATE donation SET paid_at=? WHERE id=?", (now(),row["id"]))
                return self.redirect(self.href("kiosk",step="done",id=row["id"]))
            if LINKPAYMENT_TEST or not row or row["reference"].startswith("lp_test_") or outcome not in {"PAID","FAILED","CANCELLED","PENDING"}:
                raise ValueError("결제 결과를 확인할 수 없습니다.")
            # PENDING = Mock 결제의 '승인 결과 지연': 상태를 바꾸지 않고 처리 중 화면으로 이동합니다.
            if row["status"] == "PENDING" and outcome != "PENDING":
                event = {"reference":row["reference"],"amount":row["amount"],"status":outcome}
                status = PG.verify_event(row,event,PG.sign_event(event))
                DB.execute("UPDATE payment SET status=? WHERE id=? AND status='PENDING'",(status,row["id"]))
                if status == "PAID":
                    DB.execute("UPDATE donation SET paid_at=? WHERE id=?", (now(),row["id"]))
            return self.redirect(self.href("result",id=row["id"]))
        if page == "kiosk-pay":
            if not KIOSK_TEST:
                raise ValueError("키오스크 결제는 아직 연결되지 않았습니다.")
            try:
                total = int(f.get("amount",""))
            except ValueError:
                total = 0
            if not 1000 <= total <= 100000000:
                raise ValueError("1,000원부터 100,000,000원까지 입력해 주세요.")
            category = one("SELECT * FROM category WHERE id=? AND church=? AND active=1",f.get("category"),cid)
            if not category:
                raise ValueError("사용 가능한 헌금 항목을 선택해 주세요.")
            request = f.get("request","")
            if not re.fullmatch(r"[A-Za-z0-9_-]{32}",request):
                raise ValueError("헌금 내용을 다시 확인해 주세요.")
            if not hmac.compare_digest(review_signature(owner,cid,category["id"],total,request,"").encode(), f.get("signature","").encode()):
                raise ValueError("변경된 헌금 내용을 다시 확인해 주세요.")
            existing = one("SELECT id FROM payment WHERE owner=? AND request=?",owner,request)
            if existing:
                return self.redirect(self.href("kiosk",step="pay",id=existing["id"]))
            oid = secrets.token_urlsafe(24)
            DB.execute("INSERT INTO donation (id,church,category,amount,owner,day,lookup,donor_name) VALUES (?,?,?,?,?,?,?,?)",
                       (oid,cid,category["title"],total,owner,now(),secrets.token_urlsafe(24),""))
            reference = "kiosk_" + KIOSK_PG.create_session({"id":oid,"amount":total,"church":cid})["reference"]
            DB.execute("INSERT INTO payment (id,request,owner,reference,status) VALUES (?,?,?,?,?)",(oid,request,owner,reference,"PENDING"))
            return self.redirect(self.href("kiosk",step="pay",id=oid))
        if page == "edit":
            # 확인 화면의 '수정' · '입력으로': 입력값을 그대로 들고 입력 화면으로 돌아갑니다.
            return self.give({k: f.get(k,"") for k in ("category","amount","name")})
        if page not in {"review","start"}:
            raise ValueError("지원하지 않는 요청입니다.")
        try:
            total = int(f.get("amount","").replace(",",""))
        except ValueError:
            total = 0
        if not 1000 <= total <= 100000000:
            raise ValueError("1,000원부터 100,000,000원까지 입력해 주세요.")
        category = one("SELECT * FROM category WHERE id=? AND church=? AND active=1",f.get("category"),cid)
        if not category:
            raise ValueError("사용 가능한 헌금 항목을 선택해 주세요.")
        donor_name = f.get("name","")
        if len(donor_name) > 240:
            raise ValueError("이름은 240자 이내로 입력해 주세요.")
        if page == "review":
            # 성도 / 헌금 / 내용 확인 — 금액을 크게, 교회·항목·이름을 아래 표로. '수정'은 입력값을 들고 입력 화면으로.
            keep = hidden("category",category["id"])+hidden("amount",total)+hidden("name",donor_name)
            def edit_button(label="수정", cls="edit"):
                return self.form("edit", keep + f'<button class="{cls}">{label}</button>')
            who = (e(donor_name) if donor_name else '익명<small>이름을 입력하지 않았습니다</small>')
            body = ('<div class="confirm"><div class="confirm-amount"><small>헌금 금액</small>'
                    f'<strong>{total:,}<span>원</span></strong><em>{korean_amount(total)}</em></div>'
                    '<dl class="facts">'
                    f'<div><dt>교회</dt><dd>{e(self.church["name"])}</dd><span></span></div>'
                    f'<div><dt>헌금 항목</dt><dd>{e(category["title"])}</dd>{edit_button()}</div>'
                    f'<div><dt>이름</dt><dd>{who}</dd>{edit_button()}</div></dl></div>')
            pg_name = "링크페이먼트 카드 결제창" if LINKPAYMENT_TEST else "테스트 결제창(Mock)"
            body += f'<p class="note lock">다음 단계에서 {pg_name}이 열립니다. 카드 정보는 온길에 저장되지 않으며, 금액은 결제창에서 바꿀 수 없습니다.</p>'
            request = secrets.token_urlsafe(24)
            fields = keep + hidden("request",request)
            fields += hidden("signature",review_signature(owner,cid,category["id"],total,request,donor_name))
            actions = self.form("start",fields+f'<button class="primary block">{total:,}원 결제하기</button>') + edit_button("금액 수정","quiet block")
            back = self.form("edit", keep + '<button class="back">' + ico("chev-l", "sm") + '입력으로</button>')
            return self.layout("헌금 내용을 확인해 주세요",body,"give",step=2,eyebrow="Confirmation",actions=actions,back=back,width="narrow")
        request = f.get("request","")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32}",request):
            raise ValueError("헌금 내용을 다시 확인해 주세요.")
        signature = review_signature(owner,cid,category["id"],total,request,donor_name)
        if not hmac.compare_digest(signature.encode(),f.get("signature","").encode()):
            raise ValueError("변경된 헌금 내용을 다시 확인해 주세요.")
        existing = one("SELECT id FROM payment WHERE owner=? AND request=?",owner,request)
        if existing:
            return self.redirect(self.href("checkout",id=existing["id"]))
        oid = secrets.token_urlsafe(24)
        DB.execute(
            "INSERT INTO donation (id,church,category,amount,owner,day,lookup,donor_name) VALUES (?,?,?,?,?,?,?,?)",
            (oid,cid,category["title"],total,owner,now(),secrets.token_urlsafe(24),donor_name),
        )
        DB.execute("INSERT INTO payment (id,request,owner,reference,status) VALUES (?,?,?,?,?)",(oid,request,owner,"","READY"))
        pg_session = PG.create_session({"id":oid,"amount":total,"church":cid})
        DB.execute("UPDATE payment SET status='PENDING',reference=? WHERE id=?",(pg_session["reference"],oid))
        return self.redirect(self.href("checkout",id=oid))

    # ───────────── 관리자 (Figma: 02 관리자 PC 1440 · 모바일 390) ─────────────
    def admin(self):
        role = self.session["role"]
        if not role:
            return self.admin_login()
        if not self.church:
            return self.admin_home(role)
        cid = self.church["id"]
        if role not in {"operator","admin:"+cid}:
            return self.layout("교회 관리 권한이 없어요",card('<a href="/admin">관리할 교회 선택</a>'),status=403,admin=True)
        sec = ""
        if self.args.get("tab") == "settings":
            sec = self.args.get("sec","info")
            sec = sec if sec in dict(ADMIN_SECTIONS) else "info"
        shell = self.admin_shell(sec)
        foot = (f'<footer class="adm-mfoot"><span>{"서비스 운영자" if role == "operator" else "교회 관리자"}</span>'
                + self.form("logout",'<button class="linkish">로그아웃</button>') + '</footer>')
        if not sec:
            return self.layout("헌금 내역", self.admin_donations(cid) + foot, admin=True, side=shell, heading=False)
        self.layout("교회 설정", self.admin_settings(sec) + foot, admin=True, side=shell, heading=False)

    def admin_login(self):
        """관리자 / 로그인 — PC: 왼쪽 교회 소개 · 오른쪽 로그인 카드, 모바일: 한 단"""
        c = self.church
        grace = one("SELECT name FROM church WHERE id='grace'")
        title = f'{e(c["name"])}<br>관리자' if c else '온길<br>교회 관리자'
        plate = (f'<figure class="plate"><div class="frame"><img src="{e(c["cover"])}" alt="" referrerpolicy="no-referrer"></div></figure>'
                 if c and c["cover"] else "")
        options = (f'<option value="admin">{e(grace["name"] if grace else "교회")} 관리자</option>'
                   '<option value="operator">서비스 운영자</option>')
        form = self.form("login",
            '<h2>로그인</h2>'
            f'<label>계정 유형<select name="role">{options}</select></label>'
            + field("비밀번호","password",required=True,kind="password")
            + '<button class="primary block">로그인</button>'
            '<p class="hint">로그인하면 헌금 내역이 먼저 열립니다. 비밀번호를 잊으셨다면 온길 고객센터로 문의해 주세요.</p>'
            '<p class="dev-note">테스트 기간에는 개발용 계정으로 로그인합니다. 계정 정보는 README에 있습니다.</p>')
        body = (f'<div class="adm-login"><div class="intro"><p class="eyebrow" lang="en">Ongil · Church admin</p><h1>{title}</h1>{plate}</div>'
                f'<div class="panel"><section class="login-card">{form}</section></div></div>')
        self.layout("관리자 로그인", body, admin=True, heading=False, chrome=False, width="login")

    def admin_home(self, role):
        """교회 목록 (서비스 운영자 · 여러 교회 관리) — 시안 범위 밖, 같은 디자인 언어로 단순하게"""
        logout = self.form("logout",'<button class="linkish">로그아웃</button>')
        churches = [c for c in query("SELECT * FROM church ORDER BY name") if role in {"operator","admin:"+c["id"]}]
        links = "".join(f'<a class="item" href="/c/{c["id"]}/admin">{self.badge(c)}<b>{e(c["name"])}</b>{ico("chev-r","chev")}</a>' for c in churches)
        body = self.admin_head("Church list","관리할 교회") + card(links or '<p class="muted">관리할 수 있는 교회가 없습니다.</p>')
        if role == "operator":
            add = self.admin_form("add-church",field("교회 이름","name",required=True)+field("주소에 사용할 영문 ID","id",required=True))
            editions = "".join(self.admin_form("translation",hidden("id",t["id"])+field("번역본 이름","name",t["name"],True)+field("라이선스","license",t["license"],True)+field("출처 URL","source",t["source"],True,kind="url")) for t in query("SELECT * FROM translation") if t["id"] in METADATA)
            body += card('<h2>교회 추가</h2>'+add)+card('<h2>성경 번역본</h2>'+editions)+card(f'<h2>결제 모드</h2><p>{PG_MODE_LABEL}</p>')
        body += f'<footer class="adm-homefoot"><span>{"서비스 운영자" if role == "operator" else "교회 관리자"}</span>{logout}</footer>'
        return self.layout("관리할 교회",body,admin=True,heading=False)

    @staticmethod
    def admin_head(eyebrow, title, right=""):
        return (f'<div class="adm-head"><div class="heading"><p class="eyebrow" lang="en">{e(eyebrow)}</p><h1>{e(title)}</h1></div>'
                f'{right}</div>')

    def admin_shell(self, sec):
        """PC: 왼쪽 메뉴(온길 · 교회 · 헌금 내역 · 교회 설정 5개 · 계정), 모바일: 위쪽 바"""
        c = self.church
        role = self.session["role"]
        here = ' aria-current="page"'
        denom = (c["meta"] or "").split("·")[0].strip()
        subs = "".join(f'<a href="{e(self.href("admin",tab="settings",sec=key))}"{here if key == sec else ""}>{label}</a>' for key, label in ADMIN_SECTIONS)
        side = (
            '<aside class="adm-side"><a class="adm-brand" href="/admin"><b>온길</b><small>교회 관리자</small></a>'
            f'<div class="adm-church">{self.badge(c, "lg")}<div><b>{e(c["name"])}</b>' + (f'<small>{e(denom)}</small>' if denom else "") + '</div></div>'
            '<nav class="adm-nav" aria-label="관리 메뉴">'
            f'<a class="main" href="{e(self.href("admin"))}"{here if not sec else ""}>{ico("history")}헌금 내역</a>'
            f'<p class="adm-nav-label">교회 설정</p>{subs}</nav>'
            f'<div class="adm-account"><b>{"서비스 운영자" if role == "operator" else "교회 관리자"}</b>'
            f'<small>{e(c["name"])}</small>'
            f'<a href="{e(self.base)}/" target="_blank" rel="noopener">성도 화면 보기</a>'
            + ('<a href="/admin">교회 목록</a>' if role == "operator" else "")
            + self.form("logout",'<button class="linkish">로그아웃</button>') + '</div></aside>'
        )
        other_url, other_label = (self.href("admin"),"헌금 내역") if sec else (self.href("admin",tab="settings",sec="info"),"설정")
        mbar = (f'<header class="adm-mbar"><a class="church-link" href="{e(self.href("admin"))}">{self.badge(c)}<b>{e(c["name"])}</b></a>'
                f'<a class="change" href="{e(other_url)}">{other_label}</a></header>')
        return side + mbar

    # ── 헌금 내역 ──
    def admin_donations(self, church):
        a = self.args
        name = a.get("name","").strip()[:40]
        period = a.get("period","")
        period = period if period in dict(ADMIN_PERIODS) else ""
        start, end = a.get("from",""), a.get("to","")
        start = start if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) else ""
        end = end if re.fullmatch(r"\d{4}-\d{2}-\d{2}", end) else ""
        if start or end:
            period = ""          # 날짜를 직접 넣으면 기간 선택보다 우선
        today = datetime.now(KST).date()
        q_start, q_end = start, end
        if period:
            first = {"today":today,"7d":today-timedelta(days=6),"month":today.replace(day=1),"30d":today-timedelta(days=29)}[period]
            q_start, q_end = first.isoformat(), today.isoformat()
        state = a.get("status","")
        state = state if state in {"PAID","PENDING","FAILED","CANCELLED"} else ""

        conditions, params = ["d.church=?"], [church]
        if name == "익명":
            conditions.append("d.donor_name=''")
        elif name:
            conditions.append("instr(lower(d.donor_name), lower(?)) > 0")
            params.append(name)
        if q_start:
            conditions.append("substr(d.day,1,10) >= ?")
            params.append(q_start)
        if q_end:
            conditions.append("substr(d.day,1,10) <= ?")
            params.append(q_end)
        if state == "PENDING":
            conditions.append("p.status IN ('PENDING','READY')")
        elif state:
            conditions.append("p.status=?")
            params.append(state)
        source = " FROM donation d JOIN payment p USING(id) WHERE " + " AND ".join(conditions)
        totals = one("SELECT COUNT(*) AS count, COALESCE(SUM(CASE WHEN p.status='PAID' THEN d.amount ELSE 0 END),0) AS paid,"
                     " COALESCE(SUM(p.status='PAID'),0) AS paid_count" + source, *params)
        page_size = 10
        pages = max(1, (totals["count"] + page_size - 1) // page_size)
        requested = a.get("page","")
        page = min(pages, max(1, int(requested))) if re.fullmatch(r"[0-9]{1,9}", requested) else 1
        rows = query("SELECT d.*, p.status" + source + " ORDER BY d.day DESC, d.rowid DESC LIMIT ? OFFSET ?",
                     *params, page_size, (page - 1) * page_size)

        def link(**changes):
            values = {"name":name,"period":period,"from":start,"to":end,"status":state}
            values.update(changes)
            kept = {k: v for k, v in values.items() if v}   # 'page'는 href()의 인자 이름과 겹쳐서 직접 붙입니다
            return e(self.href("admin") + ("?" + urlencode(kept) if kept else ""))

        chips = []
        if name:
            chips.append((f"이름: {name}", link(name="")))
        if period:
            chips.append((f"기간: {dict(ADMIN_PERIODS)[period]}", link(period="")))
        elif start or end:
            chips.append((f"기간: {(start or '처음').replace('-','.')} ~ {(end or '오늘').replace('-','.')}", link(**{"from":"","to":""})))
        if state:
            chips.append((f"상태: {MEMBER_STATUSES[state]}", link(status="")))
        reset = e(self.href("admin"))

        def options(items, current):
            return "".join(f'<option value="{k}"{" selected" if k == current else ""}>{label}</option>' for k, label in items)
        period_opts = options(ADMIN_PERIODS, period)
        status_opts = options([("","전체")] + [(k, MEMBER_STATUSES[k]) for k in ["PAID","PENDING","FAILED","CANCELLED"]], state)

        # 머리 — PC: 마지막 갱신 + 새로고침, 모바일: 필터 버튼
        stamp_now = datetime.now(KST).strftime("%Y.%m.%d %H:%M")
        refresh = f'<div class="adm-refresh"><span>마지막 갱신 {stamp_now}</span><a class="ghost sm" href="{e(self.path)}">새로고침</a></div>'
        mfilter = (
            f'<details class="adm-mfilter"{" open" if a.get("f") == "1" else ""}><summary>필터{" · " + str(len(chips)) if chips else ""}</summary>'
            f'<form method="get" action="{reset}">'
            + (hidden("from", start) if start else "") + (hidden("to", end) if end else "")
            + f'<label>헌금자 이름<input type="search" name="name" value="{e(name)}" maxlength="40" placeholder="이름, 익명"></label>'
            f'<label>기간<select name="period">{period_opts}</select></label>'
            f'<label>결제 상태<select name="status">{status_opts}</select></label>'
            f'<div class="pair"><button class="primary">조회</button><a class="ghost" href="{reset}">초기화</a></div></form></details>'
        )
        head = f'<div class="adm-listhead">{self.admin_head("Offering ledger", "헌금 내역", refresh)}{mfilter}</div>'

        filters = (
            f'<form class="adm-filter" method="get" action="{reset}">'
            f'<label class="f-name">헌금자 이름<input type="search" name="name" value="{e(name)}" maxlength="40" placeholder="이름, 익명"></label>'
            f'<label class="f-period">기간 (접수 기준)<select name="period">{period_opts}</select></label>'
            f'<div class="f-range"><span>시작일 ~ 종료일</span><div><input type="date" name="from" value="{start}" aria-label="시작일">'
            f'<i aria-hidden="true">~</i><input type="date" name="to" value="{end}" aria-label="종료일"></div></div>'
            f'<label class="f-status">결제 상태<select name="status">{status_opts}</select></label>'
            f'<button class="primary sm">조회</button><a class="quiet sm" href="{reset}">초기화</a></form>'
        )
        chip_html = "".join(f'<a class="adm-chip" href="{url}" aria-label="{e(label)} 조건 해제">{e(label)}<span aria-hidden="true">×</span></a>' for label, url in chips)
        summary = (
            '<div class="adm-summary"><dl>'
            f'<div><dt>조회</dt><dd>{totals["count"]:,}건</dd></div>'
            f'<div class="sum"><dt>결제 완료 합계</dt><dd>{totals["paid"]:,}원</dd></div>'
            f'<div class="basis"><dt class="sr">기준</dt><dd>완료 {totals["paid_count"]:,}건 기준</dd></div></dl>'
            + (f'<div class="adm-chips">{chip_html}</div>' if chips else "") + '</div>'
        )

        def stamp(value):
            return e((value or "")[:19].replace("-", "."))

        waiting = {"PENDING":"승인 대기","READY":"승인 대기","FAILED":"승인 거절","CANCELLED":"결제 취소"}
        trs, cards = [], []
        for row in rows:
            who = row["donor_name"]
            if who is None:
                donor = '<span class="d-none">이름 기록 없음</span>'
            elif not who:
                donor = '<span class="d-anon">익명</span>'
            else:
                donor = f'<b>{e(who)}</b>'
            status = row["status"]
            done = stamp(row["paid_at"]) if row["paid_at"] else waiting.get(status, "—")
            when = (f'<dl class="d-when"><div><dt>완료</dt><dd>{done}</dd></div>'
                    f'<div><dt>접수</dt><dd>{stamp(row["day"])}</dd></div></dl>')
            cls = "is-" + e(status.lower())
            badge = status_badge(status, member=True)
            trs.append(f'<tr class="{cls}"><td class="d-donor">{donor}</td><td>{when}</td><td>{e(row["category"])}</td>'
                       f'<td class="num d-amt">{row["amount"]:,}</td><td>{badge}</td></tr>')
            cards.append(f'<li class="{cls}"><div class="c-top">{donor}<span class="d-amt">{row["amount"]:,}원</span></div>'
                         f'<div class="c-mid">{badge}<span>{e(row["category"])}</span></div>{when}</li>')
        if trs:
            table = ('<div class="adm-tablewrap"><table class="adm-table" aria-label="헌금 내역 · 시각은 한국 시간">'
                     '<thead><tr><th scope="col">헌금자</th><th scope="col">일시<small>접수 최신순</small></th><th scope="col">헌금 항목</th>'
                     '<th scope="col" class="num">금액<small>원</small></th><th scope="col">결제 상태</th></tr></thead>'
                     f'<tbody>{"".join(trs)}</tbody></table></div>'
                     f'<ul class="adm-cards" aria-label="헌금 내역">{"".join(cards)}</ul>')
            first = (page - 1) * page_size + 1
            last = first + len(rows) - 1
            lo = max(1, min(page - 3, pages - 6))
            nums = "".join(f'<a class="num{" cur" if n == page else ""}" href="{link(page=n)}"{" aria-current=" + chr(34) + "page" + chr(34) if n == page else ""}>{n}</a>'
                           for n in range(lo, min(pages, lo + 6) + 1))
            prev_l = f'<a href="{link(page=page-1)}">{ico("chev-l","sm")}이전</a>' if page > 1 else f'<span class="off">{ico("chev-l","sm")}이전</span>'
            next_l = f'<a href="{link(page=page+1)}">다음{ico("chev-r","sm")}</a>' if page < pages else f'<span class="off">다음{ico("chev-r","sm")}</span>'
            pager = (f'<div class="adm-pager"><span class="info">{first}–{last} / {totals["count"]:,}건</span>'
                     f'<nav aria-label="페이지 이동">{prev_l}{nums}<span class="mcur">{page} / {pages}</span>{next_l}</nav></div>')
            content = table + pager
        elif chips:
            content = ('<section class="adm-empty"><h2>조건에 맞는 헌금 내역이 없습니다</h2>'
                       f'<p>{e(", ".join(label for label, _ in chips))} 조건으로 찾은 결과가 없습니다. 이름의 일부만 입력하거나 기간을 넓혀 보세요.</p>'
                       f'<a class="ghost sm" href="{reset}">조건 초기화</a></section>')
        else:
            content = '<section class="adm-empty"><h2>아직 접수된 헌금이 없습니다</h2><p>성도가 헌금하면 이곳에 바로 표시됩니다.</p></section>'
        legend = ('<p class="adm-legend"><span><b>완료</b> 카드 승인이 확인된 시각</span><span><b>접수</b> 성도가 결제를 시작한 시각</span>'
                  '<span><b>익명</b> 헌금자가 이름을 비운 거래</span><span><b>이름 기록 없음</b> 이름을 저장하기 전의 거래</span>'
                  '<span><b>합계</b> 결제 완료 건만 더하며 처리 중·실패·취소는 제외</span>'
                  f'<span><b>결제 모드</b> {PG_MODE_LABEL}</span></p>')
        return head + filters + summary + content + legend

    # ── 교회 설정 ──
    def admin_settings(self, sec):
        here = ' aria-current="page"'
        tabs = ('<nav class="adm-tabs" aria-label="교회 설정">' + "".join(
            f'<a href="{e(self.href("admin",tab="settings",sec=key))}"{here if key == sec else ""}>{label}</a>' for key, label in ADMIN_SECTIONS) + '</nav>')
        pane = {"info":self.admin_info,"logo":self.admin_logo,"site":self.admin_site,
                "categories":self.admin_categories,"notices":self.admin_notices}[sec]()
        pc_only = ('<section class="adm-pconly adm-card"><h2>PC에서 편집해 주세요</h2>'
                   '<p>교회 설정은 표와 미리보기가 함께 있어 1024px 이상 화면에서 편집하도록 했습니다. 모바일에서는 헌금 내역 확인을 우선합니다.</p>'
                   f'<a class="ghost block" href="{e(self.href("admin"))}">헌금 내역으로</a></section>')
        return self.admin_head("Church settings","교회 설정") + pc_only + f'<div class="adm-settings">{tabs}{pane}</div>'

    def admin_post(self, op, body, extra=""):
        return f'<form method="post" action="{e(self.href("admin-save"))}"{extra}>{hidden("csrf",self.session["csrf"])}{hidden("op",op)}{body}</form>'

    def admin_info(self):
        c = self.church

        def inp(label, key, wide=False, optional=False, multiline=False, preview=False):
            pv = f' data-preview="{key}"' if preview else ""
            opt = '<span class="optional">선택</span>' if optional else ""
            control = (f'<textarea name="{key}" maxlength="2000" rows="4">{e(c[key])}</textarea>' if multiline
                       else f'<input name="{key}" value="{e(c[key])}" maxlength="240"{pv}{" required" if key == "name" else ""}>')
            return f'<label class="{"full" if wide else ""}">{label}{opt}{control}</label>'
        form = self.admin_post("church",
            '<div class="adm-grid">'
            + inp("교회 이름","name",preview=True) + inp("영문 소개 문구","tagline",optional=True,preview=True)
            + inp("교단 · 지역","meta",wide=True,optional=True,preview=True)
            + inp("주소","address",wide=True,optional=True) + inp("담임목사 · 연락처","contact",wide=True,optional=True)
            + '<h3 class="full">교회 첫 화면</h3><p class="hint full">비워 둔 항목은 성도 화면에 표시되지 않습니다.</p>'
            + inp("이번 주 말씀","verse",wide=True,optional=True,multiline=True)
            + inp("말씀 출처 (예: 시편 121:1–2)","verse_ref",optional=True) + inp("대표 그림 주소 (https://)","cover",optional=True)
            + inp("예배 안내 (한 줄에 하나, 예: 주일 1부 예배 | 오전 9:00)","services",wide=True,optional=True,multiline=True)
            + '</div><div class="adm-actions"><button class="primary sm">변경 내용 저장</button><button class="quiet sm" type="reset">되돌리기</button></div>'
            + hidden("cover_caption", c["cover_caption"]))
        tagline, meta = c["tagline"], c["meta"]
        preview = ('<aside class="adm-card"><h2>성도 화면 미리보기</h2>'
                   '<p class="hint">교회 이름과 영문 소개 문구는 첫 화면 표지에, 주소와 연락처는 첫 화면 하단에 표시됩니다.</p>'
                   f'<div class="adm-pv"><p class="eyebrow" lang="en" data-pv="tagline"{"" if tagline else " hidden"}>{e(tagline)}</p>'
                   f'<b data-pv="name">{e(c["name"])}</b><small data-pv="meta"{"" if meta else " hidden"}>{e(meta)}</small></div></aside>')
        return f'<div class="adm-split"><div>{form}</div>{preview}</div>'

    def admin_logo(self):
        c = self.church
        now_text = "등록된 로고를 성도 화면과 관리자 메뉴에 쓰고 있습니다." if c["logo"] else "로고가 없으면 교회 이름 첫 글자로 표시합니다."
        clear = self.admin_post("logo", hidden("logo","") + '<button class="quiet sm">로고 지우기</button>') if c["logo"] else ""
        form = self.admin_post("logo",
            '<div class="adm-drop"><b>로고 이미지 주소를 입력해 주세요</b>'
            '<small>https://로 시작하는 PNG·SVG 주소 · 정사각형 512×512px 이상 권장</small>'
            f'<label class="sr-label"><span class="sr">로고 이미지 주소</span><input type="url" name="logo" value="{e(c["logo"])}" maxlength="240" placeholder="https://"></label>'
            '<button class="ghost sm">저장</button>'
            '<small class="later">파일을 직접 올리는 기능은 정식 서버로 옮긴 뒤 제공할 예정입니다.</small></div>')
        left = (f'<section class="adm-card"><h2>현재 로고</h2><div class="adm-logo-now">{self.badge(c, "xl")}'
                f'<div><p>{now_text}</p><small>사용 위치 · 성도 화면 상단, 관리자 메뉴, 결제창 가맹점 표시</small>{clear}</div></div>{form}</section>')
        rules = ('<aside class="adm-card"><h2>표시 규칙</h2><ul class="adm-rules">'
                 '<li>상단 바 32px, 메뉴 40px 크기로 줄여 씁니다.</li><li>모서리는 4px로 다듬어 원형으로 자르지 않습니다.</li>'
                 '<li>배경이 투명한 로고는 아이보리 바탕 위에 놓입니다.</li></ul></aside>')
        return f'<div class="adm-split">{left}{rules}</div>'

    def admin_site(self):
        site = self.church["site"]
        check = f'<a class="ghost sm" href="{e(site)}" target="_blank" rel="noopener noreferrer">연결 확인</a>' if site else ""
        form = self.admin_post("site",
            f'<label>주소 (https://로 시작)<input type="url" name="site" value="{e(site)}" maxlength="240" placeholder="https://"></label>'
            '<p class="hint">성도 화면 하단 메뉴 ‘교회사이트’를 누르면 이 주소가 새 창으로 열립니다. 비워 두면 ‘아직 등록하지 않았습니다’ 안내가 나옵니다.</p>'
            f'<div class="adm-actions"><button class="primary sm">저장</button>{check}</div>')
        host = urlsplit(site).hostname if site else ""
        shown = ('<aside class="adm-card"><h2>성도 화면 표시</h2>'
                 f'<p class="adm-url">{e(host) if host else "등록된 주소 없음"}</p>'
                 '<p class="hint">교회사이트 메뉴 · 새 창 열림 · 온길 밖으로 이동 안내 포함</p></aside>')
        return f'<div class="adm-split"><section class="adm-card"><h2>공식 홈페이지 주소</h2>{form}</section>{shown}</div>'

    def admin_categories(self):
        cid = self.church["id"]
        items = query("SELECT * FROM category WHERE church=? ORDER BY sort,id", cid)
        rows = ""
        for i, c in enumerate(items):
            up = "" if i else " disabled"
            down = "" if i < len(items) - 1 else " disabled"
            rows += self.admin_post("category",
                '<button class="sr" tabindex="-1">저장</button>' + hidden("id", c["id"])
                + f'<span class="n">{i + 1}</span>'
                f'<input class="inline strong" name="title" value="{e(c["title"])}" maxlength="40" required aria-label="{i + 1}번 항목 이름">'
                f'<input class="inline" name="note" value="{e(c["note"])}" maxlength="80" placeholder="—" aria-label="{i + 1}번 항목 설명">'
                f'<label class="chk"><input type="checkbox" name="active" value="1"{" checked" if c["active"] else ""}><span class="sr">사용</span></label>'
                f'<span class="move"><button name="move" value="up" aria-label="위로"{up}>{ico("up")}</button>'
                f'<button name="move" value="down" aria-label="아래로"{down}>{ico("down")}</button></span>',
                ' class="cat-row" data-autosave')
        if self.args.get("add") == "1" or not items:
            rows += self.admin_post("category",
                f'<span class="n">{len(items) + 1}</span>'
                '<input class="inline strong" name="title" maxlength="40" required placeholder="새 항목 이름" aria-label="새 항목 이름" autofocus>'
                '<input class="inline" name="note" maxlength="80" placeholder="설명 (선택)" aria-label="새 항목 설명">'
                '<label class="chk"><input type="checkbox" name="active" value="1" checked><span class="sr">사용</span></label>'
                '<span class="move"><button class="primary sm">추가</button></span>',
                ' class="cat-row new" id="new"')
        add = f'<a class="ghost sm" href="{e(self.href("admin",tab="settings",sec="categories",add="1"))}#new">항목 추가</a>'
        return ('<section class="adm-card adm-wide"><div class="card-head"><h2>헌금 항목</h2>' + add + '</div>'
                '<p class="hint">‘사용’을 끈 항목은 성도 헌금 화면에서 숨겨지고, 이미 받은 내역은 그대로 남습니다. 순서는 성도 화면의 표시 순서입니다. '
                '이름·설명은 고친 뒤 Enter를 누르거나 칸을 벗어나면 저장됩니다.</p>'
                '<div class="cat-table"><div class="cat-row head" aria-hidden="true"><span>순서</span><span>항목 이름</span><span>설명</span><span>사용</span><span>순서 변경</span></div>'
                f'{rows}</div></section>')

    def admin_notices(self):
        cid = self.church["id"]
        items = query("SELECT * FROM notice WHERE church=? ORDER BY day DESC,id DESC", cid)
        edit = self.args.get("edit","")
        chosen = None
        if edit != "new":
            chosen = next((n for n in items if str(n["id"]) == edit), items[0] if items else None)
        trs = ""
        for n in items:
            on = chosen is not None and n["id"] == chosen["id"]
            day = fmt_day(n["day"]).split(" ")
            state = '<span class="status status-paid">게시 중</span>' if n["public"] else '<span class="status status-cancelled">임시저장</span>'
            tag = f'<span class="tagbox">{e(n["tag"])}</span>' if n["tag"] else ""
            trs += (f'<tr{" class=" + chr(34) + "on" + chr(34) if on else ""}><td class="day">{day[0]}<br>{day[1] if len(day) > 1 else ""}</td><td>{tag}</td>'
                    f'<td><a href="{e(self.href("admin",tab="settings",sec="notices",edit=n["id"]))}"{" aria-current=" + chr(34) + "true" + chr(34) if on else ""}>{e(n["title"])}</a></td><td>{state}</td></tr>')
        table = ('<table class="adm-table ntable"><thead><tr><th scope="col">게시일</th><th scope="col">분류</th><th scope="col">제목</th><th scope="col">상태</th></tr></thead>'
                 f'<tbody>{trs}</tbody></table>') if items else '<p class="muted">아직 공지가 없습니다. ‘새 공지 작성’으로 첫 공지를 올려 보세요.</p>'
        public_count = sum(1 for n in items if n["public"])
        left = (f'<div><div class="list-head"><span class="muted">게시 {public_count}건 · 전체 {len(items)}건</span>'
                f'<a class="primary sm" href="{e(self.href("admin",tab="settings",sec="notices",edit="new"))}">새 공지 작성</a></div>{table}</div>')
        current_tag = chosen["tag"] if chosen else ""
        tags = NOTICE_TAGS + ([current_tag] if current_tag and current_tag not in NOTICE_TAGS else [])
        tag_opts = '<option value="">분류 없음</option>' + "".join(f'<option{" selected" if t == current_tag else ""}>{e(t)}</option>' for t in tags)
        form = self.admin_post("notice",
            (hidden("id", chosen["id"]) if chosen else "")
            + f'<label>제목<input name="title" value="{e(chosen["title"]) if chosen else ""}" maxlength="240" required></label>'
            f'<label>분류<select name="tag">{tag_opts}</select></label>'
            f'<label>본문<textarea name="body" maxlength="4000" rows="8" required>{e(chosen["body"]) if chosen else ""}</textarea></label>'
            '<div class="adm-actions"><button class="primary sm" name="public" value="1">게시</button>'
            '<button class="ghost sm" name="public" value="0">임시저장</button></div>'
            '<p class="hint">임시저장한 공지는 성도 화면에 보이지 않습니다.</p>')
        editor = f'<aside class="adm-card"><h2>{"공지 편집" if chosen else "새 공지 작성"}</h2>{form}</aside>'
        return f'<div class="adm-split notices">{left}{editor}</div>'

    def admin_form(self, operation, body):
        return self.form("admin-save",hidden("op",operation)+body+'<button>저장</button>')

    def admin_save(self):
        f = self.form_data
        op = f.get("op")
        role = self.session["role"]
        cid = self.church["id"] if self.church else None
        if op in {"translation","add-church"} and role=="operator" and not cid:
            if op=="translation":
                DB.execute("UPDATE translation SET name=?,license=?,source=? WHERE id=?",(self.require("name"),self.require("license"),web_url(self.require("source")),f.get("id")))
            else:
                slug = f.get("id","")
                if not re.fullmatch(r"[a-z][a-z0-9-]{1,39}",slug) or one("SELECT id FROM church WHERE id=?",slug):
                    raise ValueError("사용 가능한 영문 교회 ID를 입력해 주세요.")
                DB.execute("INSERT INTO church(id,name) VALUES (?,?)",(slug,self.require("name")))
            return self.redirect("/admin")
        if not cid or role not in {"operator","admin:"+cid}:
            return self.layout("관리 권한이 없어요",card('<a href="/admin">관리자 로그인</a>'),status=403,admin=True)
        back = {"sec":"info"}
        if op=="church":
            DB.execute("UPDATE church SET name=? WHERE id=?",(self.require("name"),cid))
            profile = {}
            for key, _label in CHURCH_PROFILE:
                value = f.get(key, "")
                if len(value) > (2000 if key in {"verse","services"} else 240):
                    raise ValueError("교회 첫 화면 항목의 길이를 확인해 주세요.")
                # 대표 그림은 https 주소만 (기존에 저장된 값은 그대로 허용)
                profile[key] = value if key != "cover" or value == self.church["cover"] else web_url(value)
            DB.execute("UPDATE church SET " + ",".join(f"{k}=?" for k in profile) + " WHERE id=?", (*profile.values(), cid))
        elif op=="logo":
            DB.execute("UPDATE church SET logo=? WHERE id=?",(web_url(f.get("logo","").strip()),cid))
            back = {"sec":"logo"}
        elif op=="site":
            DB.execute("UPDATE church SET site=? WHERE id=?",(web_url(f.get("site","").strip()),cid))
            back = {"sec":"site"}
        elif op=="category":
            back = {"sec":"categories"}
            title, note = self.require("title",40), f.get("note","").strip()
            if len(note) > 80:
                raise ValueError("설명은 80자 이내로 입력해 주세요.")
            values = (title, note, int(f.get("active")=="1"))
            if f.get("id"):
                row = one("SELECT * FROM category WHERE id=? AND church=?",f["id"],cid)
                if not row:
                    raise ValueError("헌금 항목을 찾을 수 없습니다.")
                DB.execute("UPDATE category SET title=?,note=?,active=? WHERE id=? AND church=?",(*values,row["id"],cid))
                move = f.get("move")
                if move in {"up","down"}:
                    order = query("SELECT id,sort FROM category WHERE church=? ORDER BY sort,id",cid)
                    ids = [r["id"] for r in order]
                    i = ids.index(row["id"])
                    j = i - 1 if move == "up" else i + 1
                    if 0 <= j < len(ids):
                        ids[i], ids[j] = ids[j], ids[i]
                        for n, item in enumerate(ids, 1):
                            DB.execute("UPDATE category SET sort=? WHERE id=?",(n,item))
            else:
                top = one("SELECT COALESCE(MAX(sort),0) AS m FROM category WHERE church=?",cid)["m"]
                DB.execute("INSERT INTO category(title,note,active,church,sort) VALUES (?,?,?,?,?)",(*values,cid,top+1))
        elif op=="notice":
            tag = f.get("tag","")
            if len(tag) > 10:
                raise ValueError("분류는 10자 이내로 입력해 주세요.")
            values = (self.require("title"),self.require("body",4000),int(f.get("public")=="1"),tag)
            if f.get("id"):
                DB.execute("UPDATE notice SET title=?,body=?,public=?,tag=? WHERE id=? AND church=?",(*values,f["id"],cid))
                saved = f["id"]
            else:
                saved = DB.execute("INSERT INTO notice(title,body,public,tag,church,day) VALUES (?,?,?,?,?,?)",(*values,cid,now()[:10])).lastrowid
            back = {"sec":"notices","edit":saved}
        else:
            raise ValueError("지원하지 않는 설정입니다.")
        self.redirect(self.href("admin",tab="settings",**back))

    def require(self, name, limit=240):
        value = self.form_data.get(name,"")
        if not 0 < len(value) <= limit:
            raise ValueError("필수 항목과 입력 길이를 확인해 주세요.")
        return value


if __name__ == "__main__":
    port = int(os.getenv("PORT","8765"))
    print(f"온길 · http://127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1",port),App).serve_forever()
