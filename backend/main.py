import os
import hashlib
import json
from uuid import UUID
from collections import defaultdict
from decimal import Decimal
from datetime import date
from typing import Annotated
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://bento:bento@localhost:5432/bentostock")
app = FastAPI(title="BentoStock API")
origins = ["http://localhost:5173", "http://127.0.0.1:5173"] + [x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

class Item(BaseModel):
    menu_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=1000)
class OrderIn(BaseModel):
    customer_id: int = Field(gt=0)
    items: list[Item] = Field(min_length=1, max_length=50)
class MenuIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r".*\S.*")
    price: int = Field(ge=0, le=10000000)
    active: bool = True
class Receipt(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)

class IngredientIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r".*\S.*")
    unit: str = Field(min_length=1, max_length=20, pattern=r".*\S.*")
    threshold: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
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
    with connect() as db: return db.execute("SELECT *, stock <= threshold AS low FROM ingredients ORDER BY id").fetchall()
@app.get("/api/ingredients/{ingredient_id}/movements")
def movements(ingredient_id:int):
    with connect() as db: return db.execute("SELECT * FROM stock_movements WHERE ingredient_id=%s ORDER BY id DESC LIMIT 100",(ingredient_id,)).fetchall()
@app.post("/api/ingredients/{ingredient_id}/receipts")
def receipt(ingredient_id:int, data:Receipt):
    with connect() as db:
        row=db.execute("UPDATE ingredients SET stock=stock+%s WHERE id=%s RETURNING *",(data.amount,ingredient_id)).fetchone()
        if not row: raise HTTPException(404,"재료가 없습니다.")
        db.execute("INSERT INTO stock_movements(ingredient_id,delta,reason) VALUES (%s,%s,'RECEIPT')",(ingredient_id,data.amount))
        return row

@app.post("/api/orders", status_code=201)
def create_order(data:OrderIn, idempotency_key: UUID = Header(alias="Idempotency-Key")):
    quantities=defaultdict(int)
    for item in data.items: quantities[item.menu_id]+=item.quantity
    if any(q > 1000 for q in quantities.values()): raise HTTPException(422,"메뉴별 수량은 1000 이하입니다.")
    payload=json.dumps({'customer_id':data.customer_id,'items':sorted(quantities.items())},separators=(',',':'))
    digest=hashlib.sha256(payload.encode()).hexdigest()
    with connect() as db:
        key=str(idempotency_key)
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(key,))
        previous=db.execute("SELECT * FROM orders WHERE request_key=%s",(key,)).fetchone()
        if previous:
            if previous['request_hash']!=digest: raise HTTPException(409,"같은 주문 키를 다른 내용에 사용할 수 없습니다.")
            return previous
        if not db.execute("SELECT 1 FROM customers WHERE id=%s",(data.customer_id,)).fetchone(): raise HTTPException(404,"고객이 없습니다.")
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
        for row in stock:
            if row['stock'] < needs[row['id']]: raise HTTPException(409,f"재고 부족: {row['name']}")
        order=db.execute("INSERT INTO orders(customer_id,request_key,request_hash) VALUES (%s,%s,%s) RETURNING *",(data.customer_id,key,digest)).fetchone()
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
        items=db.execute("SELECT ingredient_id,amount FROM order_ingredients WHERE order_id=%s ORDER BY ingredient_id",(order_id,)).fetchall()
        db.execute("SELECT id FROM ingredients WHERE id=ANY(%s) ORDER BY id FOR UPDATE",([x['ingredient_id'] for x in items],)).fetchall()
        for x in items:
            db.execute("UPDATE ingredients SET stock=stock+%s WHERE id=%s",(x['amount'],x['ingredient_id']))
            db.execute("INSERT INTO stock_movements(ingredient_id,order_id,delta,reason) VALUES (%s,%s,%s,'CANCEL')",(x['ingredient_id'],order_id,x['amount']))
        return db.execute("UPDATE orders SET status='CANCELLED',cancelled_at=now() WHERE id=%s RETURNING *",(order_id,)).fetchone()

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
