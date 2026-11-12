BEGIN;
CREATE TABLE stock_usages (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ingredient_id bigint NOT NULL REFERENCES ingredients(id), amount numeric(12,2) NOT NULL CHECK (amount > 0), policy text NOT NULL CHECK (policy IN ('FIFO','FEFO')), request_key uuid NOT NULL UNIQUE, request_hash text NOT NULL, note text NOT NULL DEFAULT '', status text NOT NULL DEFAULT 'CONFIRMED' CHECK (status IN ('CONFIRMED','CANCELLED')), created_at timestamptz NOT NULL DEFAULT now(), cancelled_at timestamptz, CHECK ((status = 'CANCELLED') = (cancelled_at IS NOT NULL)));
CREATE TABLE usage_lot_allocations (usage_id bigint NOT NULL REFERENCES stock_usages(id), lot_id bigint NOT NULL REFERENCES inventory_lots(id), amount numeric(12,2) NOT NULL CHECK (amount > 0), PRIMARY KEY (usage_id,lot_id));
CREATE INDEX idx_stock_usages_created ON stock_usages(created_at DESC, id DESC);
ALTER TABLE stock_movements DROP CONSTRAINT stock_movements_reason_check;
ALTER TABLE stock_movements ADD CONSTRAINT stock_movements_reason_check CHECK (reason IN ('RECEIPT','ORDER','CANCEL','DISPOSE','USE','USE_CANCEL'));
ALTER TABLE stock_movements ADD COLUMN usage_id bigint REFERENCES stock_usages(id);
COMMIT;
