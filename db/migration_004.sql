BEGIN;
ALTER TABLE stock_movements DROP CONSTRAINT stock_movements_reason_check;
ALTER TABLE stock_movements ADD CONSTRAINT stock_movements_reason_check CHECK (reason IN ('RECEIPT','ORDER','CANCEL','DISPOSE'));
ALTER TABLE stock_movements ADD COLUMN lot_id bigint REFERENCES inventory_lots(id);
COMMIT;
