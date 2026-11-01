import asyncio
import hashlib
import os
import re
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from sqlalchemy.exc import IntegrityError

from app import bale
from app.ai import ask_ai
from app.config import BALE_TOKEN, ADMIN_CHAT_ID
from app.database import (
    SessionLocal, init_db, Product, Customer, Message, Order, ProcessedUpdate
)

app = FastAPI()

SECRET = hashlib.sha256(BALE_TOKEN.encode()).hexdigest()[:24]
PUBLIC_URL = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
WELCOME = os.getenv(
    "WELCOME_MESSAGE",
    "سلام! به فروشگاه ما خوش اومدید 🌹\nچطور می‌تونم کمکتون کنم؟",
)

HELP = (
    "راهنمای مدیر:\n\n"
    "💬 پاسخ به مشتری: مشتری رو انتخاب کن و پیامت رو بنویس.\n"
    "↩️ یا هر پیام رسیده از مشتری رو ریپلای کن و جوابت رو بنویس.\n"
    "➕ افزودن محصول، 💰 قیمت، 📊 موجودی، ❌ حذف: ربات مرحله‌به‌مرحله ازت می‌پرسه.\n"
    "🔕 / 🔔 توقف و فعال‌سازی پاسخ خودکار برای یک مشتری.\n"
    "👥 مشتریان و 🧾 سفارش‌ها: لیست‌ها.\n"
    "🔙 انصراف: برگشت به منوی اصلی."
)

CUSTOMER_KB = [
    ["🛍 محصولات", "🛒 ثبت سفارش"],
    ["❓ سوالات متداول", "👤 ارتباط با اپراتور"],
]

ADMIN_KB = [
    ["📦 محصولات", "🧾 سفارش‌ها"],
    ["👥 مشتریان", "💬 پاسخ به مشتری"],
    ["➕ افزودن محصول", "💰 تغییر قیمت"],
    ["📊 تغییر موجودی", "❌ حذف محصول"],
    ["🔕 توقف ربات", "🔔 فعال‌سازی ربات"],
    ["📖 راهنما"],
]

MENU_TEXTS = set(b for row in ADMIN_KB for b in row)

ADMIN_BUTTONS = {
    "📦 محصولات": "/products",
    "🧾 سفارش‌ها": "/orders",
    "👥 مشتریان": "/customers",
    "📖 راهنما": "/help",
}

CUSTOMER_BUTTON_TEXTS = {
    "🛒 ثبت سفارش": "می‌خوام سفارش ثبت کنم",
    "❓ سوالات متداول": "سوالات متداول فروشگاه چیه؟",
}

CANCEL = "🔙 انصراف"
END_CHAT = "🔙 پایان گفتگو"
SKIP = "⏭ بدون توضیحات"
CONFIRM_DEL = "✅ بله، حذف شود"

REPLY_HINT = "↩️ برای جواب دادن، همین پیام رو ریپلای کن و جوابت رو بنویس."

ADMIN_STATE = {}

DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def to_int(value):
    return int(str(value).translate(DIGITS).replace(",", "").strip())


def is_admin_chat(chat_id):
    return bool(ADMIN_CHAT_ID) and str(chat_id) == str(ADMIN_CHAT_ID)


def kb_for(chat_id):
    return ADMIN_KB if is_admin_chat(chat_id) else CUSTOMER_KB


@app.get("/")
async def home():
    return {"status": "ok"}


@app.on_event("startup")
async def startup():
    init_db()
    if not BALE_TOKEN:
        print("BALE_TOKEN is not set")
        return
    if PUBLIC_URL:
        res = await bale.set_webhook(f"{PUBLIC_URL}/webhook/{SECRET}")
        print("webhook:", res)
    else:
        await bale.delete_webhook()
        asyncio.create_task(polling_loop())


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request, background: BackgroundTasks):
    if secret != SECRET:
        raise HTTPException(status_code=403)
    update = await request.json()
    background.add_task(handle_update, update)
    return {"ok": True}


async def polling_loop():
    offset = None
    while True:
        updates = await bale.get_updates(offset)
        for u in updates:
            offset = u["update_id"] + 1
            await handle_update(u)
        if not updates:
            await asyncio.sleep(1)


async def handle_update(update):
    try:
        await process_update(update)
    except Exception as e:
        print("handle_update error:", e)


