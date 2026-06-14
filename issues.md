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
`prev_amount_usd = NULL`인 기관을 웹 UI에서 "신규 보고" 태그로 표시.
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

---

## 4. 90X/95X CUSIP 변종 오신고로 인한 종목 파편화 [조치 완료 / 일부 잔존]

### 현상
동일 종목이 서로 다른 CUSIP으로 쪼개져 `holdings`에 저장됨.
- NVIDIA: `67066G104`(정본, ticker NVDA) 외에 `67066G904`·`67066G954`
- SPY: `78462F103`(정본, ticker SPY) 외에 `78462F953`·`78462F903`

결과적으로:
- 검색 시 같은 종목이 여러 건 중복 노출
- `/stock/<cusip>` 보유 기관이 변종별로 쪼개짐
- consensus 집계가 변종별로 분리되어 순위 오염 (가짜 SPY `78462F953`이 비중변화 **1위**, 진짜 SPY는 4위로 밀림)

### 원인
일부 기관이 13F에서 같은 종목을 CUSIP 7~8번째 자리만 `90` 또는 `95`로 바꾼 비정상 번호로 신고.
이 변종 번호는 실재하지 않아 OpenFIGI 조회가 실패하므로 ticker가 NULL이 됨 ("ticker 없음"이 곧 변종 신호).
현재 분기 기준 전체 distinct CUSIP의 약 **37%(6,998/19,077)**가 이 패턴이었음.

(13F 원본에는 ticker가 없고, `collect_holdings.py`가 CUSIP→OpenFIGI 조회로 ticker를 붙이는 구조라 발생.)

### 조치 (`collect_holdings.py`: `normalize_variant_cusips`)
변종 `XXXXXX{90|95}C` 중 정본 `XXXXXX10C`(체크숫자 `C` 동일)가 holdings에 **실제로 존재하는 경우에만**
정본으로 재지정하고 `value`·`shares`를 합산. 정본 짝이 없는 변종(전부 ticker 없음)은 보존.
`main()` 끝에서 자동 실행되어 향후 수집 시 자동 정규화됨.
일회성 마이그레이션으로 기존 DB 변종 **26,165행 통합**(distinct CUSIP 20,373→14,749) 후 `build_consensus.py` 재실행.

### 한계 (잔존)
체크숫자가 정본과 다르거나 정본 `10X` 자체가 holdings에 없는 변종은 자동 병합되지 않음.
- ALPHABET INC `02079K905/955` — 정본 후보 `02079K105`가 없고 실제는 GOOG `02079K107`/GOOGL `02079K305`(체크숫자 불일치로 자동 매핑 불가)
- ISHARES TR `464287905/955` 등 — 공통 `10X`가 없는 펀드 패밀리

이들은 검색 드롭다운에서 ticker 없는 중복으로 노출됨. consensus 상위 50위 내에는 없음(잘 보이는 영역은 영향 없음).

### 검색 노출 차단 (`v2/app.py`: `fetch_stock_search`)
병합되지 못한 잔존 변종을 검색에서 숨기는 필터 추가:
```sql
HAVING NOT (MAX(ticker) IS NULL AND substring(cusip FROM 7 FOR 2) IN ('90', '95'))
```
근거: 정상 종목 중 7~8번째 자리가 90/95인 CUSIP은 **0개**(전수 확인)이므로, ticker 없는 90/95만 숨기면
정상 종목은 절대 누락되지 않음. CSG SYSTEMS(`126349109`)·VERIS RESIDENTIAL(`554489104`)처럼 일반형식이면서
OpenFIGI 미해결로 ticker만 없는 정당한 종목은 그대로 노출됨(보유 기관 수 무관).
보유 기관 수 임계값 방식도 가능하나, 보유 기관이 적은 일반형식 정당 종목까지 숨길 위험이 있어 패턴 기준을 채택.

---

## 5. v2 웹 레이어 모바일 대응 부족 [미해결]

### 현상
v2 템플릿(`base.html`·`index.html`·`institution.html`·`consensus.html`·`stock.html`) 전체에
`@media` 브레이크포인트가 **0개**. 화면 폭에 따라 레이아웃이 바뀌는 반응형 분기가 전무함.
모바일에서 "유동적이지만 최적화되지 않은" 상태.

### 현재 모바일에서 그럭저럭 동작하는 이유 (의도된 반응형이 아닌 부수효과)
- 컨테이너가 `max-width + margin:auto` 방식이라 좁은 화면에서 그냥 줄어듦 (텍스트는 안 잘림).
- 테이블만 `.table-scroll { overflow-x:auto }`로 가로 스크롤 처리됨 — 유일한 명시적 모바일 배려.

### 모바일에서 어색해지는 부분
- **AUM 카드**(`institution.html` `.aum-card`)가 `display:flex`인데 wrap이 없어, 좁은 화면에서
  금액 텍스트와 막대 차트가 한 줄에 눌림.
- **탭**(`base.html` `.tabs`)이 `display:flex`에 스크롤/줄바꿈 처리가 없어 폰 폭에서 잘리거나 넘침
  (기관 분석 5탭·통합분석 4탭).
- 큰 디스플레이 폰트(`--fs-display-xl: 48px` 등)에 모바일 축소 분기 없음.
- **포트폴리오 구성 트리맵**(이슈 5 기록 후 추가됨): 데스크톱 기준으로만 구현됨. 좁은 화면에서
  칸이 납작해지고 라벨이 뭉개짐.

### 해결 방안 (미구현)
기본 모바일 대응 한 벌을 사이트 전체에 일괄 적용:
- AUM 카드 `flex-wrap` 허용
- 탭 컨테이너 `overflow-x:auto`
- 디스플레이 폰트 모바일 축소 `@media`
- 트리맵: 모바일에서 표시 종목 수 축소(상위 N개+기타) + 라벨 적응형 표시 + 임계폭 이하 폴백(누적 막대/리스트)
