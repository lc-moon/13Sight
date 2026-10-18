# 13Sight 배포 가이드 (Oracle Cloud Always Free · IP:포트)

> **🌐 외부 접속 URL: http://161.33.143.61:5001** · VM 접속: `ssh -i ~/Downloads/ssh-key-2026-06-15.key ubuntu@161.33.143.61`

단일 VM 한 대에서 **Flask 웹(gunicorn) + APScheduler + PostgreSQL**을 함께 띄운다.
접속은 우선 `http://<VM공인IP>:5001` 형태(도메인/HTTPS는 추후).

- **OS 기준**: Ubuntu 22.04 (VM 생성 시 Ubuntu 이미지 선택)
- **데이터 이전**: 로컬 PostgreSQL → `pg_dump` → VM 복원
- **민준님이 직접 하셔야 하는 것**: Oracle 계정/VM 생성, SSH 접속, 비밀번호 입력, 보안목록/방화벽 포트 개방

> 명령어의 `<...>` 부분은 본인 값으로 치환. VM 안에서 도는 명령은 `[VM]`, 로컬(Windows)에서 도는 건 `[로컬]`로 표기.

---

## 1단계 — Oracle VM 생성 [Oracle 콘솔]

1. cloud.oracle.com 로그인 → **Compute > Instances > Create Instance**
2. **Image**: Canonical **Ubuntu 22.04** 선택
3. **Shape**: `VM.Standard.A1.Flex` (Ampere ARM, Always Free)
   - OCPU **2** / Memory **12 GB** 권장 (무료 한도 내, 90만행 DB·gunicorn에 여유)
   - AMD 마이크로(1GB)도 가능하나 Postgres+앱엔 빠듯함
4. **SSH keys**: "Generate a key pair for me"로 **private key 다운로드**(`.key`) 하거나 본인 공개키 등록
5. Create → 인스턴스의 **Public IP** 메모

## 2단계 — 포트 5001 개방 (Oracle 보안목록) [Oracle 콘솔]

VM이 속한 서브넷의 Security List에 인그레스 규칙 추가:
- Instance 상세 → **Virtual Cloud Network** → **Subnet** → **Security List** 클릭
- **Add Ingress Rules**:
  - Source CIDR: `0.0.0.0/0`
  - IP Protocol: **TCP**
  - Destination Port Range: **5001**
  - Add

> 이걸 빼먹으면 브라우저에서 절대 접속 안 됨 (가장 흔한 실수).

---

## 3단계 — SSH 접속 & 시스템 준비 [로컬 → VM]

```powershell
# [로컬] PowerShell. private key 권한이 너무 열려있으면 접속 거부되니 주의.
ssh -i <다운받은키.key> ubuntu@<VM공인IP>
```

```bash
# [VM] 패키지 설치
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv python3-pip postgresql postgresql-contrib git
```

## 4단계 — PostgreSQL 설정 [VM]

```bash
# [VM] postgres 계정 비밀번호 설정 — 비밀번호는 직접 입력하세요.
sudo -u postgres psql
```
psql 안에서:
```sql
ALTER USER postgres WITH PASSWORD '여기에_강력한_비밀번호';
\q
```

DB는 기본 `postgres`를 그대로 사용한다(앱 기본값과 일치). 로컬 접속만 쓰므로 추가 설정 불필요.

---

## 5단계 — 코드 배포 [로컬 → VM]

> GitHub 저장소가 **비공개**라 VM에서 `git clone`은 인증 때문에 실패한다
> (`could not read Username for 'https://github.com'`). 그래서 **로컬 코드를 tar로 묶어
> scp로 보낸다.** (institution_names_kr.json 포함, .git/venv/.env/덤프는 제외.)

```bash
# [로컬] 프로젝트 루트에서 코드 압축 후 전송
tar czf /tmp/13sight_code.tar.gz \
  --exclude='.git' --exclude='venv' --exclude='__pycache__' \
  --exclude='.env' --exclude='13sight.sql' --exclude='.claude' .
scp -i <키.key> /tmp/13sight_code.tar.gz ubuntu@<VM공인IP>:/tmp/
```

```bash
# [VM] /opt/13sight 에 풀기
sudo rm -rf /opt/13sight && sudo mkdir -p /opt/13sight
sudo tar xzf /tmp/13sight_code.tar.gz -C /opt/13sight
sudo chown -R ubuntu:ubuntu /opt/13sight
cd /opt/13sight

# 가상환경 + 의존성
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```
> 저장소를 공개로 바꾸면 `git clone https://github.com/lc-moon/13Sight.git /opt/13sight`로
> 대체 가능. 코드 업데이트 시에도 비공개인 한 위 tar+scp 방식을 쓴다.

## 6단계 — .env 작성 [VM]

```bash
# [VM] .env 는 git에 없으므로 직접 생성. DB_PASSWORD는 4단계에서 정한 값.
nano /opt/13sight/.env
```
내용:
```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=postgres
DB_USER=postgres
DB_PASSWORD=4단계에서_정한_비밀번호

OPENFIGI_API_KEY=
GEMINI_API_KEY=
```
> `OPENFIGI_API_KEY`/`GEMINI_API_KEY`는 분기 수집·번역에만 필요. 웹 구동에는 없어도 됨.

---

## 7단계 — 데이터 이전 (로컬 → VM) [로컬 → VM]

> **버전 주의**: 로컬은 PostgreSQL 18, VM(Ubuntu 22.04 기본)은 14다. 메이저 버전이
> 달라 커스텀 포맷(`-Fc`)+`pg_restore`는 복원이 안 된다. 그래서 **버전 독립적인
> 평문 SQL(`-Fp`)을 `psql`로 복원**한다. 덤프 파일(`13sight.sql`, 약 111MB)은
> 프로젝트 루트에 이미 생성해 둠.