def is_duplicate(update_id):
    db = SessionLocal()
    try:
        db.add(ProcessedUpdate(update_id=update_id))
        db.commit()
        return False
    except IntegrityError:
        db.rollback()
        return True
    finally:
        db.close()


def save_message(chat_id, role, text):
    db = SessionLocal()
    try:
        db.add(Message(chat_id=chat_id, role=role, text=text))
        db.commit()
    finally:
        db.close()


def set_paused(chat_id, value):
    db = SessionLocal()
    try:
        customer = db.query(Customer).filter(Customer.chat_id == chat_id).first()
        if not customer:
            return False
        customer.bot_paused = value
        db.commit()
        return True
    finally:
        db.close()


async def notify_admin(text):
    if ADMIN_CHAT_ID:
        busy = ADMIN_STATE.get(str(ADMIN_CHAT_ID))
        await bale.send_message(
            ADMIN_CHAT_ID, text, keyboard=None if busy else ADMIN_KB
        )


def list_customers(limit=8):
    db = SessionLocal()
    try:
        q = db.query(Customer)
        if ADMIN_CHAT_ID:
            q = q.filter(Customer.chat_id != str(ADMIN_CHAT_ID))
        items = q.order_by(Customer.id.desc()).limit(limit).all()
        return [(c.chat_id, c.name or "-") for c in items]
    finally:
        db.close()


def customers_rows():
    rows = [[f"👤 {name} | {cid}"] for cid, name in list_customers()]
    rows.append([CANCEL])
    return rows


def list_products():
    db = SessionLocal()
    try:
        items = db.query(Product).filter(Product.active == True).all()
        return [(p.id, p.name) for p in items]
    finally:
        db.close()


def products_rows():
    rows = [[f"{pid}) {name}"] for pid, name in list_products()]
    rows.append([CANCEL])
    return rows


def get_product_name(pid):
    db = SessionLocal()
    try:
        p = db.query(Product).filter(Product.id == pid, Product.active == True).first()
        return p.name if p else None
    finally:
        db.close()


def update_product(pid, field, value):
    db = SessionLocal()
    try:
        p = db.query(Product).filter(Product.id == pid).first()
        if not p:
            return False
        if field == "price":
            p.price = value
        elif field == "stock":
            p.stock = value
        elif field == "delete":
            p.active = False
        db.commit()
        return True
    finally:
        db.close()


def create_product(name, price, stock, description):
    db = SessionLocal()
    try:
        p = Product(name=name, price=price, stock=stock, description=description)
        db.add(p)
        db.commit()
        return p.id
    finally:
        db.close()


def product_list_text():
    db = SessionLocal()
    try:
        items = db.query(Product).filter(Product.active == True).all()
        if not items:
            return "فعلاً محصولی ثبت نشده است."
        blocks = []
        for p in items:
            block = f"🛍 {p.name}\n💰 قیمت: {p.price} تومان\n📦 موجودی: {p.stock}"
            if p.description:
                block += f"\n📝 {p.description}"
            blocks.append(block)
        return "لیست محصولات:\n\n" + "\n\n".join(blocks)
    finally:
        db.close()


async def send_reply(chat_id, user_text, reply):
    save_message(chat_id, "assistant", reply)
    await bale.send_message(chat_id, reply, keyboard=kb_for(chat_id))
    if not is_admin_chat(chat_id):
        await notify_admin(
            f"👤 مشتری {chat_id}:\n{user_text}\n\n🤖 ربات:\n{reply}\n\n{REPLY_HINT}"
        )


async def send_to_customer(admin_id, cid, text, keyboard=None, note=""):
    await bale.send_message(cid, text, keyboard=CUSTOMER_KB)
    save_message(cid, "assistant", text)
    set_paused(cid, True)
    await bale.send_message(admin_id, "✅ ارسال شد." + note, keyboard=keyboard)


