"""SEC EDGAR form.idx에서 해당 분기 전체 13F-HR 기관 목록과 운용금액을 수집해 institution 테이블에 UPSERT한다."""
import sys
import os
import re
import time
import requests
import psycopg2
from psycopg2.extras import execute_values
from xml.etree import ElementTree as ET
from utils import DB_CONFIG, sec_session

# 사용자 분기 → SEC 인덱스 파일 쿼터 매핑
# 13F-HR은 분기말 45일 이내 제출 → 인덱스 쿼터가 보고 분기와 다름
# (year_offset, index_qtr): year_offset=1 이면 다음 해
QUARTER_TO_INDEX = {
    1: (0, 2),  # Q1(03-31) → 같은 해 QTR2 (4~6월 제출)
    2: (0, 3),  # Q2(06-30) → 같은 해 QTR3
    3: (0, 4),  # Q3(09-30) → 같은 해 QTR4
    4: (1, 1),  # Q4(12-31) → 다음 해 QTR1 (1~2월 제출)
}

# form.idx 파싱용 정규식
# 컬럼: FormType  CompanyName  CIK  DateFiled  Filename
_IDX_RE = re.compile(
    r'^(.+?)\s{2,}(.+?)\s{2,}(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(edgar/.+)$'
)


# ─────────────────────────────────────────────────────────────────────────────
# 전체 기관 13F 수집 / institution 테이블
# ─────────────────────────────────────────────────────────────────────────────

def quarter_to_index_quarter(year: int, quarter: int) -> tuple:
    """사용자 분기를 SEC 인덱스 파일의 연도·쿼터로 변환합니다."""
    year_offset, idx_qtr = QUARTER_TO_INDEX[quarter]
    return year + year_offset, idx_qtr


def fetch_all_13f_filings(session: requests.Session, idx_year: int, idx_qtr: int) -> list:
    """
    EDGAR 분기 인덱스(form.idx)에서 13F-HR 제출 목록을 반환합니다.
    반환: [(cik, accession_number, company_name), ...]
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

            # 스키마 버전: 단위 결정에 사용
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

        # 스키마 버전에 따른 단위 변환
        # X0202 이상: 달러 단위 직접 기재 → 변환 없음
        # X0106 및 구버전: 천 달러 단위 → ×1000
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


def save_institutions_to_db(records: list) -> int:
    """
    institution 테이블에 (cik, institution, amount_usd) 레코드를 UPSERT합니다.
    cik 기준 중복 시 institution·amount_usd를 최신 값으로 갱신합니다.
    같은 배치 내 cik 중복은 마지막 값으로 유지합니다.
    반환: 중복 제거된 CIK 수
    """
    # 같은 배치 내 cik 중복 제거 (PostgreSQL ON CONFLICT는 배치 내 중복을 허용하지 않음)
    deduped = {row[0]: row for row in records}
    unique_records = list(deduped.values())
    dup_count = len(records) - len(unique_records)

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS institution (
                        cik         VARCHAR(20)  NOT NULL,
                        institution VARCHAR(500) NOT NULL,
                        amount_usd  BIGINT       NOT NULL,
                        CONSTRAINT institution_cik_unique UNIQUE (cik)
                    )
                    """
                )
                execute_values(
                    cur,
                    """
                    INSERT INTO institution (cik, institution, amount_usd)
                    VALUES %s
                    ON CONFLICT (cik) DO UPDATE
                        SET institution = EXCLUDED.institution,
                            amount_usd  = EXCLUDED.amount_usd
                    """,
                    unique_records,
                )
    finally:
        conn.close()

    return dup_count


# ─────────────────────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────────────────────

def main(year_arg: str, quarter_str: str) -> None:
    year_val = int(year_arg)
    year     = year_val if year_val >= 1000 else 2000 + year_val  # 2자리/4자리 모두 허용
    quarter  = int(quarter_str)

    print(f"\n{year}년 {quarter}분기 전체 기관 13F 수집을 시작합니다.")

    idx_year, idx_qtr = quarter_to_index_quarter(year, quarter)
    session = sec_session()

    filings = fetch_all_13f_filings(session, idx_year, idx_qtr)

    records    = []
    skip_count = 0

    for i, (cik, accession) in enumerate(filings, 1):
        institution, amount = get_institution_data(session, cik, accession)
        if institution and amount is not None:
            records.append((cik, institution, amount))
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



if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python collect_institutions.py <year> <quarter>")
        print("  예시: python collect_institutions.py 25 1  또는  python collect_institutions.py 2025 1")
        sys.exit(1)

    main(sys.argv[1], sys.argv[2])
