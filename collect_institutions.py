"""SEC EDGAR form.idx에서 해당 분기 전체 13F-HR 기관 목록과 운용금액을 수집해 all_institutions 테이블에 저장한다."""
import sys
import re
import time
import requests
import psycopg2
from datetime import date
from psycopg2.extras import execute_values
from xml.etree import ElementTree as ET
from utils import DB_CONFIG, sec_session, prev_quarter_date

# 분기말 날짜 (report_date 계산용)
QUARTER_END = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}

# 사용자 분기 → SEC 인덱스 파일 쿼터 매핑
# 13F-HR은 분기말 45일 이내 제출 → 인덱스 쿼터가 보고 분기와 다름
# (year_offset, index_qtr): year_offset=1 이면 다음 해
QUARTER_TO_INDEX = {
    1: (0, 2),  # Q1(03-31) → 같은 해 QTR2 (4~6월 제출)
    2: (0, 3),  # Q2(06-30) → 같은 해 QTR3
    3: (0, 4),  # Q3(09-30) → 같은 해 QTR4
    4: (1, 1),  # Q4(12-31) → 다음 해 QTR1 (1~2월 제출)
}

# top100 선정에서 제외할 CIK 목록 (비정상적인 AUM 신고 기관)
# all_institutions 테이블에는 원본 데이터를 유지하되 top100 선정 시에만 제외한다.
EXCLUDED_CIKS: set = {
    "0001569395",  # Mirae Asset Global Investments Co., Ltd. — BRK/A 오신고로 AUM 과대계상 (실제값의 ~1500배)
}

# form.idx 파싱용 정규식
# 컬럼: FormType  CompanyName  CIK  DateFiled  Filename
_IDX_RE = re.compile(
    r'^(.+?)\s{2,}(.+?)\s{2,}(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(edgar/.+)$'
)


# ─────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def quarter_to_index_quarter(year: int, quarter: int) -> tuple:
    """사용자 분기를 SEC 인덱스 파일의 연도·쿼터로 변환합니다."""
    year_offset, idx_qtr = QUARTER_TO_INDEX[quarter]
    return year + year_offset, idx_qtr


# ─────────────────────────────────────────────────────────────────────────────
# 데이터 수집
# ─────────────────────────────────────────────────────────────────────────────

def fetch_all_13f_filings(session: requests.Session, idx_year: int, idx_qtr: int) -> list:
    """
    EDGAR 분기 인덱스(form.idx)에서 13F-HR 제출 목록을 반환합니다.
    반환: [(cik, accession_number), ...]
    """
    url = (
        f"https://www.sec.gov/Archives/edgar/full-index/"
        f"{idx_year}/QTR{idx_qtr}/form.idx"
    )
    print(f"분기 인덱스 다운로드 중: {url}")

    resp = session.get(url, timeout=120)
    resp.raise_for_status()

    results = []
    for line in resp.text.splitlines():
        m = _IDX_RE.match(line)
        if not m:
            continue
        form_type, _company_name, _cik_col, _date_filed, filename = m.groups()
        if form_type.strip() != "13F-HR":
            continue

        # filename 형식: edgar/data/{cik}/{accession}.txt
        parts = filename.strip().split("/")
        if len(parts) < 4:
            continue
        cik       = parts[2]                    # e.g. "824142"
        acc_raw   = parts[3]                    # e.g. "0000824142-25-000012.txt"
        accession = acc_raw.rsplit(".", 1)[0]   # "0000824142-25-000012"

        results.append((cik, accession))        # 기관명은 primary_doc.xml에서 직접 파싱

    print(f"총 {len(results)}개 13F-HR 보고서를 발견했습니다.")
    return results


