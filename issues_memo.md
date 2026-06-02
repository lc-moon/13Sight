# 13Sight 데이터 이슈 메모

> 작성일: 2026-06-03  
> 분기 업데이트(2026 Q1) 작업 중 발견 및 조치한 사항을 기록합니다.

---

## 1. Vanguard Group 13F 신고 구조 변경 (2026 Q1~)

**현상**  
`prev_amount_usd = NULL`인 기관 4개가 top100 상위권에 진입.  
웹 앱에서 전분기 대비 변동이 표시되지 않음.

**원인**  
2026년 1월 12일, Vanguard Group Inc.가 내부 조직개편을 단행.  
SEC Release No. 34-39538에 따라 기존 통합 신고 방식을 폐지하고  
아래 4개 자회사가 각자 독립적으로 13F-HR 신고를 시작함.

| 순위 | 자회사 |
|------|--------|
| 2위  | Vanguard Capital Management LLC |
| 5위  | Vanguard Portfolio Management LLC |
| 32위 | Vanguard Fiduciary Trust Co |
| 57위 | Vanguard Global Advisers, LLC |

이 자회사들은 2025 Q4까지 별도 13F를 제출한 이력이 없으므로 직전 분기 데이터가 존재하지 않음.

**조치**  
`prev_amount_usd = NULL`인 기관을 웹 UI에서 "신규 진입" 태그로 표시.  
`build_consensus.py`는 직전 분기 데이터가 없는 기관을 매수 판단에서 자동 제외함.

---

## 2. 미래에셋 BRK/A 오신고로 인한 AUM 과대계상

**기관**: Mirae Asset Global Investments Co., Ltd. (CIK: `0001569395`)

**현상**  
2026 Q1 13F에 BRK/A(버크셔 해서웨이 Class A) 368,452주, 평가금액 $264.6B를 신고.  
이로 인해 해당 기관의 `tableValueTotal`이 $299B로 집계되어 top100 상위권에 진입.

**실제 상황**  
- 직전 분기(Q4 2025) AUM: $35.95B → Q1 2026: $299B (분기 만에 763% 급증)  
- BRK/A 단일 종목이 포트폴리오의 88.4%를 차지  
- BRK/A 총 발행 주식 약 145만 주 중 25%를 한 기관이 보유한다는 의미  

**추정 원인**  
BRK/B(버크셔 Class B, 주당 ~$480)를 BRK/A(주당 ~$718,000)로 착각하여 신고한 오류 가능성.  
또는 외부 고객 자산에 대한 투자 재량권(investment discretion) 부여로  
실소유자가 다른 대규모 BRK/A 블록을 대리 신고하는 구조일 수 있음.  
(13F는 실소유 여부와 무관하게 투자 재량권이 있는 모든 증권을 신고 대상으로 함)

**조치**  
`collect_institutions.py`에 `EXCLUDED_CIKS` 목록 추가.  
`all_institutions` 원본 데이터는 보존하되, top100 선정 쿼리에서만 제외.  
`--top100-only` 플래그로 전체 재수집 없이 top100만 신속하게 재선정 가능하도록 구현.

```python
EXCLUDED_CIKS: set = {
    "0001569395",  # Mirae Asset — BRK/A 오신고로 AUM 과대계상
}
```
