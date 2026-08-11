#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prism_db.py — PRISM 정책연구 DB 수집·검색기 (공용 도구)

정책연구관리시스템(PRISM, prism.go.kr) 74,000여 건의 정부·지자체 **정책연구용역**을
공공데이터포털 오픈API(apis.data.go.kr/1741000/prism_v2)로 수집해
로컬 SQLite(FTS5)에 축적하고 전문검색한다.

★PRISM ≠ NTIS — PRISM은 행정수요 기반 **정책연구용역**(발주·계약·보고서),
NTIS는 **국가 R&D 과제**다. 두 모집단을 한 시계열에 섞으면 즉시 오류.

인증: ~/.prism/key (또는 환경변수 PRISM_API_KEY). 디코딩 키(끝이 ==)를 넣는다.
쿼터: 개발계정 일 1,000회 — 950회에서 자동 정지하고 다음 실행에서 이어받는다(resume).
의존: 표준 라이브러리만. FTS5 없으면 LIKE 검색으로 자동 강등.

서브커맨드:
  init                              스키마 생성(멱등)
  sweep [--from 1998] [--to 2026]   목록 전수 수집(연도 스윕). 약 100~120회 호출
  enrich [--sci] [--limit N]        상세 보강(과제ID당 1회). --sci = 과학기술 필터만
  files  [--sci] [--limit N] [--outdir P]   원문 파일 다운로드
  search <검색어> [--sci] [--org 기관] [--year YYYY] [--limit N]   로컬 전문검색
  show <research_id>                한 과제 전체 레코드
  stats                             DB 현황
  quota                             오늘 API 호출 수

린터(카드 §5와 1:1):
  - 상세 조회 실패 시 pnnMetaData_v2 폴백 + 사유 기록 (부분공개 ≠ 부재)
  - 계약방식 코드(D01…) 미매핑 경고
  - 공공누리 유형 미표기 = 재배포 금지 기본값
  - totalCount 대 실제 적재 건수 불일치 경고
  - 최근 연도 등록시차 경고
