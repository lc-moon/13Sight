"""분기별 제출 마감일(2/20, 5/20, 8/20, 11/20)에 collect_institutions.py를 자동 실행하는 APScheduler 스케줄러."""
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
    기준일에 따른 분기 판별 로직:
    - 1분기: 02/20 ~ 05/19
    - 2분기: 05/20 ~ 08/19
    - 3분기: 08/20 ~ 11/19
    - 4분기: 11/20 ~ 익년 02/19
    """
    curr_year = date_obj.year
    
    # 각 분기의 시작 날짜 설정
    q1_start = datetime(curr_year, 2, 20)
    q2_start = datetime(curr_year, 5, 20)
    q3_start = datetime(curr_year, 8, 20)
    q4_start = datetime(curr_year, 11, 20)

    if date_obj < q1_start:
        # 1월 1일 ~ 2월 19일 사이라면 작년 4분기
        year = curr_year - 1
        quarter = 4
    elif q1_start <= date_obj < q2_start:
        year = curr_year
        quarter = 1
    elif q2_start <= date_obj < q3_start:
        year = curr_year
        quarter = 2
    elif q3_start <= date_obj < q4_start:
        year = curr_year
        quarter = 3
    else:
        # 11월 20일 ~ 12월 31일 사이라면 올해 4분기
        year = curr_year
        quarter = 4
        
    return year, quarter

def check_and_run():
    """날짜를 확인하고 조건에 맞으면 collect_institutions.py를 실행합니다."""
    now = datetime.now()
    current_month = now.month
    current_day = now.day

    timestamp = now.strftime('%Y-%m-%d %H:%M:%S')

    # 오늘이 지정된 날짜 중 하나인지 확인
    if (current_month, current_day) in TARGET_DATES:
        print(f"[{timestamp}] ✓ 지정된 날짜입니다. collect_institutions.py를 실행합니다.")

        # 연도와 분기 정보 가져오기
        year, quarter = get_quarter(now)

        script_dir = os.path.dirname(os.path.abspath(__file__))
        aa_py_path = os.path.join(script_dir, 'collect_institutions.py')

        try:
            result = subprocess.run([sys.executable, aa_py_path, str(year), str(quarter)], capture_output=True, text=True)
            print(f"[{timestamp}] collect_institutions.py 실행 완료")
            if result.stdout:
                print(f"[{timestamp}] 출력: {result.stdout.strip()}")
            if result.stderr:
                print(f"[{timestamp}] 오류: {result.stderr.strip()}")
        except Exception as e:
            print(f"[{timestamp}] collect_institutions.py 실행 중 오류 발생: {e}")
    else:
        print(f"[{timestamp}] ✗ 지정된 날짜가 아닙니다. (현재: {current_month}월 {current_day}일)")

def main():
    """스케줄러를 시작합니다."""
    scheduler = BackgroundScheduler()

    # 매일 자정(00:00)에 작업 실행
    scheduler.add_job(check_and_run, 'cron', hour=0, minute=0)

    scheduler.start()

    print("=" * 60)
    print("스케줄러가 시작되었습니다.")
    print("매일 자정(00:00)에 다음 날짜를 확인합니다:")
    print("- 2월 20일")
    print("- 5월 20일")
    print("- 8월 20일")
    print("- 11월 20일")
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
