# 운영

ClickHouse Server가 필수다. 단일 호스트 에이전트가 Docker Engine과 ClickHouse에 접근하며 웹 컨테이너는 에이전트의 제한된 API만 사용한다. 앱 계정은 SELECT/INSERT, 초기화·보존 변경은 별도 운영 계정을 사용한다. 접속 정보는 workspace 안의 비추적 JSON 파일에서 읽는다.

설정 예시는 config.example.json이다. clickhouse_config, spool_path, log_state_path, lock_path는 workspace 내부 절대 경로여야 한다. 서버는 운영 환경에서 별도 컨테이너로 배포하며 데이터·설정·임시 파일·로그를 workspace에 bind mount한다. 공인 DB 포트는 열지 않는다.

서비스 inventory의 선택 항목 `port_labels`는 `3000/tcp`처럼 컨테이너 수신 포트를 키로 사용한다. 값은 `title`(1~80자), 선택적인 `kind`(`http`, `https`, `redirect`, `other`), `group`(`entry`, `service`), `detail`(최대 240자)이다. `redirect`에는 호스트 측 목적지 포트인 `redirect_to`(1~65535)가 필요하다. 이 값은 표시용 설명이며 실제 라우팅을 바꾸지 않는다. 현재 Docker에 게시된 바인딩에만 결합하므로 설정만 있는 미게시 포트를 만들지 않는다. 설명이 없으면 프로토콜이나 용도를 추정하지 않고 실제 바인딩을 표시한다. localhost 전용 여부는 실제 바인딩 주소에서 판단한다.

측정 수집 10초, 원문 로그 polling 10초, flush 30초가 기본이다. 화면은 최근 60회 메모리 캐시를 사용하고 재시작 시 최근 하루에서 복구한다. 과거 데이터는 DB에서 계속 보존한다. 저장 오류·로그 수집 오류·대기 용량을 별도로 표시하며 제어는 영구 intent 기록에 실패하면 실행하지 않는다.

## 보존 기간

측정·원문·제어 이벤트 각각 기본 3년이다. 3~100년의 정수로 설정한다. 기간은 달력상의 YEAR이며 수신 시각 기준이다. 월 파티션/part 단위 만료로 일부 기록은 기간보다 오래 남는다. 관리자 명령은 서버의 실제 TTL을 확인한다.

```sh
python3 -B -m backend.admin --credentials /workspace/operations/clickhouse/admin.json status
python3 -B -m backend.admin --credentials /workspace/operations/clickhouse/admin.json retention --table container_logs --years 5
python3 -B -m backend.admin --credentials /workspace/operations/clickhouse/admin.json retention --table container_logs --years 5 --apply
```

첫 명령은 상태, 두 번째는 영향 미리보기, 세 번째는 적용이다. 축소 전에도 반드시 미리보기를 확인한다. 이미 삭제된 데이터는 기간 확대로 복원되지 않는다. 설정을 바꿔도 즉각적인 전체 TTL materialization을 실행하지 않는다. 시작할 때 TTL을 자동으로 단축하지 않는다. 3년 미만의 직접 서버 변경은 저장 오류로 표시한다.

## 원문 조회

```sh
python3 -B -m backend.admin --credentials /workspace/operations/clickhouse/app.json logs --module mailbox --limit 100
```

기본 최근 15분이며 --start/--end에는 시간대가 있는 ISO 시각을 준다. 최대 31일/1,000행/2MiB/5초다. 공개 웹 원문 endpoint는 없다. body와 encoding으로 바이트를 복원한다. 큰 레코드는 같은 record_id의 chunk 순서로 결합한다. 동일 내용의 반복 발생은 occurrence로 구분한다.

## 장애와 복구

outbox는 기본 1GiB의 제한된 파일 대기열이다. 기록 확인 전 삭제하지 않는다. 전송은 at-least-once이며 불확정 응답 재시도 시 중복 가능성이 있다. 고정 batch token과 레코드 ID를 사용하며 중복 제거 창은 유한하다. 원문 조회는 record_id/chunk로 식별 가능한 중복을 접는다. 운영 데이터의 정확한 분석 집계에서는 이 특성을 고려한다.

Docker의 local/json-file 로그를 API로 읽는다. 전환 시각 이전의 기록을 backfill하지 않는다. 회전·삭제된 원본과 파일에만 남긴 앱 로그는 복원하지 못한다. TTY는 stdout/stderr가 이미 합쳐진 tty 스트림이다. TTY의 body는 Docker가 붙인 시각을 포함한 구간 원문이며, 조각은 chunk 순서로 결합한다. event_time은 해당 구간 첫 Docker 시각이고 본문 속 개별 시각도 보존한다. 완료한 구간만 offset을 전진하여 조용한 스트림의 과거를 반복 조회하지 않는다. 수집기를 장기간 중단하면 Docker 보관 기간을 넘는 공백이 생길 수 있으므로 오류와 대기열을 확인한다.

단일 host writer와 공유 flock이 제어 직렬화를 담당한다. 재시작 전 pending은 unknown으로 표시하고 재실행하지 않는다. DB 장애 동안 최신 수집은 계속하지만 제어 intent를 저장하지 못하면 제어는 거부한다. 단일 디스크 보관은 외부 백업·복제 보장을 대신하지 않는다.

긴 행의 미완료 partial은 Docker가 같은 과거 시각으로 후속 조각을 기록할 수 있으므로 해당 시각과 이미 보관한 occurrence를 유지해 재개합니다. TTY 미완료 구간은 원문 prefix 길이와 SHA256을 대조한 뒤 증가분만 보관합니다. 원본 회전으로 prefix가 달라지면 조용히 건너뛰지 않고 오류로 표시합니다. 열린 긴 행이 오래 지속되면 해당 원본의 재독 비용이 늘 수 있습니다.

원문 조회 결과는 rows, start, end, next_cursor를 포함합니다. 다음 페이지는 같은 --module/--start/--end와 --cursor 값을 사용합니다. 반환 용량을 넘는 큰 조각에서는 page_limit을 자동으로 낮춥니다. TTY chunk는 바이트 위치이며 body_bytes는 인코딩 전 길이입니다.

non-TTY의 열린 partial도 이미 보관한 조각 수와 prefix SHA256을 함께 검증합니다. 회전으로 prefix가 바뀌거나 부족해지면 새 조각을 과거 조각으로 오인해 건너뛰지 않고 오류를 표시하며 cursor를 전진하지 않습니다. 페이지 한도는 최종 JSON 직렬화 결과에도 적용합니다.
