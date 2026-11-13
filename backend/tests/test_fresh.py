import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import psycopg,pytest
from fastapi.testclient import TestClient
URL=os.environ.get('TEST_DATABASE_URL')
if not URL: pytest.skip('TEST_DATABASE_URL is required', allow_module_level=True)
os.environ['DATABASE_URL']=URL
from fresh_main import app
@pytest.fixture(autouse=True)
def setup():
    with psycopg.connect(URL,autocommit=True) as db:
        db.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
        db.execute((Path(__file__).resolve().parents[2]/'db/fresh_schema.sql').read_text(encoding='utf-8'))
        db.execute("INSERT INTO ingredients(name,unit,stock,threshold) VALUES ('상추','g',200,0)")
        db.execute("INSERT INTO inventory_lots(ingredient_id,received_at,expires_on,location,initial_amount,remaining) VALUES (1,now()-interval '2 days',(now() AT TIME ZONE 'Asia/Seoul')::date+20,'냉장고',100,100),(1,now()-interval '1 day',(now() AT TIME ZONE 'Asia/Seoul')::date+2,'냉장고',100,100)")
def usage(amount=70,policy='FIFO',key=None):
    with TestClient(app) as c: return c.post('/api/usages',headers={'Idempotency-Key':str(key or uuid4())},json={'ingredient_id':1,'amount':amount,'policy':policy})
def test_fifo_fefo():
    a,b=usage(30,'FIFO'),usage(30,'FEFO'); assert a.status_code==b.status_code==201
    with psycopg.connect(URL) as db:
        assert db.execute('select lot_id from usage_lot_allocations where usage_id=%s',(a.json()['id'],)).fetchone()[0]==1
        assert db.execute('select lot_id from usage_lot_allocations where usage_id=%s',(b.json()['id'],)).fetchone()[0]==2
def test_concurrent_and_cancel():
    with ThreadPoolExecutor(max_workers=2) as ex: r=list(ex.map(lambda _:usage(150),range(2)))
    assert sorted(x.status_code for x in r)==[201,409]
    with TestClient(app) as c:
        id=next(x.json()['id'] for x in r if x.status_code==201)
        assert c.post(f'/api/usages/{id}/cancel').status_code==200
        assert c.post(f'/api/usages/{id}/cancel').status_code==409
    with psycopg.connect(URL) as db: assert db.execute('select stock from ingredients where id=1').fetchone()[0]==200
def test_retry_and_error_rollback():
    key=uuid4(); a=usage(10,key=key); assert usage(10,key=key).json()['id']==a.json()['id']; assert usage(20,key=key).status_code==409
    with psycopg.connect(URL) as db:
        db.execute("CREATE FUNCTION fail_use() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.reason='USE' THEN RAISE EXCEPTION 'test'; END IF; RETURN NEW; END $$")
        db.execute('CREATE TRIGGER fail_use BEFORE INSERT ON stock_movements FOR EACH ROW EXECUTE FUNCTION fail_use()')
    with pytest.raises(psycopg.Error): usage(5)
    with psycopg.connect(URL) as db: assert db.execute('select stock from ingredients where id=1').fetchone()[0]==190

def make_slot(capacity=1, days=1):
    from datetime import datetime,timedelta,timezone
    start=datetime.now(timezone.utc)+timedelta(days=days)
    with TestClient(app) as c:
        r=c.post('/api/slots',json={'starts_at':start.isoformat(),'ends_at':(start+timedelta(hours=1)).isoformat(),'capacity':capacity})
        assert r.status_code==201
        return r.json()['id']

def reserve(slot,amount=50,policy='FEFO',key=None):
    with TestClient(app) as c:
        return c.post('/api/reservations',headers={'Idempotency-Key':str(key or uuid4())},json={'slot_id':slot,'ingredient_id':1,'amount':amount,'policy':policy})

