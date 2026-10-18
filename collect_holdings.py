"""institution 테이블 상위 100개 기관의 최근 2개 분기 13F 보유 종목을 수집해 holdings 테이블에 UPSERT한다."""
import os
import time
import requests
import psycopg2
from psycopg2.extras import execute_values
from xml.etree import ElementTree as ET
from utils import DB_CONFIG, sec_session, prev_quarter_date, CUSIP_ALIAS

# 주식 분할·병합 판별에 쓰는 후보 비율 (정분할 F:1 과 역분할 1:F)
_SPLIT_FACTORS = [f for n in (2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50) for f in (n, 1 / n)]

OPENFIGI_URL        = "https://api.openfigi.com/v3/mapping"
OPENFIGI_BATCH_FREE = 10   # API 키 없을 때 최대 10개
OPENFIGI_BATCH_KEY  = 100  # API 키 있을 때 최대 100개

_BOND_CUSIP = "__BOND__"  # 채권·고정수익 CUSIP을 cache에서 구분하는 sentinel
_BOND_TYPES = {"Bond", "Note", "Fixed Income", "Government", "Municipal"}  # securityType2 키워드


def create_holdings_table(conn) -> None:
    """holdings 테이블과 유니크 인덱스를 생성한다. 이미 존재하면 유지한다."""
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS holdings (
                    id               SERIAL PRIMARY KEY,
                    cik              VARCHAR(20)  NOT NULL,
                    accession_number VARCHAR(25),
                    company_name     VARCHAR(255),
                    cusip            VARCHAR(20),
                    ticker           VARCHAR(50),
                    value            BIGINT,
                    shares           BIGINT,
                    report_date      DATE,
                    created_at       TIMESTAMP DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS holdings_cik_cusip_acc_uidx
                    ON holdings (cik, cusip, accession_number)
                """
            )
            # 종목→보유기관 역방향 조회(/stock/<cusip>)용 인덱스
            cur.execute(
                "CREATE INDEX IF NOT EXISTS holdings_cusip_idx ON holdings (cusip)"
            )
            # 기존 테이블 컬럼 크기 마이그레이션
            cur.execute(
                "ALTER TABLE holdings ALTER COLUMN ticker TYPE VARCHAR(50)"
            )


def fetch_tickers_by_cusip(session: requests.Session, cusips: list, cache: dict) -> dict:
    """OpenFIGI API로 CUSIP → ticker 매핑을 반환합니다. cache로 중복 요청을 방지합니다."""
    new_cusips = [c for c in cusips if c not in cache]

    api_key    = os.environ.get("OPENFIGI_API_KEY", "")
    headers    = {"Content-Type": "application/json"}
    batch_size = OPENFIGI_BATCH_KEY
    sleep_sec  = 0.3
    if not api_key:
        batch_size = OPENFIGI_BATCH_FREE
        sleep_sec  = 2.5  # 25req/min 제한
    else:
        headers["X-OPENFIGI-APIKEY"] = api_key

    for i in range(0, len(new_cusips), batch_size):
        batch = new_cusips[i:i + batch_size]
        # 알파벳으로 시작하면 캐나다 증권(CINS), 그 외는 미국 CUSIP
        payload = [
            {"idType": "ID_CINS" if c[0].isalpha() else "ID_CUSIP", "idValue": c}
            for c in batch
        ]
        try:
            resp = session.post(OPENFIGI_URL, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            for cusip, item in zip(batch, resp.json()):
                data = item.get("data")
                if data:
                    # 응답은 거래소별 목록(첫 항목이 독일 등 해외 거래소일 수 있음) → 미국 통합(US) 티커 우선
                    entry     = next((d for d in data if d.get("exchCode") == "US"), data[0])
                    sec_type2 = entry.get("securityType2", "")
                    ticker    = entry.get("ticker") or ""
                    if any(t in sec_type2 for t in _BOND_TYPES) or " " in ticker:
                        cache[cusip] = _BOND_CUSIP
                    else:
                        cache[cusip] = ticker or None
                else:
                    cache[cusip] = None
        except BaseException as e:
            print(f"  [OpenFIGI] 배치 오류: {e}")
            for cusip in batch:
                cache.setdefault(cusip, None)
        time.sleep(sleep_sec)

    return {c: cache.get(c) for c in cusips}


def fetch_top100_ciks(conn) -> list:
    """top100_institutions 테이블에서 rank 순으로 CIK 목록을 반환한다."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cik FROM top100_institutions ORDER BY rank ASC"
        )
        return [row[0] for row in cur.fetchall()]


