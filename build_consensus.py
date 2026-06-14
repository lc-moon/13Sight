"""top100 기관의 공통 매수 종목을 집계·점수화하여 consensus 테이블에 저장하는 전처리 스크립트."""
import math
import psycopg2
from utils import DB_CONFIG, prev_quarter_date, canonical_cusip, is_cusip_event


def fetch_target_dates(conn):
    """top100_institutions.report_date를 현재 분기로, 직전 분기말을 계산해 반환한다."""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(report_date) FROM top100_institutions")
        curr_date = cur.fetchone()[0]
    return curr_date, prev_quarter_date(curr_date)


def fetch_top100(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cik, curr_amount_usd, prev_amount_usd FROM top100_institutions"
        )
        return cur.fetchall()


def load_holdings(conn, report_date):
    """특정 report_date의 holdings를 {cik: {cusip: (company_name, ticker, value, shares)}} 로 반환한다."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cik, cusip, company_name, ticker, value, shares FROM holdings "
            "WHERE report_date = %s AND value > 0 AND cusip != '000000000'",
            (report_date,),
        )
        rows = cur.fetchall()

    result = {}
    for cik, cusip, company_name, ticker, value, shares in rows:
        # 구 CUSIP을 정본으로 통합해 분기 간 동일 종목으로 인식시킨다.
        result.setdefault(cik, {})[canonical_cusip(cusip)] = (company_name, ticker, value or 0, shares or 0)
    return result


def rank_desc(items):
    """(key, value) 쌍을 value 내림차순으로 RANK() 방식 순위 부여."""
    sorted_items = sorted(items, key=lambda x: -x[1])
    ranks = {}
    for i, (key, val) in enumerate(sorted_items):
        if i > 0 and val == sorted_items[i - 1][1]:
            ranks[key] = ranks[sorted_items[i - 1][0]]
        else:
            ranks[key] = i + 1
    return ranks


def build_consensus():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        curr_date, prev_date = fetch_target_dates(conn)
        print(f"현재 분기: {curr_date}  /  이전 분기: {prev_date}")

        top100 = fetch_top100(conn)
        print(f"top100 기관 수: {len(top100)}")

        curr_h = load_holdings(conn, curr_date)
        prev_h = load_holdings(conn, prev_date)

        cusip_meta = {}  # {cusip: (company_name, ticker)}
        inst_count = {}
        buy_amount = {}
        weight_chg = {}

        for cik, curr_aum, prev_aum in top100:
            curr = curr_h.get(cik, {})
            prev = prev_h.get(cik, {})

            if not prev:  # CT 또는 prev 데이터 없음 → 변화 판단 불가, 건너뜀
                continue

            for cusip, (company_name, ticker, curr_val, curr_shares) in curr.items():
                prev_entry   = prev.get(cusip)

                # 식별자 변경(ADR→보통주 등) 종목의 캐리오버는 주식수 환산비가 달라
                # 매수 판단이 불가능하므로 집계에서 제외한다(진짜 신규 보유만 통과).
                if is_cusip_event(cusip, prev_date, curr_date) and prev_entry is not None:
                    continue

                prev_val     = prev_entry[2] if prev_entry else 0
                prev_shares  = prev_entry[3] if prev_entry else 0

                if cusip not in cusip_meta:
                    cusip_meta[cusip] = (company_name, ticker)

                if curr_shares > prev_shares:  # 보유 주식 수 기준으로 매수 판단
                    inst_count[cusip] = inst_count.get(cusip, 0) + 1
                    buy_amount[cusip] = buy_amount.get(cusip, 0) + round(curr_val * (curr_shares - prev_shares) / curr_shares)
                    if curr_aum:
                        weight_chg[cusip] = weight_chg.get(cusip, 0) + curr_val * (curr_shares - prev_shares) / curr_shares / curr_aum

        inst_ranks   = rank_desc(inst_count.items())
        buy_ranks    = rank_desc(buy_amount.items())
        weight_ranks = rank_desc(weight_chg.items())

        n_inst   = len(inst_ranks)
        n_buy    = len(buy_ranks)
        n_weight = len(weight_ranks)

        active = set(inst_count) | set(weight_chg)

        scores = {}
        composite = {}
        for cusip in active:
            ir = inst_ranks.get(cusip)
            br = buy_ranks.get(cusip)
            wr = weight_ranks.get(cusip)
            i_s = 100 * (1 - math.log(ir) / math.log(n_inst + 1)) if ir is not None else 0.0
            b_s = 100 * (1 - math.log(br) / math.log(n_buy + 1)) if br is not None else 0.0
            w_s = 100 * (1 - math.log(wr) / math.log(n_weight + 1)) if wr is not None else 0.0
            scores[cusip] = (i_s, b_s, w_s)
            composite[cusip] = i_s * 0.5 + b_s * 0.3 + w_s * 0.2

        comp_ranks = rank_desc(composite.items())

        rows = []
        for cusip in active:
            company_name, ticker = cusip_meta[cusip]
            ir = inst_ranks.get(cusip)
            br = buy_ranks.get(cusip)
            wr = weight_ranks.get(cusip)
            cr = comp_ranks.get(cusip)
            i_s, b_s, w_s = scores[cusip]
            rows.append((
                cusip, ticker, company_name, curr_date,
                inst_count.get(cusip, 0), ir, round(i_s),
                buy_amount.get(cusip, 0), br, round(b_s),
                weight_chg.get(cusip, 0.0), wr, round(w_s),
                composite[cusip], cr,
            ))

        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE consensus")
            cur.executemany(
                """
                INSERT INTO consensus (
                    cusip, ticker, company_name, report_date,
                    institution_count, inst_rank, inst_score,
                    total_buy_amount, buy_rank, buy_score,
                    total_weight_change, weight_rank, weight_score,
                    composite_score, composite_rank
                ) VALUES (%s,%s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s)
                """,
                rows,
            )
            conn.commit()

        print(f"consensus 저장 완료: {len(rows)}개 종목")

    finally:
        conn.close()


if __name__ == "__main__":
    build_consensus()
