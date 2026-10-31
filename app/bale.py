import httpx
from app.config import BALE_TOKEN

BASE_URL = f"https://tapi.bale.ai/bot{BALE_TOKEN}"


async def send_message(chat_id, text):
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.post(
                f"{BASE_URL}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            return r.json()
        except Exception as e:
            print("send_message error:", e)
            return None


async def set_webhook(url):
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{BASE_URL}/setWebhook", json={"url": url})
        return r.json()


async def delete_webhook():
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{BASE_URL}/deleteWebhook")
        return r.json()


async def get_updates(offset=None):
    params = {"timeout": 25}
    if offset:
        params["offset"] = offset
    async with httpx.AsyncClient(timeout=40) as client:
        try:
            r = await client.get(f"{BASE_URL}/getUpdates", params=params)
            return r.json().get("result", [])
        except Exception as e:
            print("get_updates error:", e)
            return []
