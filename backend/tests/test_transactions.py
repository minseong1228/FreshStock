"""실제 PostgreSQL 전용 통합 테스트. 반드시 별도 테스트 DB를 지정할 것."""
import os
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
import psycopg
from fastapi.testclient import TestClient

URL=os.getenv('TEST_DATABASE_URL')
if not URL:
    pytest.skip('TEST_DATABASE_URL이 없어 PostgreSQL 통합 테스트를 건너뜁니다.',allow_module_level=True)
os.environ['DATABASE_URL']=URL
from main import app

@pytest.fixture(autouse=True)
def reset():
    # 공개 스키마를 삭제하므로 오직 전용 테스트 DB에만 연결해야 한다.
    with psycopg.connect(URL,autocommit=True) as db:
        db.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
        root=Path(__file__).resolve().parents[2]
        db.execute((root/'db/schema.sql').read_text(encoding='utf-8'))
        db.execute((root/'db/seed.sql').read_text(encoding='utf-8'))

def state():
    with psycopg.connect(URL) as db:
        return (db.execute('SELECT id,stock FROM ingredients ORDER BY id').fetchall(),db.execute('SELECT count(*) FROM orders').fetchone()[0],db.execute('SELECT count(*) FROM stock_movements').fetchone()[0])

def order(q=1, key=None):
    with TestClient(app) as client:
        return client.post('/api/orders',headers={'Idempotency-Key':str(key or uuid4())},json={'customer_id':1,'items':[{'menu_id':1,'quantity':q}]})

def test_order_cancel_and_repeat():
    before=state()
    r=order(); assert r.status_code==201
    with TestClient(app) as client:
        assert client.post(f"/api/orders/{r.json()['id']}/cancel").status_code==200
        assert client.post(f"/api/orders/{r.json()['id']}/cancel").status_code==409
    assert state()[0]==before[0]

def test_insufficient_stock_rolls_back():
    before=state(); assert order(100).status_code==409; assert state()==before

def test_concurrent_last_stock():
    with psycopg.connect(URL) as db: db.execute('UPDATE ingredients SET stock=150 WHERE id=2')
    before=state()
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _: order(),range(2)))
    assert sorted(r.status_code for r in results)==[201,409]
    after=state(); assert after[1]==before[1]+1
    assert dict(after[0])[2]==0

def test_failure_after_writes_rolls_back():
    # 주문 재고 차감 도중 DB 오류를 강제해 앞선 쓰기까지 롤백되는지 확인한다.
    with psycopg.connect(URL) as db:
        db.execute("""CREATE FUNCTION fail_order_movement() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.reason='ORDER' THEN RAISE EXCEPTION 'injected failure'; END IF; RETURN NEW; END $$""")
        db.execute('CREATE TRIGGER injected_failure BEFORE INSERT ON stock_movements FOR EACH ROW EXECUTE FUNCTION fail_order_movement()')
    before=state()
    with pytest.raises(psycopg.Error): order()
    assert state()==before

def test_invalid_quantity():
    before=state(); assert order(0).status_code==422; assert state()==before


def test_same_key_concurrent_is_one_order():
    before=state(); key=uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _: order(1,key),range(2)))
    assert [r.status_code for r in results]==[201,201]
    assert results[0].json()['id']==results[1].json()['id']
    assert state()[1]==before[1]+1
    assert order(2,key).status_code==409

def test_recipe_update_and_ingredient():
    with TestClient(app) as c:
        r=c.post('/api/ingredients',json={'name':'달걀','unit':'개','threshold':3})
        assert r.status_code==201
        assert c.post('/api/ingredients',json={'name':'달걀','unit':'개','threshold':3}).status_code==409
        ingredient=r.json()['id']
        assert c.put('/api/menus/1/recipe',json={'items':[{'ingredient_id':ingredient,'amount':1}]}).status_code==200
        assert order().status_code==409
        assert c.post(f'/api/ingredients/{ingredient}/receipts',json={'amount':3}).status_code==200
        assert order().status_code==201


def test_connection_termination_rolls_back():
    before=state()
    conn=psycopg.connect(URL)
    try:
        pid=conn.execute('SELECT pg_backend_pid()').fetchone()[0]
        conn.execute('INSERT INTO orders(customer_id) VALUES (1)')
        conn.execute('UPDATE ingredients SET stock=stock-100 WHERE id=1')
        with psycopg.connect(URL,autocommit=True) as killer:
            assert killer.execute('SELECT pg_terminate_backend(%s)',(pid,)).fetchone()[0]
        with pytest.raises(psycopg.Error): conn.commit()
    finally:
        conn.close()
    assert state()==before
