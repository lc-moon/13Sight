#!/usr/bin/env bash
# 로컬 코드 변경분을 프로덕션 VM에 배포하고 웹 서비스를 재시작한다.
# .env·venv·DB·데이터는 건드리지 않고 코드만 갱신한다.
# institution_names_kr.json은 VM의 분기 수집이 갱신하므로 덮어쓰지 않는다(VM 파일이 기준).
#
# 사용법: 프로젝트 루트에서  bash deploy/redeploy.sh
#         (스케줄러 코드도 바꿨으면)  bash deploy/redeploy.sh --scheduler
set -e

KEY="$HOME/Downloads/ssh-key-2026-06-15.key"   # SSH 개인키 경로
HOST="ubuntu@161.33.143.61"                     # VM 접속 대상
URL="http://161.33.143.61:5001"

# 프로젝트 루트인지 확인 (utils.py·v2가 있어야 함)
if [ ! -f utils.py ] || [ ! -d v2 ]; then
  echo "에러: 프로젝트 루트에서 실행하세요 (utils.py / v2 폴더가 보이는 위치)."
  exit 1
fi

echo "[1/4] 코드 압축..."
tar czf /tmp/13sight_code.tar.gz \
  --exclude='.git' --exclude='venv' --exclude='__pycache__' \
  --exclude='.env' --exclude='13sight.sql' --exclude='.claude' \
  --exclude='institution_names_kr.json' .

echo "[2/4] 서버로 전송..."
scp -i "$KEY" /tmp/13sight_code.tar.gz "$HOST:/tmp/"

echo "[3/4] 서버에 적용 + 서비스 재시작..."
RESTART_SCHED=""
[ "$1" = "--scheduler" ] && RESTART_SCHED="sudo systemctl restart 13sight-scheduler"
ssh -i "$KEY" "$HOST" "bash -s" <<EOF
set -e
# 코드만 덮어쓰기 (.env·venv는 tar에서 제외돼 보존됨)
sudo tar xzf /tmp/13sight_code.tar.gz -C /opt/13sight
sudo chown -R ubuntu:ubuntu /opt/13sight
# requirements 바뀌었으면 반영
/opt/13sight/venv/bin/pip install -q -r /opt/13sight/requirements.txt
sudo systemctl restart 13sight-web
$RESTART_SCHED
sleep 3
echo "web 상태: \$(systemctl is-active 13sight-web)"
curl -s -o /dev/null -w "로컬 응답: HTTP %{http_code}\n" http://localhost:5001/
EOF

echo "[4/4] 완료 → $URL"
