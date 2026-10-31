import asyncio
import hashlib
import os
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
    "دستورات مدیر:\n"
    "/products ← لیست محصولات\n"
    "/addproduct نام | قیمت | موجودی | توضیحات\n"
    "/setprice شماره | قیمت\n"
    "/setstock شماره | موجودی\n"
    "/delproduct شماره\n"
    "/orders ← آخرین سفارش‌ها\n"
    "/customers ← آخرین مشتری‌ها\n"
    "/reply شناسه_مشتری پیام ← جواب دادن به مشتری\n"
    "/pause شناسه_مشتری ← توقف پاسخ خودکار\n"
    "/resume شناسه_مشتری ← فعال کردن دوباره"
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

ADMIN_BUTTONS = {
    "📦 محصولات": "/products",
    "🧾 سفارش‌ها": "/orders",
    "👥 مشتریان": "/customers",
    "📖 راهنما": "/help",
}

ADMIN_TEMPLATES = {
    "💬 پاسخ به مشتری": (
        "برای جواب دادن به مشتری، این قالب رو کپی کن، پر کن و بفرست:\n\n"
        "/reply شناسه_مشتری پیام\n\n"
        "شناسه‌ی مشتری رو از دکمه‌ی «👥 مشتریان» یا از پیام‌های رسیده بردار."
    ),
    "➕ افزودن محصول": (
        "برای افزودن محصول، این قالب رو کپی کن، پر کن و بفرست:\n\n"
        "/addproduct نام | قیمت | موجودی | توضیحات"
    ),
    "💰 تغییر قیمت": (
        "برای تغییر قیمت، این قالب رو کپی کن، پر کن و بفرست:\n\n"
        "/setprice شماره | قیمت\n\n"
        "شماره‌ی محصول رو از دکمه‌ی «📦 محصولات» ببین."
    ),
    "📊 تغییر موجودی": (
        "برای تغییر موجودی، این قالب رو کپی کن، پر کن و بفرست:\n\n"
        "/setstock شماره | موجودی\n\n"
        "شماره‌ی محصول رو از دکمه‌ی «📦 محصولات» ببین."
    ),
    "❌ حذف محصول": (
        "برای حذف محصول، این قالب رو کپی کن، پر کن و بفرست:\n\n"
        "/delproduct شماره\n\n"
        "شماره‌ی محصول رو از دکمه‌ی «📦 محصولات» ببین."
    ),
    "🔕 توقف ربات": (
        "برای توقف پاسخ خودکار برای یک مشتری، این رو کپی کن، پر کن و بفرست:\n\n"
        "/pause شناسه_مشتری"
    ),
    "🔔 فعال‌سازی ربات": (
        "برای فعال کردن دوباره‌ی پاسخ خودکار برای یک مشتری، این رو کپی کن، پر کن و بفرست:\n\n"
        "/resume شناسه_مشتری"
    ),
}

CUSTOMER_BUTTON_TEXTS = {
    "🛒 ثبت سفارش": "می‌خوام سفارش ثبت کنم",
    "❓ سوالات متداول": "سوالات متداول فروشگاه چیه؟",
}

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
        await bale.send_message(ADMIN_CHAT_ID, text, keyboard=ADMIN_KB)


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
        await notify_admin(f"👤 مشتری {chat_id}:\n{user_text}\n\n🤖 ربات:\n{reply}")


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
        if text == "/start":
            await bale.send_message(
                chat_id,
                "سلام مدیر 👋\nبا دکمه‌های پایین صفحه ربات رو مدیریت کن.",
                keyboard=ADMIN_KB,
            )
            return

        if text in ADMIN_TEMPLATES:
            await bale.send_message(chat_id, ADMIN_TEMPLATES[text], keyboard=ADMIN_KB)
            return

        text = ADMIN_BUTTONS.get(text, text)

        if text.startswith("/reply"):
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                await bale.send_message(
                    chat_id, "فرمت: /reply شناسه_مشتری پیام", keyboard=ADMIN_KB
                )
                return
            await bale.send_message(parts[1], parts[2], keyboard=CUSTOMER_KB)
            save_message(parts[1], "assistant", parts[2])
            await bale.send_message(chat_id, "✅ پیام ارسال شد.", keyboard=ADMIN_KB)
            return

        if text.startswith("/"):
            answer = handle_admin(text)
            await bale.send_message(chat_id, answer, keyboard=ADMIN_KB)
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
        await notify_admin(
            f"💬 پیام از مشتری {chat_id}:\n{text}\n\n"
            f"جواب دادن: /reply {chat_id} پیام\n"
            f"فعال کردن ربات: /resume {chat_id}"
        )
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
            f"🙋 مشتری {chat_id} اپراتور انسانی خواست.\n\n"
            f"جواب دادن: /reply {chat_id} پیام\n"
            f"فعال کردن ربات: /resume {chat_id}"
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
            f"آخرین پیام: {text}\n\n"
            f"جواب دادن: /reply {chat_id} پیام\n"
            f"فعال کردن ربات: /resume {chat_id}"
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
            items = db.query(Customer).order_by(Customer.id.desc()).limit(15).all()
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