async def admin_flow(chat_id, text, msg):
    state = ADMIN_STATE.get(chat_id)

    reply_to = msg.get("reply_to_message") or {}
    if reply_to and not text.startswith("/") and text not in MENU_TEXTS:
        m = re.search(r"مشتری[\s:]+(\d{5,})", reply_to.get("text") or "")
        if m:
            await send_to_customer(
                chat_id,
                m.group(1),
                text,
                note="\n(ربات برای این مشتری متوقف شد. با «🔔 فعال‌سازی ربات» دوباره فعالش کن.)",
            )
            return True

    if text.startswith("/") and state and state["step"] != "reply_text":
        ADMIN_STATE.pop(chat_id, None)
        state = None

    if text in (CANCEL, END_CHAT):
        out = "باشه، به منوی اصلی برگشتیم."
        if state and state["step"] == "reply_text" and text == END_CHAT:
            set_paused(state["data"]["cid"], False)
            out = "گفتگو تموم شد و ربات برای این مشتری دوباره فعال شد."
        ADMIN_STATE.pop(chat_id, None)
        await bale.send_message(chat_id, out, keyboard=ADMIN_KB)
        return True

    if text == "/start":
        ADMIN_STATE.pop(chat_id, None)
        await bale.send_message(
            chat_id,
            "سلام مدیر 👋\nبا دکمه‌های پایین صفحه ربات رو مدیریت کن.",
            keyboard=ADMIN_KB,
        )
        return True

    if text in ADMIN_BUTTONS:
        ADMIN_STATE.pop(chat_id, None)
        answer = handle_admin(ADMIN_BUTTONS[text])
        await bale.send_message(chat_id, answer, keyboard=ADMIN_KB)
        return True

    if text == "💬 پاسخ به مشتری":
        if not list_customers():
            await bale.send_message(chat_id, "هنوز مشتری‌ای پیام نداده.", keyboard=ADMIN_KB)
            return True
        ADMIN_STATE[chat_id] = {"step": "reply_pick", "data": {}}
        await bale.send_message(
            chat_id, "به کدوم مشتری جواب بدی؟ یکی رو انتخاب کن:", keyboard=customers_rows()
        )
        return True

    if text in ("🔕 توقف ربات", "🔔 فعال‌سازی ربات"):
        if not list_customers():
            await bale.send_message(chat_id, "هنوز مشتری‌ای پیام نداده.", keyboard=ADMIN_KB)
            return True
        ADMIN_STATE[chat_id] = {
            "step": "toggle_pick",
            "data": {"value": text == "🔕 توقف ربات"},
        }
        await bale.send_message(
            chat_id, "کدوم مشتری؟ یکی رو انتخاب کن:", keyboard=customers_rows()
        )
        return True

    if text == "➕ افزودن محصول":
        ADMIN_STATE[chat_id] = {"step": "add_name", "data": {}}
        await bale.send_message(chat_id, "اسم محصول چیه؟", keyboard=[[CANCEL]])
        return True

    if text in ("💰 تغییر قیمت", "📊 تغییر موجودی", "❌ حذف محصول"):
        if not list_products():
            await bale.send_message(chat_id, "هنوز محصولی ثبت نشده.", keyboard=ADMIN_KB)
            return True
        actions = {
            "💰 تغییر قیمت": "price",
            "📊 تغییر موجودی": "stock",
            "❌ حذف محصول": "delete",
        }
        ADMIN_STATE[chat_id] = {"step": "product_pick", "data": {"action": actions[text]}}
        await bale.send_message(chat_id, "کدوم محصول؟", keyboard=products_rows())
        return True

    if not state:
        return False

    step = state["step"]
    data = state["data"]

    if step == "reply_pick":
        cid = text.split("|")[-1].strip()
        if not cid.isdigit():
            await bale.send_message(chat_id, "لطفاً یکی از دکمه‌های مشتری رو بزن.")
            return True
        set_paused(cid, True)
        ADMIN_STATE[chat_id] = {"step": "reply_text", "data": {"cid": cid}}
        await bale.send_message(
            chat_id,
            f"✍️ پیامت رو برای مشتری {cid} بنویس.\n"
            "هر پیامی بنویسی برای اون ارسال می‌شه و ربات تا پایان گفتگو ساکت می‌مونه.",
            keyboard=[[END_CHAT]],
        )
        return True

    if step == "reply_text":
        await send_to_customer(chat_id, data["cid"], text, keyboard=[[END_CHAT]])
        return True

    if step == "toggle_pick":
        cid = text.split("|")[-1].strip()
        if not cid.isdigit():
            await bale.send_message(chat_id, "لطفاً یکی از دکمه‌های مشتری رو بزن.")
            return True
        ok = set_paused(cid, data["value"])
        ADMIN_STATE.pop(chat_id, None)
        if not ok:
            out = "مشتری پیدا نشد."
        elif data["value"]:
            out = "✅ ربات برای این مشتری متوقف شد."
        else:
            out = "✅ ربات برای این مشتری فعال شد."
        await bale.send_message(chat_id, out, keyboard=ADMIN_KB)
        return True

    if step == "add_name":
        data["name"] = text
        state["step"] = "add_price"
        await bale.send_message(
            chat_id, "قیمت چقدره؟ (تومان، فقط عدد)", keyboard=[[CANCEL]]
        )
        return True

    if step == "add_price":
        try:
            data["price"] = to_int(text)
        except Exception:
            await bale.send_message(chat_id, "فقط عدد بنویس. مثلاً 450000")
            return True
        state["step"] = "add_stock"
        await bale.send_message(chat_id, "موجودی چنده؟ (فقط عدد)", keyboard=[[CANCEL]])
        return True

    if step == "add_stock":
        try:
            data["stock"] = to_int(text)
        except Exception:
            await bale.send_message(chat_id, "فقط عدد بنویس. مثلاً 10")
            return True
        state["step"] = "add_desc"
        await bale.send_message(
            chat_id,
            "توضیحات محصول رو بنویس، یا دکمه‌ی «بدون توضیحات» رو بزن:",
            keyboard=[[SKIP], [CANCEL]],
        )
        return True

    if step == "add_desc":
        desc = "" if text == SKIP else text
        pid = create_product(data["name"], data["price"], data["stock"], desc)
        ADMIN_STATE.pop(chat_id, None)
        await bale.send_message(
            chat_id,
            f"✅ محصول «{data['name']}» اضافه شد (شماره {pid}).",
            keyboard=ADMIN_KB,
        )
        return True

    if step == "product_pick":
        try:
            pid = to_int(text.split(")")[0])
        except Exception:
            await bale.send_message(chat_id, "لطفاً یکی از دکمه‌های محصول رو بزن.")
            return True
        name = get_product_name(pid)
        if not name:
            await bale.send_message(chat_id, "این محصول پیدا نشد. یکی از دکمه‌ها رو بزن.")
            return True
        action = data["action"]
        if action == "delete":
            ADMIN_STATE[chat_id] = {"step": "del_confirm", "data": {"pid": pid}}
            await bale.send_message(
                chat_id, f"مطمئنی «{name}» حذف بشه؟", keyboard=[[CONFIRM_DEL], [CANCEL]]
            )
        else:
            ADMIN_STATE[chat_id] = {
                "step": "edit_value",
                "data": {"pid": pid, "field": action},
            }
            label = "قیمت جدید (تومان)" if action == "price" else "موجودی جدید"
            await bale.send_message(
                chat_id, f"{label} «{name}» رو بنویس (فقط عدد):", keyboard=[[CANCEL]]
            )
        return True

    if step == "edit_value":
        try:
            value = to_int(text)
        except Exception:
            await bale.send_message(chat_id, "فقط عدد بنویس.")
            return True
        ok = update_product(data["pid"], data["field"], value)
        ADMIN_STATE.pop(chat_id, None)
        await bale.send_message(
            chat_id, "✅ انجام شد." if ok else "محصول پیدا نشد.", keyboard=ADMIN_KB
        )
        return True

    if step == "del_confirm":
        if text == CONFIRM_DEL:
            update_product(data["pid"], "delete", 0)
            ADMIN_STATE.pop(chat_id, None)
            await bale.send_message(chat_id, "✅ محصول حذف شد.", keyboard=ADMIN_KB)
        else:
            await bale.send_message(
                chat_id, "برای حذف دکمه‌ی «بله، حذف شود» رو بزن، یا انصراف."
            )
        return True

    ADMIN_STATE.pop(chat_id, None)
    return False