"""

import argparse
import json
import os
import re
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = "https://apis.data.go.kr/1741000/prism_v2"
HOME = Path.home() / ".prism"
DB_PATH = Path(os.environ.get("PRISM_DB_PATH") or (HOME / "prism.db"))
KEY_PATH = HOME / "key"
FILES_DIR = Path(os.environ.get("PRISM_FILES_DIR") or (HOME / "files"))

DAILY_LIMIT = 1000
SAFETY_STOP = 950          # 예비 50회를 남기고 정지
SLEEP_BETWEEN = 0.12       # 서버 예의

KST = timezone(timedelta(hours=9))

# ── 과학기술정책 필터 ──────────────────────────────────────────────
# BRM 대분류가 '과학기술'이거나, 아래 키워드가 제목·초록·주제어에 잡히면 sci=1.
# ※ ODA·국제개발은 사용자 지정으로 제외한다.
# ★단순 부분일치는 한국어에서 오탐을 대량 만든다(실측):
#   "AI" → AIS·CAIV·Aid for Trade (213건 중 다수) / "양자" → 兩者間·휴양자원·양자FTA (21건 대부분)
#   "실증" → 실증연구·실증적 분석(경험적 연구, 과학기술 아님) (121건 대부분)
#   그래서 라틴 약어는 단어경계를, 다의어는 뒤따르는 말을 요구한다.
SCI_KEYWORDS = [
    "과학기술", "연구개발", "알앤디", "기술개발", "기술혁신", "혁신정책",
    "인공지능", "빅데이터", "데이터경제", "디지털전환", "디지털 전환",
    "반도체", "이차전지", "배터리", "디스플레이", "바이오", "제약", "신약", "헬스케어",
    "우주", "위성", "발사체", "항공우주", "무인기", "드론", "로봇", "자율주행", "모빌리티",
    "수소", "재생에너지", "탄소중립", "기후기술", "원자력", "핵융합",
    "소재부품장비", "소부장", "나노", "첨단전략", "국가전략기술",
    "특허", "지식재산", "기술이전", "기술사업화", "산학협력", "연구인력", "이공계",
    "출연연", "과학관", "테스트베드", "규제샌드박스",
    "정보통신", "클라우드", "사이버보안", "정보보호",
    "메타버스", "블록체인", "디지털트윈", "스마트시티", "스마트공장",
    "과학문화", "과학교육", "소프트웨어", "플랫폼정부",
]
# 경계·문맥이 필요한 패턴 (위 목록과 합쳐 사용)
SCI_REGEX_EXTRA = [
    r"(?<![A-Za-z0-9])R&D(?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])AI(?![A-Za-z0-9])",        # AIS·CAIV·Aid 차단
    r"(?<![A-Za-z0-9])(?:ICT|DX|SW|5G|6G)(?![A-Za-z0-9])",
    r"양자\s*(?:컴퓨|기술|암호|정보|통신|산업|소자|센서|과학|역학|화학|얽힘|우위|내성)",  # 兩者間 차단
    r"실증\s*(?:사업|단지|센터|특례|시험|플랫폼|실험|랩|단계)",   # 실증연구(경험적 연구) 차단
]
# 오탐 억제 — 단독으로는 과학기술로 보지 않는 맥락
SCI_EXCLUDE_CONTEXT = ["ODA", "공적개발원조", "국제개발협력", "개발도상국"]

_SCI_RE = re.compile(
    "|".join([re.escape(k) for k in SCI_KEYWORDS] + SCI_REGEX_EXTRA), re.IGNORECASE)
_EXC_RE = re.compile("|".join(re.escape(k) for k in SCI_EXCLUDE_CONTEXT), re.IGNORECASE)

# ★한국 정부 문서에서 'AI'는 조류인플루엔자(Avian Influenza)인 경우가 흔하다 — 단어경계로는
#   못 거른다("AI 방역체계", "고병원성 AI"). 이 맥락에서는 AI 매치만 무효화하고
#   나머지 키워드로 다시 판정한다(빅데이터 등 진짜 신호가 있으면 그대로 살린다).
_AI_RE = re.compile(r"(?<![A-Za-z0-9])AI(?![A-Za-z0-9])", re.IGNORECASE)
_AI_VET_RE = re.compile(
    r"조류|인플루엔자|방역|가축|살처분|축산|양계|가금|구제역|철새|HPAI|농림축산|오리|산란계|종계")


# ── 공통 유틸 ─────────────────────────────────────────────────────
def _ssl_ctx():
    """기본 신뢰 CA가 비어 있으면 certifi로 보강. 검증은 끄지 않는다."""
    ctx = ssl.create_default_context()
    try:
        if ctx.cert_store_stats().get("x509_ca", 0) == 0:
            import certifi
            ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


SSL_CTX = _ssl_ctx()

# ── 원문 파일 서버 전용 SSL 컨텍스트 ─────────────────────────────
# ★api.prism.go.kr은 중간 인증서 대신 리프를 두 번 보낸다(체인 불완전). 시스템 openssl은
# 캐시된 중간 인증서로 통과하지만 파이썬 신뢰저장소에는 없어 CERTIFICATE_VERIFY_FAILED가 난다.
# 해결: 리프의 AIA가 가리키는 발급자(Sectigo DV, USERTrust 루트로 연결) 인증서를 받아
# 신뢰번들에 더한다. **검증을 끄지 않는다** — 서버가 빠뜨린 조각을 채워 정상 검증한다.
AIA_ISSUER_URL = "http://crt.sectigo.com/SectigoRSADomainValidationSecureServerCA.crt"
CA_BUNDLE = HOME / "ca_bundle.pem"


def _build_file_ssl_ctx():
    try:
        if not CA_BUNDLE.exists():
            HOME.mkdir(parents=True, exist_ok=True)
            import subprocess
            der = urllib.request.urlopen(AIA_ISSUER_URL, timeout=60).read()
            pem = subprocess.run(["openssl", "x509", "-inform", "DER"], input=der,
                                 capture_output=True, timeout=30).stdout.decode()
            if "BEGIN CERTIFICATE" not in pem:
                return SSL_CTX
            roots = ""
            try:
                import certifi
                roots = Path(certifi.where()).read_text(encoding="utf-8")
            except Exception:
                pass
            CA_BUNDLE.write_text(roots + "\n" + pem, encoding="utf-8")
        return ssl.create_default_context(cafile=str(CA_BUNDLE))
    except Exception:
        return SSL_CTX


def today_kst():
    return datetime.now(KST).strftime("%Y-%m-%d")


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def get_key():
    key = (os.environ.get("PRISM_API_KEY") or "").strip()
    if not key and KEY_PATH.exists():
        key = KEY_PATH.read_text(encoding="utf-8").strip()
    if not key:
        sys.exit(
            "인증키 없음 — ~/.prism/key 파일에 공공데이터포털 일반 인증키(Decoding, 끝이 ==)를 넣거나\n"
            "환경변수 PRISM_API_KEY를 설정하세요. 발급: https://www.data.go.kr/data/15080254/openapi.do"
        )
    return key


def is_sci(*texts, brm=""):
    """과학기술정책 관련 여부 판정."""
    blob = " ".join(t or "" for t in texts)
    if (brm or "").startswith("과학기술"):
        return True
    test = _AI_RE.sub(" ", blob) if _AI_VET_RE.search(blob) else blob
    if not _SCI_RE.search(test):
        return False
    # 키워드가 잡혔어도 ODA 맥락이 지배적이면 제외
    if _EXC_RE.search(blob) and not (brm or "").startswith("과학기술"):
        hits = len(_SCI_RE.findall(blob))
        if hits <= 1:
            return False
    return True


# ── 계약방식 코드표 ───────────────────────────────────────────────
# API는 contract_type_name에 명칭이 아니라 코드를 반환한다(웹 화면만 명칭 표시).
# 아래 매핑은 코드별 표본 과제의 PRISM 웹 화면과 1:1 대조해 복원했다(2026-08-10).
CONTRACT_TYPE = {
    "D0130001": "일반경쟁입찰",
    "D0130002": "제한경쟁입찰",
    "D0130004": "수의계약",
    "D0130005": "기타",
    "D0130006": "경쟁입찰 후 수의계약",
}


def contract_label(code):
    """코드를 명칭으로. 미등록 코드는 원값을 그대로 두고 호출부가 경고하게 한다."""
    if not code:
        return code
    return CONTRACT_TYPE.get(code, code)


_TAG_RE = re.compile(r"<[^>]{1,40}>")


def clean_html(s):
    """PRISM 원문 필드에는 <br />·&nbsp; 등 HTML 잔재가 섞여 온다 — 읽을 수 있게 정리."""
    if not s:
        return s
    import html as _html
    t = _TAG_RE.sub("\n", str(s))
    t = _html.unescape(t)
    t = t.replace(" ", " ").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def won_to_int(s):
    """'44000000원' → 44000000. 파싱 실패 시 None."""
    if not s:
        return None
    digits = re.sub(r"[^0-9]", "", str(s))
    return int(digits) if digits else None


# ── DB ───────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS research (
  research_id TEXT PRIMARY KEY,
  organ_code TEXT,
  research_name TEXT,
  organ_name TEXT,
  researcher_name TEXT,
  charge_person_department TEXT,
  biz_name TEXT,
  research_organName TEXT,
  research_date TEXT,
  issued_year TEXT,
  report_open_yn TEXT,
  sci INTEGER DEFAULT 0,
  first_seen TEXT,
  last_seen TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_organ ON research(organ_name);
CREATE INDEX IF NOT EXISTS idx_research_date ON research(research_date);
CREATE INDEX IF NOT EXISTS idx_research_sci ON research(sci);

CREATE TABLE IF NOT EXISTS research_detail (
  research_id TEXT PRIMARY KEY,
  research_start_date TEXT,
  research_end_date TEXT,
  brm_biz_id TEXT,
  brm_biz_name TEXT,
  research_outline TEXT,
  research_organ_id TEXT,
  research_organ_type_name TEXT,
  contract_date TEXT,
  contract_type_name TEXT,
  contract_cost TEXT,
  contract_cost_won INTEGER,
  title TEXT,
  table_contents TEXT,
  summary TEXT,
  keyword TEXT,
  detail_issued_year TEXT,
  use_note TEXT,
  kogl_open_yn TEXT,
  kogl_content TEXT,
  detail_status TEXT,     -- ok | meta_only | error
  detail_note TEXT,
  fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS research_file (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  research_id TEXT,
  file_type TEXT,
  file_name TEXT,
  file_url TEXT,
  file_size INTEGER,
  local_path TEXT,
  downloaded_at TEXT,
  UNIQUE(research_id, file_url)
);
CREATE INDEX IF NOT EXISTS idx_file_rid ON research_file(research_id);

CREATE TABLE IF NOT EXISTS research_text (
  file_id INTEGER PRIMARY KEY,
  research_id TEXT,
  method TEXT,          -- pymupdf | pypdf | hwpx | hwp | zip
  n_chars INTEGER,
  status TEXT,          -- ok | empty | unsupported | error
  note TEXT,
  extracted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_text_rid ON research_text(research_id);

CREATE TABLE IF NOT EXISTS api_quota (
  day TEXT PRIMARY KEY,
  calls INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS harvest_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, command TEXT, params TEXT,
  n_returned INTEGER, n_total INTEGER, note TEXT
);

CREATE TABLE IF NOT EXISTS sweep_progress (
  range_key TEXT PRIMARY KEY,
  total_count INTEGER,
  last_page INTEGER DEFAULT 0,
  collected INTEGER DEFAULT 0,
  done INTEGER DEFAULT 0,
  updated_at TEXT
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS research_fts USING fts5(
  research_id UNINDEXED, research_name, organ_name, summary, keyword,
  table_contents, research_outline, tokenize='unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS research_fulltext USING fts5(
  file_id UNINDEXED, research_id UNINDEXED, file_name UNINDEXED, body,
  tokenize='unicode61'
);
"""


def has_fts(con):
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        con.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def connect():
    HOME.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    if has_fts(con):
        con.executescript(FTS_SCHEMA)
    con.commit()
    return con


def fts_available(con):
    r = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='research_fts'"
    ).fetchone()
    return r is not None


def log_harvest(con, command, params, n_returned, n_total=None, note=None):
    con.execute(
        "INSERT INTO harvest_log(ts,command,params,n_returned,n_total,note) VALUES (?,?,?,?,?,?)",
        (now_kst(), command, json.dumps(params, ensure_ascii=False), n_returned, n_total, note),
    )
    con.commit()


# ── 쿼터 가드 ─────────────────────────────────────────────────────
def quota_used(con):
    r = con.execute("SELECT calls FROM api_quota WHERE day=?", (today_kst(),)).fetchone()
    return r["calls"] if r else 0


def quota_bump(con, n=1):
    con.execute(
        "INSERT INTO api_quota(day,calls) VALUES(?,?) "
        "ON CONFLICT(day) DO UPDATE SET calls=calls+excluded.calls",
        (today_kst(), n),
    )
    con.commit()


class QuotaExhausted(Exception):
    pass


# ── API 호출 ──────────────────────────────────────────────────────
def call(con, op, params, retries=3):
    if quota_used(con) >= SAFETY_STOP:
        raise QuotaExhausted(
            f"일일 안전 한도({SAFETY_STOP}회) 도달 — 오늘 {quota_used(con)}회 호출. "
            f"내일 같은 명령을 다시 실행하면 이어서 진행합니다(resume)."
        )
    q = dict(params)
    q["serviceKey"] = get_key()
    q["type"] = "json"
    url = f"{BASE}/{op}?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": "prism-db/1.0"})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=90, context=SSL_CTX) as r:
                raw = r.read().decode("utf-8", errors="replace")
            quota_bump(con)
            time.sleep(SLEEP_BETWEEN)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                if "SERVICE_KEY_IS_NOT_REGISTERED" in raw or "SERVICE ERROR" in raw:
                    sys.exit(f"인증키 오류 — 응답: {raw[:400]}")
                return {"resultCode": "PARSE_ERROR", "resultMsg": raw[:400]}
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            quota_bump(con)
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
            time.sleep(1.5 * (attempt + 1))
    return {"resultCode": "NETWORK_ERROR", "resultMsg": last}