def fetch_target_dates(conn) -> tuple:
    """top100_institutions.report_date를 현재 분기로, 직전 분기말을 계산해 반환한다."""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(report_date) FROM top100_institutions")
        curr_date = cur.fetchone()[0]
    return curr_date, prev_quarter_date(curr_date)


def get_13f_filings_for_dates(session: requests.Session, cik: str, target_dates: list) -> list:
    """Submissions API에서 target_dates에 해당하는 13F-HR (accession, report_date) 목록을 반환한다."""
    url = f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json"
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    recent = resp.json()["filings"]["recent"]

    target_strs = {str(d) for d in target_dates}
    found = {}
    for i, form in enumerate(recent["form"]):
        if form == "13F-HR":
            rd = recent["reportDate"][i]
            if rd in target_strs and rd not in found:
                found[rd] = recent["accessionNumber"][i]
                if len(found) == len(target_strs):
                    break
    if not found:
        raise ValueError(f"CIK {cik}: 대상 분기 13F-HR 없음")
    return [(acc, rd) for rd, acc in found.items()]


def get_infotable_filename(session: requests.Session, cik: str, accession: str) -> tuple:
    """filing index.json에서 infotable XML 파일명과 value 단위 multiplier를 반환합니다."""
    acc_nodash = accession.replace("-", "")
    cik_int = int(cik)
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/index.json"
    resp = session.get(url, timeout=30)
    resp.raise_for_status()

    items = resp.json().get("directory", {}).get("item", [])
    # type이 "13F-HR TABLE"인 항목 우선
    filename = None
    for item in items:
        if item.get("type", "") == "13F-HR TABLE":
            filename = item["name"]
            break
    # 파일명에 "infotable" 포함 항목
    if not filename:
        for item in items:
            if "infotable" in item["name"].lower():
                filename = item["name"]
                break
    # primary_doc.xml이 아닌 유일한 .xml 파일 (커스텀 파일명 폴백)
    if not filename:
        xml_files = [
            item["name"] for item in items
            if item["name"].lower().endswith(".xml") and item["name"].lower() != "primary_doc.xml"
        ]
        if len(xml_files) == 1:
            filename = xml_files[0]
    if not filename:
        raise ValueError(f"CIK {cik}: infotable 파일을 찾을 수 없음 — {[i['name'] for i in items]}")

    # primary_doc.xml에서 schemaVersion 확인 → X0202는 달러 단위, 그 외는 천 달러 단위
    multiplier = 1000
    try:
        pdoc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/primary_doc.xml"
        pdoc_resp = session.get(pdoc_url, timeout=30)
        pdoc_resp.raise_for_status()
        pdoc_root = ET.fromstring(pdoc_resp.content)
        for el in pdoc_root.iter():
            if el.tag.split("}")[-1] == "schemaVersion" and el.text:
                if el.text.strip() == "X0202":
                    multiplier = 1
                break
    except Exception:
        pass  # 실패 시 천 달러 단위(×1000)로 처리

    return filename, multiplier


def parse_infotable(session: requests.Session, cik: str, accession: str, filename: str, multiplier: int = 1000) -> list:
    """infotable XML에서 보유 종목 목록을 반환합니다."""
    acc_nodash = accession.replace("-", "")
    cik_int = int(cik)
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{filename}"
    resp = session.get(url, timeout=60)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)

    rows = []

    for info_table in root.iter():
        if info_table.tag.split("}")[-1] != "infoTable":
            continue

        company_name = cusip = value_raw = shares = None
        for el in info_table:
            tag = el.tag.split("}")[-1]
            if tag == "nameOfIssuer" and el.text:
                company_name = el.text.strip()
            elif tag == "cusip" and el.text:
                cusip = el.text.strip()
            elif tag == "value" and el.text:
                value_raw = int(el.text.strip())
            elif tag == "shrsOrPrnAmt":
                for child in el:
                    if child.tag.split("}")[-1] == "sshPrnamt" and child.text:
                        shares = int(child.text.strip())

        if company_name and cusip and value_raw is not None:
            rows.append((company_name, cusip, value_raw * multiplier, shares))

    return rows