def get_institution_data(session: requests.Session, cik: str, accession: str):
    """
    primary_doc.xml에서 기관명(filingManager/name)과 tableValueTotal을 파싱합니다.
    성공 시 (institution_name, amount_usd), 실패 시 (None, None) 반환.
    """
    acc_clean = accession.replace("-", "")
    xml_url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik}/{acc_clean}/primary_doc.xml"
    )
    try:
        resp = session.get(xml_url, timeout=60)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        schema_version = None
        institution    = None
        amount_raw     = None

        for el in root.iter():
            tag = el.tag.split("}")[-1]

            if tag == "schemaVersion" and el.text:
                schema_version = el.text.strip()

            # 기관명: <filingManager><name>...</name></filingManager>
            if tag == "filingManager":
                for child in el:
                    if child.tag.split("}")[-1] == "name" and child.text:
                        institution = child.text.strip()
                        break

            # 운용금액 raw 값 수집
            if tag == "tableValueTotal" and el.text:
                amount_raw = int(el.text.strip())

        # X0202(2023년 이후): 달러 단위 직접 기재 → 변환 없음
        # X0106 및 구버전: 천 달러 단위 → ×1000
        # 단, 일부 기관이 X0202 스키마에서 천달러 단위로 잘못 신고하는 filing 오류가 있음 (e.g. T. Rowe Price)
        if amount_raw is not None:
            amount = amount_raw if schema_version == "X0202" else amount_raw * 1000
        else:
            amount = None

    except Exception as e:
        print(f"  [SKIP] CIK={cik} acc={accession} | {e}")
        return None, None

    if institution is None:
        print(f"  [SKIP] CIK={cik} | filingManager/name 태그 없음")
        return None, None
    if amount is None:
        print(f"  [SKIP] CIK={cik} | tableValueTotal 태그 없음")
        return None, None

    return institution, amount


# ─────────────────────────────────────────────────────────────────────────────
# DB 저장
# ─────────────────────────────────────────────────────────────────────────────

def fetch_prev_quarter_amount(session: requests.Session, cik_padded: str, prev_report_date: str):
    """
    EDGAR submissions API로 특정 CIK의 직전 분기 13F-HR accession을 찾아
    primary_doc.xml에서 운용금액을 파싱합니다.
    성공 시 amount_usd, 실패 시 None 반환.
    """
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    try:
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        recent = resp.json()["filings"]["recent"]

        accession = None
        for i, form in enumerate(recent["form"]):
            if form == "13F-HR" and recent["reportDate"][i] == prev_report_date:
                accession = recent["accessionNumber"][i]
                break

        if accession is None:
            return None

        cik_raw = str(int(cik_padded))  # EDGAR data URL은 숫자 CIK 사용
        _, amount = get_institution_data(session, cik_raw, accession)
        return amount
    except Exception as e:
        print(f"  [PREV] CIK={cik_padded} | {e}")
        return None


