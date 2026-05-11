# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

SEC EDGAR에서 기관투자자의 13F 보고서를 수집·분석하는 도구. 전체 기관 운용금액을 수집하고 상위 100개 기관의 보유 종목을 웹으로 시각화한다.

## 실행 명령

```bash
# 전체 기관 운용금액 수집 (all_institutions·top100_institutions 테이블 갱신)
python collect_institutions.py <year> <quarter>
# 예: python collect_institutions.py 2025 4   또는   python collect_institutions.py 25 4

# 상위 100개 기관 보유 종목 수집 (holdings 테이블 갱신, 최근 2분기)
python collect_holdings.py

# 웹 서버 실행 (http://localhost:5000)
python app.py

# 스케줄러 실행 (매년 2/20, 5/20, 8/20, 11/20 자정에 collect_institutions.py 자동 실행)
python scheduler.py
```

## 환경 변수 (.env)

```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=postgres
DB_USER=postgres
DB_PASSWORD=

OPENFIGI_API_KEY=   # 없으면 배치 10개/2.5초 제한, 있으면 100개/0.3초
```

## 파일 역할

| 파일 | 역할 |
|---|---|
| `collect_institutions.py` | SEC EDGAR form.idx에서 해당 분기 전체 13F-HR 기관 목록과 운용금액(tableValueTotal)을 수집 → `all_institutions` 테이블 TRUNCATE+INSERT, 상위 100개를 `top100_institutions` 테이블에 rank 포함 저장 |
| `collect_holdings.py` | `top100_institutions` 테이블 CIK를 rank 순으로 조회, 각 기관의 최근 2개 분기 13F 보유 종목을 수집 → `holdings` 테이블 UPSERT |
| `app.py` | Flask 웹 서버. `/`(검색+기관 목록), `/institution/<cik>`(5탭 비교 분석), `/consensus`(통합분석), `/api/search`(자동완성 JSON) |
| `build_consensus.py` | top100 기관의 공통 매수 종목을 집계·점수화하여 `consensus` 테이블에 저장. 종합·기관수·매수금액·비중변화 4개 지표 산출. |
| `scheduler.py` | APScheduler로 분기별 제출 마감일(분기말+45일)에 `collect_institutions.py` 자동 실행 |

## 페이지 구성 및 용어

| 페이지 | URL | 설명 |
|---|---|---|
| 메인 페이지 | `/` | 검색창 + 운용금액 상위 100개 기관 목록 |
| 상세 분석 페이지 | `/institution/<cik>` | 기관별 보유 종목 5탭 비교 분석 |
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

**consensus 집계 로직** (`build_consensus.py`): `curr_shares > prev_shares`인 기관만 매수로 판단하여 세 지표를 계산한다.
- `total_buy_amount`: `round(curr_val * (curr_shares - prev_shares) / curr_shares)` — 신규 취득 주식의 현재 평가금액 합산 (가격 상승분 제외)
- `total_weight_change`: `curr_val * (curr_shares - prev_shares) / curr_shares / curr_aum` — 신규 취득 주식이 현재 운용금액에서 차지하는 비중 합산 (기관 규모 중립 확신 지표)
- `institution_count`: 매수 기관 수
- 점수 공식: `100 * (1 - log(rank) / log(n+1))` 로그 스케일. 전체 활성 종목에 순위 부여 (컷오프 없음, 최하위도 0점 이상)
- 종합점수: 기관수(50%) + 매수금액(30%) + 비중변화(20%) 가중합산
- 각 탭 500위까지 표시