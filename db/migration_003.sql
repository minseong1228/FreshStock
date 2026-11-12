BEGIN;
ALTER TABLE orders ADD COLUMN allocation_policy text NOT NULL DEFAULT 'FIFO' CHECK (allocation_policy IN ('FIFO','FEFO'));
COMMIT;
