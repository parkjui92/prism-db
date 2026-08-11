#!/bin/bash
# daily_update.sh — PRISM DB 일일 갱신 + 발주 레이더 (launchd/cron 무인 실행용)
#
# ① 최근 2년 범위를 다시 훑어 신규 등록 과제를 잡는다(날짜필터가 포함관계라 범위를 쪼개지 않는다).
# ② 과학기술 분류 중 상세 미수집분을 쿼터 한도까지 보강한다(950회에서 도구가 자동 정지).
# ③ 과학기술 원문을 조금씩 내려받는다(디스크 상한 가드 내에서).
# ④ 최근 30일 내 신규 관측된 과제를 레이더 리포트로 남긴다.
#
# 수동: bash ~/.claude/tools/prism-db/daily_update.sh
# 자동: ~/Library/LaunchAgents/local.prism-db.daily.plist (매일 09:00, launchd)
#
# ★launchd는 최소 환경으로 실행되므로 PATH에 의존하지 않고 절대경로를 쓴다.

set -uo pipefail

# 경로는 스크립트 위치 기준으로 스스로 찾는다 — 클론한 저장소에서도, ~/.claude/tools/에서도 동작.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY="${PRISM_PYTHON:-}"
[ -n "$PY" ] && [ -x "$PY" ] || PY="$(command -v python3 || echo /usr/bin/python3)"
SQLITE="$(command -v sqlite3 || echo /usr/bin/sqlite3)"

TOOL="${PRISM_TOOL:-$SCRIPT_DIR/prism_db.py}"
DB="$HOME/.prism/prism.db"
REPORT="$HOME/.prism/radar_$(date +%Y%m%d).md"
THIS_YEAR=$(date +%Y)
LAST_YEAR=$((THIS_YEAR - 1))

# 원문 저장 상한(GB) — 넘으면 도구가 다운로드를 보류한다
BUDGET_GB="${PRISM_BUDGET_GB:-20}"
# 하루 원문 다운로드 건수
FILES_PER_DAY="${PRISM_FILES_PER_DAY:-40}"

echo "════════ PRISM 일일 갱신 $(date '+%Y-%m-%d %H:%M:%S') ════════"

if [ ! -f "$HOME/.prism/key" ] && [ -z "${PRISM_API_KEY:-}" ]; then
  echo "✗ 인증키 없음(~/.prism/key) — 중단"; exit 0
fi

# ① 최근 2년 재스윕 (신규 등록분 흡수). --force로 진행상태를 리셋해 매일 새로 훑는다.
echo "── ① 신규 과제 스윕 (${LAST_YEAR}~${THIS_YEAR})"
"$PY" "$TOOL" sweep --start-date "${LAST_YEAR}0101" --end-date "${THIS_YEAR}1231" --force || echo "  (스윕 실패 — 다음 실행에서 재시도)"

# ② 과학기술 상세 보강 (쿼터 남는 만큼, 도구가 950회에서 자동 정지)
echo "── ② 과학기술 상세 보강"
"$PY" "$TOOL" enrich --sci --limit 600 || echo "  (보강 실패 — 다음 실행에서 이어받음)"

# ③ 원문 수집 (API 쿼터와 무관, 디스크 상한 내)
echo "── ③ 과학기술 원문 수집"
"$PY" "$TOOL" files --sci --limit "$FILES_PER_DAY" --budget-gb "$BUDGET_GB" || echo "  (다운로드 실패 — 다음 실행에서 재시도)"

# ③b 내려받은 원문에서 본문 텍스트 추출 → 전문검색 편입 (네트워크·쿼터 불필요)
echo "── ③b 본문 텍스트 추출"
"$PY" "$TOOL" extract --limit 200 || echo "  (추출 실패 — 다음 실행에서 재시도)"

# ④ 발주 레이더 — 최근 30일 내 처음 관측된 과제
echo "── ④ 발주 레이더"
"$SQLITE" -init /dev/null "$DB" <<SQL > "$REPORT" 2>/dev/null
.mode markdown
.headers on
SELECT '# PRISM 발주 레이더 ' || date('now','localtime') AS ' ';
SELECT research_name AS 과제명, organ_name AS 발주기관, research_date AS 연구시작,
       CASE sci WHEN 1 THEN '과학기술' ELSE '' END AS 분류,
       'https://www.prism.go.kr/homepage/asmt/' || research_id AS 링크
FROM research
WHERE first_seen >= date('now','-30 day')
  -- 최초 대량 적재일은 제외한다(그날 74,129건이 전부 first_seen을 받아 전량이 '신규'로 잡힌다)
  AND date(first_seen) <> (SELECT date(MIN(first_seen)) FROM research)
ORDER BY sci DESC, research_date DESC
LIMIT 100;
SQL

N=$("$SQLITE" "$DB" "SELECT COUNT(*) FROM research WHERE first_seen >= date('now','-30 day') AND date(first_seen) <> (SELECT date(MIN(first_seen)) FROM research);" 2>/dev/null || echo 0)
NSCI=$("$SQLITE" "$DB" "SELECT COUNT(*) FROM research WHERE sci=1 AND first_seen >= date('now','-30 day') AND date(first_seen) <> (SELECT date(MIN(first_seen)) FROM research);" 2>/dev/null || echo 0)
echo "  최근 30일 신규 ${N}건(과학기술 ${NSCI}건) → $REPORT"

# 진행 현황 한 줄 요약
"$SQLITE" "$DB" "SELECT '  진행: 전체 '||(SELECT COUNT(*) FROM research)||'건 / 상세 '||(SELECT COUNT(*) FROM research_detail)||'건 / 과학기술 미보강 '||(SELECT COUNT(*) FROM research r LEFT JOIN research_detail d USING(research_id) WHERE d.research_id IS NULL AND r.sci=1)||'건 / 원문 '||(SELECT COUNT(*) FROM research_file WHERE local_path IS NOT NULL)||'건 / 본문 '||(SELECT COUNT(*) FROM research_text WHERE status='ok')||'건';" 2>/dev/null

# 로그 비대 방지 — 5MB 넘으면 잘라낸다
LOG="$HOME/.prism/daily.log"
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5000000 ]; then
  tail -c 1000000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# 레이더 리포트 30일 이상된 것 정리
find "$HOME/.prism" -name 'radar_*.md' -mtime +30 -delete 2>/dev/null

echo "════════ 완료 $(date '+%H:%M:%S') ════════"
