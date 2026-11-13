import os, hashlib, asyncio, logging, json
from contextlib import asynccontextmanager
from uuid import UUID
from decimal import Decimal
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Literal
import psycopg
from psycopg.rows import dict_row
from starlette.concurrency import run_in_threadpool
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://freshstock:freshstock@localhost:5432/freshstock")

@asynccontextmanager
async def lifespan(app):
    async def expiry_worker():
        while True:
            await asyncio.sleep(60)
            try: await run_in_threadpool(dispose_expired)
            except Exception: logging.exception("유통기한 경과 재고 자동 정리 실패")
    task=asyncio.create_task(expiry_worker())
    try: yield
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
app = FastAPI(title="FreshStock API", lifespan=lifespan)
origins = ["http://localhost:5173", "http://127.0.0.1:5173", "https://freshstock-ingredients-app.web.app", "https://freshstock-ingredients-app.firebaseapp.com"] + [x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()]
app.add_middleware(CORSMiddleware,allow_origins=origins,allow_methods=["*"],allow_headers=["*"])

def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def dispose_expired():
    """한국 날짜로 기한이 지난 로트를 원자적으로 폐기. 서버 중단 중에는 동작하지 않는다."""
    with connect() as db:
        ids=db.execute("SELECT id FROM ingredients WHERE id IN (SELECT ingredient_id FROM inventory_lots WHERE remaining>0 AND expires_on < (now() AT TIME ZONE 'Asia/Seoul')::date) ORDER BY id FOR UPDATE").fetchall()
        for row in ids:
            lots=db.execute("SELECT id,remaining FROM inventory_lots WHERE ingredient_id=%s AND remaining>0 AND expires_on < (now() AT TIME ZONE 'Asia/Seoul')::date ORDER BY received_at,id FOR UPDATE",(row['id'],)).fetchall()
            for lot in lots:
                db.execute("UPDATE inventory_lots SET remaining=0 WHERE id=%s",(lot['id'],))
                db.execute("UPDATE ingredients SET stock=stock-%s WHERE id=%s",(lot['remaining'],row['id']))
                db.execute("INSERT INTO stock_movements(ingredient_id,lot_id,delta,reason) VALUES (%s,%s,%s,'DISPOSE')",(row['id'],lot['id'],-lot['remaining']))

class Receipt(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    expires_on: date
    location: str = Field(min_length=1, max_length=100, pattern=r".*\S.*")

class LotDetails(BaseModel):
    expires_on: date
    location: str = Field(min_length=1, max_length=100, pattern=r".*\S.*")

class IngredientIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r".*\S.*")
    unit: str = Field(min_length=1, max_length=20, pattern=r".*\S.*")
    threshold: Decimal = Field(ge=0, max_digits=12, decimal_places=2)

class UsageIn(BaseModel):
    ingredient_id: int = Field(gt=0)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    policy: Literal['FIFO', 'FEFO'] = 'FIFO'
    note: str = Field(default='', max_length=200)

class SlotIn(BaseModel):
    starts_at: datetime
    ends_at: datetime
    capacity: int = Field(gt=0, le=10000)

class SlotCapacity(BaseModel):
    capacity: int = Field(gt=0, le=10000)

class ReservationIn(BaseModel):
    slot_id: int = Field(gt=0)
    ingredient_id: int = Field(gt=0)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    policy: Literal['FIFO','FEFO'] = 'FEFO'
    note: str = Field(default='', max_length=200)

@app.get('/api/slots')
def list_slots():
    with connect() as db:
        return db.execute('SELECT *, capacity-reserved AS available FROM pickup_slots ORDER BY starts_at').fetchall()

