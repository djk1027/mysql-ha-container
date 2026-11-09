###########################
# 1. 기존 충돌 패키지 제거
###########################
sudo apt update

for pkg in docker.io docker-compose docker-compose-v2 \
  docker-doc podman-docker containerd runc; do
    sudo apt remove -y "$pkg" 2>/dev/null || true
done

###########################
# 2. Docker 공식 저장소 등록
###########################
sudo apt install -y ca-certificates curl

sudo install -m 0755 -d /etc/apt/keyrings

sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc

sudo chmod a+r /etc/apt/keyrings/docker.asc

#################################################################

# 재시도
sudo apt install -y ca-certificates curl

sudo install -m 0755 -d /etc/apt/keyrings

sudo curl -fsSL \
  https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc

sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF

###########################
# 3. Docker Engine + Compose 설치
###########################
sudo apt update

sudo apt install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin


###########################
# 4. Docker 서비스 시작
###########################
sudo systemctl enable --now docker
sudo systemctl status docker

# 에러 조치
sudo systemctl stop docker.service docker.socket
sudo systemctl reset-failed docker.service docker.socket
sudo systemctl start docker.socket
sudo systemctl start docker.service

###########################
# 5. sudo 없이 Docker 사용
###########################
sudo usermod -aG docker "$USER"
newgrp docker


###########################
# 6. 컨테이너 시작
###########################
cd ~/mysql-ha-container

# YAML 설정 검증
docker compose config --services

# 컨테이너 생성 및 실행
docker compose up -d

# 실행 상태 확인
docker compose ps


###########################
# 7. 컨테이너 조작 
###########################
docker exec -it mysql-server-1 mysql -uroot -p

# Replica 1
docker exec -it mysql-server-2 mysql -uroot -p

# Replica 2
docker exec -it mysql-server-3 mysql -uroot -p