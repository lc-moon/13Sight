import sys
import os
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import psycopg2
from xml.etree import ElementTree as ET

# ── DB 연결 정보 (환경변수로 설정) ──────────────────────────────────────────
DB_CONFIG = {
    "host":     os.environ.get("DB_HOST",     "localhost"),
    "port":     os.environ.get("DB_PORT",     "5432"),
    "dbname":   os.environ.get("DB_NAME",     "postgres"),
    "user":     os.environ.get("DB_USER",     "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
}

# ── 국민연금 SEC EDGAR CIK ───────────────────────────────────────────────────
NPS_CIK = "0001608046"   # National Pension Service of Korea (전주시, 전라북도)

# ── 사용자 분기 → SEC period_of_report 매핑 ─────────────────────────────────
# 사용자 정의 분기와 SEC 13F 제출 기준 분기말 날짜를 일치시킵니다.
QUARTER_PERIOD = {
    1: "03-31",
    2: "06-30",
    3: "09-30",
    4: "12-31",
}

SEC_HEADERS = {
    "User-Agent": "NPS-13F-Tracker lcmoony1@gmail.com",  # SEC 정책: User-Agent 필수
    "Accept-Encoding": "gzip, deflate",
}


def sec_session() -> requests.Session:
    """타임아웃·재시도 설정이 적용된 requests Session을 반환합니다."""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(SEC_HEADERS)
    return session


def find_filing(year: int, quarter: int) -> tuple:
    """EDGAR에서 해당 연도·분기의 13F-HR accession number와 primaryDocument를 반환합니다."""
    period = f"{year}-{QUARTER_PERIOD[quarter]}"
    url = f"https://data.sec.gov/submissions/CIK{NPS_CIK}.json"

    resp = sec_session().get(url, timeout=60)
    resp.raise_for_status()

    filings = resp.json()["filings"]["recent"]
    for i, form in enumerate(filings["form"]):
        if form == "13F-HR" and filings["reportDate"][i] == period:
            return filings["accessionNumber"][i], filings["primaryDocument"][i]

    raise ValueError(
        f"{year}년 {quarter}분기({period}) 13F-HR 보고서를 EDGAR에서 찾을 수 없습니다."
    )


def get_total_value_usd(accession_number: str, primary_document: str) -> int:
    """primary_doc.xml의 tableValueTotal(천 달러 단위)을 달러로 반환합니다."""
    acc_clean = accession_number.replace("-", "")
    cik_int   = int(NPS_CIK)

    filename = primary_document.split("/")[-1]  # xslForm13F_X02/primary_doc.xml → primary_doc.xml
    xml_url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{acc_clean}/{filename}"
    )
    resp = sec_session().get(xml_url, timeout=60)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    for el in root.iter():
        if el.tag.split("}")[-1] == "tableValueTotal":
            return int(el.text.strip()) * 1000  # 천 달러 → 달러

    raise ValueError("primary_doc.xml에서 tableValueTotal을 찾을 수 없습니다.")


def save_to_db(institution: str, amount_usd: int) -> None:
    """PostgreSQL에 기관명과 운용금액을 저장합니다."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pension_holdings (
                        id          SERIAL PRIMARY KEY,
                        institution VARCHAR(255) NOT NULL,
                        amount_usd  BIGINT       NOT NULL,
                        created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
                    )
                    """,
                )
                cur.execute(
                    "INSERT INTO pension_holdings (institution, amount_usd) VALUES (%s, %s)",
                    (institution, amount_usd),
                )
    finally:
        conn.close()


def main(year_2digit: str, quarter_str: str) -> None:
    year    = 2000 + int(year_2digit)
    quarter = int(quarter_str)

    print(f"{year}년 {quarter}분기 국민연금 13F 보고서를 조회합니다...")

    accession, primary_doc = find_filing(year, quarter)
    print(f"보고서 accession number: {accession}")

    total_usd = get_total_value_usd(accession, primary_doc)

    save_to_db("국민연금", total_usd)

    print(f"{year}년도 {quarter}분기 국민연금의 운용금액은 {total_usd:,} 달러 입니다.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python a.py <year_2digit> <quarter>")
        print("  예시: python a.py 25 3")
        sys.exit(1)
 
    main(sys.argv[1], sys.argv[2])
