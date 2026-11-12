BEGIN;
INSERT INTO customers(name) VALUES ('김민수'),('이서연'),('박지훈');
INSERT INTO menus(name,price) VALUES ('제육 도시락',8500),('닭가슴살 도시락',7500),('채소 도시락',7000);
INSERT INTO ingredients(name,unit,stock,threshold) VALUES ('쌀','g',5000,1000),('돼지고기','g',3000,500),('닭가슴살','g',3000,500),('채소','g',4000,800);
INSERT INTO recipes VALUES (1,1,200),(1,2,150),(1,4,100),(2,1,200),(2,3,150),(2,4,100),(3,1,200),(3,4,250);
INSERT INTO stock_movements(ingredient_id,delta,reason) SELECT id,stock,'RECEIPT' FROM ingredients WHERE stock > 0;
INSERT INTO inventory_lots(ingredient_id,expires_on,location,initial_amount,remaining)
SELECT id,(now() AT TIME ZONE 'Asia/Seoul')::date + 30,'냉장고',stock,stock FROM ingredients WHERE stock>0;
INSERT INTO pickup_slots(starts_at,ends_at,capacity)
SELECT date_trunc('day',now()) + (n || ' day')::interval + interval '12 hours',date_trunc('day',now()) + (n || ' day')::interval + interval '13 hours',30 FROM generate_series(1,7) AS n;
COMMIT;
