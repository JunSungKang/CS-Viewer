# CS-Viewer

Docker 컨테이너 상태·포트·이미지 계층·마운트를 확인하고 등록된 컨테이너를 시작·중지·재시작하는 운영 관제 도구입니다. 주소를 열면 바로 관제 보드가 표시됩니다.

## 화면

- 관제 보드: 호스트와 Docker VM 메모리, HTTP 응답 시간, 실제 측정 추세, OOM 이력과 재시작 횟수.
- 컨테이너: 명시한 모듈과 실행 역할, 실제 ID·이름·포트, 시작·중지·재시작 및 작업 이력.
- 레이어 계층: 실제 파일시스템 diffID의 공통 prefix와 앱 이미지 계층.
- 마운트·백업: 원본·대상·RO/RW·공유 경로 및 백업 절차. 백업·복원 자동 실행은 미연동입니다.

## 실행 구조

브라우저 → 웹/API 컨테이너(내부 8080) → macOS 호스트 에이전트(127.0.0.1:20135) → 지정한 Docker context의 Unix socket.

웹과 에이전트 API는 접속 즉시 사용할 수 있습니다. 별도 사용자 계정이나 자격 증명 파일은 필요하지 않습니다. 공개 네트워크에 배포하면 해당 주소에 접속한 방문자가 관리 인벤토리의 조회와 제어를 사용할 수 있습니다.

웹 컨테이너에는 Docker socket을 마운트하지 않습니다. 에이전트는 관리 인벤토리와 실제 daemon/컨테이너 ID를 확인하고 공유 macOS flock 안에서 세 가지 lifecycle 작업만 실행합니다. 이름 확인, 예상 상태, 요청 UUID 중복 방지, 실제 결과 검증은 유지합니다.

## 시작

Python 3.14와 Docker Desktop의 실제 운영 context가 필요합니다. 추가 Python/JavaScript 패키지는 없습니다.

1. [예제 설정](docs/config.example.json)을 workspace의 `operations/cs-viewer/agent.json`에 복사합니다. 실제 Docker socket·daemon ID, 공유 잠금·측정 DB 경로, 관리 인벤토리와 측정 주소를 지정합니다.
2. 호스트에서 `python3.14 -B scripts/supervise-agent.py /absolute/workspace/operations/cs-viewer/agent.json`을 실행합니다. 로그·PID·DB는 workspace 안에 둡니다.
3. 현재 아키텍처에서 이미지를 빌드합니다. `PYTHON_IMAGE`로 공통 Ubuntu 26.04 / CPython 3.14 기반 이미지를 지정할 수 있습니다.
4. 공유 운영 잠금 안에서 해당 웹 서비스를 교체합니다. `CS_AGENT_URL`로 로컬 에이전트 주소를 지정합니다. 직접 실행 Compose는 호스트 20035에서 내부 8080으로 연결합니다.

실제 공통 운영은 `operations/compose.yaml`이 포함한 현재 모델을 사용합니다. Nginx TLS 구성의 공개 주소는 **https://175.195.62.165:20035/**이며 HTTP 20036은 HTTPS로 이동합니다. 기존 공통 프록시·인증서·다른 서비스는 이 앱의 배포로 변경하지 않습니다.

## 검증

```sh
python3.14 -B -m unittest discover -s tests -v
node --check web/app.js
docker --context desktop-linux compose -f compose.platform.yaml config --quiet
python3.14 -B scripts/smoke-docker.py --config /absolute/workspace/operations/cs-viewer/agent.json
```

마지막 명령은 소유 표식이 있는 격리 컨테이너 하나로 웹→에이전트→공유 잠금→실제 시작/중지/재시작과 중복 방지를 확인한 뒤 그 컨테이너만 제거합니다. 운영 컨테이너·볼륨은 변경하지 않습니다. 임시 산출물은 checkout의 `.runtime/`에 둡니다.

[구조·수용 기준](docs/architecture.md) · [운영](docs/operations.md) · [검증](docs/validation.md)