def save_holdings(conn, cik: str, accession: str, report_date: str, rows: list, ticker_map: dict) -> int:
    """holdings 테이블에 UPSERT하고 저장된 행 수를 반환합니다."""
    # 같은 CUSIP이 SOLE/SHARED/NONE 재량 구분으로 여러 행일 수 있으므로 합산
    agg: dict = {}
    for company_name, cusip, value, shares in rows:
        if cusip not in agg:
            agg[cusip] = [company_name, 0, 0]
        agg[cusip][1] += value  or 0
        agg[cusip][2] += shares or 0

    records_dict = {}
    for cusip, (company_name, value, shares) in agg.items():
        ticker = ticker_map.get(cusip)
        if ticker == _BOND_CUSIP:
            continue
        key = (cik, cusip, accession)
        records_dict[key] = (cik, accession, company_name, cusip, ticker, value, shares, report_date)

    records = list(records_dict.values())
    if not records:
        return 0

    with conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO holdings
                    (cik, accession_number, company_name, cusip, ticker, value, shares, report_date)
                VALUES %s
                ON CONFLICT (cik, cusip, accession_number)
                DO UPDATE SET
                    company_name = EXCLUDED.company_name,
                    ticker       = EXCLUDED.ticker,
                    value        = EXCLUDED.value,
                    shares       = EXCLUDED.shares,
                    report_date  = EXCLUDED.report_date
                """,
                records,
            )
    return len(records)


def normalize_variant_cusips(conn) -> int:
    """잘못 신고된 90X/95X CUSIP 변종을 정본(10X)으로 통합한다.

    일부 기관이 같은 종목을 7~8번째 자리만 90/95로 바꾼 비정상 CUSIP으로 신고해
    동일 종목이 여러 CUSIP으로 쪼개진다(예: NVIDIA 67066G104 ↔ 67066G904/954).
    정본 10X 짝이 holdings에 존재하는 변종만 정본으로 재지정하고 value·shares를
    합산한다. 정본 짝이 없는 변종(전부 ticker 없음)은 그대로 둔다.
    반환값: 통합된 변종 행 수.
    """
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT cusip FROM holdings WHERE cusip IS NOT NULL")
            all_cusips = {r[0] for r in cur.fetchall()}

            # 정본(10X) 짝이 실제로 존재하는 변종만 매핑
            mapping = {
                c: c[:6] + "10" + c[8]
                for c in all_cusips
                if len(c) == 9 and c[6:8] in ("90", "95") and (c[:6] + "10" + c[8]) in all_cusips
            }
            if not mapping:
                return 0
            return _merge_cusips(cur, mapping)


def _merge_cusips(cur, mapping: dict) -> int:
    """mapping(구 CUSIP → 정본 CUSIP)의 구 CUSIP 행을 정본으로 재지정하고 value·shares를 합산한다.
    반환값: 재지정된 구 CUSIP 행 수."""
    olds = list(mapping.keys())
    cur.execute(
        """
        SELECT cik, accession_number, cusip, company_name, value, shares, report_date
        FROM holdings WHERE cusip = ANY(%s)
        """,
        (olds,),
    )
    old_rows = cur.fetchall()

    # (cik, 정본 cusip, accession) 기준으로 value·shares 합산
    merged: dict = {}
    for cik, acc, cusip, name, value, shares, rdate in old_rows:
        key = (cik, mapping[cusip], acc)
        if key not in merged:
            merged[key] = [name, 0, 0, rdate]
        merged[key][1] += value  or 0
        merged[key][2] += shares or 0

    cur.execute("DELETE FROM holdings WHERE cusip = ANY(%s)", (olds,))

    # 정본 행이 있으면 누적, 없으면 신규 삽입. ticker는 정본 것 유지(건드리지 않음)
    records = [
        (cik, acc, name, canon, value, shares, rdate)
        for (cik, canon, acc), (name, value, shares, rdate) in merged.items()
    ]
    if records:
        execute_values(
            cur,
            """
            INSERT INTO holdings
                (cik, accession_number, company_name, cusip, value, shares, report_date)
            VALUES %s
            ON CONFLICT (cik, cusip, accession_number)
            DO UPDATE SET
                value  = holdings.value  + EXCLUDED.value,
                shares = holdings.shares + EXCLUDED.shares
            """,
            records,
        )
    return len(old_rows)


def normalize_cusip_changes(conn, curr_date, prev_date) -> dict:
    """분기 사이 기업 액션으로 CUSIP이 재발급된 종목을 새 CUSIP으로 통합한다.

    같은 발행사(앞 6자리) 안에서 구 CUSIP 보유 기관이 거의 사라지고(직전 대비 20% 이하)
    새 CUSIP이 직전 분기엔 거의 없다가 이번 분기 다수 보유된 경우를 1:1로 짝지어 자동 매핑한다
    (예: Honeywell 438516106 → 438516205). 발행사 코드까지 바뀐 경우는 자동 판별하지 않고
    utils.CUSIP_ALIAS 수동 매핑을 함께 적용한다. 주식 수 환산비가 1:1이 아니면
    이어서 실행되는 normalize_splits()가 보정한다.
    반환값: 적용된 {구 CUSIP: 새 CUSIP}.
    """
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cusip, report_date, COUNT(*) FROM holdings
                WHERE value > 0 AND cusip IS NOT NULL GROUP BY cusip, report_date
                """
            )
            counts: dict = {}  # cusip → [직전 분기 보유 기관 수, 현재 분기 보유 기관 수]
            for cusip, rdate, n in cur.fetchall():
                if rdate in (prev_date, curr_date):
                    counts.setdefault(cusip, [0, 0])[rdate == curr_date] = n

            by_issuer: dict = {}
            for cusip in counts:
                if len(cusip) == 9:
                    by_issuer.setdefault(cusip[:6], []).append(cusip)

            mapping = {}
            for cusips in by_issuer.values():
                gone = [c for c in cusips if counts[c][0] >= 10 and counts[c][1] <= counts[c][0] * 0.2]
                new  = [c for c in cusips if counts[c][1] >= 10 and counts[c][0] <= counts[c][1] * 0.2]
                if len(gone) == 1 and len(new) == 1 and counts[new[0]][1] >= counts[gone[0]][0] * 0.5:
                    mapping[gone[0]] = new[0]
            mapping.update({old: new for old, new in CUSIP_ALIAS.items() if old in counts})

            if mapping:
                _merge_cusips(cur, mapping)
    return mapping


