"""공통 유틸리티: 환경변수 로드, DB 설정, SEC HTTP 세션."""
import os
from datetime import date
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def load_env() -> None:
    """.env 파일을 읽어 환경변수로 등록한다. 이미 설정된 키는 덮어쓰지 않는다."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_env()  # 임포트 시 자동 실행

DB_CONFIG = {
    "host":     os.environ.get("DB_HOST",     "localhost"),
    "port":     os.environ.get("DB_PORT",     "5432"),
    "dbname":   os.environ.get("DB_NAME",     "postgres"),
    "user":     os.environ.get("DB_USER",     "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
}

_SEC_HEADERS = {
    "User-Agent": "NPS-13F-Tracker lcmoony1@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}


# 분기 사이 식별자가 바뀐 종목(기업 액션). 구 CUSIP → 정본(현재) CUSIP.
# 분기 비교 시 같은 종목으로 인식시켜 캐리오버가 '신규 매수'로 오인되는 것을 막는다.
# (구 CUSIP은 다음 분기엔 데이터에서 사라지므로 항목을 영구히 둬도 무해 = no-op)
CUSIP_ALIAS = {
    "046353108": "G0593M107",  # AstraZeneca: ADR 종료 → 보통주 전환 (2026-01)
    "143658300": "G2004J103",  # Carnival Corp: 발행사 코드 변경 (2026 Q2) — 앞 6자리가 달라 자동 판별 불가
    "04351P101": "K08588103",  # Ascendis Pharma: ADS → 보통주 직상장 1:1 (2026-04)
    "09173B107": "486917107",  # Bitfarms → Keel Infrastructure: 미국 이전·사명 변경 (2026-04)
    "811916105": "811927102",  # Seabridge Gold: Valor Gold 분사 후 신주 1:1 교환 (2026-06)
}

# 정본 CUSIP → (사용자 표시 라벨, 전환 반영 분기말). 이 종목들은 ADR↔보통주처럼
# 주식수 환산비가 1:1이 아니라 '전환 경계 분기'에선 수량·거래금액 변화를 신뢰할 수 없다.
# is_cusip_event()가 비교 구간이 경계를 가로지를 때만 True를 돌려주므로, 다음 분기부터는
# 자동으로 일반 비교로 복귀한다 — 수동 정리 불필요(항목은 영구히 둬도 무해).
CUSIP_EVENT = {
    "G0593M107": ("ADR→보통주 전환", date(2026, 3, 31)),
}


# 분사·합병·상장 이전으로 '받은 주식'이 매수로 잡히는 종목 → (사유, 이벤트 반영 분기말).
# 13F만으로는 받은 주식과 실제 매수를 구분할 수 없어, 비교 구간이 이벤트 분기를 가로지를 때
# 해당 종목 전체를 consensus 집계에서 제외한다. 다음 분기부터는 자동으로 일반 비교로 복귀(항목 영구 보존 무해).
# 신규 상장(IPO)은 실제 매수이므로 등록하지 않는다.
CONSENSUS_EXCLUDE = {
    "43849R105": ("분사: Honeywell Aerospace ← Honeywell", date(2026, 6, 30)),
    "314352105": ("분사: FedEx Freight ← FedEx", date(2026, 6, 30)),
    "G9600F104": ("분사: Versigent ← Aptiv", date(2026, 6, 30)),
    "25179M103": ("합병: Devon ← Coterra 주식교환", date(2026, 6, 30)),
    "G2004J103": ("합병: Carnival plc → Carnival Corp 통합", date(2026, 6, 30)),
    # 해외 상장사의 미국 거래소 이전: 기존 보유분이 새로 13F 대상이 된 것이라 매수가 아님
    "G9723Y105": ("상장 이전: Wise 런던 → 나스닥 주상장", date(2026, 6, 30)),
    "675222400": ("상장 이전: OceanaGold 장외 → NYSE", date(2026, 6, 30)),
}


def is_consensus_excluded(cusip, prev_date, curr_date):
    """prev_date→curr_date 비교 구간이 이 종목의 분사·합병 분기를 가로지르는지."""
    ev = CONSENSUS_EXCLUDE.get(cusip)
    return bool(ev) and prev_date is not None and prev_date < ev[1] <= curr_date


def canonical_cusip(cusip):
    """구 CUSIP을 정본으로 정규화한다. 별칭이 없으면 입력 그대로 반환."""
    return CUSIP_ALIAS.get(cusip, cusip)


def cusip_event_label(cusip):
    """식별자 전환 종목의 표시 라벨. 해당 없으면 None."""
    ev = CUSIP_EVENT.get(cusip)
    return ev[0] if ev else None


def is_cusip_event(cusip, prev_date, curr_date):
    """prev_date→curr_date 비교 구간이 이 종목의 식별자 전환 경계를 가로지르는지.
    전환 분기말이 (직전 분기, 현재 분기] 안에 들면 직전은 구 CUSIP 기준이라 수량 비교 불가.
    경계를 안 가로지르면(다음 분기부터) False → 일반 비교로 복귀."""
    ev = CUSIP_EVENT.get(cusip)
    return bool(ev) and prev_date is not None and prev_date < ev[1] <= curr_date


def prev_quarter_date(d: date) -> date:
    """분기말 날짜를 받아 직전 분기말 날짜를 반환한다."""
    if d.month == 3:  return date(d.year - 1, 12, 31)
    if d.month == 6:  return date(d.year, 3, 31)
    if d.month == 9:  return date(d.year, 6, 30)
    if d.month == 12: return date(d.year, 9, 30)
    raise ValueError(f"분기말 날짜가 아님: {d}")


def sec_session() -> requests.Session:
    """타임아웃·재시도 설정이 적용된 requests Session을 반환한다."""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(_SEC_HEADERS)
    return session
