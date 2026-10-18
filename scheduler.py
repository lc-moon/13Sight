"""분기별 제출 마감일(2/20, 5/20, 8/20, 11/20)에 수집 파이프라인(기관 → 보유 종목 → consensus)을,
매일 미국 장 마감 후 주가 갱신(collect_prices)을 자동 실행하는 APScheduler 스케줄러."""
import os
import sys
import time
import subprocess
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler



# 특정 날짜 확인 (2월 20일, 5월 20일, 8월 20일, 11월 20일)
TARGET_DATES = [(2, 20), (5, 20), (8, 20), (11, 20)]

def get_quarter(date_obj):
    """
    기준일 시점에 13F 제출 마감(분기말+45일)이 지난 가장 최근 보고 분기를 반환:
    - 02/20 ~ 05/19: 작년 4분기 (12/31 보고 → 2/14 마감)
    - 05/20 ~ 08/19: 1분기 (3/31 보고 → 5/15 마감)
    - 08/20 ~ 11/19: 2분기 (6/30 보고 → 8/14 마감)
    - 11/20 ~ 익년 02/19: 3분기 (9/30 보고 → 11/14 마감)
    """
    curr_year = date_obj.year

    # 각 구간의 시작 날짜 설정
    q1_start = datetime(curr_year, 2, 20)
    q2_start = datetime(curr_year, 5, 20)
    q3_start = datetime(curr_year, 8, 20)
    q4_start = datetime(curr_year, 11, 20)

    if date_obj < q1_start:
        # 1월 1일 ~ 2월 19일 사이라면 작년 3분기
        year = curr_year - 1
        quarter = 3
    elif q1_start <= date_obj < q2_start:
        year = curr_year - 1
        quarter = 4
    elif q2_start <= date_obj < q3_start:
        year = curr_year
        quarter = 1
    elif q3_start <= date_obj < q4_start:
        year = curr_year
        quarter = 2
    else:
        # 11월 20일 ~ 12월 31일 사이라면 올해 3분기
        year = curr_year
        quarter = 3

    return year, quarter

def run_pipeline(year, quarter, timestamp):
    """collect_institutions → collect_holdings → build_consensus를 순서대로 실행한다.
    앞 단계가 실패(종료코드 ≠ 0)하면 뒤 단계는 실행하지 않는다."""
    return run_steps([
        ['collect_institutions.py', str(year), str(quarter)],
        ['collect_holdings.py'],
        ['build_consensus.py'],
    ], timestamp)


def run_daily_prices():
    """매일 미국 장 마감 후 일봉·분할 이력을 증분 수집하고 분기 가격대 요약을 갱신한다."""
    run_steps([['collect_prices.py']], datetime.now().strftime('%Y-%m-%d %H:%M:%S'))


def run_steps(steps, timestamp):
    """[스크립트, *인자] 목록을 순서대로 실행한다. 실패(종료코드 ≠ 0) 시 중단하고 False 반환."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for script, *args in steps:
        path = os.path.join(script_dir, script)
        try:
            result = subprocess.run([sys.executable, path, *args], capture_output=True, text=True)
        except Exception as e:
            print(f"[{timestamp}] {script} 실행 중 오류 발생: {e} → 파이프라인 중단")
            return False
        if result.stdout:
            print(f"[{timestamp}] {script} 출력: {result.stdout.strip()}")
        if result.stderr:
            print(f"[{timestamp}] {script} 오류: {result.stderr.strip()}")
        if result.returncode != 0:
            print(f"[{timestamp}] {script} 실패 (종료코드 {result.returncode}) → 파이프라인 중단")
            return False
        print(f"[{timestamp}] {script} 실행 완료")
    return True


def check_and_run():
    """날짜를 확인하고 조건에 맞으면 수집 파이프라인을 실행합니다."""
    now = datetime.now()
    current_month = now.month
    current_day = now.day

    timestamp = now.strftime('%Y-%m-%d %H:%M:%S')

    # 오늘이 지정된 날짜 중 하나인지 확인
    if (current_month, current_day) in TARGET_DATES:
        # 연도와 분기 정보 가져오기
        year, quarter = get_quarter(now)
        print(f"[{timestamp}] ✓ 지정된 날짜입니다. {year}년 {quarter}분기 수집 파이프라인을 실행합니다.")
        run_pipeline(year, quarter, timestamp)
    else:
        print(f"[{timestamp}] ✗ 지정된 날짜가 아닙니다. (현재: {current_month}월 {current_day}일)")

def main():
    """스케줄러를 시작합니다."""
    scheduler = BackgroundScheduler()

    # 매일 자정(00:00)에 작업 실행
    scheduler.add_job(check_and_run, 'cron', hour=0, minute=0)
    # 매일 23:30(VM 시간 UTC, 한국 08:30)에 주가 갱신 — 미국 장 마감(20:00~21:00 UTC) 이후
    scheduler.add_job(run_daily_prices, 'cron', hour=23, minute=30)

    scheduler.start()

    print("=" * 60)
    print("스케줄러가 시작되었습니다.")
    print("매일 자정(00:00)에 다음 날짜를 확인합니다:")
    print("- 2월 20일")
    print("- 5월 20일")
    print("- 8월 20일")
    print("- 11월 20일")
    print("매일 23:30에 주가(collect_prices.py)를 갱신합니다.")
    print("\n종료하려면 Ctrl+C를 누르세요.")
    print("=" * 60)

    try:
        # 스케줄러를 계속 실행 (busy-loop 대신 sleep으로 CPU 점유 방지)
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n스케줄러를 종료합니다.")
        scheduler.shutdown()

if __name__ == '__main__':
    main()