@app.post('/api/slots', status_code=201)
def create_slot(data: SlotIn):
    if data.starts_at.utcoffset() is None or data.ends_at.utcoffset() is None: raise HTTPException(422,'시간대가 포함된 시각을 입력해 주세요.')
    if data.starts_at <= datetime.now(ZoneInfo('UTC')) or data.ends_at <= data.starts_at: raise HTTPException(422,'미래의 시작 시간과 종료 시간을 확인해 주세요.')
    try:
        with connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended('freshstock:slot-schedule',0))")
            if db.execute('SELECT 1 FROM pickup_slots WHERE starts_at < %s AND ends_at > %s LIMIT 1',(data.ends_at,data.starts_at)).fetchone():
                raise HTTPException(409,'겹치는 시간대가 이미 있습니다.')
            return db.execute('INSERT INTO pickup_slots(starts_at,ends_at,capacity) VALUES (%s,%s,%s) RETURNING *',(data.starts_at,data.ends_at,data.capacity)).fetchone()
    except psycopg.errors.UniqueViolation: raise HTTPException(409,'같은 시작 시간의 시간대가 이미 있습니다.')

@app.put('/api/slots/{slot_id}')
def edit_slot(slot_id: int, data: SlotCapacity):
    with connect() as db:
        slot=db.execute('SELECT * FROM pickup_slots WHERE id=%s FOR UPDATE',(slot_id,)).fetchone()
        if not slot: raise HTTPException(404,'시간대가 없습니다.')
        if data.capacity < slot['reserved']: raise HTTPException(409,'현재 예약 건수보다 작게 설정할 수 없습니다.')
        return db.execute('UPDATE pickup_slots SET capacity=%s WHERE id=%s RETURNING *',(data.capacity,slot_id)).fetchone()

@app.post('/api/reservations', status_code=201)
def create_reservation(data: ReservationIn, idempotency_key: UUID = Header(alias='Idempotency-Key')):
    dispose_expired()
    payload={'slot_id':data.slot_id,'ingredient_id':data.ingredient_id,'amount':str(data.amount.normalize()),'policy':data.policy,'note':data.note.strip()}
    digest=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    with connect() as db:
        key=str(idempotency_key)
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,))
        previous=db.execute('SELECT * FROM reservations WHERE request_key=%s',(key,)).fetchone()
        if previous:
            if previous['request_hash'] != digest: raise HTTPException(409,'같은 요청 식별자를 다른 예약에 사용할 수 없습니다.')
            return previous
        slot=db.execute('SELECT * FROM pickup_slots WHERE id=%s FOR UPDATE',(data.slot_id,)).fetchone()
        if not slot: raise HTTPException(404,'시간대가 없습니다.')
        if slot['starts_at'] <= datetime.now(ZoneInfo('UTC')): raise HTTPException(409,'이미 시작한 시간대는 예약할 수 없습니다.')
        if slot['reserved'] >= slot['capacity']: raise HTTPException(409,'해당 시간대의 예약이 마감되었습니다.')
        if not db.execute('SELECT id FROM ingredients WHERE id=%s FOR UPDATE',(data.ingredient_id,)).fetchone(): raise HTTPException(404,'식재료가 없습니다.')
        lots=db.execute("SELECT * FROM inventory_lots WHERE ingredient_id=%s AND remaining>0 AND expires_on >= GREATEST((now() AT TIME ZONE 'Asia/Seoul')::date,(%s::timestamptz AT TIME ZONE 'Asia/Seoul')::date) ORDER BY CASE WHEN %s='FEFO' THEN expires_on END, received_at,id FOR UPDATE",(data.ingredient_id,slot['starts_at'],data.policy)).fetchall()
        if sum((lot['remaining'] for lot in lots),Decimal(0)) < data.amount: raise HTTPException(409,'선택한 수령일까지 유효한 재고가 부족합니다.')
        r=db.execute('INSERT INTO reservations(slot_id,ingredient_id,amount,policy,note,request_key,request_hash) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *',(data.slot_id,data.ingredient_id,data.amount,data.policy,data.note.strip(),key,digest)).fetchone()
        left=data.amount
        for lot in lots:
            take=min(left,lot['remaining'])
            if not take: break
            db.execute('UPDATE inventory_lots SET remaining=remaining-%s WHERE id=%s',(take,lot['id']))
            db.execute('INSERT INTO reservation_lot_allocations VALUES (%s,%s,%s)',(r['id'],lot['id'],take))
            db.execute("INSERT INTO stock_movements(ingredient_id,lot_id,reservation_id,delta,reason) VALUES (%s,%s,%s,%s,'RESERVE')",(data.ingredient_id,lot['id'],r['id'],-take))
            left-=take
        db.execute('UPDATE ingredients SET stock=stock-%s WHERE id=%s',(data.amount,data.ingredient_id))
        db.execute('UPDATE pickup_slots SET reserved=reserved+1 WHERE id=%s',(data.slot_id,))
        return r

