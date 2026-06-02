"""institution 테이블 상위 100개 기관의 최근 2개 분기 13F 보유 종목을 수집해 holdings 테이블에 UPSERT한다."""
import os
import time
import requests
import psycopg2
from psycopg2.extras import execute_values
from xml.etree import ElementTree as ET
from utils import DB_CONFIG, sec_session, prev_quarter_date

OPENFIGI_URL        = "https://api.openfigi.com/v3/mapping"
OPENFIGI_BATCH_FREE = 10   # API 키 없을 때 최대 10개
OPENFIGI_BATCH_KEY  = 100  # API 키 있을 때 최대 100개


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
                cache[cusip] = data[0].get("ticker") if data else None
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
        key = (cik, cusip, accession)
        ticker = ticker_map.get(cusip)
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

    conn.close()
    print("\n완료.")


if __name__ == "__main__":
    main()
