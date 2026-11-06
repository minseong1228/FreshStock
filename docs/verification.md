# 검증과 설계 메모

## 테스트 결과

2026-11-05, PostgreSQL 16의 별도 임시 DB에서 `pytest -q`: **8개 통과**. 정상 주문·취소 및 중복 취소, 재고 부족 롤백, 마지막 재고에 대한 동시 주문, 동일 요청 키 동시 재시도, 잘못된 수량, 레시피와 식재료 등록, 트리거 오류 주입, 처리 도중 PostgreSQL 연결 강제 종료를 검증했다. 통합 테스트는 테스트용 DB의 public 스키마를 초기화하므로 운영 DB에서 실행하면 안 된다.

## 실행 계획

2026-11-05 실행 중인 PostgreSQL 16, `orders` 총 1행(고객 1의 주문 1행)에서 `EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM orders WHERE customer_id = 1 ORDER BY created_at DESC LIMIT 20`을 실행했다. 결과는 `Seq Scan on orders → Sort → Limit`, 실행 시간 0.106 ms, shared hit 4였다. 행이 매우 적어 PostgreSQL이 인덱스보다 순차 스캔을 선택한 사례다. `idx_orders_customer_created(customer_id, created_at DESC)`는 고객별 최신 주문 검색을 위한 후보이지만, 이 결과만으로 성능 개선을 입증할 수 없다. 충분한 데이터에서 인덱스 유무에 따른 계획과 반복 측정을 별도로 기록해야 한다.

## 정규화와 보존 값

고객, 메뉴, 재료를 각각 별도 테이블로 두고 메뉴와 재료의 다대다 관계를 `recipes`로 분리했다. 주문과 메뉴의 다대다 관계는 `order_items`로 분리한다. 각 테이블의 식별자와 종속성을 기준으로 3NF를 목표로 설계했다. 주문 시점의 단가(`order_items.unit_price`)와 소비량(`order_ingredients.amount`)은 시간이 지나 메뉴 가격·레시피가 바뀌어도 당시 주문과 취소를 재현하기 위한 이력 값이다. `ingredients.stock`은 동시 주문에서 잠글 수 있는 현재 재고이고 `stock_movements`는 변동 이력이다. 이 둘은 중복 저장되므로 애플리케이션 트랜잭션으로 함께 갱신하며 직접 SQL로 변경하면 불일치할 수 있다.

주문 중복 방지는 동일 UUID 키를 사용하는 재시도에 적용된다. 다른 키로 생성한 별도 주문은 의도된 주문일 수 있어 자동으로 합치지 않는다. 현재 웹은 통신 실패 후 같은 내용을 다시 보낼 때 같은 키를 유지하지만, 새로고침 이후에는 새 키를 사용한다. 교착 상태 자동 재시도와 인증·권한 분리는 아직 구현하지 않았다.
