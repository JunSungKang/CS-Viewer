# CS Viewer 구조

웹/API 컨테이너 → macOS 호스트 에이전트 → 지정 Docker Engine Unix socket 및 ClickHouse Server 구조다. 웹에 DB 자격 증명이나 Docker socket을 전달하지 않는다. 현재 상태·이미지·마운트와 등록 컨테이너의 시작/중지/재시작을 제공한다.

ClickHouse가 유일한 영구 DB다. service_samples는 고정 자료형 지표, container_logs는 시간·서비스·컨테이너·stream·원문, action_events는 append-only 제어 이력이다. 월 파티션과 서비스/시간 정렬, 낮은 단계의 압축, 제한된 배치를 사용한다. 기본 보존은 최소 3년이며 운영 명령으로 변경한다.

수집·최신 캐시와 DB writer를 분리한다. outbox와 offset은 workspace 안의 원자적 파일로 관리한다. DB 장애를 정상 기록으로 표시하지 않는다. 요청 UUID·fingerprint·실제 컨테이너 ID·예상 상태·확인 이름을 검증하고, 공유 flock 안에서 intent 영구 기록 후 동작한다. 동일 요청과 미완료 요청을 재실행하지 않는다. ClickHouse 정렬 키는 유일성 제약으로 사용하지 않는다.

원문 로그 조회는 인증된 로컬 운영 CLI다. 웹/API에는 내장 인증이나 역할별 권한 기능이 없으므로 접속 가능한 사용자는 관리 인벤토리를 조회하고 제어할 수 있다. 공유 배포에서는 인증된 프록시나 신뢰할 수 있는 접근 경계를 사용한다. 컨테이너 생성·삭제·exec·임의 파일/Engine API·자동 백업/복원은 제공하지 않는다. 다중 host writer는 별도 조정 계층 없이 지원하지 않는다.
