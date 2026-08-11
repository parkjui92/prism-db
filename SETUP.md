# 설치·연결 안내

이 도구는 **코드만 배포하고 데이터는 각자 받는다.** 아래대로 하면 30분 안에 74,000여 건 검색이 가능해진다.

## 왜 데이터를 같이 배포하지 않나

- **인증키는 개인 자격증명이다.** 공공데이터포털 키는 계정에 묶여 있고 **하루 1,000회 쿼터가 계정 단위**다. 공유하면 한 사람이 쓸 때 나머지가 못 쓰고, 자격증명 유출이기도 하다.
- **원문 재배포는 라이선스가 갈린다.** API 메타는 "이용허락범위 제한 없음"이지만 **보고서 원문은 과제마다 공공누리 제1~제4유형**이다. 제3·4유형은 변경금지·상업금지라 일괄 재배포가 불가능하다.
- **데이터가 크고 금방 낡는다.** 메타 DB만 140MB대, 원문까지 받으면 30GB를 넘는다. 반면 **직접 받으면 목록 전수가 약 75회 호출·10분**이면 끝난다. git에 담을 이유가 없다.

## 0. 내려받기

원하는 위치에 클론한다. 아래 예시는 편의를 위해 `PRISM_DIR` 변수를 쓴다.

```bash
git clone https://github.com/parkjui92/prism-db.git
export PRISM_DIR="$PWD/prism-db"        # 셸 설정에 넣어두면 편하다
```

## 1. 인증키 발급 (5~10분, 무료)