@app.get('/api/reservations')
def list_reservations():
    with connect() as db:
        return db.execute('SELECT r.*,i.name,i.unit,s.starts_at,s.ends_at FROM reservations r JOIN ingredients i ON i.id=r.ingredient_id JOIN pickup_slots s ON s.id=r.slot_id ORDER BY r.created_at DESC,r.id DESC LIMIT 100').fetchall()

@app.post('/api/reservations/{reservation_id}/cancel')
def cancel_reservation(reservation_id: int):
    with connect() as db:
        r=db.execute('SELECT * FROM reservations WHERE id=%s FOR UPDATE',(reservation_id,)).fetchone()
        if not r: raise HTTPException(404,'예약이 없습니다.')
        if r['status']!='CONFIRMED': raise HTTPException(409,'이미 취소된 예약입니다.')
        slot=db.execute('SELECT * FROM pickup_slots WHERE id=%s FOR UPDATE',(r['slot_id'],)).fetchone()
        if slot['starts_at'] <= datetime.now(ZoneInfo('UTC')): raise HTTPException(409,'수령 시간이 시작된 예약은 취소할 수 없습니다.')
        db.execute('SELECT id FROM ingredients WHERE id=%s FOR UPDATE',(r['ingredient_id'],))
        lots=db.execute('SELECT a.lot_id,a.amount,l.expires_on FROM reservation_lot_allocations a JOIN inventory_lots l ON l.id=a.lot_id WHERE a.reservation_id=%s ORDER BY l.id FOR UPDATE OF l',(reservation_id,)).fetchall()
        today=datetime.now(ZoneInfo('Asia/Seoul')).date()
        for lot in lots:
            db.execute("INSERT INTO stock_movements(ingredient_id,lot_id,reservation_id,delta,reason) VALUES (%s,%s,%s,%s,'RESERVE_CANCEL')",(r['ingredient_id'],lot['lot_id'],reservation_id,lot['amount']))
            if lot['expires_on'] is None or lot['expires_on'] < today:
                db.execute("INSERT INTO stock_movements(ingredient_id,lot_id,reservation_id,delta,reason) VALUES (%s,%s,%s,%s,'DISPOSE')",(r['ingredient_id'],lot['lot_id'],reservation_id,-lot['amount']))
            else:
                db.execute('UPDATE inventory_lots SET remaining=remaining+%s WHERE id=%s',(lot['amount'],lot['lot_id']))
                db.execute('UPDATE ingredients SET stock=stock+%s WHERE id=%s',(lot['amount'],r['ingredient_id']))
        db.execute('UPDATE pickup_slots SET reserved=reserved-1 WHERE id=%s',(r['slot_id'],))
        return db.execute("UPDATE reservations SET status='CANCELLED',cancelled_at=now() WHERE id=%s RETURNING *",(reservation_id,)).fetchone()

