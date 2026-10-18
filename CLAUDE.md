# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **🌐 외부 접속 URL: http://161.33.143.61:5001** (운영 VM · 브라우저에서 바로 접속)

## 프로젝트 개요

SEC EDGAR에서 기관투자자의 13F 보고서를 수집·분석하는 도구. 전체 기관 운용금액을 수집하고 상위 100개 기관의 보유 종목을 웹으로 시각화한다.

## 웹 레이어: v2 전용

웹 프론트엔드/서버는 **`v2/` 디렉토리만 작업한다.** 루트의 `app.py`와 `templates/`는 구버전(v1)으로 동결 상태이며 더 이상 사용·수정하지 않는다. 새 기능·수정·리디자인은 모두 `v2/app.py`와 `v2/templates/`에만 추가한다. v1에 맞춘 코드를 별도로 작성하지 않는다.

## 실행 명령

분기 업데이트 시 실행 순서: `collect_institutions` → `collect_holdings` → `build_consensus`

```bash
# 1. 전체 기관 운용금액 수집 (all_institutions·top100_institutions 테이블 갱신)
python collect_institutions.py <year> <quarter>
# 예: python collect_institutions.py 2025 4   또는   python collect_institutions.py 25 4

# 2. 상위 100개 기관 보유 종목 수집 (holdings 테이블 갱신, 최근 2분기)
#    — top100_institutions 테이블이 먼저 채워져 있어야 함
python collect_holdings.py

# 3. consensus 테이블 갱신 (collect_holdings 완료 후 실행)
python build_consensus.py

# 기관명 한국어 번역 (institution_names_kr.json 갱신 — 필요 시만 실행)
python translate_institutions.py

# 웹 서버 실행 (v2 전용, http://localhost:5001)
python v2/app.py

# 스케줄러 실행 (매년 2/20, 5/20, 8/20, 11/20 자정에 위 1→2→3단계를 순서대로 자동 실행, 실패 시 중단)
# 수집 분기: 2/20→작년 Q4, 5/20→Q1, 8/20→Q2, 11/20→Q3 (제출 마감 분기말+45일이 지난 최신 분기)
python scheduler.py
```

## 현재 데이터 상태

| 항목 | 값 |
|---|---|
| **DB 최신 분기** | 2026 Q2 (report_date = 2026-06-30) — 운영 VM 기준 |
| **holdings 직전 분기** | 2026 Q1 (report_date = 2026-03-31) |
| **알려진 이슈** | `issues.md` 참조 (T. Rowe Price AUM 과소계상·Vanguard 분사·미래에셋 오신고·CUSIP 변종 등) |

## 배포 방식

**Oracle Cloud Always Free VM** (단일 서버) — **현재 운영 중**.
Flask 웹서버 + APScheduler + PostgreSQL을 VM 한 대에서 함께 실행한다.
단일 서버이므로 `collect_institutions.py`가 생성하는 `institution_names_kr.json`을
`app.py`가 같은 경로에서 바로 읽을 수 있다 (파일 동기화 불필요).

| 항목 | 값 |
|---|---|
| **프로덕션 URL** | http://161.33.143.61:5001 |
| **VM 접속** | `ssh -i ~/Downloads/ssh-key-2026-06-15.key ubuntu@161.33.143.61` |
| **코드 경로 (VM)** | `/opt/13sight/` |
| **재배포** | 프로젝트 루트에서 `bash deploy/redeploy.sh` (스케줄러 포함: `--scheduler` 옵션) |

> GitHub repo가 비공개라 VM에서 git clone이 안 됨. 코드 업데이트는 항상 로컬에서 `redeploy.sh` 실행.
> SSH 키(`ssh-key-2026-06-15.key`)는 git에 없으므로 포맷 시 별도 백업 또는 Oracle Cloud 콘솔에서 재발급 필요.
> `redeploy.sh`의 tar 제외 목록에 `test/`가 없으므로, `test/holdings_cache.json`(30MB)이 로컬에 존재하면 배포 시 VM에 딸려감. 배포 전 삭제 또는 무시(기능에 영향 없음).

## 환경 변수 (.env)

```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=postgres
DB_USER=postgres
DB_PASSWORD=

OPENFIGI_API_KEY=   # 없으면 배치 10개/2.5초 제한, 있으면 100개/0.3초

GEMINI_API_KEY=     # Gemini API 키 (기관명 한국어 번역용 — translate_institutions.py)
```

## 파일 역할

