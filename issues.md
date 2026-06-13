# 이슈 트래커

---

## 1. T. Rowe Price AUM 과소계상 → 실제 순위 미반영 [미해결]

### 현상
CIK `0000080255` (T. Rowe Price)의 운용금액이 실제 ~$9,274억이나 DB에 ~$9.27억으로 저장됨 (실제의 1/1000 수준).
결과적으로 top100 랭킹에서 실제 순위보다 훨씬 낮게 집계되거나 top100에 포함되지 못함.

### 원인
SEC 13F 스키마 버전에 따른 단위 규칙:
- **X0106 이하 (2023-01-03 이전)**: `tableValueTotal` 단위 = 천 달러 → 코드에서 ×1000
- **X0202 (2023-01-03 이후)**: `tableValueTotal` 단위 = 달러 → 코드에서 그대로 사용

T. Rowe Price는 X0202 스키마를 사용하면서도 `tableValueTotal`과 개별 보유 종목 `value`를 모두 천 달러 단위로 신고하는 비준수 filing을 제출하고 있음.
EDGAR가 schemaVersion을 자동 삽입하므로 filer가 단위를 잘못 기재해도 스키마 버전과 실제 단위가 불일치하는 상황이 발생함.

### 비고
과대계상이 아닌 과소계상이므로 top100에 잘못 진입하는 문제는 없음.
단, 실제 규모의 기관이 랭킹에서 누락되는 데이터 정확도 문제.
미래에셋처럼 `EXCLUDED_CIKS`로 제외해도 목록에서 아예 사라질 뿐 근본 해결이 아님.

### 해결 방안 (미구현)

**구조화 데이터셋 전환 + implied price 검증** 세트로 구현:

1. SEC 13F 구조화 데이터셋(ZIP)으로 수집 방식 전환
   - 기관당 개별 XML 요청 수천 건 → ZIP 1회 다운로드로 대체
   - INFOTABLE.tsv가 ZIP에 포함되어 있어 추가 요청 없음

2. 기준 종목(Apple, CUSIP `037833100`) implied price 검증
   - 각 기관의 infotable에서 `value ÷ shares` 계산
   - Apple 분기말 종가와 비교해 약 1000배 차이나면 ×1000 보정 적용
   - Apple을 보유하지 않는 기관은 다른 대형 종목으로 대체

3. 스케줄러 실행 시각 조정 필요
   - 구조화 데이터셋은 분기 제출 기간 종료 후 약 3일 뒤 공개됨
   - 현재 스케줄(2/20, 5/20, 8/20, 11/20)보다 약 2주 늦게 실행해야 함
   - 권장: 3/5, 6/5, 9/5, 12/5

### 참고
- SEC 13F 구조화 데이터셋 URL 패턴:
  - Q1: `https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01mar{Y}-31may{Y}_form13f.zip`
  - Q2: `https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01jun{Y}-31aug{Y}_form13f.zip`
  - Q3: `https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01sep{Y}-30nov{Y}_form13f.zip`
  - Q4: `https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01dec{Y}-{28or29}feb{Y+1}_form13f.zip`
- 데이터셋 공개 시점: 기간 종료 후 3~4일 이내 (매달 3일경)
- Apple CUSIP: `037833100`
- T. Rowe Price CIK: `0000080255`

---

## 2. Vanguard Group 13F 신고 구조 변경 (2026 Q1~) [조치 완료]

### 현상
2026 Q1 업데이트 시 `prev_amount_usd = NULL`인 기관 4개가 top100 상위권에 진입.
웹 앱에서 전분기 대비 변동이 표시되지 않음.

### 원인
2026년 1월 12일, Vanguard Group Inc.가 내부 조직개편을 단행.
기존 통합 신고 방식을 폐지하고 아래 4개 자회사가 각자 독립적으로 13F-HR 신고를 시작함.

| 순위 | 자회사 |
|------|--------|
| 2위  | Vanguard Capital Management LLC |
| 5위  | Vanguard Portfolio Management LLC |
| 32위 | Vanguard Fiduciary Trust Co |
| 57위 | Vanguard Global Advisers, LLC |

### 조치
`prev_amount_usd = NULL`인 기관을 웹 UI에서 "신규 진입" 태그로 표시.
`build_consensus.py`는 직전 분기 데이터가 없는 기관을 매수 판단에서 자동 제외.

---

## 3. 미래에셋 BRK/A 오신고로 인한 AUM 과대계상 [조치 완료]

### 현상
2026 Q1 13F에 BRK/A 368,452주, 평가금액 $264.6B를 신고.
`tableValueTotal`이 $299B로 집계되어 top100 상위권에 진입.

### 실제 상황
- 직전 분기(2025 Q4) AUM: $35.95B → 2026 Q1: $299B (분기 만에 763% 급증)
- BRK/A 단일 종목이 포트폴리오의 88.4%를 차지
- BRK/B(주당 ~$480)를 BRK/A(주당 ~$718,000)로 착각하여 신고한 오류 가능성

### 조치
`collect_institutions.py`의 `EXCLUDED_CIKS`에 추가.
`all_institutions` 테이블에는 원본 데이터를 보존하되 top100 선정 쿼리에서만 제외.

```python
EXCLUDED_CIKS: set = {
    "0001569395",  # Mirae Asset — BRK/A 오신고로 AUM 과대계상 (실제값의 ~1500배)
}
```
