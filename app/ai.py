import os
import re
import json
import httpx
from app.config import AI_API_KEY
from app.database import SessionLocal, Product, Message

AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "فروشگاه ما")

FALLBACK = "متأسفم، الان مشکلی پیش اومده. لطفاً چند لحظه دیگه دوباره پیام بدید."


def build_system_prompt():
    db = SessionLocal()
    try:
        products = db.query(Product).filter(Product.active == True).all()
        lines = [
            f"- {p.name} | قیمت: {p.price} تومان | موجودی: {p.stock} | توضیحات: {p.description}"
            for p in products
        ]
    finally:
        db.close()

    catalog = "\n".join(lines) if lines else "فعلاً هیچ محصولی ثبت نشده است."

    return f"""تو دستیار فروش و پشتیبانی «{BUSINESS_NAME}» در پیام‌رسان بله هستی.
به فارسی، مودبانه، کوتاه و طبیعی جواب بده.

قوانین:
- فقط براساس لیست محصولات زیر جواب بده. قیمت، موجودی یا مشخصات ساختگی نگو.
- اگر جواب سؤالی رو نمی‌دونی، بگو که موضوع به اپراتور انسانی ارجاع داده می‌شه.
- برای ثبت سفارش این اطلاعات رو یکی‌یکی از مشتری بگیر: نام، شماره تماس، آدرس (در صورت نیاز)، محصول و تعداد.
- قبل از ثبت نهایی، خلاصه‌ی سفارش و قیمت کل رو بنویس و تأیید صریح مشتری رو بگیر.
- فقط بعد از تأیید صریح مشتری، در آخرین خط پیامت دقیقاً این فرمت رو بنویس:
ORDER_JSON: {{"name": "...", "phone": "...", "address": "...", "product": "...", "quantity": 1}}
- اگر مشتری اپراتور انسانی خواست، در آخرین خط پیامت فقط بنویس: HANDOFF
- به موجودی محصول دقت کن و بیشتر از موجودی سفارش نپذیر.

لیست محصولات:
{catalog}
"""


def get_history(chat_id, limit=12):
    db = SessionLocal()
    try:
        rows = (
            db.query(Message)
            .filter(Message.chat_id == str(chat_id))
            .order_by(Message.id.desc())
            .limit(limit)
            .all()
        )
    finally:
        db.close()
    rows.reverse()
    return [{"role": m.role, "content": m.text} for m in rows]


async def ask_ai(chat_id):
    """پیام آخر مشتری باید قبل از صدا زدن این تابع در دیتابیس ذخیره شده باشد."""
    if not AI_API_KEY:
        return {"text": FALLBACK, "order": None, "handoff": False}

    messages = [{"role": "system", "content": build_system_prompt()}]
    messages += get_history(chat_id)

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{AI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {AI_API_KEY}"},
                json={"model": AI_MODEL, "messages": messages, "temperature": 0.3},
            )
            data = r.json()
            text = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("ask_ai error:", e)
        return {"text": FALLBACK, "order": None, "handoff": False}

    order = None
    match = re.search(r"ORDER_JSON:\s*(\{.*\})", text, re.DOTALL)
    if match:
        try:
            order = json.loads(match.group(1))
        except Exception:
            order = None
        text = text[: match.start()].strip()

    handoff = False
    if "HANDOFF" in text:
        handoff = True
        text = text.replace("HANDOFF", "").strip()

    return {"text": text, "order": order, "handoff": handoff}