| 파일 | 역할 |
|---|---|
| `collect_institutions.py` | SEC EDGAR form.idx에서 해당 분기 전체 13F-HR 기관 목록과 운용금액(tableValueTotal)을 수집 → `all_institutions` 테이블 TRUNCATE+INSERT, 상위 100개를 `top100_institutions` 테이블에 rank 포함 저장 |
| `collect_holdings.py` | `top100_institutions` 테이블 CIK를 rank 순으로 조회, 각 기관의 최근 2개 분기 13F 보유 종목을 수집 → `holdings` 테이블 UPSERT. OpenFIGI API로 CUSIP→ticker 매핑. 수집 후 기업 액션 정규화(90X/95X 변종 → CUSIP 재발급 통합 → 분할 보정) 자동 실행. |
| `build_consensus.py` | top100 기관의 공통 매수 종목을 집계·점수화하여 `consensus` 테이블에 저장. 종합·기관수·매수금액·비중변화 4개 지표 산출. |
| `translate_institutions.py` | `all_institutions` 기관명을 Gemini API로 한국어 번역 → `institution_names_kr.json` 갱신. 분기 수집 후 필요 시 수동 실행. |
| `scheduler.py` | APScheduler로 분기별 제출 마감일(분기말+45일) 이후 `collect_institutions` → `collect_holdings` → `build_consensus`를 순서대로 자동 실행. 앞 단계 실패(종료코드≠0) 시 중단. `collect_institutions`는 수집 0건이면 DB를 비우지 않고 실패 종료하며, 번역 실패는 파이프라인을 막지 않음 |
| `utils.py` | 공통 유틸리티. DB_CONFIG(환경변수 로드), sec_session()(재시도 포함 HTTP 세션), prev_quarter_date(), canonical_cusip()/is_cusip_event()(CUSIP 이벤트 처리), CUSIP_ALIAS/CUSIP_EVENT 테이블, CONSENSUS_EXCLUDE/is_consensus_excluded()(분사·합병·상장 이전 종목 consensus 제외) |
| `institution_names_kr.json` | 기관명 한국어 번역 캐시. `translate_institutions.py`가 생성, `v2/app.py`가 기관 목록 표시 시 로드. git 추적됨. **VM 파일이 기준** — VM 분기 수집이 갱신하므로 `redeploy.sh`는 이 파일을 덮어쓰지 않음(로컬 최신화가 필요하면 VM에서 scp로 받아옴). |
| `v2/app.py` | **(활성)** Flask 웹 서버. `/`(검색+기관 목록), `/institution/<cik>`(기관 분석), `/stock/<cusip>`(종목 분석), `/consensus`(통합분석), `/api/search`(기관·종목 자동완성 JSON). 포트 5001 |
| `app.py` · `templates/` | **(동결, v1)** 구버전 웹 레이어. 사용·수정하지 않음 |
| `test/backtest_2025q1.py` | 백테스트 스크립트. 현재 top100 기관의 25Q1(2025-03-31) vs 24Q4(2024-12-31) 13F를 SEC에서 직접 수집해 consensus 계산 후 수익률 검증. DB 읽기 전용, 결과는 `test/top10_consensus.json`에 저장. `holdings_cache.json`(30MB) 캐시를 생성하므로 재실행 시 빠름. |
| `test/top10_consensus.json` | 백테스트 결과 — 25Q1 기준 통합분석 Top 10 종목과 실제 수익률(2026-06-16 기준). 평균 +47%(SNDK 제외), SNDK(SanDisk) +4,083% 포함. |

## 페이지 구성 및 용어

| 페이지 | URL | 설명 |
|---|---|---|
| 메인 페이지 | `/` | 검색창(기관·종목) + 운용금액 상위 100개 기관 목록 |
| 기관 분석 페이지 | `/institution/<cik>` | 기관 → 보유 종목 5탭 비교 분석 |
| 종목 분석 페이지 | `/stock/<cusip>` | 종목 → 보유 기관 목록·매수/매도 요약·통합분석 점수 (역방향 조회) |
| 통합분석 페이지 | `/consensus` | top100 기관 공통 매수 종목 4탭(종합·기관수·매수금액·비중변화) 순위 |

## DB 스키마

```sql
-- 해당 분기 전체 기관 운용금액 (실행 시 TRUNCATE 후 재삽입)
CREATE TABLE all_institutions (
    cik         VARCHAR(20)  NOT NULL,
    institution VARCHAR(500) NOT NULL,
    amount_usd  BIGINT       NOT NULL,
    report_date DATE,
    CONSTRAINT all_institutions_cik_date_unique UNIQUE (cik, report_date)
);

-- 현재 분기 운용금액 상위 100개 기관 (실행 시 TRUNCATE 후 재삽입)
CREATE TABLE top100_institutions (
    rank            SMALLINT     NOT NULL,
    cik             VARCHAR(20)  NOT NULL,
    institution     VARCHAR(500) NOT NULL,
    prev_amount_usd BIGINT,
    curr_amount_usd BIGINT       NOT NULL,
    report_date     DATE
);

-- top100 기관 공통 매수 종목 집계 (build_consensus.py 실행 시 TRUNCATE 후 재삽입)
CREATE TABLE consensus (
    cusip                 VARCHAR(20)       NOT NULL,
    ticker                VARCHAR(50),
    company_name          VARCHAR(255),
    report_date           DATE              NOT NULL,
    institution_count     INT               NOT NULL DEFAULT 0,
    inst_rank             SMALLINT,
    inst_score            SMALLINT          NOT NULL DEFAULT 0,
    total_buy_amount      BIGINT            NOT NULL DEFAULT 0,
    buy_rank              SMALLINT,
    buy_score             SMALLINT          NOT NULL DEFAULT 0,
    total_weight_change   DOUBLE PRECISION  NOT NULL DEFAULT 0,
    weight_rank           SMALLINT,
    weight_score          SMALLINT          NOT NULL DEFAULT 0,
    composite_score       DOUBLE PRECISION  NOT NULL DEFAULT 0,
    composite_rank        SMALLINT
);

-- 기관별 보유 종목 (2개 분기)
CREATE TABLE holdings (
    id               SERIAL PRIMARY KEY,
    cik              VARCHAR(20)  NOT NULL,
    accession_number VARCHAR(25),
    company_name     VARCHAR(255),
    cusip            VARCHAR(20),
    ticker           VARCHAR(50),
    value            BIGINT,
    shares           BIGINT,
    report_date      DATE,
    created_at       TIMESTAMP DEFAULT NOW(),
    UNIQUE (cik, cusip, accession_number)
);
```

