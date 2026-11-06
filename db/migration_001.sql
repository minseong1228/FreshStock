-- 기존 주문과 재고를 유지하며 중복 요청 방지용 컬럼을 추가합니다.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS request_key uuid;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS request_hash text;
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_request_key ON orders(request_key);