def save_institutions_to_db(records: list) -> int:
    """
    all_institutions 테이블을 TRUNCATE 후 records를 bulk INSERT합니다.
    같은 배치 내 (cik, report_date) 중복은 마지막 값으로 유지합니다.
    반환: 중복 제거 전후 차이(중복 수)
    """
    deduped = {(row[0], row[3]): row for row in records}
    unique_records = list(deduped.values())
    dup_count = len(records) - len(unique_records)

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS all_institutions (
                        cik         VARCHAR(20)  NOT NULL,
                        institution VARCHAR(500) NOT NULL,
                        amount_usd  BIGINT       NOT NULL,
                        report_date DATE,
                        CONSTRAINT all_institutions_cik_date_unique UNIQUE (cik, report_date)
                    )
                    """
                )
                cur.execute("TRUNCATE TABLE all_institutions")
                execute_values(
                    cur,
                    "INSERT INTO all_institutions (cik, institution, amount_usd, report_date) VALUES %s",
                    unique_records,
                )
    finally:
        conn.close()

    return dup_count


def build_top100_institutions(session: requests.Session, report_date: str, prev_report_date: str) -> None:
    """
    all_institutions에서 현재 분기 상위 100개 기관을 선정하고,
    각 기관의 직전 분기 운용금액을 EDGAR API로 조회해
    top100_institutions 테이블을 TRUNCATE 후 rank 포함 INSERT합니다.
    """
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS top100_institutions (
                    rank            SMALLINT     NOT NULL,
                    cik             VARCHAR(20)  NOT NULL,
                    institution     VARCHAR(500) NOT NULL,
                    prev_amount_usd BIGINT,
                    curr_amount_usd BIGINT       NOT NULL,
                    report_date     DATE
                )
                """
            )
            excluded = tuple(EXCLUDED_CIKS) if EXCLUDED_CIKS else ("",)
            cur.execute(
                """
                SELECT cik, institution, amount_usd
                FROM all_institutions
                WHERE report_date = %s
                  AND cik NOT IN %s
                ORDER BY amount_usd DESC
                LIMIT 100
                """,
                (report_date, excluded)
            )
            if EXCLUDED_CIKS:
                print(f"  [제외] {len(EXCLUDED_CIKS)}개 CIK 제외됨: {', '.join(EXCLUDED_CIKS)}")
            top100 = cur.fetchall()
        conn.commit()
    finally:
        conn.close()

    print(f"\n직전 분기({prev_report_date}) 운용금액 수집 중... (100개 기관)")
    records = []
    for rank, (cik, inst, curr_amt) in enumerate(top100, 1):
        prev_amt = fetch_prev_quarter_amount(session, cik, prev_report_date)
        records.append((rank, cik, inst, prev_amt, curr_amt, report_date))
        print(f"  [{rank:3d}/100] {inst[:40]:<40} prev={prev_amt:>15,}" if prev_amt else f"  [{rank:3d}/100] {inst[:40]:<40} prev=None")
        time.sleep(0.2)

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE top100_institutions")
                execute_values(
                    cur,
                    """
                    INSERT INTO top100_institutions
                        (rank, cik, institution, prev_amount_usd, curr_amount_usd, report_date)
                    VALUES %s
                    """,
                    records,
                )
    finally:
        conn.close()

    print(f"top100_institutions 갱신 완료 ({len(records)}개 기관)")


# ─────────────────────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────────────────────

def main(year_arg: str, quarter_str: str, top100_only: bool = False) -> None:
    year_val = int(year_arg)
    year     = year_val if year_val >= 1000 else 2000 + year_val  # 2자리/4자리 모두 허용
    quarter  = int(quarter_str)

    report_date = f"{year}-{QUARTER_END[quarter]}"
    p_date      = str(prev_quarter_date(date.fromisoformat(report_date)))
    session     = sec_session()

    if not top100_only:
        print(f"\n{year}년 {quarter}분기 전체 기관 13F 수집을 시작합니다.")
        idx_year, idx_qtr = quarter_to_index_quarter(year, quarter)
        filings = fetch_all_13f_filings(session, idx_year, idx_qtr)

        records    = []
        skip_count = 0

        for i, (cik, accession) in enumerate(filings, 1):
            institution, amount = get_institution_data(session, cik, accession)
            if institution and amount is not None:
                records.append((cik.zfill(10), institution, amount, report_date))
            else:
                skip_count += 1

            if i % 100 == 0:
                print(
                    f"  진행 중: {i}/{len(filings)} "
                    f"(수집 {len(records)} / 스킵 {skip_count})"
                )

            time.sleep(0.1)  # SEC 정책: 초당 10 요청 이내

        print(f"\nDB 저장 중... ({len(records)}개 기관)")
        dup_count = save_institutions_to_db(records)
        print(
            f"완료: {year}년 {quarter}분기 — "
            f"총 {len(records) - dup_count}개 기관 저장 / {skip_count}개 스킵 / 중복된 CIK {dup_count}개"
        )
    else:
        print(f"\n{year}년 {quarter}분기 top100 재선정을 시작합니다. (전체 기관 재수집 생략)")

    build_top100_institutions(session, report_date, p_date)

    from translate_institutions import translate_top100
    translate_top100()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python collect_institutions.py <year> <quarter> [--top100-only]")
        print("  예시: python collect_institutions.py 2026 1")
        print("        python collect_institutions.py 2026 1 --top100-only  # top100만 재선정")
        sys.exit(1)

    main(sys.argv[1], sys.argv[2], top100_only="--top100-only" in sys.argv)