## 주요 규칙 및 주의사항

**CIK 표기**: 항상 10자리 zero-padded 형식 사용 (`0000102909`). URL, 표시, API 응답, DB 저장 모두 동일.

**SEC API 레이트 리밋**: `time.sleep(0.1)` — 초당 10 요청 이내 유지 (SEC 정책). 위반 시 IP 차단.

**분기↔인덱스 매핑**: 13F는 분기말 45일 이내 제출이므로 Q1(3월말) 보고서는 같은 해 QTR2(4~6월) 인덱스에 존재. `QUARTER_TO_INDEX` 참조.

**스키마 버전**: `primary_doc.xml`의 `tableValueTotal`은 X0202(2023년 1월 이후) 스키마에서 달러 단위, X0106 및 구버전에서 천 달러 단위(×1000). 단, T. Rowe Price(CIK 0000080255) 등 일부 기관이 X0202 스키마를 쓰면서 천달러 단위로 잘못 신고하는 filing 오류가 존재하며, 이 경우 실제 AUM이 1000배 작게 저장되는 한계가 있음.

**CUSIP 합산**: 같은 종목이 SOLE/SHARED/NONE 재량 구분으로 여러 행일 수 있어 `save_holdings()`에서 CUSIP 기준으로 합산 후 저장.

**폰트(한글 금지 폰트)**: `--font-mono`(JetBrains Mono)는 한글 글리프가 없어 한글에 적용하면 깨진 폴백 폰트로 렌더링된다. 한글이 들어가는 모든 텍스트(캡션·라벨·제목 등)에는 `--font-sans`를 사용한다. `--font-mono`는 숫자·CUSIP·티커·순위·점수 등 **ASCII 전용** 요소에만 쓴다.

**기업 액션 처리** (분기 비교 왜곡 방지 — 상세는 `issues.md` 8번):
- 자동 (`collect_holdings.py`): ① 같은 발행사(CUSIP 앞 6자리) 안의 CUSIP 재발급을 새 CUSIP으로 통합(`normalize_cusip_changes`), ② 주식 분할·병합을 감지해 직전 분기 shares를 분할 비율로 환산(`normalize_splits`). 둘 다 재실행해도 이중 적용되지 않음.
- 수동 (`utils.py`, 분기 업데이트 후 consensus 상위권 점검 시 등록): 발행사 코드까지 바뀐 CUSIP 변경 → `CUSIP_ALIAS`, 분사·합병·해외 상장사 미국 이전 → `CONSENSUS_EXCLUDE`(이벤트 분기에만 해당 종목 consensus 제외, 다음 분기 자동 복귀). 신규 상장(IPO)은 실제 매수이므로 등록하지 않음.
- 점검 신호: 직전 분기 보유 기관이 0곳이거나 현재의 25% 이하인 상위 종목(분사/IPO/상장 이전 구분 필요), 수량 그대로인 기관이 10% 이하인 종목(합병·분할 의심).

**consensus 집계 로직** (`build_consensus.py`): `curr_shares > prev_shares`인 기관만 매수로 판단하여 세 지표를 계산한다.
- `total_buy_amount`: `round(curr_val * (curr_shares - prev_shares) / curr_shares)` — 신규 취득 주식의 현재 평가금액 합산 (가격 상승분 제외)
- `total_weight_change`: `curr_val * (curr_shares - prev_shares) / curr_shares / curr_aum` — 신규 취득 주식이 현재 운용금액에서 차지하는 비중 합산 (기관 규모 중립 확신 지표)
- `institution_count`: 매수 기관 수
- 점수 공식: `100 * (1 - log(rank) / log(n+1))` 로그 스케일. 전체 활성 종목에 순위 부여 (컷오프 없음, 최하위도 0점 이상)
- 종합점수: 기관수(50%) + 매수금액(30%) + 비중변화(20%) 가중합산
- 각 탭 500위까지 표시