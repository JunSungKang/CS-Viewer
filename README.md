# CS-Viewer

Docker 컨테이너의 상태·실제 포트·공유 이미지 계층·영속 마운트를 확인하고, 허용한 컨테이너를 시작·중지·재시작하는 개인 운영 관제 도구입니다. 데스크톱 관제 화면과 모바일 화면을 지원합니다.

## 화면

- **관제 보드**: Mac 메모리, Docker VM 총 메모리, 공통 인프라를 최상단에 표시합니다. 서비스는 HTTP 오류를 우선하고 같은 상태에서는 전체 응답 시간이 느린 순으로 정렬합니다. 실제 수집 이력으로 최근 추세와 p95를 표시합니다. OOM 이력과 재시작 횟수도 구분합니다.
- **컨테이너**: 명시한 모듈 → 역할로 그룹핑합니다. 이름의 대시를 계층으로 해석하지 않습니다. 실제 ID·이름·호스트/내부 포트와 시작·중지·재시작, 최근 요청 이력을 제공합니다.
- **레이어 계층**: 실제 파일시스템 diffID의 공통 prefix로 기반을 나눕니다. 기반별 탭에서 공유 계층과 독립 앱 계층을 표시합니다.
- **마운트·백업**: 실제 원본/대상 경로, 볼륨 이름, 읽기/쓰기, 공유 컨테이너를 조회합니다. 백업·복원은 절차 안내이며 자동 실행과 백업 검증은 미연동입니다.

컨테이너 생성·삭제, exec, 이미지 빌드·배포·삭제, 포트 변경, 레지스트리 관리 메뉴는 제공하지 않습니다.

## 실행 구조

```text
브라우저 → 웹/API 컨테이너 (내부 8080, 로컬 20035)
              ↓ 별도 비밀 토큰
           macOS 호스트 에이전트 (127.0.0.1:20135)
              ├─ 고정한 Docker context의 Unix socket
              ├─ 호스트 메모리 / HTTP·연결 시간 측정
              ├─ SQLite 측정·감사 이력
              └─ 실제 macOS flock → 허용한 lifecycle 작업
```

웹 컨테이너는 Docker socket을 직접 마운트하지 않습니다. 임의 Engine API나 파일 접근을 프록시하지 않습니다. 에이전트 토큰은 사실상 운영 제어 권한이므로 로컬 비밀 파일로 관리합니다.

## 시작

Python **3.14**, Docker Desktop의 실제 운영 context, 로컬 공통 Ubuntu **26.04 / CPython 3.14** 이미지가 필요합니다. 추가 Python/JavaScript 패키지는 없습니다.

1. [예제 설정](docs/config.example.json)을 로컬 `operations/cs-viewer/agent.json`에 복사합니다. workspace 경계, 실제 Docker socket·daemon ID, 공유 잠금, 허용 서비스와 측정 대상을 설정합니다.
2. 별도 CSPRNG 비밀 파일 두 개를 최초 한 번 생성해 0600으로 보관합니다. `CS_VIEWER_PASSWORD_FILE`, `CS_VIEWER_AGENT_TOKEN_FILE`에는 값이 아닌 파일 경로를 전달합니다. 재시작 때 비밀을 재생성하지 않습니다.
3. 호스트에서 `python3.14 -B scripts/supervise-agent.py /absolute/workspace/operations/cs-viewer/agent.json`을 실행합니다. 로그·PID·DB는 지정한 workspace 안에 둡니다.
4. 실제 아키텍처에서 `docker --context desktop-linux buildx bake --load`로 빌드합니다. 공통 이미지는 `PYTHON_IMAGE` 빌드 ARG로 교체할 수 있습니다.
5. 비밀 파일 소유자의 `CS_VIEWER_UID`, `CS_VIEWER_GID`와 로컬 경로를 지정한 env 파일로 Compose를 기동합니다. 공유 운영 잠금을 보유한 프로세스 안에서 대상 서비스만 교체합니다.

```sh
docker --context desktop-linux compose --env-file /absolute/workspace/operations/cs-viewer/compose.env -f compose.platform.yaml config --quiet
docker --context desktop-linux compose --env-file /absolute/workspace/operations/cs-viewer/compose.env -f compose.platform.yaml up -d --no-build cs-viewer-web
```

접속 주소는 **http://127.0.0.1:20035**, 계정은 `operator`입니다. 비밀번호는 로컬 비밀 파일에서 확인합니다. 모바일 외부 접근은 별도의 사설 연결 또는 인증된 HTTPS 경로가 필요합니다. 기본 설정은 loopback으로만 공개합니다.

## 검증

```sh
python3.14 -B -m unittest discover -s tests -v
node --check web/app.js
python3.14 -B scripts/smoke-docker.py --config /absolute/workspace/operations/cs-viewer/agent.json
```

마지막 명령은 임의 이름의 격리 컨테이너 하나만 생성해 인증·CSRF·실제 잠금과 시작/중지/재시작/중복 요청을 검사하고, 소유권을 확인한 뒤 그 테스트 컨테이너만 제거합니다. 운영 컨테이너와 볼륨은 변경하지 않습니다. 테스트 산출물은 checkout의 `.runtime/`에 둡니다.

[구조·수용 기준](docs/architecture.md) · [운영](docs/operations.md) · [검증 결과](docs/validation.md)
