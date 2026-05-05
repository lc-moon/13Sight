# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

SEC EDGAR에서 기관투자자의 13F 보고서를 수집·분석하는 도구. 전체 기관 운용금액을 수집하고 상위 100개 기관의 보유 종목을 웹으로 시각화한다.

## 실행 명령

```bash
# 전체 기관 운용금액 수집 (institution 테이블 갱신)
python collect_institutions.py <year> <quarter>
# 예: python collect_institutions.py 2025 1   또는   python collect_institutions.py 25 4

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
| `collect_institutions.py` | SEC EDGAR form.idx에서 해당 분기 전체 13F-HR 기관 목록과 운용금액(tableValueTotal)을 수집 → `institution` 테이블 UPSERT |
| `collect_holdings.py` | `institution` 테이블 상위 100개 CIK를 조회, 각 기관의 최근 2개 분기 13F 보유 종목을 수집 → `holdings` 테이블 UPSERT |
| `app.py` | Flask 웹 서버. `/`(검색+기관 목록), `/institution/<cik>`(5탭 비교 분석), `/api/search`(자동완성 JSON) |
| `scheduler.py` | APScheduler로 분기별 제출 마감일(분기말+45일)에 `collect_institutions.py` 자동 실행 |

## DB 스키마

```sql
-- 전체 기관 운용금액
CREATE TABLE institution (
    cik         VARCHAR(20)  NOT NULL,
    institution VARCHAR(500) NOT NULL,
    amount_usd  BIGINT       NOT NULL,
    CONSTRAINT institution_cik_unique UNIQUE (cik)
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

**CIK 표기**: 항상 10자리 zero-padded 형식 사용 (`0000102909`). URL, 표시, API 응답 모두 동일. DB 조회 시에는 leading zero 제거 필요 (`str(int(cik))`).

**SEC API 레이트 리밋**: `time.sleep(0.1)` — 초당 10 요청 이내 유지 (SEC 정책). 위반 시 IP 차단.

**분기↔인덱스 매핑**: 13F는 분기말 45일 이내 제출이므로 Q1(3월말) 보고서는 같은 해 QTR2(4~6월) 인덱스에 존재. `QUARTER_TO_INDEX` 참조.

**스키마 버전**: `primary_doc.xml`의 `schemaVersion`이 `X0202`이면 달러 직접, 그 외(`X0106` 등)는 천 달러 단위이므로 ×1000 필요.

**CUSIP 합산**: 같은 종목이 SOLE/SHARED/NONE 재량 구분으로 여러 행일 수 있어 `save_holdings()`에서 CUSIP 기준으로 합산 후 저장.