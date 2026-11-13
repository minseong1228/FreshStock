# FreshStock — 식재료 입고·유통기한 및 재고 관리

식재료를 등록하고 입고 로트별 잔량·유통기한·보관 위치를 관리합니다. 재료 사용 시 FIFO(입고순) 또는 FEFO(유통기한순)로 유효한 로트에서 차감하며, 사용 취소 시 차감한 로트에 복구합니다. 유통기한이 지난 로트는 서버 실행 중 자동으로 폐기 이력과 함께 처리됩니다. 날짜를 확인하지 않은 로트는 사용 대상에서 제외됩니다.

## 실행

Docker Desktop 실행 후 프로젝트 폴더에서 `docker compose up --build`를 실행하고 http://localhost:5173 에 접속합니다. 기존 Docker 볼륨이나 기존 데이터베이스가 있다면 `db/schema.sql`을 다시 실행하지 말고 기존 버전에 필요한 마이그레이션을 순서대로 적용하세요. 이전 BentoStock의 004까지 적용된 DB에는 백업 후 `db/migration_005.sql`을 한 번 실행합니다. 기존 도시락 데이터와 주문 이력은 그대로 보존됩니다.

프런트엔드는 React + Vite, 서버는 FastAPI, DB는 PostgreSQL입니다. API 문서는 http://localhost:8000/docs 에서 확인합니다. 배포 시 프런트엔드 빌드의 `VITE_API_URL`에 API 주소를 넣고, API의 `DATABASE_URL`, `CORS_ORIGINS`를 설정하세요.

## 동작 및 검증

식재료 행과 사용 대상 로트를 트랜잭션 내에서 잠그므로 동시 사용 시 재고가 음수가 되지 않습니다. 사용 요청의 UUID 식별자로 동일 요청 재시도를 중복 처리하지 않습니다. 사용 취소는 원래 로트별 차감량을 한 번만 복구합니다. 만료된 로트를 취소로 복구하면 다음 자동 폐기 처리에서 폐기됩니다. 자동 폐기는 서버가 실행되는 동안에만 작동하며 무료 인스턴스가 잠들어 있는 동안 즉시 실행되지 않습니다.

테스트는 **별도 테스트 DB를 초기화**하므로 실제 DB를 지정하지 마세요. `TEST_DATABASE_URL`을 전용 테스트 DB 주소로 지정한 후 backend 디렉터리에서 `python -m pytest tests/test_transactions.py -q`를 실행합니다. 동시 사용, 중복 요청, 취소 복구, FIFO·FEFO 선택을 포함합니다.

로그인 및 권한 분리가 없으므로 공개 서비스에는 가상 데이터만 사용하세요. 기존 메뉴·주문·수령 시간 API와 데이터는 과거 기록을 보존하기 위해 DB에 남아 있으며 새 화면은 식재료 관리 기능에 집중합니다.

## 공개 사이트 주소

FreshStock 공개 주소는 https://freshstock-ingredients.web.app 입니다. 기존 Firebase 프로젝트 ID는 변경할 수 없어 관리 콘솔과 기본 호스팅 주소에는 이전 식별자가 남아 있습니다. 기존 데이터베이스와 연결을 유지하기 위해 Docker의 기존 프로젝트·DB 이름과 자격 증명, Render 백엔드 주소는 유지합니다. GitHub 저장소와 Render 서비스 표시 이름은 FreshStock입니다. 새 사이트 배포에는 `firebase deploy --only hosting --config firebase.freshstock.json --project bentostock`을 사용합니다.