1. [공공데이터포털 · 행정안전부_정책연구 과제정보](https://www.data.go.kr/data/15080254/openapi.do) 접속
2. 로그인 후 **활용신청** — 심의 없이 **자동승인**된다
3. 마이페이지 → 오픈API → 인증키에서 **일반 인증키(Decoding)** 복사 (끝이 `==`인 쪽)

```bash
mkdir -p ~/.prism && chmod 700 ~/.prism
printf '%s' '<복사한 Decoding 키>' > ~/.prism/key
chmod 600 ~/.prism/key
```

환경변수 `PRISM_API_KEY`로 줘도 된다. **키는 절대 저장소에 커밋하지 않는다** — 기본 경로 `~/.prism/`은 저장소 밖이고, `.gitignore`에도 안전망을 걸어뒀다.

## 2. 목록 전수 수집 (약 10분, 75회 호출)

```bash
python3 "$PRISM_DIR/prism_db.py" sweep
```

끝나면 74,000여 건이 로컬 SQLite(`~/.prism/prism.db`)에 들어간다. **이 시점부터 검색은 전부 무료·즉시**다.

```bash
python3 "$PRISM_DIR/prism_db.py" search "인공지능 윤리" --limit 10
python3 "$PRISM_DIR/prism_db.py" stats
```

## 3. (선택) 상세·원문·본문

관심 분야만 골라 받는다. 상세는 과제당 1회 호출이라 쿼터를 쓴다.

```bash
# 분야 상세 보강 (하루 한도 내에서, 다음 날 이어받기)
python3 "$PRISM_DIR/prism_db.py" enrich --sci --limit 600

# 원문 PDF/HWP 내려받기 (API 쿼터와 무관, 저장 상한 내)
python3 "$PRISM_DIR/prism_db.py" files --sci --limit 40 --budget-gb 20

# 본문 텍스트 추출 → 전문검색 편입
python3 "$PRISM_DIR/prism_db.py" extract
python3 "$PRISM_DIR/prism_db.py" search "규제 샌드박스" --fulltext
```

본문 추출은 아래를 깔면 품질이 좋아진다(없어도 나머지는 그대로 동작).

```bash
pip install pymupdf olefile
```

## 4. (선택) 매일 자동 갱신

`daily_update.sh`가 신규 발주 수집 → 상세 보강 → 원문·본문 → 발주 레이더를 한 번에 한다.

**macOS (launchd 권장 — 절전으로 놓친 실행을 깨어난 뒤 따라잡는다)**

```bash
mkdir -p ~/Library/LaunchAgents ~/.prism
sed -e "s|__PRISM_DIR__|$PRISM_DIR|g" -e "s|__HOME__|$HOME|g" \
  "$PRISM_DIR/examples/local.prism-db.daily.plist" \
  > ~/Library/LaunchAgents/local.prism-db.daily.plist
launchctl load -w ~/Library/LaunchAgents/local.prism-db.daily.plist
```

**Linux (cron)**

```bash
(crontab -l 2>/dev/null; echo "0 9 * * * bash $PRISM_DIR/daily_update.sh >> \$HOME/.prism/daily.log 2>&1") | crontab -
```

| 할 일 | 명령 |
|------|------|
| 로그 보기 | `tail -50 ~/.prism/daily.log` |
| 오늘 레이더 | `cat ~/.prism/radar_$(date +%Y%m%d).md` |
| 즉시 실행(macOS) | `launchctl kickstart -k gui/$(id -u)/local.prism-db.daily` |
| 중지 / 재개(macOS) | `launchctl unload` / `load -w ~/Library/LaunchAgents/local.prism-db.daily.plist` |
| 상태(macOS) | `launchctl list \| grep prism` (둘째 열이 마지막 종료코드, 0이면 정상) |

속도·용량은 `PRISM_BUDGET_GB`(원문 저장 상한, 기본 20GB)와 `PRISM_FILES_PER_DAY`(기본 40)로 조절한다.
`python3`가 표준 경로에 없으면(pyenv·Homebrew 등) plist의 `PATH`에 추가하거나 `PRISM_PYTHON`을 지정한다.

## 5. 각자 분야에 맞게 고치기

`--sci`(과학기술) 분류는 `prism_db.py`의 `SCI_KEYWORDS`·`SCI_REGEX_EXTRA`가 정한다.
다른 분야(복지·환경·교육 등)로 바꾸려면 이 목록을 고치고 재판정한다.

```bash
python3 "$PRISM_DIR/prism_db.py" reclassify
```

★**한국어에서 단순 부분일치는 오탐을 대량 만든다.** 실제로 겪은 것들이다 — `AI`가 AIS·Aid for Trade에 걸리고,
한국 정부 문서에서 **`AI`는 조류인플루엔자**인 경우가 흔하며("AI 방역체계", "고병원성 AI"),
`양자`는 兩者間·휴양자원에, `실증`은 실증연구(경험적 연구)에 걸린다. 실측으로 11.5%가 오분류였다.
라틴 약어에는 **단어경계**를, 다의어에는 **뒤따르는 말**을 요구하도록 짜는 게 좋다. 기존 패턴이 참고가 된다.

## 문제가 생기면

| 증상 | 원인·해결 |
|---|---|
| `SERVICE_KEY_IS_NOT_REGISTERED` | 활용신청 직후 반영에 시간이 걸린다(수십 분). 잠시 후 재시도 |
| `NO_MANDATORY_REQUEST_PARAMETERS_ERROR` | 필수 파라미터 누락. 이 도구를 쓰면 발생하지 않는다 |
| 다운로드 `CERTIFICATE_VERIFY_FAILED` | PRISM 파일 서버가 중간 인증서를 빠뜨린다. 도구가 자동 보강하니 그대로 두면 된다 |
| 검색 결과가 적다 | 상세·본문 수집이 진행 중일 수 있다. `stats`로 확인 |
| 하루 만에 쿼터 소진 | 정상이다. 950회에서 멈추고 다음 날 이어받는다 |
| 본문 검색이 0건 | 원문을 아직 안 받았을 수 있다. 존재 확인은 `search`(메타 전수)로 한다 |

## 인용할 때

```
기관명(발행연도). 과제명. 정책연구관리시스템(PRISM).
https://www.prism.go.kr/homepage/asmt/<research_id>
```

원문을 인용·재배포할 때는 **과제별 공공누리 유형**을 확인한다(`show <research_id>`에 표시).
미표기는 재배포 금지로 취급한다.

## 반드시 읽을 것

수집·해석의 함정은 [README.md](README.md)에 정리돼 있다. 특히 다음 셋은 결과를 조용히 망친다.

1. **날짜 필터는 연구기간 포함관계** — 범위를 쪼개 훑으면 26%가 사라진다(`sweep`이 알아서 단일 범위로 처리)
2. **검색 0건 ≠ 선행연구 없음** — 부분공개 과제는 상세가 막히고, 한국어 합성어 누락도 있다
3. **PRISM ≠ NTIS** — 정책연구용역과 국가 R&D 과제는 모집단이 다르다. 합산 금지
