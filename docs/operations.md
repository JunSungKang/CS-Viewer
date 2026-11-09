# 운영 계약

## 접근과 제어

- 단일 운영 계정 `operator`, CSPRNG 비밀번호, HttpOnly/SameSite=Strict 세션, Origin/CSRF 검증을 적용한다. HTTPS origin에서 발급한 cookie는 로컬 origin 혼용 여부와 관계없이 Secure로 설정한다. 외부 HTTP origin 설정은 거절한다.
- 세션은 최대 8시간·유휴 30분이다. 서버 재시작 시 로그아웃된다. 로그인 실패는 IP와 전체 시도 수를 제한한다.
- 허용 Host와 Origin을 명시한다. 임의 X-Forwarded-* 헤더를 신뢰하지 않는다. 외부 접근은 인증된 사설 경로나 HTTPS reverse proxy와 정확한 `CS_ALLOWED_ORIGINS` 설정을 사용한다.
- 에이전트는 loopback만 수신하며 별도 비밀 토큰을 요구한다. Docker socket 접근은 호스트 에이전트에만 있다. 이 토큰과 호스트 실행 권한은 신뢰 경계이다.
- 앱 컨테이너는 읽기 전용 파일시스템, 비 root UID, capability 제거, no-new-privileges, 메모리/CPU 제한을 사용한다. 실제 비밀 파일 소유 UID/GID를 Compose에 전달한다.
- JSON 요청은 8KiB 이하, 전체 동시 연결은 32개로 제한한다. 환경변수·키·건강 검사 로그·메일 본문을 API 응답과 로그에 포함하지 않는다.

## 작업 순서

실제 ID와 예상 상태·확인 이름·요청 UUID를 보낸다. 호스트 에이전트는 공유 잠금을 획득한 뒤 daemon ID와 대상/허용 목록/현재 상태를 다시 확인한다. 감사 레코드를 먼저 영속화한 후 세 동작 중 하나만 호출하고 실제 상태를 확인한다. 재시작은 StartedAt 변경도 확인한다.

같은 요청 ID는 재실행하지 않는다. 다른 내용의 ID 재사용은 거절한다. 응답 단절이나 정상 상태 확인 시간 초과는 `unknown`으로 기록하고 자동 재시도하지 않는다. 에이전트 재시작 시 미완료 `pending`도 `unknown`으로 남긴다. 운영자는 상태와 이력을 먼저 확인한다.

공통 DB는 명시적으로 허용했을 때만 제어하며 화면에 다른 서비스 영향을 표시한다. 자기 자신, CRM, 복구용 이전 DB는 일반 제어에서 제외한다. 앱 외부에서 실행하는 배포와 이관도 동일한 macOS flock 파일을 사용해야 한다. 컨테이너 안의 별도 잠금 파일로 대체하지 않는다.

## 데이터와 측정

- SQLite WAL로 이력과 작업을 저장한다. 수집 10초, 최근 60회 표시, 측정 24시간/감사 90일 보관. 이 파일을 이미지나 Git에 넣지 않는다.
- HTTP GET은 연결부터 본문 수신까지 측정한다. 응답 1MiB·3초 제한, redirect 미추적이며 실제 HTTP 코드를 표시한다. TLS가 있는 연결 시간에는 TLS handshake가 포함된다. ICMP/단방향 지연이 아니다.
- p95는 화면에 표시하는 최근 측정 중 유효 응답으로 계산한다. 측정 실패는 추세의 붉은 점으로 표시한다. 수집 실패 시 이전 수집 시각을 유지하고 오류를 표시하며 제어를 잠근다.
- 호스트 사용 메모리는 vm_stat 기반 추정이다. Docker info의 MemTotal은 VM에서 보이는 총량이다. 컨테이너 사용 합계는 VM 전체 사용량이 아니다.
- OOMKilled는 Docker가 보고한 종료 이력이며 현재 프로세스가 종료되었다는 뜻으로 단정하지 않는다. RestartCount는 Docker의 누적 자동 재시작 횟수다.
- 이미지 크기는 공유 계층을 포함한 이미지 크기다. 합산해서 실제 추가 디스크 사용량으로 해석하지 않는다. RootFS diffID, RepoDigest, 로컬 image ID는 서로 다른 값이다. RepoDigest가 있다고 원격 레지스트리 배포를 검증한 것은 아니다.

실행 이미지는 표준 라이브러리만 필요하므로 pip와 wheel/venv 생성 도구를 제거한다. CPython 인터프리터와 이미 생성한 실행 venv는 유지한다. 공통 기반 이미지는 변경하지 않는다. 검사 원본과 SBOM은 로컬 운영 보고서에 별도로 남긴다. 초기 검사에서 pip의 vendored pkg_resources SBOM이 setuptools 전체 버전으로 인식되는 항목을 확인했으며, 설치 도구 제거 후 예외 필터 없이 재검사한다. 관련 [Trivy 유지관리자 설명](https://github.com/aquasecurity/trivy/discussions/11031)을 참고한다.

## 호스트 에이전트 유지

`supervise-agent.py`는 에이전트가 종료되면 5초 후 다시 기동한다. supervisor에는 SIGTERM으로 정상 종료를 요청한다. 토큰·DB·잠금은 보존한다. 재시작/코드 교체도 공유 잠금 안에서 수행한다.

현재 작업 환경의 launchd 직접 기동은 dyld 파일 열기 단계에서 멈추어 검증하지 못했다. 실제 운영은 기존 실행 환경에서 기동한 로컬 supervisor를 사용한다. Mac 재부팅 뒤 자동 기동은 별도 검증이 필요하며 현재 자동 로그인 시작을 보증하지 않는다. workspace 밖의 LaunchAgents나 OS 보안 설정은 변경하지 않았다.

## 복구와 백업

백업 실행·복원 API는 제공하지 않는다. 데이터 유형에 맞는 절차를 마운트 화면에서 안내한다. PostgreSQL은 DB별 논리 백업과 역할·권한, ZPL Market은 정상 정지한 H2 파일과 기존 서명 키, MailBox는 공유 writer를 고려한 저장소·계정·큐를 함께 다룬다. 복원은 격리 검증 후 운영 잠금 안에서 진행한다.

이 도구의 DB는 supervisor를 정상 종료한 뒤 관련 SQLite 파일과 설정을 일관되게 보존하거나 SQLite backup API를 사용한다. 새 운영 데이터가 발생한 뒤 이전 DB로 무조건 전환하지 않는다. 실제 데이터·볼륨을 삭제하는 down -v 또는 prune은 사용하지 않는다.
