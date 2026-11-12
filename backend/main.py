import os
import hashlib
import asyncio
import logging
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool
import json
from uuid import UUID
from collections import defaultdict
from decimal import Decimal
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Annotated, Literal
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://bento:bento@localhost:5432/bentostock")
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
origins = ["http://localhost:5173", "http://127.0.0.1:5173", "https://freshstock-ingredients.web.app", "https://freshstock-ingredients.firebaseapp.com"] + [x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

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

class Item(BaseModel):
    menu_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=1000)
class OrderIn(BaseModel):
    customer_id: int = Field(gt=0)
    pickup_slot_id: int = Field(gt=0)
    allocation_policy: Literal["FIFO", "FEFO"] = "FIFO"
    items: list[Item] = Field(min_length=1, max_length=50)
class MenuIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r".*\S.*")
    price: int = Field(ge=0, le=10000000)
    active: bool = True
class Receipt(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    expires_on: date
    location: str = Field(min_length=1, max_length=100, pattern=r".*\S.*")
class LotDetails(BaseModel):
    expires_on: date
    location: str = Field(min_length=1, max_length=100, pattern=r".*\S.*")
class SlotIn(BaseModel):
    starts_at: datetime
    ends_at: datetime
    capacity: int = Field(gt=0, le=100000)


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
class RecipeItem(BaseModel):
    ingredient_id: int = Field(gt=0)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
class RecipeIn(BaseModel):
    items: list[RecipeItem] = Field(min_length=1, max_length=100)

@app.post("/api/ingredients", status_code=201)
def create_ingredient(data: IngredientIn):
    try:
        with connect() as db:
            return db.execute("INSERT INTO ingredients(name,unit,threshold) VALUES (%s,%s,%s) RETURNING *", (data.name.strip(),data.unit.strip(),data.threshold)).fetchone()
    except psycopg.errors.UniqueViolation: raise HTTPException(409,"이미 존재하는 재료입니다.")

@app.put("/api/menus/{menu_id}/recipe")
def update_recipe(menu_id:int, data:RecipeIn):
    ids=[i.ingredient_id for i in data.items]
    if len(ids)!=len(set(ids)): raise HTTPException(422,"재료가 중복되었습니다.")
    with connect() as db:
        # 주문이 메뉴에 FOR SHARE를 걸기 때문에 레시피 교체 중 주문이 섞이지 않는다.
        if not db.execute("SELECT id FROM menus WHERE id=%s FOR UPDATE",(menu_id,)).fetchone(): raise HTTPException(404,"메뉴가 없습니다.")
        existing=db.execute("SELECT id FROM ingredients WHERE id=ANY(%s)",(ids,)).fetchall()
        if len(existing)!=len(ids): raise HTTPException(404,"존재하지 않는 재료입니다.")
        db.execute("DELETE FROM recipes WHERE menu_id=%s",(menu_id,))
        for item in data.items: db.execute("INSERT INTO recipes(menu_id,ingredient_id,amount) VALUES (%s,%s,%s)",(menu_id,item.ingredient_id,item.amount))
        return {"message":"레시피가 저장되었습니다."}

@app.get("/api/customers")
def customers():
    with connect() as db: return db.execute("SELECT * FROM customers ORDER BY id").fetchall()
@app.get("/api/menus")
def menus():
    with connect() as db:
        return db.execute("""SELECT m.*, COALESCE(json_agg(json_build_object('ingredient_id',i.id,'name',i.name,'amount',r.amount,'unit',i.unit) ORDER BY i.id) FILTER (WHERE i.id IS NOT NULL),'[]') AS recipe FROM menus m LEFT JOIN recipes r ON r.menu_id=m.id LEFT JOIN ingredients i ON i.id=r.ingredient_id GROUP BY m.id ORDER BY m.id""").fetchall()
@app.post("/api/menus", status_code=201)
def create_menu(data: MenuIn):
    try:
        with connect() as db: return db.execute("INSERT INTO menus(name,price,active) VALUES (%s,%s,%s) RETURNING *",(data.name.strip(),data.price,data.active)).fetchone()
    except psycopg.errors.UniqueViolation: raise HTTPException(409,"이미 존재하는 메뉴입니다.")
@app.put("/api/menus/{menu_id}")
def update_menu(menu_id: int, data: MenuIn):
    try:
        with connect() as db:
            row=db.execute("UPDATE menus SET name=%s,price=%s,active=%s WHERE id=%s RETURNING *",(data.name.strip(),data.price,data.active,menu_id)).fetchone()
            if not row: raise HTTPException(404,"메뉴가 없습니다.")
            return row
    except psycopg.errors.UniqueViolation: raise HTTPException(409,"이미 존재하는 메뉴입니다.")
@app.get("/api/ingredients")
def ingredients():
    dispose_expired()
    with connect() as db:
        return db.execute("""SELECT i.*, COALESCE(SUM(l.remaining) FILTER (WHERE l.expires_on >= (now() AT TIME ZONE 'Asia/Seoul')::date),0) usable_stock, COALESCE(SUM(l.remaining) FILTER (WHERE l.expires_on IS NULL),0) unknown_stock, COALESCE(SUM(l.remaining) FILTER (WHERE l.expires_on < (now() AT TIME ZONE 'Asia/Seoul')::date),0) expired_stock, COALESCE(SUM(l.remaining) FILTER (WHERE l.expires_on BETWEEN (now() AT TIME ZONE 'Asia/Seoul')::date AND (now() AT TIME ZONE 'Asia/Seoul')::date+3),0) expiring_stock, (COALESCE(SUM(l.remaining) FILTER (WHERE l.expires_on >= (now() AT TIME ZONE 'Asia/Seoul')::date),0) <= i.threshold) AS low FROM ingredients i LEFT JOIN inventory_lots l ON l.ingredient_id=i.id GROUP BY i.id ORDER BY i.id""").fetchall()
@app.get("/api/ingredients/{ingredient_id}/lots")
def lots(ingredient_id:int):
    dispose_expired()
    with connect() as db: return db.execute("SELECT *, CASE WHEN expires_on IS NULL THEN 'UNKNOWN' WHEN expires_on < (now() AT TIME ZONE 'Asia/Seoul')::date THEN 'EXPIRED' WHEN expires_on <= (now() AT TIME ZONE 'Asia/Seoul')::date+3 THEN 'EXPIRING' ELSE 'AVAILABLE' END status FROM inventory_lots WHERE ingredient_id=%s ORDER BY received_at,id",(ingredient_id,)).fetchall()
@app.put("/api/lots/{lot_id}")
def update_lot(lot_id:int,data:LotDetails):
    with connect() as db:
        lookup=db.execute("SELECT ingredient_id FROM inventory_lots WHERE id=%s",(lot_id,)).fetchone()
        if not lookup: raise HTTPException(404,"입고 로트가 없습니다.")
        db.execute("SELECT id FROM ingredients WHERE id=%s FOR UPDATE",(lookup['ingredient_id'],))
        return db.execute("UPDATE inventory_lots SET expires_on=%s,location=%s WHERE id=%s RETURNING *",(data.expires_on,data.location.strip(),lot_id)).fetchone()
@app.get("/api/ingredients/{ingredient_id}/movements")
def movements(ingredient_id:int):
    with connect() as db: return db.execute("SELECT * FROM stock_movements WHERE ingredient_id=%s ORDER BY id DESC LIMIT 100",(ingredient_id,)).fetchall()
@app.post("/api/ingredients/{ingredient_id}/receipts")
def receipt(ingredient_id:int, data:Receipt):
    if data.expires_on < datetime.now(ZoneInfo('Asia/Seoul')).date(): raise HTTPException(422,"지난 유통기한으로 입고할 수 없습니다.")
    with connect() as db:
        row=db.execute("UPDATE ingredients SET stock=stock+%s WHERE id=%s RETURNING *",(data.amount,ingredient_id)).fetchone()
        if not row: raise HTTPException(404,"재료가 없습니다.")
        lot=db.execute("INSERT INTO inventory_lots(ingredient_id,expires_on,location,initial_amount,remaining) VALUES (%s,%s,%s,%s,%s) RETURNING id",(ingredient_id,data.expires_on,data.location.strip(),data.amount,data.amount)).fetchone()
        db.execute("INSERT INTO stock_movements(ingredient_id,lot_id,delta,reason) VALUES (%s,%s,%s,'RECEIPT')",(ingredient_id,lot['id'],data.amount))
        return row
@app.get("/api/pickup-slots")
def pickup_slots():
    with connect() as db: return db.execute("SELECT *,capacity-reserved AS available FROM pickup_slots WHERE starts_at > now() ORDER BY starts_at").fetchall()
@app.post("/api/pickup-slots", status_code=201)
def create_pickup_slot(data:SlotIn):
    if data.starts_at.utcoffset() is None or data.ends_at.utcoffset() is None: raise HTTPException(422,"시간대가 포함된 시각을 입력하세요.")
    if data.ends_at <= data.starts_at or data.starts_at <= datetime.now(ZoneInfo('UTC')): raise HTTPException(422,"시작과 종료 시각을 확인하세요.")
    try:
        with connect() as db: return db.execute("INSERT INTO pickup_slots(starts_at,ends_at,capacity) VALUES (%s,%s,%s) RETURNING *",(data.starts_at,data.ends_at,data.capacity)).fetchone()
    except psycopg.errors.UniqueViolation: raise HTTPException(409,"같은 시작 시각의 시간대가 존재합니다.")

@app.post("/api/orders", status_code=201)
def create_order(data:OrderIn, idempotency_key: UUID = Header(alias="Idempotency-Key")):
    dispose_expired()
    quantities=defaultdict(int)
    for item in data.items: quantities[item.menu_id]+=item.quantity
    if any(q > 1000 for q in quantities.values()): raise HTTPException(422,"메뉴별 수량은 1000 이하입니다.")
    payload=json.dumps({'customer_id':data.customer_id,'pickup_slot_id':data.pickup_slot_id,'allocation_policy':data.allocation_policy,'items':sorted(quantities.items())},separators=(',',':'))
    digest=hashlib.sha256(payload.encode()).hexdigest()
    with connect() as db:
        key=str(idempotency_key)
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(key,))
        previous=db.execute("SELECT * FROM orders WHERE request_key=%s",(key,)).fetchone()
        if previous:
            if previous['request_hash']!=digest: raise HTTPException(409,"같은 주문 키를 다른 내용에 사용할 수 없습니다.")
            return previous
        if not db.execute("SELECT 1 FROM customers WHERE id=%s",(data.customer_id,)).fetchone(): raise HTTPException(404,"고객이 없습니다.")
        slot=db.execute("SELECT * FROM pickup_slots WHERE id=%s FOR UPDATE",(data.pickup_slot_id,)).fetchone()
        if not slot or slot['starts_at'] <= datetime.now(ZoneInfo('UTC')): raise HTTPException(400,"수령 가능한 시간대가 아닙니다.")
        total_units=sum(quantities.values())
        if slot['reserved']+total_units > slot['capacity']: raise HTTPException(409,"해당 수령 시간의 생산 용량이 부족합니다.")
        # 메뉴 잠금은 가격/활성 상태 변경과 충돌하여 주문 시점의 가격을 보장한다.
        ids=sorted(quantities)
        menus=db.execute("SELECT * FROM menus WHERE id=ANY(%s) ORDER BY id FOR SHARE",(ids,)).fetchall()
        if len(menus)!=len(ids) or any(not m['active'] for m in menus): raise HTTPException(400,"존재하지 않거나 판매 중지된 메뉴입니다.")
        recipes=db.execute("SELECT menu_id,ingredient_id,amount FROM recipes WHERE menu_id=ANY(%s) ORDER BY menu_id,ingredient_id",(ids,)).fetchall()
        if set(ids)!={r['menu_id'] for r in recipes}: raise HTTPException(400,"레시피가 없는 메뉴는 주문할 수 없습니다.")
        needs=defaultdict(Decimal)
        for r in recipes: needs[r['ingredient_id']] += r['amount'] * quantities[r['menu_id']]
        # 모든 주문과 취소가 재료 id 순서로 잠금: 교착 위험 최소화.
        stock=db.execute("SELECT id,name,stock FROM ingredients WHERE id=ANY(%s) ORDER BY id FOR UPDATE",(sorted(needs),)).fetchall()
        lots=db.execute("SELECT * FROM inventory_lots WHERE ingredient_id=ANY(%s) AND remaining>0 AND expires_on >= GREATEST((now() AT TIME ZONE 'Asia/Seoul')::date, (%s::timestamptz AT TIME ZONE 'Asia/Seoul')::date) ORDER BY ingredient_id, CASE WHEN %s = 'FEFO' THEN expires_on END ASC, received_at, id FOR UPDATE",(sorted(needs),slot['starts_at'],data.allocation_policy)).fetchall()
        available=defaultdict(Decimal)
        for lot in lots: available[lot['ingredient_id']]+=lot['remaining']
        for row in stock:
            if available[row['id']] < needs[row['id']]: raise HTTPException(409,f"사용 가능한 재고 부족: {row['name']}")
        db.execute("UPDATE pickup_slots SET reserved=reserved+%s WHERE id=%s",(total_units,data.pickup_slot_id))
        order=db.execute("INSERT INTO orders(customer_id,request_key,request_hash,pickup_slot_id,allocation_policy) VALUES (%s,%s,%s,%s,%s) RETURNING *",(data.customer_id,key,digest,data.pickup_slot_id,data.allocation_policy)).fetchone()
        left=dict(needs)
        for lot in lots:
            ingredient_id=lot['ingredient_id']; amount=min(left[ingredient_id],lot['remaining'])
            if amount:
                db.execute("UPDATE inventory_lots SET remaining=remaining-%s WHERE id=%s",(amount,lot['id']))
                db.execute("INSERT INTO order_lot_allocations(order_id,lot_id,amount) VALUES (%s,%s,%s)",(order['id'],lot['id'],amount))
                left[ingredient_id]-=amount
        for m in menus: db.execute("INSERT INTO order_items(order_id,menu_id,quantity,unit_price) VALUES (%s,%s,%s,%s)",(order['id'],m['id'],quantities[m['id']],m['price']))
        for ingredient_id, amount in sorted(needs.items()):
            db.execute("UPDATE ingredients SET stock=stock-%s WHERE id=%s",(amount,ingredient_id))
            db.execute("INSERT INTO order_ingredients VALUES (%s,%s,%s)",(order['id'],ingredient_id,amount))
            db.execute("INSERT INTO stock_movements(ingredient_id,order_id,delta,reason) VALUES (%s,%s,%s,'ORDER')",(ingredient_id,order['id'],-amount))
        return order