# ── ① sweep : 목록 전수 ───────────────────────────────────────────
def upsert_research(con, row, sweep_note=""):
    rid = (row.get("research_id") or "").strip()
    if not rid:
        return 0
    organ_code = rid.split("-")[0] if "-" in rid else ""
    sci = 1 if is_sci(row.get("research_name"), row.get("biz_name")) else 0
    ts = now_kst()
    con.execute(
        """INSERT INTO research(research_id,organ_code,research_name,organ_name,researcher_name,
             charge_person_department,biz_name,research_organName,research_date,issued_year,
             report_open_yn,sci,first_seen,last_seen)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(research_id) DO UPDATE SET
             research_name=excluded.research_name, organ_name=excluded.organ_name,
             researcher_name=excluded.researcher_name,
             charge_person_department=excluded.charge_person_department,
             biz_name=excluded.biz_name, research_organName=excluded.research_organName,
             research_date=excluded.research_date, issued_year=excluded.issued_year,
             report_open_yn=excluded.report_open_yn, sci=excluded.sci, last_seen=excluded.last_seen""",
        (rid, organ_code, row.get("research_name"), row.get("organ_name"),
         row.get("researcher_name"), row.get("charge_person_department"),
         row.get("biz_name"), row.get("research_organName"), row.get("research_date"),
         row.get("issued_year"), row.get("report_open_yn"), sci, ts, ts),
    )
    return 1


def reindex_fts_row(con, rid):
    if not fts_available(con):
        return
    r = con.execute(
        """SELECT r.research_id, r.research_name, r.organ_name,
                  d.summary, d.keyword, d.table_contents, d.research_outline
           FROM research r LEFT JOIN research_detail d USING(research_id)
           WHERE r.research_id=?""", (rid,)).fetchone()
    if not r:
        return
    con.execute("DELETE FROM research_fts WHERE research_id=?", (rid,))
    con.execute(
        "INSERT INTO research_fts(research_id,research_name,organ_name,summary,keyword,"
        "table_contents,research_outline) VALUES(?,?,?,?,?,?,?)",
        (r["research_id"], r["research_name"] or "", r["organ_name"] or "",
         r["summary"] or "", r["keyword"] or "", r["table_contents"] or "",
         r["research_outline"] or ""),
    )


def cmd_sweep(args):
    """★날짜 필터는 '연구기간 포함관계'다 — start_date~end_date 안에 연구 시작·종료가
    모두 들어가는 과제만 반환한다. 따라서 연도별로 쪼개 훑으면 해를 넘기는 과제가
    통째로 누락된다(실측: 2023~24년만 1,417건). 반드시 한 범위로 훑는다."""
    con = connect()
    sd, ed = args.start_date, args.end_date
    rkey = f"{sd}-{ed}"
    print(f"■ 목록 전수 스윕 {sd}~{ed} (오늘 사용 {quota_used(con)}/{DAILY_LIMIT}회)")
    st = con.execute("SELECT * FROM sweep_progress WHERE range_key=?", (rkey,)).fetchone()
    if st and st["done"] and not args.force:
        print(f"  이미 완료된 범위입니다(수집 {st['collected']:,}건). 다시 하려면 --force")
        return
    start_page = (st["last_page"] + 1) if (st and not args.force) else 1
    if st and args.force:
        # --force 실행이 중간에 끊기면 done=1이 남아 다음 일반 실행이 통째로 건너뛴다 — 먼저 내린다.
        con.execute("UPDATE sweep_progress SET done=0 WHERE range_key=?", (rkey,))
        con.commit()
    if start_page > 1:
        print(f"  ▸ {start_page}페이지부터 이어받기(resume)")
    warnings = []
    got = int(st["collected"]) if (st and not args.force) else 0
    total = int(st["total_count"]) if (st and st["total_count"] and not args.force) else 0
    page = start_page
    try:
        while True:
            res = call(con, "getResearchList_v2", {
                "start_date": sd, "end_date": ed, "numOfRows": 1000, "pageNo": page})
            if str(res.get("resultCode")) != "0":
                warnings.append(f"{page}페이지 실패: {res.get('resultMsg')}")
                break
            total = int(res.get("totalCount") or total)
            rows = res.get("research") or []
            if isinstance(rows, dict):
                rows = [rows]
            if not rows:
                break
            for row in rows:
                got += upsert_research(con, row)
            con.execute(
                "INSERT INTO sweep_progress(range_key,total_count,last_page,collected,done,updated_at) "
                "VALUES(?,?,?,?,0,?) ON CONFLICT(range_key) DO UPDATE SET "
                "total_count=excluded.total_count, last_page=excluded.last_page, "
                "collected=excluded.collected, updated_at=excluded.updated_at",
                (rkey, total, page, got, now_kst()))
            con.commit()
            pages = (total + 999) // 1000
            if page % 10 == 0 or page == pages:
                print(f"  {page}/{pages} 페이지 · 누적 {got:,}/{total:,}건 · 호출 {quota_used(con)}")
            if page >= pages:
                con.execute("UPDATE sweep_progress SET done=1 WHERE range_key=?", (rkey,))
                con.commit()
                break
            page += 1
        log_harvest(con, "sweep", {"range": rkey}, got, total)
        n_uniq = con.execute("SELECT COUNT(*) c FROM research").fetchone()["c"]
        if total and n_uniq < total * 0.98:
            warnings.append(f"고유 적재 {n_uniq:,} < totalCount {total:,} (결손 의심 — 재실행 권장)")
    except QuotaExhausted as e:
        print(f"\n⏸ {e}")
    # FTS 재색인
    if fts_available(con):
        con.execute("DELETE FROM research_fts")
        con.execute(
            "INSERT INTO research_fts(research_id,research_name,organ_name,summary,keyword,"
            "table_contents,research_outline) "
            "SELECT r.research_id, COALESCE(r.research_name,''), COALESCE(r.organ_name,''), "
            "COALESCE(d.summary,''), COALESCE(d.keyword,''), COALESCE(d.table_contents,''), "
            "COALESCE(d.research_outline,'') "
            "FROM research r LEFT JOIN research_detail d USING(research_id)")
        con.commit()
    n = con.execute("SELECT COUNT(*) c FROM research").fetchone()["c"]
    nsci = con.execute("SELECT COUNT(*) c FROM research WHERE sci=1").fetchone()["c"]
    print(f"\n■ 적재 완료 — 고유 과제 {n:,}건 (과학기술 후보 {nsci:,}건) / 오늘 호출 {quota_used(con)}회")
    if warnings:
        print("\n⚠ 경고:")
        for w in warnings[:20]:
            print("  -", w)
    print("\n※ 날짜 필터는 연구기간 '포함관계'입니다 — 범위를 쪼개 훑으면 해를 넘기는 과제가 누락됩니다.")