@app.post('/api/usages', status_code=201)
def create_usage(data: UsageIn, idempotency_key: UUID = Header(alias='Idempotency-Key')):
    dispose_expired()
    digest = hashlib.sha256(json.dumps({'ingredient_id':data.ingredient_id,'amount':str(data.amount.normalize()),'policy':data.policy,'note':data.note.strip()},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    with connect() as db:
        key = str(idempotency_key)
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,))
        previous = db.execute('SELECT * FROM stock_usages WHERE request_key=%s',(key,)).fetchone()
        if previous:
            if previous['request_hash'] != digest: raise HTTPException(409,'같은 요청 식별자를 다른 내용에 사용할 수 없습니다.')
            return previous
        ingredient = db.execute('SELECT id FROM ingredients WHERE id=%s FOR UPDATE',(data.ingredient_id,)).fetchone()
        if not ingredient: raise HTTPException(404,'식재료가 없습니다.')
        lots = db.execute("SELECT * FROM inventory_lots WHERE ingredient_id=%s AND remaining>0 AND expires_on >= (now() AT TIME ZONE 'Asia/Seoul')::date ORDER BY CASE WHEN %s='FEFO' THEN expires_on END, received_at,id FOR UPDATE",(data.ingredient_id,data.policy)).fetchall()
        if sum((lot['remaining'] for lot in lots), Decimal(0)) < data.amount: raise HTTPException(409,'사용 가능한 재고가 부족합니다.')
        usage = db.execute('INSERT INTO stock_usages(ingredient_id,amount,policy,request_key,request_hash,note) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *',(data.ingredient_id,data.amount,data.policy,key,digest,data.note.strip())).fetchone()
        remaining = data.amount
        for lot in lots:
            amount = min(remaining,lot['remaining'])
            if not amount: break
            db.execute('UPDATE inventory_lots SET remaining=remaining-%s WHERE id=%s',(amount,lot['id']))
            db.execute('INSERT INTO usage_lot_allocations VALUES (%s,%s,%s)',(usage['id'],lot['id'],amount))
            db.execute("INSERT INTO stock_movements(ingredient_id,lot_id,usage_id,delta,reason) VALUES (%s,%s,%s,%s,'USE')",(data.ingredient_id,lot['id'],usage['id'],-amount))
            remaining -= amount
        db.execute('UPDATE ingredients SET stock=stock-%s WHERE id=%s',(data.amount,data.ingredient_id))
        return usage

@app.get('/api/usages')
def list_usages():
    with connect() as db:
        return db.execute('SELECT u.*,i.name,i.unit FROM stock_usages u JOIN ingredients i ON i.id=u.ingredient_id ORDER BY u.created_at DESC,u.id DESC LIMIT 100').fetchall()

@app.post('/api/usages/{usage_id}/cancel')
def cancel_usage(usage_id: int):
    with connect() as db:
        usage = db.execute('SELECT * FROM stock_usages WHERE id=%s FOR UPDATE',(usage_id,)).fetchone()
        if not usage: raise HTTPException(404,'사용 내역이 없습니다.')
        if usage['status'] != 'CONFIRMED': raise HTTPException(409,'이미 취소된 사용 내역입니다.')
        db.execute('SELECT id FROM ingredients WHERE id=%s FOR UPDATE',(usage['ingredient_id'],))
        lots = db.execute('SELECT a.lot_id,a.amount FROM usage_lot_allocations a JOIN inventory_lots l ON l.id=a.lot_id WHERE a.usage_id=%s ORDER BY l.id FOR UPDATE OF l',(usage_id,)).fetchall()
        for lot in lots:
            db.execute('UPDATE inventory_lots SET remaining=remaining+%s WHERE id=%s',(lot['amount'],lot['lot_id']))
            db.execute("INSERT INTO stock_movements(ingredient_id,lot_id,usage_id,delta,reason) VALUES (%s,%s,%s,%s,'USE_CANCEL')",(usage['ingredient_id'],lot['lot_id'],usage_id,lot['amount']))
        db.execute('UPDATE ingredients SET stock=stock+%s WHERE id=%s',(usage['amount'],usage['ingredient_id']))
        return db.execute("UPDATE stock_usages SET status='CANCELLED',cancelled_at=now() WHERE id=%s RETURNING *",(usage_id,)).fetchone()

@app.post('/api/ingredients', status_code=201)
def create_ingredient(data: IngredientIn):
    try:
        with connect() as db:
            return db.execute("INSERT INTO ingredients(name,unit,threshold) VALUES (%s,%s,%s) RETURNING *", (data.name.strip(),data.unit.strip(),data.threshold)).fetchone()
    except psycopg.errors.UniqueViolation: raise HTTPException(409,"이미 존재하는 재료입니다.")