def normalize_splits(conn, curr_date, prev_date) -> dict:
    """주식 분할·병합 종목의 직전 분기 주식 수를 분할 비율로 환산해 분기 비교를 가능하게 한다.

    분할 종목은 기관 대부분의 주식 수가 같은 배수로 바뀌어, 수량이 그대로(±10%)인 기관이
    거의 없다. 실제 매수로 수량이 늘어난 종목은 그대로인 기관이 다수 남으므로 구분된다.
    양 분기 모두 보유한 기관이 20곳 이상이고, 한 후보 비율(±10%)에 모인 기관 비중이
    - 그대로인 기관 3% 이하일 때 25% 이상, 또는
    - 그대로인 기관 10% 이하일 때 40% 이상 (합병 주식교환처럼 비율이 흩어진 경우 배제)
    인 종목만 그 비율로 판정해 직전 분기 shares에 곱한다.
    보정 후에는 비율이 ~1이 되어 재실행해도 다시 판정되지 않는다.
    반환값: 적용된 {cusip: 분할 비율}.
    """
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cik, cusip, shares, report_date FROM holdings
                WHERE shares > 0 AND value > 0 AND report_date IN (%s, %s)
                """,
                (prev_date, curr_date),
            )
            prev, curr = {}, {}
            for cik, cusip, shares, rdate in cur.fetchall():
                (curr if rdate == curr_date else prev)[(cik, cusip)] = shares

            ratios: dict = {}  # cusip → [기관별 현재/직전 주식 수 비율]
            for key, p_shares in prev.items():
                c_shares = curr.get(key)
                if c_shares:
                    ratios.setdefault(key[1], []).append(c_shares / p_shares)

            def near(rs, f):
                return sum(f * 0.9 <= r <= f * 1.1 for r in rs)

            factors = {}
            for cusip, rs in ratios.items():
                if len(rs) < 20:
                    continue
                unchanged = near(rs, 1) / len(rs)
                if unchanged > 0.10:
                    continue
                best = max(_SPLIT_FACTORS, key=lambda f: near(rs, f))
                if near(rs, best) / len(rs) >= (0.25 if unchanged <= 0.03 else 0.40):
                    factors[cusip] = best

            for cusip, f in factors.items():
                cur.execute(
                    "UPDATE holdings SET shares = ROUND(shares * %s::numeric) WHERE cusip = %s AND report_date = %s",
                    (f, cusip, prev_date),
                )
    return factors


def refresh_tickers(conn, session: requests.Session) -> int:
    """holdings·consensus에 저장된 전 CUSIP의 ticker를 OpenFIGI 미국 통합 티커로 다시 매핑한다.
    (해외 거래소 티커로 잘못 저장된 기존 데이터 일괄 정정용. 채권 판정 CUSIP은 건드리지 않음)
    반환값: ticker가 바뀐 CUSIP 수."""
    with conn.cursor() as cur:
        cur.execute("SELECT cusip, MAX(ticker) FROM holdings WHERE cusip IS NOT NULL GROUP BY cusip")
        current = dict(cur.fetchall())
    mapped = fetch_tickers_by_cusip(session, list(current), {})
    changes = [(tk, c) for c, tk in mapped.items()
               if tk and tk != _BOND_CUSIP and tk != current.get(c)]
    with conn:
        with conn.cursor() as cur:
            for table in ("holdings", "consensus"):
                execute_values(
                    cur,
                    f"UPDATE {table} t SET ticker = v.ticker FROM (VALUES %s) AS v(ticker, cusip) "
                    "WHERE t.cusip = v.cusip",
                    changes,
                )
    return len(changes)


def main() -> None:
    """상위 100개 기관의 현재·직전 분기 13F 보유 종목을 수집해 DB에 저장한다."""
    session = sec_session()
    conn = psycopg2.connect(**DB_CONFIG)

    create_holdings_table(conn)

    curr_date, prev_date = fetch_target_dates(conn)
    print(f"\n현재 분기: {curr_date}  /  이전 분기: {prev_date}")

    ciks = fetch_top100_ciks(conn)
    print(f"상위 {len(ciks)}개 기관 holdings 수집을 시작합니다.\n")

    cusip_cache: dict = {}  # 기관 간 CUSIP 캐시 (중복 API 요청 방지)

    for i, cik in enumerate(ciks, 1):
        try:
            filings = get_13f_filings_for_dates(session, cik, [curr_date, prev_date])
            time.sleep(0.2)

            for accession, report_date in filings:
                try:
                    filename, multiplier = get_infotable_filename(session, cik, accession)
                    time.sleep(0.2)

                    rows = parse_infotable(session, cik, accession, filename, multiplier)
                    time.sleep(0.2)

                    cusips     = list({cusip for _, cusip, _, _ in rows})
                    ticker_map = fetch_tickers_by_cusip(session, cusips, cusip_cache)

                    saved = save_holdings(conn, cik, accession, report_date, rows, ticker_map)
                    print(f"[{i}/{len(ciks)}] CIK {cik} ({report_date}): {saved}개 종목 저장 완료 (캐시 {len(cusip_cache)}개)")
                except Exception as e:
                    print(f"[{i}/{len(ciks)}] CIK {cik} ({report_date}): SKIP — {e}")
                time.sleep(0.2)

        except Exception as e:
            print(f"[{i}/{len(ciks)}] CIK {cik}: SKIP — {e}")

        time.sleep(0.2)

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM holdings WHERE report_date NOT IN %s",
                (tuple([curr_date, prev_date]),),
            )
            deleted = cur.rowcount
    if deleted:
        print(f"이전 분기 데이터 {deleted}행 삭제 완료")

    merged = normalize_variant_cusips(conn)
    if merged:
        print(f"90X/95X 변종 CUSIP {merged}행을 정본으로 통합 완료")

    changes = normalize_cusip_changes(conn, curr_date, prev_date)
    if changes:
        print(f"CUSIP 재발급 {len(changes)}건 통합 완료: {changes}")

    splits = normalize_splits(conn, curr_date, prev_date)
    if splits:
        print(f"주식 분할·병합 {len(splits)}건 직전 분기 수량 보정 완료: "
              + ", ".join(f"{c}×{f:g}" for c, f in splits.items()))

    conn.close()
    print("\n완료.")


if __name__ == "__main__":
    import sys
    if "--refresh-tickers" in sys.argv:
        # 수집 없이 저장된 CUSIP의 ticker만 미국 통합 티커로 재매핑 (OpenFIGI만 호출, 수 분)
        _conn = psycopg2.connect(**DB_CONFIG)
        print(f"ticker 정정 완료: {refresh_tickers(_conn, sec_session())}개 CUSIP")
        _conn.close()
    else:
        main()