@app.post("/api/orders/{order_id}/cancel")
def cancel(order_id:int):
    with connect() as db:
        order=db.execute("SELECT * FROM orders WHERE id=%s FOR UPDATE",(order_id,)).fetchone()
        if not order: raise HTTPException(404,"주문이 없습니다.")
        if order['status']!='CONFIRMED': raise HTTPException(409,"이미 취소된 주문입니다.")
        if order['pickup_slot_id']:
            db.execute("SELECT id FROM pickup_slots WHERE id=%s FOR UPDATE",(order['pickup_slot_id'],))
            quantity=db.execute("SELECT COALESCE(sum(quantity),0) total FROM order_items WHERE order_id=%s",(order_id,)).fetchone()['total']
            db.execute("UPDATE pickup_slots SET reserved=reserved-%s WHERE id=%s",(quantity,order['pickup_slot_id']))
        items=db.execute("SELECT ingredient_id,amount FROM order_ingredients WHERE order_id=%s ORDER BY ingredient_id",(order_id,)).fetchall()
        db.execute("SELECT id FROM ingredients WHERE id=ANY(%s) ORDER BY id FOR UPDATE",([x['ingredient_id'] for x in items],)).fetchall()
        allocations=db.execute("SELECT a.lot_id,a.amount FROM order_lot_allocations a JOIN inventory_lots l ON l.id=a.lot_id WHERE a.order_id=%s ORDER BY l.ingredient_id,l.received_at,l.id FOR UPDATE OF l",(order_id,)).fetchall()
        for allocation in allocations: db.execute("UPDATE inventory_lots SET remaining=remaining+%s WHERE id=%s",(allocation['amount'],allocation['lot_id']))
        # 기존 주문에는 로트 이력이 없으므로 복구량을 확인 필요 로트로 기록한다.
        if not allocations:
            for x in items: db.execute("INSERT INTO inventory_lots(ingredient_id,location,initial_amount,remaining) VALUES (%s,'기존 주문 복구(유통기한 확인 필요)',%s,%s)",(x['ingredient_id'],x['amount'],x['amount']))
        for x in items:
            db.execute("UPDATE ingredients SET stock=stock+%s WHERE id=%s",(x['amount'],x['ingredient_id']))
            db.execute("INSERT INTO stock_movements(ingredient_id,order_id,delta,reason) VALUES (%s,%s,%s,'CANCEL')",(x['ingredient_id'],order_id,x['amount']))
        result=db.execute("UPDATE orders SET status='CANCELLED',cancelled_at=now() WHERE id=%s RETURNING *",(order_id,)).fetchone()
    try: dispose_expired()
    except Exception: logging.exception("취소 후 유통기한 경과 재고 정리 실패")
    return result

