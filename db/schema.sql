BEGIN;
CREATE TABLE customers (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name text NOT NULL UNIQUE CHECK (length(trim(name)) > 0));
CREATE TABLE menus (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name text NOT NULL UNIQUE CHECK (length(trim(name)) > 0), price integer NOT NULL CHECK (price >= 0), active boolean NOT NULL DEFAULT true);
CREATE TABLE ingredients (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name text NOT NULL UNIQUE CHECK (length(trim(name)) > 0), unit text NOT NULL CHECK (length(trim(unit)) > 0), stock numeric(12,2) NOT NULL DEFAULT 0 CHECK (stock >= 0), threshold numeric(12,2) NOT NULL DEFAULT 0 CHECK (threshold >= 0));
CREATE TABLE recipes (menu_id bigint NOT NULL REFERENCES menus(id), ingredient_id bigint NOT NULL REFERENCES ingredients(id), amount numeric(12,2) NOT NULL CHECK (amount > 0), PRIMARY KEY(menu_id, ingredient_id));
CREATE TABLE orders (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, customer_id bigint NOT NULL REFERENCES customers(id), request_key uuid UNIQUE, request_hash text, status text NOT NULL DEFAULT 'CONFIRMED' CHECK (status IN ('CONFIRMED','CANCELLED')), created_at timestamptz NOT NULL DEFAULT now(), cancelled_at timestamptz, CHECK ((status = 'CANCELLED') = (cancelled_at IS NOT NULL)));
CREATE TABLE order_items (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, order_id bigint NOT NULL REFERENCES orders(id), menu_id bigint NOT NULL REFERENCES menus(id), quantity integer NOT NULL CHECK (quantity > 0), unit_price integer NOT NULL CHECK (unit_price >= 0), UNIQUE(order_id,menu_id));
-- 주문 당시 재료 사용량을 보관하여 레시피 변경 후에도 정확히 복구한다.
CREATE TABLE order_ingredients (order_id bigint NOT NULL REFERENCES orders(id), ingredient_id bigint NOT NULL REFERENCES ingredients(id), amount numeric(12,2) NOT NULL CHECK (amount > 0), PRIMARY KEY(order_id,ingredient_id));
CREATE TABLE stock_movements (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ingredient_id bigint NOT NULL REFERENCES ingredients(id), order_id bigint REFERENCES orders(id), delta numeric(12,2) NOT NULL CHECK (delta <> 0), reason text NOT NULL CHECK (reason IN ('RECEIPT','ORDER','CANCEL')), created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX idx_orders_created ON orders(created_at DESC);
CREATE INDEX idx_orders_customer_created ON orders(customer_id,created_at DESC);
CREATE INDEX idx_order_items_menu ON order_items(menu_id);
CREATE INDEX idx_movements_ingredient_created ON stock_movements(ingredient_id,created_at DESC);
COMMIT;
