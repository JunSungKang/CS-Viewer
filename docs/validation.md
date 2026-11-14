# 검증

단위/회귀 검사는 `python3 -B -m unittest discover -s tests -v`, 프런트 구문은 `node --check web/app.js`다. 실제 ClickHouse 검사는 CS_TEST_CLICKHOUSE_CONFIG의 workspace credential JSON 또는 CI 전용 CS_TEST_CLICKHOUSE_URL로 활성화한다. 매 시험은 고유한 격리 DB를 만들고 제거한다.

필수 대상은 UUID 중복과 충돌, pending 재시작, 기록 실패 시 제어 거부, 저장 장애와 최신 상태 분리, outbox 재시작/ack 복구/한도, Docker 프레임·나노초·중복 내용, 기본 3년·확대·축소 preview·윤년이다. 운영용 Docker smoke는 별도 소유 canary만 시작/중지/재시작한다.

운영 튜닝 결과는 workspace operations/cs-viewer/reports/clickhouse-benchmark.json과 전환 보고서에 기록한다. 합성 데이터 분포·규모·warm/cold 조건·반복 횟수·쿼리 시간·CPU·메모리·압축 크기·인덱스·parts·복구 결과를 함께 해석한다. 다른 회사의 성능 배수를 이 환경의 결과로 사용하지 않는다.
