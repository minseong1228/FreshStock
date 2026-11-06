# BentoStock

도시락 사전 주문과 식재료 재고를 관리하는 1인 데이터베이스 프로젝트입니다. React + Vite, FastAPI, PostgreSQL 16을 사용합니다. 결제와 로그인은 구현하지 않은 교육용 시연입니다. 기존 고객을 선택해 주문하고, 재고 입고·취소·매출을 확인할 수 있습니다.

## 빠른 실행 (Docker Desktop)

프로젝트 폴더에서 아래 명령을 실행하세요.

```bash
docker compose up --build
```

- 웹: http://localhost:5173
- API 문서 및 직접 시험: http://localhost:8000/docs
- DB: localhost:5432 / DB `bentostock` / 사용자와 암호 `bento`

최초 실행 시 스키마와 초기 데이터가 자동 입력됩니다. 종료는 `Ctrl+C` 후 `docker compose down`입니다. 데이터를 포함해 완전히 초기화하려면 **이 프로젝트의 저장 데이터가 모두 삭제되는** `docker compose down -v` 후 다시 실행하세요. 기존 볼륨에는 SQL 초기화 파일이 재실행되지 않습니다.

## 직접 실행

PostgreSQL 16에 빈 `bentostock` 데이터베이스와 `bento` 사용자를 만들고 권한을 부여한 뒤:

```bash
psql -h localhost -U bento -d bentostock -f db/schema.sql
psql -h localhost -U bento -d bentostock -f db/seed.sql
cd backend
python -m venv .venv
```

Windows PowerShell에서는 `.venv\Scripts\Activate.ps1`, macOS/Linux에서는 `source .venv/bin/activate`를 실행합니다. 이어서:

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

다른 터미널에서 `cd frontend`, `npm install`, `npm run dev`를 실행합니다. 기본 DB 주소는 `postgresql://bento:bento@localhost:5432/bentostock`이며 환경변수 `DATABASE_URL`로 바꿀 수 있습니다. 프론트엔드 API 주소는 `VITE_API_URL`로 변경할 수 있습니다. CORS는 기본 로컬 개발 주소만 허용합니다.

## 기존 데이터 업그레이드

이전 버전 DB에는 `psql -h localhost -U bento -d bentostock -f db/migration_001.sql`을 한 번 실행하세요. 기존 주문·재고를 보존합니다. 새 DB는 schema.sql에 이미 포함되어 있습니다.

주문 API는 `Idempotency-Key` UUID 헤더가 필수입니다. 같은 키와 같은 주문 내용의 재시도는 기존 주문을 반환하며, 같은 키에 다른 내용을 보내면 409입니다. 웹은 처리 중 중복 클릭을 막고 실패 후 같은 내용을 재시도할 때 같은 키를 사용합니다. 새로고침 뒤의 별도 주문은 새로운 요청으로 처리됩니다.

## 시연

1. 고객과 메뉴 수량을 선택하고 주문합니다. 주문 내역과 재료 재고의 감소, 판매 현황을 확인합니다.
2. 재고보다 많은 수량을 주문하면 409 오류가 나타나며 기존 주문과 재고는 변하지 않습니다.
3. 주문을 취소하면 재고가 복구되고 통계에서 제외됩니다. 중복 취소는 거부됩니다.
4. 재료 입고를 실행하고 재고가 증가하는지 확인합니다. API 문서에서 메뉴 등록·수정과 고객/메뉴/상태/날짜별 주문 검색, 날짜별 매출 및 재고 이력 조회도 실행할 수 있습니다. 화면 하단에서 메뉴와 재료를 등록하고 메뉴별 레시피와 재고 변동 이력을 관리할 수 있습니다. 주문 내역은 고객·메뉴·상태·기간으로 검색하고 판매 현황은 기간을 지정할 수 있습니다.

## 트랜잭션과 설계

PostgreSQL 기본 격리 수준 READ COMMITTED에서 주문은 메뉴 조회, 재료별 필요량 합산, 재료 행 `FOR UPDATE`, 재고 검증, 주문·상세·사용량·재고 이력을 하나의 트랜잭션으로 처리합니다. 재고가 부족하거나 오류가 나면 전체 롤백합니다. 취소는 주문 행을 먼저 잠가 중복 복구를 막고, 주문 시점의 `order_ingredients`를 이용해 같은 트랜잭션에서 재고를 복구합니다. 재료는 항상 ID 오름차순으로 잠가 교착 위험을 줄입니다. 교착이나 직렬화 실패 자동 재시도는 구현하지 않았습니다. 금액은 주문 당시 단가로 보존합니다. 모든 사용자 입력 SQL은 매개변수로 전달합니다.

테이블: `customers`, `menus`, `ingredients`, `recipes`, `orders`, `order_items`, `order_ingredients`, `stock_movements`. 주문 시점의 가격·사용량은 이력 보존을 위한 스냅샷이며 현재 메뉴/레시피와 별개입니다. PK, FK, UNIQUE, CHECK, NOT NULL과 조회 인덱스를 사용합니다. `stock`은 효율적인 동시 재고 확인을 위한 현재 잔액이며, 변동 내역은 별도 원장에 남깁니다. DB를 직접 수정하면 원장과 재고가 불일치할 수 있으므로 앱을 통해 변경하세요.

## 테스트

테스트는 **전용 DB의 public 스키마를 매 테스트마다 삭제**합니다. 운영 DB나 사용 중인 DB를 지정하지 마세요. 예를 들어 PostgreSQL에 빈 `bentostock_test`를 생성하고 소유권을 `bento`에 부여한 뒤 PowerShell에서:

```powershell
cd backend
$env:TEST_DATABASE_URL='postgresql://bento:bento@localhost:5432/bentostock_test'
python -m pytest -q
```

정상 주문·취소, 재고 부족, 동일 재고에 대한 두 동시 주문, 쓰기 도중 강제 오류 롤백, 잘못된 수량을 검증합니다. 테스트 DB가 없으면 통합 테스트는 건너뜁니다. DB 오류 주입과 DB 연결 강제 종료 시 트랜잭션 롤백도 검증합니다.

## 실행 계획 확인 예시

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM orders WHERE customer_id = 1 ORDER BY created_at DESC LIMIT 20;
```

초기 데이터는 작아 순차 스캔이 선택될 수 있습니다. 성능 비교는 별도의 충분한 데이터로 수행하고 행 수·환경·실행 결과를 기록하세요.

## 과제 문서 관련

제공된 템플릿과 강의 안내의 테이블명 일치, 시나리오·UML·ERD·요구사항 추적 규칙을 참고했습니다. 이 저장소는 실행 가능한 구현물이며 제출용 `BentoStock_user-scenario.md`, ERD PDF, spec.xlsx는 아직 작성하지 않았습니다. 실제 수업 지침에 맞게 작성하고 테스트 실제 결과를 기록해야 합니다. 접근 제어, 인증, 대규모 성능 검증도 구현 범위 밖입니다.

## Firebase Hosting 배포

클라우드 배포 구성과 준비 절차는 [Firebase 배포 안내](docs/firebase-deploy.md)를 참고하세요. 현재 인증이 없으므로 공개 주소에 접근하는 누구나 데이터를 변경할 수 있습니다.
