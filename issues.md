# 미해결 이슈

## 1. 비준수 기관의 AUM 단위 오류 (T. Rowe Price 등)

### 현상
CIK `0000080255` (T. Rowe Price)의 운용금액이 실제 ~$9,274억이나 DB에 ~$9.27억으로 저장됨 (1/1000 수준).

### 원인
SEC 13F 스키마 버전에 따른 단위 규칙:
- **X0106 이하 (2023-01-03 이전)**: `tableValueTotal` 단위 = 천 달러 → 코드에서 ×1000
- **X0202 (2023-01-03 이후)**: `tableValueTotal` 단위 = 달러 → 코드에서 그대로 사용

T. Rowe Price는 X0202 스키마를 사용하면서도 `tableValueTotal`과 개별 보유 종목 `value`를 모두 천 달러 단위로 신고하는 비준수 filing을 제출하고 있음. EDGAR가 schemaVersion을 자동 삽입하므로 filer가 단위를 잘못 기재해도 스키마 버전과 실제 단위가 불일치하는 상황이 발생함.

SEC 공식 13F 구조화 데이터셋(SUMMARYPAGE.tsv)도 원본 값을 그대로 저장하므로 어떤 SEC 데이터 소스를 사용해도 동일한 문제가 발생함.

### 영향 범위
- `collect_institutions.py`: AUM 랭킹에서 T. Rowe Price가 실제보다 낮은 순위로 저장됨
- `collect_holdings.py`: 반대로 Vanguard 등 X0202 준수 기관의 보유 종목 `value`가 ×1000 되어 1000배 과대 저장될 가능성 있음 (현재 코드가 infotable value를 항상 ×1000 처리)

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
   - 현재 스케줄(2/20, 5/20, 8/20, 11/20)보다 **약 2주 늦게 실행**해야 함
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