# ── ② enrich : 상세 보강 ──────────────────────────────────────────
def store_detail(con, rid, detail, meta=None):
    res = (detail or {}).get("research") or {}
    con_ = (detail or {}).get("contract") or {}
    rep = (detail or {}).get("reportInfo") or {}
    kogl = (detail or {}).get("kogl") or {}
    status, note = "ok", None

    if not res and meta:  # 폴백 경로 — 부분공개·비공개
        mres = meta.get("research") or {}
        mrep = meta.get("reportInfo") or {}
        res = {"research_name": mres.get("research_name")}
        rep = mrep
        status = "meta_only"
        note = (detail or {}).get("message") or "상세 비공개 — pnnMetaData 폴백"

    raw_ctype = con_.get("contract_type_name") or ""
    ctype = contract_label(raw_ctype)
    if raw_ctype and ctype == raw_ctype and re.fullmatch(r"[A-Z]\d{5,}", raw_ctype):
        note = ((note + " / ") if note else "") + f"계약방식 코드표 미등록({raw_ctype})"

    kogl_yn = kogl.get("kogl_open_yn") or ""
    if not kogl_yn and status == "ok":
        note = ((note + " / ") if note else "") + "공공누리 미표기 → 재배포 금지 기본값"

    con.execute(
        """INSERT INTO research_detail(research_id,research_start_date,research_end_date,
             brm_biz_id,brm_biz_name,research_outline,research_organ_id,research_organ_type_name,
             contract_date,contract_type_name,contract_cost,contract_cost_won,title,
             table_contents,summary,keyword,detail_issued_year,use_note,kogl_open_yn,kogl_content,
             detail_status,detail_note,fetched_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(research_id) DO UPDATE SET
             research_start_date=excluded.research_start_date,
             research_end_date=excluded.research_end_date,
             brm_biz_id=excluded.brm_biz_id, brm_biz_name=excluded.brm_biz_name,
             research_outline=excluded.research_outline,
             research_organ_id=excluded.research_organ_id,
             research_organ_type_name=excluded.research_organ_type_name,
             contract_date=excluded.contract_date, contract_type_name=excluded.contract_type_name,
             contract_cost=excluded.contract_cost, contract_cost_won=excluded.contract_cost_won,
             title=excluded.title, table_contents=excluded.table_contents,
             summary=excluded.summary, keyword=excluded.keyword,
             detail_issued_year=excluded.detail_issued_year, use_note=excluded.use_note,
             kogl_open_yn=excluded.kogl_open_yn, kogl_content=excluded.kogl_content,
             detail_status=excluded.detail_status, detail_note=excluded.detail_note,
             fetched_at=excluded.fetched_at""",
        (rid, res.get("research_start_date"), res.get("research_end_date"),
         res.get("brm_biz_id"), res.get("brm_biz_name"), clean_html(res.get("research_outline")),
         con_.get("research_organ_id"), con_.get("research_organ_type_name"),
         con_.get("contract_date"), ctype, con_.get("contract_cost"),
         won_to_int(con_.get("contract_cost")), clean_html(rep.get("title")),
         clean_html(rep.get("tableContents")),
         clean_html(rep.get("summary")), clean_html(rep.get("keyword")), rep.get("issuedYear"),
         (detail or {}).get("use"), kogl_yn, kogl.get("kogl_content"), status, note, now_kst()),
    )

    urls = rep.get("url") or []
    if isinstance(urls, dict):
        urls = [urls]
    for u in urls:
        if not u.get("file_url"):
            continue
        con.execute(
            "INSERT OR IGNORE INTO research_file(research_id,file_type,file_name,file_url,file_size) "
            "VALUES(?,?,?,?,?)",
            (rid, u.get("file_type"), u.get("file_name"), u.get("file_url"),
             won_to_int(u.get("file_size"))),
        )

    # 상세로 얻은 초록·분야로 과학기술 재판정(정밀도 향상)
    sci = 1 if is_sci(res.get("research_name"), rep.get("title"), rep.get("summary"),
                      rep.get("keyword"), res.get("research_outline"),
                      brm=res.get("brm_biz_name") or "") else 0
    cur = con.execute("SELECT sci FROM research WHERE research_id=?", (rid,)).fetchone()
    if cur is not None and sci:
        con.execute("UPDATE research SET sci=1 WHERE research_id=?", (rid,))
    reindex_fts_row(con, rid)
    con.commit()
    return status


def cmd_enrich(args):
    con = connect()
    where = "WHERE d.research_id IS NULL"
    if args.sci:
        where += " AND r.sci=1"
    if args.year:
        where += f" AND substr(COALESCE(r.research_date,''),1,4)='{int(args.year)}'"
    q = (f"SELECT r.research_id FROM research r "
         f"LEFT JOIN research_detail d USING(research_id) {where} "
         f"ORDER BY r.research_date DESC LIMIT ?")
    todo = [r["research_id"] for r in con.execute(q, (args.limit,)).fetchall()]
    if not todo:
        print("보강 대상 없음 — 이미 모두 상세 수집됨.")
        return
    remaining = SAFETY_STOP - quota_used(con)
    print(f"■ 상세 보강 대상 {len(todo):,}건 / 오늘 잔여 호출 약 {max(0, remaining):,}회")
    ok = meta_only = err = 0
    try:
        for i, rid in enumerate(todo, 1):
            d = call(con, "getResearchDetail_v2", {"research_id": rid})
            m = None
            if not (d.get("research")):
                m = call(con, "pnnMetaData_v2", {"research_id": rid})
            st = store_detail(con, rid, d, m)
            if st == "ok":
                ok += 1
            elif st == "meta_only":
                meta_only += 1
            else:
                err += 1
            if i % 50 == 0:
                print(f"  … {i}/{len(todo)} (정상 {ok} / 폴백 {meta_only}) 호출 {quota_used(con)}")
    except QuotaExhausted as e:
        print(f"\n⏸ {e}")
    log_harvest(con, "enrich", {"sci": args.sci, "limit": args.limit}, ok + meta_only)
    print(f"\n■ 완료 — 상세 {ok}건 / 부분공개 폴백 {meta_only}건 / 실패 {err}건")
    if meta_only:
        print("  ※ 폴백 건은 부분공개·비공개 과제입니다. '자료 없음'이 아니라 '원문 비공개'로 읽으세요.")
    nfile = con.execute("SELECT COUNT(*) c FROM research_file").fetchone()["c"]
    print(f"  원문 파일 링크 누적 {nfile:,}건 — 내려받기: prism_db.py files --sci")


# ── ③ files : 원문 다운로드 ───────────────────────────────────────
# ── 원문 텍스트 추출 ──────────────────────────────────────────────
# 보고서 본문을 뽑아 전문검색에 넣는다. "보고서가 있다"와 "필요한 대목을 찾는다"의 차이.
# PDF는 PyMuPDF>pypdf(있으면), HWPX는 zip+XML(표준 라이브러리), HWP 5.0은 OLE+zlib(olefile).
# 어느 것도 없으면 그 형식만 건너뛴다 — 메타 검색은 그대로 동작한다.

def _extract_pdf(path):
    try:
        import fitz  # PyMuPDF
        try:  # 손상 PDF의 비치명적 경고가 무인 실행 로그를 뒤덮지 않게 한다
            fitz.TOOLS.mupdf_display_errors(False)
        except Exception:
            pass
        with fitz.open(path) as doc:
            return "\n".join(p.get_text() for p in doc), "pymupdf"
    except ImportError:
        pass
    except Exception as e:
        return None, f"pymupdf 실패: {e}"
    try:
        from pypdf import PdfReader
        r = PdfReader(str(path))
        return "\n".join((pg.extract_text() or "") for pg in r.pages), "pypdf"
    except ImportError:
        return None, "unsupported: PDF 추출기 없음(pip install pymupdf)"
    except Exception as e:
        return None, f"pypdf 실패: {e}"


def _extract_hwpx(path):
    """HWPX는 OPC(zip) 안 Contents/sectionN.xml — 표준 라이브러리로 충분."""
    import zipfile
    from xml.etree import ElementTree as ET
    try:
        out = []
        with zipfile.ZipFile(path) as z:
            secs = sorted(n for n in z.namelist()
                          if n.startswith("Contents/section") and n.endswith(".xml"))
            for n in secs:
                root = ET.fromstring(z.read(n))
                # 네임스페이스가 판본마다 달라 태그 지역명으로 훑는다.
                for el in root.iter():
                    if el.tag.rsplit("}", 1)[-1] == "t" and el.text:
                        out.append(el.text)
                    if el.tag.rsplit("}", 1)[-1] in ("p", "linesegarray"):
                        out.append("\n")
        return "".join(out), "hwpx"
    except Exception as e:
        return None, f"hwpx 실패: {e}"


