# FreshStock — 식재료 재고 관리 시스템

식재료 입고, 보관 위치, 유통기한, 로트별 재고와 사용 이력을 관리하는 React + Vite / FastAPI / PostgreSQL 프로젝트입니다. FIFO·FEFO를 선택해 재고를 차감하고, 사용 취소 시 원래 로트에 복구합니다. 유통기한 경과 로트는 서버가 실행되는 동안 자동으로 폐기합니다.

공개 웹사이트: https://freshstock-ingredients-app.web.app
API: https://freshstock-api.onrender.com

## 로컬 실행

Docker Desktop을 실행한 뒤 이 폴더에서 `docker compose up --build`를 실행하고 http://localhost:5173 을 엽니다. 새 `freshstock` Docker 볼륨은 빈 데이터베이스로 시작합니다. 관리 화면에서 식재료를 등록하고 재고 화면에서 입고하세요. 종료는 `docker compose down`입니다. 비밀번호는 로컬 개발 전용이며 외부에 노출하지 마세요.

새 데이터베이스의 구조는 `db/fresh_schema.sql`, 빈 초기 데이터는 `db/fresh_seed.sql`입니다. API 문서는 http://localhost:8000/docs 에서 확인합니다. 공개 서버는 Render 환경 변수 `DATABASE_URL`에 Supabase PostgreSQL 연결 정보를 보관합니다.

## 배포

프런트엔드를 빌드할 때 `VITE_API_URL=https://freshstock-api.onrender.com`으로 설정하고, `firebase deploy --only hosting --config firebase.new.json --project freshstock-ingredients-app`을 실행합니다. 무료 서버는 장시간 사용하지 않으면 잠들어 첫 응답이 늦어질 수 있습니다. 로그인과 관리자 권한 분리가 없으므로 시연용 가상 데이터만 사용하세요.

## 데이터 일관성 검증

재료와 로트를 트랜잭션에서 잠가 동시 요청의 재고 중복 차감을 방지합니다. 요청 식별자로 재시도 중복을 막고, 취소 시 기존 로트별 사용량을 한 번만 복구합니다. 로컬에서 별도 테스트 DB에 `TEST_DATABASE_URL`을 지정하고 `backend` 폴더에서 `python -m pytest tests/test_fresh.py -q`를 실행하세요. 이 테스트는 지정된 DB의 public 스키마를 초기화하므로 운영 DB를 절대 사용하지 마세요.
