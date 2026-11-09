# CS-Viewer

방법론: Sprint Delivery. 관제 보드, 컨테이너, 레이어 계층, 마운트·백업 네 화면을 구현한다.

## 구조

- Ubuntu 26.04 공통 CPython 3.14 이미지의 웹/API 컨테이너: 정적 UI, 로그인·세션·CSRF 검증, 호스트 에이전트에 제한된 요청 전달.
- macOS 호스트 에이전트: 명시한 Docker context의 Unix socket으로 Engine API 호출. Docker 환경 ID 확인, 허용 서비스 조회, 측정·이력 SQLite 보관, 실제 macOS flock 안에서 시작·중지·재시작 수행.
- 웹 컨테이너는 Docker socket을 마운트하지 않는다. 에이전트는 loopback으로만 수신하고 별도 비밀 토큰을 요구한다. 임의 Engine API, 파일 읽기, exec, 생성·삭제·이미지 변경은 제공하지 않는다.
- 공유 잠금 경로와 토큰·암호·수집 설정·DB는 로컬 operations에만 보관한다. 저장소에는 예제 설정만 포함한다.

## 수용 기준과 작업

1. 실제 데이터 관제
   - 호스트/VM 메모리와 공통 인프라 최상단, HTTP 측정이 느린 순의 서비스 카드.
   - 실제 측정 이력 저장과 지연·오류·수집 지연 구분. 수집 실패 시 과거 값을 정상 최신 값처럼 표시하지 않는다.
2. 제한된 운영 제어
   - 로그인/CSRF/요청 크기/인증 실패 제한. 이름 대신 실제 ID와 예상 상태 재확인.
   - 공유 flock 안에서 대상과 daemon ID 재검증, 먼저 감사 기록 저장, 상태 검증 및 결과 기록.
   - 자기 자신·CRM·복구용 이전 DB는 일반 제어 허용 목록에서 제외.
3. 이미지와 저장소 조회
   - 실제 RootFS layer digest의 공통 prefix 기준으로 공통 OS/런타임과 앱 이미지를 표시.
   - 실제 mount 원본/대상/RO/RW/공유 대상을 표시. 실제 백업·복원 실행 기능은 제공하지 않으며 미연동을 명시한다.
4. 납품
   - 백엔드 의미 있는 회귀 검사, 모바일/데스크톱 기본 조작, 격리 컨테이너 lifecycle 검증.
   - 운영 배포, GitHub PR current-head App 검토/검사, 사람 병합과 branch 세 곳 정리.

## 이름

`<module>-<role>` 표시와 명시적 module/role 설정을 사용한다. 대시를 자동 계층으로 해석하지 않는다. 실제 단일 컨테이너 이름 전환 시 Compose container_name만 로컬 override로 바꾸고 기존 service DNS와 mount를 보존한다. 실제 ID를 제어 키로 사용한다.

## 외부 계약

Docker Engine API의 info, containers/json, containers/{id}/json, containers/{id}/stats, images/{id}/json 및 start/stop/restart만 사용한다. https://docs.docker.com/reference/api/engine/version/v1.56/

변경 금지: 기존 데이터/키/볼륨 삭제, GitHub에 operations 또는 비밀값 업로드, 컨테이너 생성·삭제 메뉴, CRM 기동, 광범위한 prune/remove-orphans.