async def process_update(update):
    update_id = update.get("update_id")
    msg = update.get("message")
    if not msg or update_id is None:
        return
    if is_duplicate(update_id):
        return

    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    if not text:
        await bale.send_message(
            chat_id, "لطفاً پیامتون رو به صورت متن بفرستید.", keyboard=kb_for(chat_id)
        )
        return

    if text == "/myid":
        await bale.send_message(chat_id, f"شناسه شما: {chat_id}")
        return

    if is_admin_chat(chat_id):
        if await admin_flow(chat_id, text, msg):
            return

        if text.startswith("/reply"):
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                await bale.send_message(
                    chat_id, "فرمت: /reply شناسه_مشتری پیام", keyboard=ADMIN_KB
                )
                return
            await send_to_customer(chat_id, parts[1], parts[2], keyboard=ADMIN_KB)
            return

        if text.startswith("/"):
            answer = handle_admin(text)
            await bale.send_message(chat_id, answer, keyboard=ADMIN_KB)
            return

        await bale.send_message(
            chat_id, "از دکمه‌های منوی پایین استفاده کن 👇", keyboard=ADMIN_KB
        )
        return

    text = CUSTOMER_BUTTON_TEXTS.get(text, text)
    await handle_customer(chat_id, msg, text)


async def handle_customer(chat_id, msg, text):
    db = SessionLocal()
    try:
        customer = db.query(Customer).filter(Customer.chat_id == chat_id).first()
        if not customer:
            sender = msg.get("from") or {}
            full = f"{sender.get('first_name') or ''} {sender.get('last_name') or ''}".strip()
            customer = Customer(chat_id=chat_id, name=full)
            db.add(customer)
        paused = bool(customer.bot_paused)
        db.add(Message(chat_id=chat_id, role="user", text=text))
        db.commit()
    finally:
        db.close()

    if paused:
        await notify_admin(f"💬 پیام از مشتری {chat_id}:\n{text}\n\n{REPLY_HINT}")
        return

    if text == "/start":
        await send_reply(chat_id, text, WELCOME)
        return

    if text == "🛍 محصولات":
        await send_reply(chat_id, text, product_list_text())
        return

    if text == "👤 ارتباط با اپراتور":
        set_paused(chat_id, True)
        reply = "درخواست شما به اپراتور ارجاع داده شد. به‌زودی پاسخ می‌دیم."
        save_message(chat_id, "assistant", reply)
        await bale.send_message(chat_id, reply, keyboard=kb_for(chat_id))
        await notify_admin(
            f"🙋 مشتری {chat_id} اپراتور انسانی خواست.\n\n{REPLY_HINT}"
        )
        return

    result = await ask_ai(chat_id)
    reply = result["text"]

    if result["order"]:
        note = await create_order(chat_id, result["order"])
        reply = f"{reply}\n\n{note}".strip()

    if result["handoff"]:
        set_paused(chat_id, True)
        reply = (reply + "\n\nدرخواست شما به اپراتور ارجاع داده شد. به‌زودی پاسخ می‌دیم.").strip()
        await notify_admin(
            f"🙋 مشتری {chat_id} اپراتور انسانی خواست.\n"
            f"آخرین پیام: {text}\n\n{REPLY_HINT}"
        )

    if not reply:
        reply = "متوجه نشدم، می‌تونید دوباره بنویسید؟"

    await send_reply(chat_id, text, reply)