def test_slot_capacity_concurrent_and_cancel():
    slot=make_slot()
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _:reserve(slot),range(2)))
    assert sorted(x.status_code for x in results)==[201,409]
    result=next(x for x in results if x.status_code==201)
    with TestClient(app) as c:
        assert c.post(f"/api/reservations/{result.json()['id']}/cancel").status_code==200
        assert c.post(f"/api/reservations/{result.json()['id']}/cancel").status_code==409
    with psycopg.connect(URL) as db:
        assert db.execute('SELECT reserved FROM pickup_slots WHERE id=%s',(slot,)).fetchone()[0]==0
        assert db.execute('SELECT stock FROM ingredients WHERE id=1').fetchone()[0]==200
    assert reserve(slot).status_code==201

def test_reservation_expiry_at_pickup_and_idempotency():
    slot=make_slot(capacity=3,days=4)
    key=uuid4(); first=reserve(slot,50,'FEFO',key)
    assert first.status_code==201 and reserve(slot,50,'FEFO',key).json()['id']==first.json()['id']
    assert reserve(slot,51,'FEFO',key).status_code==409
    with psycopg.connect(URL) as db:
        lot=db.execute('SELECT lot_id FROM reservation_lot_allocations WHERE reservation_id=%s',(first.json()['id'],)).fetchone()[0]
        assert lot==1
    assert reserve(slot,60).status_code==409
    with psycopg.connect(URL) as db: assert db.execute('SELECT reserved FROM pickup_slots WHERE id=%s',(slot,)).fetchone()[0]==1

def test_reservation_rollback_and_slot_limit_edit():
    slot=make_slot(2)
    assert reserve(slot).status_code==201
    with TestClient(app) as c:
        assert c.put(f'/api/slots/{slot}',json={'capacity':0}).status_code==422
        assert c.put(f'/api/slots/{slot}',json={'capacity':1}).status_code==200
        assert c.put(f'/api/slots/{slot}',json={'capacity':2}).status_code==200
    with psycopg.connect(URL) as db:
        db.execute("CREATE FUNCTION fail_reserve() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.reason='RESERVE' THEN RAISE EXCEPTION 'test'; END IF; RETURN NEW; END $$")
        db.execute('CREATE TRIGGER fail_reserve BEFORE INSERT ON stock_movements FOR EACH ROW EXECUTE FUNCTION fail_reserve()')
    with pytest.raises(psycopg.Error): reserve(slot)
    with psycopg.connect(URL) as db:
        assert db.execute('SELECT reserved FROM pickup_slots WHERE id=%s',(slot,)).fetchone()[0]==1
        assert db.execute('SELECT stock FROM ingredients WHERE id=1').fetchone()[0]==150
        assert db.execute('SELECT count(*) FROM reservations').fetchone()[0]==1

def test_cancelling_expired_lot_discards_and_frees_slot():
    slot=make_slot(); result=reserve(slot,50,'FIFO'); assert result.status_code==201
    with psycopg.connect(URL) as db:
        db.execute("UPDATE inventory_lots SET expires_on=(now() AT TIME ZONE 'Asia/Seoul')::date-1 WHERE id=1")
    with TestClient(app) as c: assert c.post(f"/api/reservations/{result.json()['id']}/cancel").status_code==200
    with psycopg.connect(URL) as db:
        assert db.execute('SELECT reserved FROM pickup_slots WHERE id=%s',(slot,)).fetchone()[0]==0
        assert db.execute('SELECT stock FROM ingredients WHERE id=1').fetchone()[0]==150
        assert db.execute("SELECT count(*) FROM stock_movements WHERE reason='DISPOSE' AND reservation_id=%s",(result.json()['id'],)).fetchone()[0]==1

def test_slots_do_not_overlap_and_capacity_cannot_drop_below_reservations():
    from datetime import datetime,timedelta,timezone
    slot=make_slot(2)
    assert reserve(slot).status_code==201 and reserve(slot).status_code==201
    with TestClient(app) as c:
        assert c.put(f'/api/slots/{slot}',json={'capacity':1}).status_code==409
        start=datetime.now(timezone.utc)+timedelta(days=1,minutes=30)
        r=c.post('/api/slots',json={'starts_at':start.isoformat(),'ends_at':(start+timedelta(hours=1)).isoformat(),'capacity':3})
        assert r.status_code==409