def _extract_hwp(path):
    """HWP 5.0 = OLE 복합문서. BodyText/SectionN 스트림을 zlib(raw) 해제 후
    HWPTAG_PARA_TEXT(tag_id 67) 레코드의 UTF-16LE 본문만 골라낸다."""
    try:
        import olefile
    except ImportError:
        return None, "unsupported: olefile 없음(pip install olefile)"
    import struct
    import zlib
    try:
        ole = olefile.OleFileIO(str(path))
        try:
            compressed = True
            if ole.exists("FileHeader"):
                fh = ole.openstream("FileHeader").read()
                if len(fh) > 37:
                    compressed = bool(fh[36] & 0x01)
            streams = sorted(
                ("/".join(s) for s in ole.listdir() if s[0] == "BodyText"),
                key=lambda x: x)
            chunks = []
            for name in streams:
                data = ole.openstream(name).read()
                if compressed:
                    try:
                        data = zlib.decompress(data, -15)
                    except zlib.error:
                        continue
                i, n = 0, len(data)
                while i + 4 <= n:
                    header = struct.unpack_from("<I", data, i)[0]
                    tag_id = header & 0x3FF
                    size = (header >> 20) & 0xFFF
                    i += 4
                    if size == 0xFFF:           # 확장 길이
                        if i + 4 > n:
                            break
                        size = struct.unpack_from("<I", data, i)[0]
                        i += 4
                    if i + size > n:
                        break
                    if tag_id == 67:            # HWPTAG_PARA_TEXT
                        raw = data[i:i + size]
                        try:
                            s = raw.decode("utf-16-le", errors="ignore")
                        except Exception:
                            s = ""
                        # 제어문자(레코드 구분자) 제거
                        s = "".join(ch for ch in s if ch == "\n" or ord(ch) >= 32)
                        if s.strip():
                            chunks.append(s)
                    i += size
            return "\n".join(chunks), "hwp"
        finally:
            ole.close()
    except Exception as e:
        return None, f"hwp 실패: {e}"


def _extract_zip(path):
    """정부 보고서는 여러 파일을 묶은 .zip으로 올라오기도 한다 — 안의 문서를 모아 추출."""
    import tempfile
    import zipfile
    try:
        parts, methods = [], set()
        with zipfile.ZipFile(path) as z:
            infos = [i for i in z.infolist()
                     if Path(i.filename).suffix.lower() in (".pdf", ".hwp", ".hwpx")]
            if not infos:
                return None, "unsupported: zip 안에 문서 없음"
            # ★원본이 빈 껍데기인 경우가 실제로 있다(발주기관 업로드 결손) — 추출 실패와 구분한다.
            if all(i.file_size == 0 for i in infos):
                return None, "empty-source: 원본 zip 내부 파일이 0바이트(발주기관 업로드 결손)"
            with tempfile.TemporaryDirectory() as td:
                for i in sorted(infos, key=lambda x: x.filename)[:20]:  # 압축폭탄 방지
                    if i.file_size == 0:
                        continue
                    n = i.filename
                    try:  # 한국어 zip은 파일명이 CP949 — zipfile은 cp437로 오해한다
                        name = n.encode("cp437").decode("cp949")
                    except Exception:
                        name = n
                    try:
                        dest = Path(td) / (Path(name).name or "inner")
                        dest.write_bytes(z.read(n))
                    except Exception:
                        continue
                    t, m = extract_text(dest)
                    if t:
                        parts.append(t)
                        methods.add(m)
        if not parts:
            return None, "zip 내부 추출 실패"
        return "\n\n".join(parts), "zip(" + "+".join(sorted(methods)) + ")"
    except Exception as e:
        return None, f"zip 실패: {e}"


def extract_text(path):
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".pdf":
        return _extract_pdf(p)
    if ext == ".hwpx":
        return _extract_hwpx(p)
    if ext == ".hwp":
        return _extract_hwp(p)
    if ext == ".zip":
        return _extract_zip(p)
    return None, f"unsupported: {ext or '확장자 없음'}"