async def create_order(chat_id, data):
    try:
        qty = int(data.get("quantity") or 1)
    except Exception:
        qty = 1
    if qty < 1:
        qty = 1

    wanted = str(data.get("product") or "").strip()
    if not wanted:
        return "⚠️ سفارش ثبت نشد: محصول مشخص نیست."

    db = SessionLocal()
    try:
        product = (
            db.query(Product)
            .filter(Product.active == True, Product.name == wanted)
            .first()
        )
        if not product:
            product = (
                db.query(Product)
                .filter(Product.active == True, Product.name.ilike(f"%{wanted}%"))
                .first()
            )
        if not product:
            return "⚠️ سفارش ثبت نشد: محصول پیدا نشد. اسم محصول رو دقیق‌تر بگید."
        if product.stock < qty:
            return f"⚠️ سفارش ثبت نشد: موجودی کافی نیست (موجودی: {product.stock})."

        since = datetime.utcnow() - timedelta(minutes=2)
        dup = (
            db.query(Order)
            .filter(
                Order.chat_id == chat_id,
                Order.product_name == product.name,
                Order.quantity == qty,
                Order.created_at >= since,
            )
            .first()
        )
        if dup:
            return f"✅ این سفارش قبلاً ثبت شده. شماره سفارش: {dup.id}"

        name = str(data.get("name") or "").strip()
        phone = str(data.get("phone") or "").strip()
        address = str(data.get("address") or "").strip()
        total = product.price * qty
        pname = product.name

        order = Order(
            chat_id=chat_id,
            customer_name=name,
            phone=phone,
            address=address,
            product_name=pname,
            quantity=qty,
            total_price=total,
        )
        product.stock -= qty
        db.add(order)

        customer = db.query(Customer).filter(Customer.chat_id == chat_id).first()
        if customer:
            if name:
                customer.name = name
            if phone:
                customer.phone = phone
            if address:
                customer.address = address

        db.commit()
        order_id = order.id
    finally:
        db.close()

    await notify_admin(
        f"🛒 سفارش جدید #{order_id}\n"
        f"نام: {name}\nتلفن: {phone}\nآدرس: {address}\n"
        f"محصول: {pname} × {qty}\nمبلغ کل: {total} تومان\n"
        f"شناسه مشتری: {chat_id}"
    )
    return f"✅ سفارش شما ثبت شد.\nشماره سفارش: {order_id}\nمبلغ کل: {total} تومان"