```powershell
# [로컬] (이미 만들어둔 13sight.sql 사용. 다시 떠야 하면 아래 명령. PGPASSWORD에 로컬 비번)
# $env:PGPASSWORD="로컬_postgres_비번"
# pg_dump -U postgres -h localhost -d postgres --no-owner --no-privileges `
#   -t all_institutions -t top100_institutions -t consensus -t holdings `
#   -Fp -f 13sight.sql

# VM으로 전송 (프로젝트 루트에서 실행)
scp -i <키.key> 13sight.sql ubuntu@<VM공인IP>:/tmp/
```

```bash
# [VM] 복원 (postgres 비번 = 4단계 값). psql로 평문 SQL 적용.
psql -U postgres -h localhost -d postgres -f /tmp/13sight.sql

# 확인
sudo -u postgres psql -d postgres -c "SELECT
  (SELECT count(*) FROM top100_institutions) AS top100,
  (SELECT count(*) FROM holdings)            AS holdings,
  (SELECT count(*) FROM consensus)           AS consensus;"
# 기대값: top100=100, holdings≈900,908, consensus≈12,550
```

---

## 8단계 — systemd 서비스 등록 [VM]

```bash
# [VM] 리포의 서비스 파일 설치
sudo cp /opt/13sight/deploy/13sight-web.service       /etc/systemd/system/
sudo cp /opt/13sight/deploy/13sight-scheduler.service /etc/systemd/system/

# 서비스 파일의 User=opc 를 Ubuntu 사용자(ubuntu)로 변경
sudo sed -i 's/^User=opc/User=ubuntu/' /etc/systemd/system/13sight-web.service
sudo sed -i 's/^User=opc/User=ubuntu/' /etc/systemd/system/13sight-scheduler.service

sudo systemctl daemon-reload
sudo systemctl enable --now 13sight-web 13sight-scheduler

# 상태 확인 (active (running) 이어야 함)
systemctl status 13sight-web --no-pager
systemctl status 13sight-scheduler --no-pager
```

## 9단계 — VM 내부 방화벽 개방 [VM]

OCI Ubuntu 이미지는 **iptables로 인바운드를 기본 차단**한다. 2단계(보안목록)와 별개로 VM 안에서도 열어야 한다.

> ⚠️ **규칙 순서가 핵심.** INPUT 체인 끝에 `REJECT all`이 있어서, 5001 ACCEPT를
> 그 **뒤**에 넣으면 효과가 없다(패킷이 REJECT에 먼저 걸림). 반드시 REJECT **앞**에 삽입한다.
> REJECT의 위치는 VM마다 다를 수 있으니(예: 5번 또는 6번) 먼저 줄번호를 확인하고 그 자리에 끼워 넣는다.

```bash
# [VM] 현재 INPUT 체인에서 REJECT 줄번호 확인
sudo iptables -L INPUT -n --line-numbers
# 예) 5번이 'REJECT all ...' 이면 5번 자리에 삽입 → REJECT가 6번으로 밀림
sudo iptables -I INPUT 5 -m state --state NEW -p tcp --dport 5001 -j ACCEPT
# 확인: 5001 ACCEPT가 REJECT 줄보다 위에 있어야 함
sudo iptables -L INPUT -n --line-numbers

# 영구 저장
sudo apt install -y iptables-persistent   # 저장 여부 물으면 Yes
sudo netfilter-persistent save
```

## 10단계 — 접속 확인

```bash
# [VM] 로컬 응답 확인 (HTML이 나오면 OK)
curl -s localhost:5001 | head -20
```
브라우저에서 **`http://<VM공인IP>:5001`** 접속 → 메인 페이지가 뜨면 완료.

---

## 운영 메모

```bash
# 로그 보기
journalctl -u 13sight-web -f
journalctl -u 13sight-scheduler -f

# 코드 업데이트 후 재배포
cd /opt/13sight && git pull
./venv/bin/pip install -r requirements.txt   # 의존성 변경 시
sudo systemctl restart 13sight-web

# 재시작 / 중지
sudo systemctl restart 13sight-web
sudo systemctl stop 13sight-scheduler
```

## 트러블슈팅

| 증상 | 점검 |
|---|---|
| 브라우저 접속 안 됨 | 2단계(보안목록 5001) + 9단계(iptables) 둘 다 했는지. `curl localhost:5001`은 되는데 외부만 안 되면 방화벽 문제 |
| 502/응답 없음 | `systemctl status 13sight-web`, `journalctl -u 13sight-web` 로 에러 확인 |
| DB 연결 실패 | `.env`의 DB_PASSWORD가 4단계 비번과 일치하는지, postgresql 실행 중인지(`systemctl status postgresql`) |
| 데이터 비어있음 | 7단계 복원 카운트 확인. 0이면 pg_restore 다시 |
| 분기 자동수집 검증 | `journalctl -u 13sight-scheduler` 에 자정마다 날짜 확인 로그가 찍히는지 |

## 보안 참고 (추후)

- 현재는 gunicorn이 5001을 공개 노출. 추후 도메인+HTTPS로 갈 땐 nginx 리버스 프록시(80/443) → gunicorn(127.0.0.1:5001)으로 바꾸고, 보안목록/iptables에서 5001 외부 개방을 닫는 걸 권장.
- postgres는 localhost 전용이라 외부 노출 없음(별도 포트 개방 금지).