@app.get("/api/orders")
def orders(customer_id:int|None=None, menu_id:int|None=None, status:str|None=None, start:date|None=None, end:date|None=None, limit:Annotated[int,Query(ge=1,le=100)]=20, offset:Annotated[int,Query(ge=0)]=0):
    if status and status not in ('CONFIRMED','CANCELLED'): raise HTTPException(422,"잘못된 상태입니다.")
    with connect() as db:
        return db.execute("""SELECT o.*, c.name customer_name, COALESCE(sum(oi.quantity*oi.unit_price),0) total, json_agg(json_build_object('menu_id',m.id,'name',m.name,'quantity',oi.quantity,'unit_price',oi.unit_price) ORDER BY oi.id) items FROM orders o JOIN customers c ON c.id=o.customer_id JOIN order_items oi ON oi.order_id=o.id JOIN menus m ON m.id=oi.menu_id WHERE (%s::bigint IS NULL OR o.customer_id=%s) AND (%s::text IS NULL OR o.status=%s) AND (%s::date IS NULL OR o.created_at >= %s::date) AND (%s::date IS NULL OR o.created_at < %s::date + interval '1 day') AND (%s::bigint IS NULL OR EXISTS (SELECT 1 FROM order_items x WHERE x.order_id=o.id AND x.menu_id=%s)) GROUP BY o.id,c.name ORDER BY o.created_at DESC,o.id DESC LIMIT %s OFFSET %s""",(customer_id,customer_id,status,status,start,start,end,end,menu_id,menu_id,limit,offset)).fetchall()
@app.get("/api/stats")
def stats(start:date|None=None,end:date|None=None):
    with connect() as db:
        rows=db.execute("""SELECT m.id,m.name,COALESCE(sum(oi.quantity) FILTER (WHERE o.id IS NOT NULL),0) quantity,COALESCE(sum(oi.quantity*oi.unit_price) FILTER (WHERE o.id IS NOT NULL),0) revenue FROM menus m LEFT JOIN order_items oi ON oi.menu_id=m.id LEFT JOIN orders o ON o.id=oi.order_id AND o.status='CONFIRMED' AND (%s::date IS NULL OR o.created_at >= %s::date) AND (%s::date IS NULL OR o.created_at < %s::date + interval '1 day') GROUP BY m.id ORDER BY quantity DESC,m.id""",(start,start,end,end)).fetchall()
        return {'menus':rows,'total_revenue':sum(r['revenue'] for r in rows),'total_quantity':sum(r['quantity'] for r in rows)}