def handle_admin(text):
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/help":
        return HELP

    db = SessionLocal()
    try:
        if cmd == "/products":
            items = db.query(Product).filter(Product.active == True).all()
            if not items:
                return "هنوز محصولی ثبت نشده."
            return "\n".join(
                f"{p.id}) {p.name} | {p.price} تومان | موجودی: {p.stock}" for p in items
            )

        if cmd == "/addproduct":
            f = [x.strip() for x in arg.split("|")]
            if len(f) < 3:
                return "فرمت: /addproduct نام | قیمت | موجودی | توضیحات"
            p = Product(
                name=f[0],
                price=to_int(f[1]),
                stock=to_int(f[2]),
                description=f[3] if len(f) > 3 else "",
            )
            db.add(p)
            db.commit()
            return f"✅ محصول اضافه شد (شماره {p.id})"

        if cmd in ("/setprice", "/setstock"):
            f = [x.strip() for x in arg.split("|")]
            if len(f) != 2:
                return f"فرمت: {cmd} شماره | مقدار"
            p = db.query(Product).filter(Product.id == to_int(f[0])).first()
            if not p:
                return "محصولی با این شماره نیست."
            if cmd == "/setprice":
                p.price = to_int(f[1])
            else:
                p.stock = to_int(f[1])
            db.commit()
            return "✅ انجام شد."

        if cmd == "/delproduct":
            p = db.query(Product).filter(Product.id == to_int(arg)).first()
            if not p:
                return "محصولی با این شماره نیست."
            p.active = False
            db.commit()
            return "✅ محصول حذف شد."

        if cmd == "/orders":
            items = db.query(Order).order_by(Order.id.desc()).limit(10).all()
            if not items:
                return "هنوز سفارشی ثبت نشده."
            return "\n\n".join(
                f"#{o.id} | {o.customer_name} | {o.phone}\n"
                f"{o.product_name} × {o.quantity} | {o.total_price} تومان"
                for o in items
            )

        if cmd == "/customers":
            q = db.query(Customer)
            if ADMIN_CHAT_ID:
                q = q.filter(Customer.chat_id != str(ADMIN_CHAT_ID))
            items = q.order_by(Customer.id.desc()).limit(15).all()
            if not items:
                return "هنوز مشتری‌ای ثبت نشده."
            lines = []
            for c in items:
                mark = " 🔕" if c.bot_paused else ""
                lines.append(f"{c.chat_id} | {c.name or '-'} | {c.phone or '-'}{mark}")
            return "آخرین مشتری‌ها (🔕 یعنی ربات متوقف شده):\n\n" + "\n".join(lines)

        if cmd in ("/pause", "/resume"):
            ok = set_paused(arg.strip(), cmd == "/pause")
            if not ok:
                return "مشتری با این شناسه پیدا نشد."
            return "✅ پاسخ خودکار متوقف شد." if cmd == "/pause" else "✅ پاسخ خودکار فعال شد."

        return HELP
    except Exception as e:
        db.rollback()
        return f"⚠️ خطا: {e}"
    finally:
        db.close()
