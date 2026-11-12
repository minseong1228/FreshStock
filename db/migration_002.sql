-- 기존 데이터 보존. 배포 전 백업 및 구버전 API 중지 필수. 반드시 한 번만 실행.
BEGIN;
LOCK TABLE ingredients, orders, stock_movements IN ACCESS EXCLUSIVE MODE;
CREATE TABLE inventory_lots (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ingredient_id bigint NOT NULL REFERENCES ingredients(id), received_at timestamptz NOT NULL DEFAULT now(), expires_on date, location text NOT NULL CHECK(length(trim(location)) > 0), initial_amount numeric(12,2) NOT NULL CHECK(initial_amount > 0), remaining numeric(12,2) NOT NULL CHECK(remaining >= 0 AND remaining <= initial_amount));
CREATE TABLE pickup_slots (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, starts_at timestamptz NOT NULL UNIQUE, ends_at timestamptz NOT NULL CHECK(ends_at > starts_at), capacity integer NOT NULL CHECK(capacity > 0), reserved integer NOT NULL DEFAULT 0 CHECK(reserved >= 0 AND reserved <= capacity));
ALTER TABLE orders ADD COLUMN pickup_slot_id bigint REFERENCES pickup_slots(id);
CREATE TABLE order_lot_allocations (order_id bigint NOT NULL REFERENCES orders(id), lot_id bigint NOT NULL REFERENCES inventory_lots(id), amount numeric(12,2) NOT NULL CHECK(amount > 0), PRIMARY KEY(order_id,lot_id));
CREATE INDEX idx_inventory_lots_fifo ON inventory_lots(ingredient_id,received_at,id) WHERE remaining > 0;
CREATE INDEX idx_inventory_lots_expiry ON inventory_lots(expires_on) WHERE remaining > 0;
CREATE INDEX idx_orders_slot ON orders(pickup_slot_id);
-- 이관 잔량은 기존에 유통기한을 기록하지 않았으므로 확인 전 주문 차감 불가.
INSERT INTO inventory_lots(ingredient_id,expires_on,location,initial_amount,remaining)
SELECT id,NULL,'기존 재고(유통기한 확인 필요)',stock,stock FROM ingredients WHERE stock > 0;
COMMIT;
