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