@app.get('/api/ingredients')
def ingredients():
    dispose_expired()
    with connect() as db:
        return db.execute("""SELECT i.*, COALESCE(SUM(l.remaining) FILTER (WHERE l.expires_on >= (now() AT TIME ZONE 'Asia/Seoul')::date),0) usable_stock, COALESCE(SUM(l.remaining) FILTER (WHERE l.expires_on IS NULL),0) unknown_stock, COALESCE(SUM(l.remaining) FILTER (WHERE l.expires_on < (now() AT TIME ZONE 'Asia/Seoul')::date),0) expired_stock, COALESCE(SUM(l.remaining) FILTER (WHERE l.expires_on BETWEEN (now() AT TIME ZONE 'Asia/Seoul')::date AND (now() AT TIME ZONE 'Asia/Seoul')::date+3),0) expiring_stock, (COALESCE(SUM(l.remaining) FILTER (WHERE l.expires_on >= (now() AT TIME ZONE 'Asia/Seoul')::date),0) <= i.threshold) AS low FROM ingredients i LEFT JOIN inventory_lots l ON l.ingredient_id=i.id GROUP BY i.id ORDER BY i.id""").fetchall()

@app.get('/api/ingredients/{ingredient_id}/lots')
def lots(ingredient_id:int):
    dispose_expired()
    with connect() as db: return db.execute("SELECT *, CASE WHEN expires_on IS NULL THEN 'UNKNOWN' WHEN expires_on < (now() AT TIME ZONE 'Asia/Seoul')::date THEN 'EXPIRED' WHEN expires_on <= (now() AT TIME ZONE 'Asia/Seoul')::date+3 THEN 'EXPIRING' ELSE 'AVAILABLE' END status FROM inventory_lots WHERE ingredient_id=%s ORDER BY received_at,id",(ingredient_id,)).fetchall()

@app.put('/api/lots/{lot_id}')
def update_lot(lot_id:int,data:LotDetails):
    with connect() as db:
        lookup=db.execute("SELECT ingredient_id FROM inventory_lots WHERE id=%s",(lot_id,)).fetchone()
        if not lookup: raise HTTPException(404,"입고 로트가 없습니다.")
        db.execute("SELECT id FROM ingredients WHERE id=%s FOR UPDATE",(lookup['ingredient_id'],))
        conflict=db.execute("SELECT 1 FROM reservation_lot_allocations a JOIN reservations r ON r.id=a.reservation_id JOIN pickup_slots s ON s.id=r.slot_id WHERE a.lot_id=%s AND r.status='CONFIRMED' AND (s.starts_at AT TIME ZONE 'Asia/Seoul')::date > %s LIMIT 1",(lot_id,data.expires_on)).fetchone()
        if conflict: raise HTTPException(409,'이 로트가 예약된 수령일보다 유통기한을 앞당길 수 없습니다.')
        return db.execute("UPDATE inventory_lots SET expires_on=%s,location=%s WHERE id=%s RETURNING *",(data.expires_on,data.location.strip(),lot_id)).fetchone()

@app.get('/api/ingredients/{ingredient_id}/movements')
def movements(ingredient_id:int):
    with connect() as db: return db.execute("SELECT * FROM stock_movements WHERE ingredient_id=%s ORDER BY id DESC LIMIT 100",(ingredient_id,)).fetchall()

@app.post('/api/ingredients/{ingredient_id}/receipts')
def receipt(ingredient_id:int, data:Receipt):
    if data.expires_on < datetime.now(ZoneInfo('Asia/Seoul')).date(): raise HTTPException(422,"지난 유통기한으로 입고할 수 없습니다.")
    with connect() as db:
        row=db.execute("UPDATE ingredients SET stock=stock+%s WHERE id=%s RETURNING *",(data.amount,ingredient_id)).fetchone()
        if not row: raise HTTPException(404,"재료가 없습니다.")
        lot=db.execute("INSERT INTO inventory_lots(ingredient_id,expires_on,location,initial_amount,remaining) VALUES (%s,%s,%s,%s,%s) RETURNING id",(ingredient_id,data.expires_on,data.location.strip(),data.amount,data.amount)).fetchone()
        db.execute("INSERT INTO stock_movements(ingredient_id,lot_id,delta,reason) VALUES (%s,%s,%s,'RECEIPT')",(ingredient_id,lot['id'],data.amount))
        return row