def cmd_extract(args):
    con = connect()
    rows = con.execute(
        "SELECT f.id, f.research_id, f.file_name, f.local_path "
        "FROM research_file f LEFT JOIN research_text t ON t.file_id=f.id "
        "WHERE f.local_path IS NOT NULL AND t.file_id IS NULL LIMIT ?",
        (args.limit,)).fetchall()
    if not rows:
        print("추출 대상 없음 — 내려받은 원문이 모두 처리됨.")
        return
    print(f"■ 원문 텍스트 추출 {len(rows)}건")
    ok = empty = unsup = err = 0
    for r in rows:
        path = Path(r["local_path"])
        if not path.exists():
            con.execute("INSERT OR REPLACE INTO research_text"
                        "(file_id,research_id,method,n_chars,status,note,extracted_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (r["id"], r["research_id"], None, 0, "error", "파일 없음", now_kst()))
            err += 1
            continue
        text, method = extract_text(path)
        if text is None:
            msg = str(method)
            if msg.startswith("unsupported"):
                status = "unsupported"
            elif msg.startswith("empty-source"):
                status = "empty"      # 우리 실패가 아니라 원본 결손
            else:
                status = "error"
            con.execute("INSERT OR REPLACE INTO research_text"
                        "(file_id,research_id,method,n_chars,status,note,extracted_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (r["id"], r["research_id"], None, 0, status, str(method), now_kst()))
            unsup += (status == "unsupported")
            err += (status == "error")
            empty += (status == "empty")
            continue
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        n = len(text)
        status = "ok" if n >= 200 else "empty"
        con.execute("INSERT OR REPLACE INTO research_text"
                    "(file_id,research_id,method,n_chars,status,note,extracted_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (r["id"], r["research_id"], method, n, status,
                     None if status == "ok" else "추출 텍스트 200자 미만(스캔본 가능성)", now_kst()))
        if status == "ok" and fts_available(con):
            con.execute("DELETE FROM research_fulltext WHERE file_id=?", (r["id"],))
            con.execute("INSERT INTO research_fulltext(file_id,research_id,file_name,body) "
                        "VALUES(?,?,?,?)", (r["id"], r["research_id"], r["file_name"], text))
        ok += (status == "ok")
        empty += (status == "empty")
        con.commit()
    con.commit()
    print(f"\n■ 완료 — 본문 확보 {ok}건 / 빈결과 {empty}건 / 미지원 {unsup}건 / 실패 {err}건")
    if empty:
        print("  ※ 빈결과는 스캔 이미지 PDF일 수 있습니다(OCR 필요). 메타 검색은 그대로 동작합니다.")
    tot = con.execute("SELECT COUNT(*) c, SUM(n_chars) s FROM research_text "
                      "WHERE status='ok'").fetchone()
    if tot["c"]:
        print(f"  누적 본문 {tot['c']:,}건 · {tot['s']:,}자 — 검색: prism_db.py search \"<검색어>\" --fulltext")


def _dir_size_bytes(p):
    try:
        return sum(f.stat().st_size for f in Path(p).glob("**/*") if f.is_file())
    except Exception:
        return 0


def cmd_files(args):
    con = connect()
    outdir = Path(args.outdir) if args.outdir else FILES_DIR
    outdir.mkdir(parents=True, exist_ok=True)
    # ★무인 실행(launchd/cron) 대비 디스크 가드 — 원문은 건당 수 MB~수십 MB라 상한이 없으면
    #   수천 건 누적 시 수십 GB가 된다. 상한 도달 시 조용히 멈추지 않고 명시적으로 알린다.
    budget_bytes = int(args.budget_gb * 1e9)
    used = _dir_size_bytes(outdir)
    if used >= budget_bytes:
        print(f"⏸ 원문 저장 상한 도달 — {used/1e9:.1f} GB / 상한 {args.budget_gb} GB ({outdir})\n"
              f"   더 받으려면 --budget-gb 를 올리거나 기존 파일을 옮기세요.")
        return
    where = "WHERE f.local_path IS NULL"
    if args.sci:
        where += " AND r.sci=1"
    rows = con.execute(
        f"SELECT f.id,f.research_id,f.file_name,f.file_url,f.file_size,f.file_type,"
        f"       r.research_name, d.kogl_open_yn "
        f"FROM research_file f JOIN research r USING(research_id) "
        f"LEFT JOIN research_detail d ON d.research_id=f.research_id {where} "
        f"ORDER BY r.research_date DESC, f.file_size ASC LIMIT ?", (args.limit,)).fetchall()
    if not rows:
        print("내려받을 파일 없음.")
        return
    total_mb = sum((r["file_size"] or 0) for r in rows) / 1e6
    print(f"■ 원문 다운로드 {len(rows)}건 (합계 약 {total_mb:,.1f} MB) → {outdir}"
          f"  [저장 {used/1e9:.1f}/{args.budget_gb} GB]")
    file_ctx = _build_file_ssl_ctx()
    done = fail = 0
    for r in rows:
        if used >= budget_bytes:
            print(f"⏸ 저장 상한 {args.budget_gb} GB 도달 — 남은 {len(rows)-done-fail}건 보류")
            break
        name = re.sub(r'[/\\:*?"<>|]', "_", r["file_name"] or f"{r['research_id']}.pdf")
        dest = outdir / f"{r['research_id']}__{name}"
        try:
            req = urllib.request.Request(r["file_url"], headers={"User-Agent": "prism-db/1.0"})
            with urllib.request.urlopen(req, timeout=180, context=file_ctx) as resp:
                data = resp.read()
            # ★내려받았다고 온전한 게 아니다 — 서버가 0바이트나 잘린 응답을 주는 사례가 있다.
            #   그대로 기록하면 DB는 '수집 완료'인데 파일은 비어 있는 조용한 손상이 된다.
            expected = r["file_size"] or 0
            if len(data) == 0:
                raise ValueError("0바이트 응답(서버측 결손)")
            if expected and len(data) < expected * 0.5:
                raise ValueError(f"잘린 응답({len(data):,}B < 예상 {expected:,}B의 50%)")
            dest.write_bytes(data)
            con.execute("UPDATE research_file SET local_path=?, downloaded_at=? WHERE id=?",
                        (str(dest), now_kst(), r["id"]))
            con.commit()
            used += len(data)
            done += 1
            print(f"  ✓ {dest.name} ({len(data)/1e6:.1f} MB) [{r['kogl_open_yn'] or '공공누리 미표기'}]")
        except Exception as e:
            fail += 1
            print(f"  ✗ {r['research_id']}: {e}")
        time.sleep(0.3)
    print(f"\n■ 완료 {done}건 / 실패 {fail}건")
    print("  ※ 원문 재배포·인용 시 과제별 공공누리 유형을 확인하세요(제3·4유형은 변경금지·상업금지).")


# ── 검색·조회 ─────────────────────────────────────────────────────
# ── 검색 재현율 보정 ──────────────────────────────────────────────
# ★FTS5 unicode61은 한국어를 어절 단위로 쪼갠다. 그래서 "규제"로 찾으면 "규제개선·규제혁신"
#   같은 합성어에 든 것을 통째로 놓친다 — 실측 누락률: 규제 71% · 특허 66% · 양자 55%.
#   (긴 합성어일수록 낫다: 인공지능 2%.) trigram 토크나이저는 3글자 미만 질의를 못 해
#   2음절 정책용어(규제·양자·특허)에 쓸 수 없다.
#   → FTS(순위)와 LIKE(전수)를 합친다. LIKE 스캔은 메타 0.02초·본문 0.21초로 사실상 공짜다.

def _tokens(q):
    return [t for t in re.split(r"\s+", (q or "").strip()) if t]


def _like_and(cols, tokens):
    """모든 토큰이 어느 컬럼에든 나타나야 한다 — FTS의 암묵 AND와 같은 의미."""
    parts, params = [], []
    for t in tokens:
        sub = " OR ".join(f"COALESCE({c},'') LIKE ?" for c in cols)
        parts.append(f"({sub})")
        params += [f"%{t}%"] * len(cols)
    return (" AND ".join(parts) or "1=0"), params


def _manual_snippet(text, tokens, width=110):
    """LIKE로만 걸린 건은 FTS snippet()을 못 쓰므로 직접 잘라낸다."""
    if not text:
        return ""
    low = text
    pos = -1
    for t in tokens:
        pos = low.find(t)
        if pos >= 0:
            break
    if pos < 0:
        pos = 0
    s = max(0, pos - width // 3)
    frag = re.sub(r"\s+", " ", text[s:s + width]).strip()
    for t in tokens:
        frag = frag.replace(t, f"《{t}》")
    return frag


def cmd_search_fulltext(args):
    """내려받은 보고서 **본문**에서 찾고 해당 대목을 스니펫으로 보여준다."""
    con = connect()
    if not fts_available(con):
        print("FTS5 미지원 빌드 — 본문 검색 불가.")
        return
    n_docs = con.execute("SELECT COUNT(*) c FROM research_text WHERE status='ok'").fetchone()["c"]
    if not n_docs:
        print("본문이 아직 없습니다 — 먼저 `files`로 원문을 받고 `extract`로 추출하세요.")
        return
    toks = _tokens(args.query)
    filt, fparams = [], []
    if args.sci:
        filt.append("r.sci=1")
    if args.org:
        filt.append("r.organ_name LIKE ?")
        fparams.append(f"%{args.org}%")
    fsql = (" AND " + " AND ".join(filt)) if filt else ""

    fts_rows = []
    try:
        fts_rows = con.execute(
            "SELECT ft.file_id, ft.research_id, ft.file_name, r.research_name, r.organ_name, "
            "       r.research_date, snippet(research_fulltext, 3, '《', '》', ' … ', 24) AS snip "
            "FROM research_fulltext ft JOIN research r ON r.research_id=ft.research_id "
            "WHERE research_fulltext MATCH ?" + fsql + " ORDER BY rank LIMIT ?",
            [args.query] + fparams + [args.limit]).fetchall()
    except sqlite3.OperationalError as e:
        print(f"(FTS 구문 오류 — 전수검색으로 진행: {e})")

    # ★전수 보정 — 본문에서도 합성어 내부 일치를 LIKE로 건진다(0.2초 수준).
    lwhere, lparams = _like_and(["ft.body"], toks)
    like_rows = con.execute(
        "SELECT ft.file_id, ft.research_id, ft.file_name, r.research_name, r.organ_name, "
        "       r.research_date, ft.body AS body "
        "FROM research_fulltext ft JOIN research r ON r.research_id=ft.research_id "
        "WHERE " + lwhere + fsql + " LIMIT ?",
        lparams + fparams + [args.limit * 3]).fetchall()

    seen, rows, n_extra = set(), [], 0
    for r in fts_rows:
        seen.add(r["file_id"])
        rows.append((r, r["snip"], False))
    if not args.strict:
        for r in like_rows:
            if len(rows) >= args.limit:
                break
            if r["file_id"] not in seen:
                seen.add(r["file_id"])
                rows.append((r, _manual_snippet(r["body"], toks), True))
                n_extra += 1

    if not rows:
        print(f"본문에서 '{args.query}' 없음. (본문 보유 {n_docs:,}건 — 전체 대비 일부입니다)")
        print("※ 과제 존재 여부는 `search`(메타 74,129건 전수)로 확인하세요.")
        return
    tail = f" · [보정] {n_extra}건 포함" if n_extra else ""
    print(f"■ 본문 검색 '{args.query}' — {len(rows)}건 (본문 보유 {n_docs:,}건 중){tail}\n")
    for r, snip, supp in rows:
        print(f"· {r['research_name']}{'  [보정]' if supp else ''}")
        print(f"  {r['organ_name']} | {r['research_date'] or '-'} | 파일 {r['file_name']}")
        print(f"  ▸ …{re.sub(chr(10), ' ', snip or '')}…")
        print(f"  https://www.prism.go.kr/homepage/asmt/{r['research_id']}\n")
    print("※ 본문 검색은 내려받아 추출된 보고서만 대상입니다. 여기 없다고 과제가 없는 것이 아닙니다 —")
    print("  과제 존재 여부는 `search`(메타 74,129건 전수)로 확인하세요.")


def cmd_search(args):
    if getattr(args, "fulltext", False):
        return cmd_search_fulltext(args)
    con = connect()
    lim = args.limit
    toks = _tokens(args.query)
    SEL = ("SELECT r.research_id, r.research_name, r.organ_name, r.research_date, "
           "r.report_open_yn, r.sci, d.contract_cost_won, d.brm_biz_name, d.summary ")

    filt, fparams = [], []
    if args.sci:
        filt.append("r.sci=1")
    if args.org:
        filt.append("r.organ_name LIKE ?")
        fparams.append(f"%{args.org}%")
    if args.year:
        filt.append("substr(COALESCE(r.research_date,''),1,4)=?")
        fparams.append(str(args.year))
    fsql = (" AND " + " AND ".join(filt)) if filt else ""

    fts_rows = []
    if fts_available(con) and not args.like:
        try:
            fts_rows = con.execute(
                SEL + "FROM research_fts f JOIN research r ON r.research_id=f.research_id "
                      "LEFT JOIN research_detail d ON d.research_id=r.research_id "
                      "WHERE research_fts MATCH ?" + fsql +
                      " ORDER BY r.research_date DESC LIMIT ?",
                [args.query] + fparams + [lim]).fetchall()
        except sqlite3.OperationalError as e:
            print(f"(FTS 구문 오류 — 전수검색으로 진행: {e})")

    # ★전수 보정 — FTS가 놓친 합성어 내부 일치를 LIKE로 건진다.
    cols = ["r.research_name", "r.organ_name", "d.summary", "d.keyword",
            "d.table_contents", "d.research_outline"]
    lwhere, lparams = _like_and(cols, toks)
    like_rows = con.execute(
        SEL + "FROM research r LEFT JOIN research_detail d USING(research_id) "
              "WHERE " + lwhere + fsql + " ORDER BY r.research_date DESC LIMIT ?",
        lparams + fparams + [lim * 3]).fetchall()

    seen, rows, n_extra = set(), [], 0
    for r in fts_rows:
        seen.add(r["research_id"])
        rows.append((r, False))
    if not args.strict:
        for r in like_rows:
            if len(rows) >= lim:
                break
            if r["research_id"] not in seen:
                seen.add(r["research_id"])
                rows.append((r, True))
                n_extra += 1

    if not rows:
        total = con.execute("SELECT COUNT(*) c FROM research").fetchone()["c"]
        mode = "정확일치만" if args.strict else "합성어 내부까지"
        print(f"'{args.query}' 검색 결과 없음. (DB {total:,}건 전수 대상, {mode} 검색 완료)")
        return
    tail = f" · [보정] {n_extra}건 포함" if n_extra else ""
    print(f"■ '{args.query}' 검색 결과 {len(rows)}건{tail}\n")
    for r, supp in rows:
        cost = f"{r['contract_cost_won']:,}원" if r["contract_cost_won"] else "계약금액 미수집"
        tag = ("[과학기술]" if r["sci"] else "") + (" [보정]" if supp else "")
        print(f"· {r['research_name']}  {tag}")
        print(f"  {r['organ_name']} | {r['research_date'] or '-'} | {cost} | 원문 {r['report_open_yn'] or '-'}"
              f"{' | ' + r['brm_biz_name'] if r['brm_biz_name'] else ''}")
        if r["summary"]:
            s = re.sub(r"\s+", " ", r["summary"])[:110]
            print(f"  ▸ {s}…")
        print(f"  ID {r['research_id']} · https://www.prism.go.kr/homepage/asmt/{r['research_id']}")
        print()
    if n_extra:
        print("※ [보정] = 어절 경계를 무시한 부분일치로 건진 건(FTS가 놓치는 합성어 대응).")
        print("  재현율을 위해 기본 포함하지만 무관한 걸침이 섞일 수 있습니다"
              " — 예: '양자'에 兩者間·분양자료. 제목으로 걸러 읽으세요. 정확일치만 원하면 --strict.")


def cmd_show(args):
    con = connect()
    r = con.execute(
        "SELECT * FROM research r LEFT JOIN research_detail d USING(research_id) "
        "WHERE research_id=?", (args.research_id,)).fetchone()
    if not r:
        print("해당 과제가 DB에 없습니다. sweep을 먼저 실행하세요.")
        return
    d = dict(r)
    print(f"# {d.get('research_name')}\n")
    if not d.get("detail_status"):
        print("※ 상세 미수집 상태입니다(목록 메타만 보유). '자료 없음'이 아닙니다 — "
              f"보강: prism_db.py enrich 또는 원본 페이지 확인.\n")
    for label, key in [("기관", "organ_name"), ("담당부서", "charge_person_department"),
                       ("연구책임자", "researcher_name"), ("수행기관", "research_organName"),
                       ("연구기간", None), ("연구분야(BRM)", "brm_biz_name"),
                       ("계약방식", "contract_type_name"), ("계약금액", "contract_cost"),
                       ("원문공개", "report_open_yn"), ("공공누리", "kogl_open_yn"),
                       ("상세상태", "detail_status"), ("비고", "detail_note")]:
        if label == "연구기간":
            v = f"{d.get('research_start_date') or '-'} ~ {d.get('research_end_date') or '-'}"
        else:
            v = d.get(key)
        if v:
            print(f"- {label}: {v}")
    for k, t in [("summary", "초록"), ("table_contents", "목차"), ("research_outline", "개요"),
                 ("keyword", "주제어")]:
        if d.get(k):
            print(f"\n## {t}\n{d[k]}")
    files = con.execute("SELECT * FROM research_file WHERE research_id=?",
                        (args.research_id,)).fetchall()
    if files:
        print("\n## 원문 파일")
        for f in files:
            mb = f"{(f['file_size'] or 0)/1e6:.1f} MB"
            loc = f" → {f['local_path']}" if f["local_path"] else ""
            print(f"- [{f['file_type']}] {f['file_name']} ({mb}){loc}")
    print(f"\n원본: https://www.prism.go.kr/homepage/asmt/{args.research_id}")


def cmd_stats(args):
    con = connect()
    g = lambda q: con.execute(q).fetchone()[0]
    n = g("SELECT COUNT(*) FROM research")
    print(f"■ PRISM DB 현황  ({DB_PATH})\n")
    print(f"- 총 과제: {n:,}건")
    print(f"- 과학기술 분류: {g('SELECT COUNT(*) FROM research WHERE sci=1'):,}건")
    n_ok = con.execute("SELECT COUNT(*) FROM research_detail WHERE detail_status='ok'").fetchone()[0]
    n_meta = con.execute("SELECT COUNT(*) FROM research_detail WHERE detail_status='meta_only'").fetchone()[0]
    print(f"- 상세 수집: {g('SELECT COUNT(*) FROM research_detail'):,}건 "
          f"(정상 {n_ok:,}, 부분공개 폴백 {n_meta:,})")
    print(f"- 원문 링크: {g('SELECT COUNT(*) FROM research_file'):,}건 "
          f"(다운로드 완료 {g('SELECT COUNT(*) FROM research_file WHERE local_path IS NOT NULL'):,})")
    print(f"- 오늘 API 호출: {quota_used(con)}/{DAILY_LIMIT}회")
    rng = con.execute("SELECT MIN(research_date), MAX(research_date) FROM research "
                      "WHERE research_date IS NOT NULL AND research_date<>''").fetchone()
    print(f"- 연구일자 범위: {rng[0]} ~ {rng[1]}")
    print("\n▸ 연도별 상위")
    for r in con.execute(
            "SELECT substr(research_date,1,4) y, COUNT(*) c FROM research "
            "WHERE research_date<>'' GROUP BY y ORDER BY y DESC LIMIT 8"):
        print(f"   {r['y']}: {r['c']:,}건")
    print("\n▸ 발주기관 상위")
    for r in con.execute(
            "SELECT organ_name, COUNT(*) c FROM research GROUP BY organ_name "
            "ORDER BY c DESC LIMIT 8"):
        print(f"   {r['organ_name']}: {r['c']:,}건")
    cost = con.execute("SELECT COUNT(*) c, SUM(contract_cost_won) s FROM research_detail "
                       "WHERE contract_cost_won IS NOT NULL").fetchone()
    if cost["c"]:
        print(f"\n▸ 계약금액 수집 {cost['c']:,}건 · 합계 {cost['s']:,}원 "
              f"(평균 {cost['s']//cost['c']:,}원)")
    print("\n※ PRISM은 정책연구용역입니다. 국가 R&D 과제(NTIS)와 모집단이 다릅니다.")
    print("※ 최근 연도 건수가 적은 것은 발주 감소가 아니라 등록 시차입니다.")


def cmd_clean(args):
    """이미 적재된 상세의 HTML 잔재(<br />·&nbsp;)를 소급 정리하고 FTS를 재색인한다."""
    con = connect()
    rows = con.execute(
        "SELECT research_id,title,table_contents,summary,keyword,research_outline "
        "FROM research_detail").fetchall()
    n = 0
    for r in rows:
        vals = [clean_html(r[k]) for k in
                ("title", "table_contents", "summary", "keyword", "research_outline")]
        orig = [r[k] for k in
                ("title", "table_contents", "summary", "keyword", "research_outline")]
        if vals == orig:
            continue
        con.execute(
            "UPDATE research_detail SET title=?,table_contents=?,summary=?,keyword=?,"
            "research_outline=? WHERE research_id=?", (*vals, r["research_id"]))
        n += 1
    con.commit()
    # 계약방식 코드 → 명칭 소급 변환
    m = 0
    for code, label in CONTRACT_TYPE.items():
        cur = con.execute(
            "UPDATE research_detail SET contract_type_name=?, "
            "detail_note=NULLIF(REPLACE(COALESCE(detail_note,''),?,''),'') "
            "WHERE contract_type_name=?", (label, f"계약방식 코드 미매핑({code})", code))
        m += cur.rowcount
    con.commit()
    if fts_available(con):
        con.execute("DELETE FROM research_fts")
        con.execute(
            "INSERT INTO research_fts(research_id,research_name,organ_name,summary,keyword,"
            "table_contents,research_outline) "
            "SELECT r.research_id, COALESCE(r.research_name,''), COALESCE(r.organ_name,''), "
            "COALESCE(d.summary,''), COALESCE(d.keyword,''), COALESCE(d.table_contents,''), "
            "COALESCE(d.research_outline,'') "
            "FROM research r LEFT JOIN research_detail d USING(research_id)")
        con.commit()
    print(f"HTML 잔재 정리 {n:,}건 · 계약방식 코드→명칭 변환 {m:,}건 · FTS 재색인 완료")


def cmd_reclassify(args):
    """분류기를 고친 뒤 전체 과제를 다시 판정한다. 상세가 있으면 초록·BRM까지 본다."""
    con = connect()
    rows = con.execute(
        "SELECT r.research_id, r.research_name, r.biz_name, r.sci, "
        "       d.summary, d.keyword, d.research_outline, d.brm_biz_name "
        "FROM research r LEFT JOIN research_detail d USING(research_id)").fetchall()
    added = removed = 0
    for r in rows:
        new = 1 if is_sci(r["research_name"], r["biz_name"], r["summary"], r["keyword"],
                          r["research_outline"], brm=r["brm_biz_name"] or "") else 0
        if new != r["sci"]:
            con.execute("UPDATE research SET sci=? WHERE research_id=?", (new, r["research_id"]))
            added += (new == 1)
            removed += (new == 0)
    con.commit()
    n = con.execute("SELECT COUNT(*) c FROM research WHERE sci=1").fetchone()["c"]
    print(f"재분류 완료 — 과학기술 {n:,}건 (신규 편입 {added:,} / 제외 {removed:,})")
    if removed:
        print("  ※ 제외된 건의 이미 수집한 상세·원문은 그대로 남습니다(삭제하지 않음).")


def cmd_quota(args):
    con = connect()
    used = quota_used(con)
    print(f"오늘({today_kst()}) API 호출: {used}/{DAILY_LIMIT}회 · 안전정지 {SAFETY_STOP}회 · 잔여 {max(0, SAFETY_STOP-used)}회")


def cmd_init(args):
    con = connect()
    print(f"스키마 생성 완료 → {DB_PATH}  (FTS5: {'사용' if fts_available(con) else '미지원 → LIKE 강등'})")


def main():
    ap = argparse.ArgumentParser(
        description="PRISM 정책연구 DB 수집·검색기",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="스키마 생성").set_defaults(func=cmd_init)

    s = sub.add_parser("sweep", help="목록 전수 수집(단일 범위 페이징 — 쪼개면 누락됨)")
    s.add_argument("--start-date", dest="start_date", default="19000101")
    s.add_argument("--end-date", dest="end_date", default="20991231")
    s.add_argument("--force", action="store_true", help="완료된 범위도 처음부터 다시")
    s.set_defaults(func=cmd_sweep)

    e = sub.add_parser("enrich", help="상세 보강(과제당 1회 호출)")
    e.add_argument("--sci", action="store_true", help="과학기술 분류만")
    e.add_argument("--year", type=int)
    e.add_argument("--limit", type=int, default=900)
    e.set_defaults(func=cmd_enrich)

    f = sub.add_parser("files", help="원문 파일 다운로드")
    f.add_argument("--sci", action="store_true")
    f.add_argument("--limit", type=int, default=20)
    f.add_argument("--outdir")
    f.add_argument("--budget-gb", dest="budget_gb", type=float, default=20.0,
                   help="원문 저장 폴더 총량 상한(GB). 무인 실행 시 디스크 보호")
    f.set_defaults(func=cmd_files)

    q = sub.add_parser("search", help="로컬 전문검색")
    q.add_argument("query")
    q.add_argument("--sci", action="store_true")
    q.add_argument("--org")
    q.add_argument("--year", type=int)
    q.add_argument("--limit", type=int, default=10)
    q.add_argument("--like", action="store_true", help="FTS 대신 LIKE 검색")
    q.add_argument("--fulltext", action="store_true",
                   help="메타가 아니라 내려받은 보고서 '본문'에서 검색(해당 대목 스니펫 표시)")
    q.add_argument("--strict", action="store_true",
                   help="어절 정확일치만(FTS). 기본은 합성어 내부까지 훑는 재현율 우선 모드")
    q.set_defaults(func=cmd_search)

    x = sub.add_parser("extract", help="내려받은 원문에서 본문 텍스트 추출(PDF·HWPX·HWP)")
    x.add_argument("--limit", type=int, default=200)
    x.set_defaults(func=cmd_extract)

    sh = sub.add_parser("show", help="한 과제 전체 레코드")
    sh.add_argument("research_id")
    sh.set_defaults(func=cmd_show)

    sub.add_parser("stats", help="DB 현황").set_defaults(func=cmd_stats)
    sub.add_parser("clean", help="적재분 HTML 잔재 정리 + FTS 재색인").set_defaults(func=cmd_clean)
    sub.add_parser("reclassify", help="과학기술 분류 재판정(분류기 수정 후)").set_defaults(func=cmd_reclassify)
    sub.add_parser("quota", help="오늘 호출 수").set_defaults(func=cmd_quota)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
