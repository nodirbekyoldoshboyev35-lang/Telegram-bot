"""Taksi Telegram bot — bitta faylda (bot + baza + admin sahifa)."""
import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlparse
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ======================= SOZLAMALAR =======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "taxi.db")
MEDIA_DIR = os.getenv("MEDIA_DIR", "media")
# Bir foydalanuvchi ketma-ket reklama ko'rishi orasidagi minimal vaqt (soniya).
# 0 qo'ysangiz cheklov bo'lmaydi.
AD_COOLDOWN = int(os.getenv("AD_COOLDOWN_SECONDS", "30"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent"))
# Mini App manzili (Render buni RENDER_EXTERNAL_URL orqali o'zi beradi)
WEBAPP_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
# Reklama ko'rsatilgach tanga berilishi uchun minimal kutish (soniya)
AD_WATCH_SECONDS = int(os.getenv("AD_WATCH_SECONDS", "5"))

# Manzillar ro'yxati (o'zingizga moslab o'zgartiring)
DESTINATIONS = [
    "Toshkent", "Samarqand", "Buxoro", "Andijon", "Farg'ona", "Namangan",
    "Qarshi", "Termiz", "Urganch", "Navoiy", "Jizzax", "Guliston", "Nukus",
]

NO_AD_TEXT = "Bu yerda sizning reklamangiz bo'lishi mumkin edi"
SEARCH_WINDOW_MIN = 30

# ======================= BAZA =======================
class db:
    """SQLite funksiyalari."""


    @staticmethod
    async def init():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    coins INTEGER NOT NULL DEFAULT 0,
                    last_ad_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS taxis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    driver_id INTEGER NOT NULL,
                    phone TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    departure TEXT NOT NULL,   -- 'YYYY-MM-DD HH:MM' (mahalliy vaqt)
                    car TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_taxi_dest_dep ON taxis(destination, departure);
                CREATE TABLE IF NOT EXISTS ads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    link TEXT NOT NULL DEFAULT '',
                    image_path TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL
                );
                """
            )
            await db.commit()


    # ---------- tangalar ----------
    @staticmethod
    async def ad_wait_seconds(uid: int, cooldown: int) -> int:
        if cooldown <= 0:
            return 0
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
            await db.commit()
            cur = await db.execute("SELECT last_ad_at FROM users WHERE user_id=?", (uid,))
            last = (await cur.fetchone())[0]
        return max(0, int(last + cooldown - time.time()) + 1)

    @staticmethod
    async def get_coins(uid: int) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
            await db.commit()
            cur = await db.execute("SELECT coins FROM users WHERE user_id=?", (uid,))
            return (await cur.fetchone())[0]


    @staticmethod
    async def claim_ad_coin(uid: int, cooldown: int):
        """(True, yangi_balans) yoki (False, kutish_soniyasi)."""
        now = time.time()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
            cur = await db.execute("SELECT last_ad_at FROM users WHERE user_id=?", (uid,))
            last = (await cur.fetchone())[0]
            wait = int(last + cooldown - now) + 1
            if cooldown > 0 and wait > 0:
                return False, wait
            await db.execute(
                "UPDATE users SET coins=coins+1, last_ad_at=? WHERE user_id=?", (now, uid)
            )
            await db.commit()
            cur = await db.execute("SELECT coins FROM users WHERE user_id=?", (uid,))
            return True, (await cur.fetchone())[0]


    # ---------- taksilar ----------
    @staticmethod
    async def add_taxi_paid(uid, phone, dest, departure, car, price) -> bool:
        """1 tanga yechib, taksini qo'shadi. Tanga yetmasa False."""
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE users SET coins=coins-1 WHERE user_id=? AND coins>=1", (uid,)
            )
            if cur.rowcount == 0:
                return False
            await db.execute(
                "INSERT INTO taxis(driver_id,phone,destination,departure,car,price,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (uid, phone, dest, departure, car, price, time.time()),
            )
            await db.commit()
            return True


    @staticmethod
    async def search_taxis(dest: str, dep_from: str, dep_to: str, now: str, limit=20):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT phone, departure, car, price FROM taxis"
                " WHERE destination=? AND departure>=? AND departure<=? AND departure>=?"
                " ORDER BY departure LIMIT ?",
                (dest, dep_from, dep_to, now, limit),
            )
            return await cur.fetchall()


    @staticmethod
    async def my_taxis(uid: int, now: str):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id, destination, departure, car, price, phone FROM taxis"
                " WHERE driver_id=? AND departure>=? ORDER BY departure LIMIT 10",
                (uid, now),
            )
            return await cur.fetchall()


    @staticmethod
    async def delete_taxi(taxi_id: int, uid: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "DELETE FROM taxis WHERE id=? AND driver_id=?", (taxi_id, uid)
            )
            await db.commit()
            return cur.rowcount > 0


    @staticmethod
    async def cleanup_expired(now: str) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("DELETE FROM taxis WHERE departure<?", (now,))
            await db.commit()
            return cur.rowcount


    # ---------- reklamalar ----------
    @staticmethod
    async def add_ad(text: str, link: str, image_path: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO ads(text,link,image_path,created_at) VALUES(?,?,?,?)",
                (text, link, image_path, time.time()),
            )
            await db.commit()


    @staticmethod
    async def list_ads():
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id,text,link,image_path,active FROM ads ORDER BY id DESC"
            )
            return await cur.fetchall()


    @staticmethod
    async def random_ad():
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT text,link,image_path FROM ads WHERE active=1 ORDER BY RANDOM() LIMIT 1"
            )
            return await cur.fetchone()


    @staticmethod
    async def toggle_ad(ad_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE ads SET active=1-active WHERE id=?", (ad_id,))
            await db.commit()


    @staticmethod
    async def delete_ad(ad_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT image_path FROM ads WHERE id=?", (ad_id,))
            row = await cur.fetchone()
            await db.execute("DELETE FROM ads WHERE id=?", (ad_id,))
            await db.commit()
        if row and row[0] and os.path.exists(row[0]):
            os.remove(row[0])


    @staticmethod
    async def stats():
        async with aiosqlite.connect(DB_PATH) as db:
            u = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
            t = (await (await db.execute("SELECT COUNT(*) FROM taxis")).fetchone())[0]
        return u, t

# ======================= ADMIN SAHIFA =======================
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE = 5 * 1024 * 1024

PAGE = """<!doctype html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin - Reklamalar</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:760px;margin:20px auto;padding:0 14px;background:#f5f6f8;color:#222}}
.card{{background:#fff;border-radius:10px;padding:14px;margin:12px 0;box-shadow:0 1px 3px #0002}}
input,textarea{{width:100%;padding:9px;margin:5px 0 10px;border:1px solid #ccc;border-radius:6px;box-sizing:border-box;font:inherit}}
button{{padding:8px 14px;border:0;border-radius:6px;background:#2a7de1;color:#fff;cursor:pointer;font:inherit}}
button.red{{background:#d33}} button.gray{{background:#777}}
.row{{display:flex;gap:8px;align-items:center}} img{{max-width:140px;border-radius:6px}}
.off{{opacity:.5}}
</style></head><body>
<h2>📢 Reklamalar</h2>
<p>Foydalanuvchilar: <b>{users}</b> · Faol taksilar: <b>{taxis}</b></p>
<div class="card"><h3>Yangi reklama</h3>
<form method="post" action="/admin/add" enctype="multipart/form-data">
<label>Matn (maks. 900 belgi)</label><textarea name="text" rows="4" maxlength="900" required></textarea>
<label>Havola (ixtiyoriy, https://...)</label><input name="link" type="url">
<label>Rasm (ixtiyoriy, jpg/png/webp, maks. 5 MB)</label><input name="image" type="file" accept="image/*">
<button type="submit">Qo'shish</button></form></div>
{ads}
</body></html>"""


def build_admin_app() -> web.Application:
    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path in ("/", "/health") or request.path.startswith(("/media/", "/api/")):
            return await handler(request)
        header = request.headers.get("Authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                ok = hmac.compare_digest(pwd.encode(), ADMIN_PASSWORD.encode())
            except Exception:
                ok = False
        if not ok:
            return web.Response(
                status=401,
                text="Parol kerak",
                headers={"WWW-Authenticate": 'Basic realm="Admin"'},
            )
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                return web.Response(status=403, text="Forbidden")
        return await handler(request)

    async def home(request: web.Request):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        if os.path.isfile(path):
            resp = web.FileResponse(path)
            resp.headers["Cache-Control"] = "no-cache"
            return resp
        return web.Response(text="Bot ishlayapti", content_type="text/plain")

    async def health(request: web.Request):
        return web.Response(text="ok")

    async def index(request: web.Request):
        ads = await db.list_ads()
        users, taxis = await db.stats()
        parts = []
        for ad_id, text, link, image, active in ads:
            img = f'<img src="/media/{html.escape(os.path.basename(image))}">' if image else ""
            lnk = f'<div><a href="{html.escape(link)}" target="_blank" rel="noopener">{html.escape(link)}</a></div>' if link else ""
            parts.append(
                f'<div class="card {"" if active else "off"}">{img}'
                f'<p>{html.escape(text)}</p>{lnk}<div class="row">'
                f'<form method="post" action="/admin/toggle/{ad_id}"><button class="gray">{"O\'chirib qo\'yish" if active else "Yoqish"}</button></form>'
                f'<form method="post" action="/admin/delete/{ad_id}" onsubmit="return confirm(\'O\\\'chirilsinmi?\')"><button class="red">O\'chirish</button></form>'
                f"</div></div>"
            )
        body = "".join(parts) or '<div class="card">Hozircha reklama yo\'q.</div>'
        return web.Response(
            text=PAGE.format(users=users, taxis=taxis, ads=body),
            content_type="text/html",
        )

    async def add(request: web.Request):
        fields, image_path = {}, ""
        reader = await request.multipart()
        async for part in reader:
            if part.name in ("text", "link"):
                fields[part.name] = (await part.text()).strip()
            elif part.name == "image" and part.filename:
                ext = os.path.splitext(part.filename)[1].lower()
                if ext not in ALLOWED_EXT:
                    raise web.HTTPBadRequest(text="Faqat jpg/png/webp rasm mumkin")
                os.makedirs(MEDIA_DIR, exist_ok=True)
                path = os.path.join(MEDIA_DIR, uuid.uuid4().hex + ext)
                size = 0
                with open(path, "wb") as f:
                    while chunk := await part.read_chunk():
                        size += len(chunk)
                        if size > MAX_IMAGE:
                            f.close()
                            os.remove(path)
                            raise web.HTTPBadRequest(text="Rasm 5 MB dan katta")
                        f.write(chunk)
                image_path = path
        text = fields.get("text", "")
        link = fields.get("link", "")
        if not text or len(text) > 900:
            if image_path and os.path.exists(image_path):
                os.remove(image_path)
            raise web.HTTPBadRequest(text="Matn bo'sh yoki juda uzun")
        if not link.startswith(("http://", "https://")):
            link = ""
        await db.add_ad(text, link, image_path)
        raise web.HTTPSeeOther("/admin")

    async def toggle(request: web.Request):
        await db.toggle_ad(int(request.match_info["id"]))
        raise web.HTTPSeeOther("/admin")

    async def delete(request: web.Request):
        await db.delete_ad(int(request.match_info["id"]))
        raise web.HTTPSeeOther("/admin")

    async def media(request: web.Request):
        name = os.path.basename(request.match_info["name"])
        path = os.path.join(MEDIA_DIR, name)
        if not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    app = web.Application(middlewares=[auth], client_max_size=MAX_IMAGE + 1024 * 1024)
    app.add_routes(
        [
            web.get("/", home),
            web.get("/health", health),
            web.get("/admin", index),
            web.post("/admin/add", add),
            web.post("/admin/toggle/{id:\\d+}", toggle),
            web.post("/admin/delete/{id:\\d+}", delete),
            web.get("/media/{name}", media),
            web.get("/api/me", api_me),
            web.post("/api/taxis", api_add_taxi),
            web.delete("/api/taxis/{id:\\d+}", api_del_taxi),
            web.get("/api/my", api_my),
            web.get("/api/search", api_search),
            web.post("/api/ad/start", api_ad_start),
            web.post("/api/ad/claim", api_ad_claim),
        ]
    )
    return app

# ======================= BOT =======================
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN topilmadi (.env yoki Secrets).")
if not ADMIN_PASSWORD:
    raise SystemExit("ADMIN_PASSWORD topilmadi (.env yoki Secrets).")

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

BTN_ADD = "Taksi qo'shish +"
BTN_SEARCH = "🔍 Taksi qidirish"
BTN_COINS = "💰 Tanga olish"
BTN_MY = "📋 Mening taksilarim"
BTN_CANCEL = "❌ Bekor qilish"

TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")
PHONE_RE = re.compile(r"^\+?\d{9,13}$")
FMT = "%Y-%m-%d %H:%M"


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_SEARCH)],
            [KeyboardButton(text=BTN_COINS), KeyboardButton(text=BTN_MY)],
        ],
        resize_keyboard=True,
    )


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True
    )


def dest_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, name in enumerate(DESTINATIONS):
        b.button(text=name, callback_data=f"dst:{i}")
    b.adjust(2)
    return b.as_markup()


def watch_ad_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📺 Reklama ko'rish (+1 tanga)", callback_data="watch_ad")]
        ]
    )


def money(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " so'm"


def short(dt_str: str) -> str:
    return datetime.strptime(dt_str, FMT).strftime("%d.%m %H:%M")


# ---------------- boshlash / bekor qilish ----------------
def webapp_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚖 Ilovani ochish", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await db.get_coins(message.from_user.id)
    if WEBAPP_URL.startswith("https://"):
        await message.answer(
            "👋 <b>Salom!</b> Taksi botga xush kelibsiz!\n\nIlovani ochish uchun tugmani bosing 👇",
            reply_markup=webapp_kb(),
        )
        await message.answer("Yoki chatdagi tugmalardan foydalaning:", reply_markup=main_kb())
        return
    await message.answer(
        "Assalomu alaykum! 🚖\n\n"
        "<b>Haydovchi</b> bo'lsangiz — «Taksi qo'shish +» tugmasini bosing.\n"
        "<b>Yo'lovchi</b> bo'lsangiz — «Taksi qidirish» tugmasini bosing.",
        reply_markup=main_kb(),
    )


@dp.message(F.text == BTN_CANCEL)
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=main_kb())


# ---------------- tanga / reklama ----------------
async def show_ad(message: Message):
    ad = await db.random_ad()
    if not ad:
        await message.answer(NO_AD_TEXT)
        return
    text, link, image = ad
    markup = None
    if link:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔗 Batafsil", url=link)]]
        )
    text = html.escape(text)
    if image and os.path.isfile(image):
        await message.answer_photo(FSInputFile(image), caption=text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@dp.message(F.text == BTN_COINS)
async def coins_menu(message: Message, state: FSMContext):
    await state.clear()
    coins = await db.get_coins(message.from_user.id)
    await message.answer(
        f"Balansingiz: <b>{coins}</b> tanga.\n"
        "Reklama ko'rsangiz 1 tanga olasiz. 1 ta taksi qo'shish = 1 tanga.",
        reply_markup=watch_ad_kb(),
    )


@dp.callback_query(F.data == "watch_ad")
async def watch_ad(call: CallbackQuery):
    ok, val = await db.claim_ad_coin(call.from_user.id, AD_COOLDOWN)
    if not ok:
        await call.answer(f"Keyingi reklamani {val} soniyadan so'ng ko'ring", show_alert=True)
        return
    await show_ad(call.message)
    await call.message.answer(
        f"✅ +1 tanga! Balansingiz: <b>{val}</b>", reply_markup=watch_ad_kb()
    )
    await call.answer()


# ---------------- taksi qo'shish (haydovchi) ----------------
class AddTaxi(StatesGroup):
    phone = State()
    dest = State()
    time = State()
    car = State()
    price = State()


@dp.message(F.text == BTN_ADD)
async def add_start(message: Message, state: FSMContext):
    await state.clear()
    coins = await db.get_coins(message.from_user.id)
    if coins < 1:
        await message.answer(
            "Taksi qo'shish uchun 1 tanga kerak, sizda 0 tanga.\n"
            "Reklama ko'rib tanga oling 👇",
            reply_markup=watch_ad_kb(),
        )
        return
    await state.set_state(AddTaxi.phone)
    await message.answer(
        "📞 Telefon raqamingizni kiriting (masalan: +998901234567):",
        reply_markup=cancel_kb(),
    )


@dp.message(AddTaxi.phone, F.text)
async def add_phone(message: Message, state: FSMContext):
    phone = re.sub(r"[\s\-()]", "", message.text)
    if not PHONE_RE.match(phone):
        await message.answer("Raqam noto'g'ri. Masalan: +998901234567")
        return
    await state.update_data(phone=phone)
    await state.set_state(AddTaxi.dest)
    await message.answer("📍 Qayerga borasiz?", reply_markup=dest_kb())


@dp.callback_query(AddTaxi.dest, F.data.startswith("dst:"))
async def add_dest(call: CallbackQuery, state: FSMContext):
    dest = DESTINATIONS[int(call.data.split(":")[1])]
    await state.update_data(dest=dest)
    await state.set_state(AddTaxi.time)
    await call.message.answer(
        f"Manzil: <b>{dest}</b>\n🕒 Ketish vaqti (soat:daqiqa, masalan 14:30):"
    )
    await call.answer()


@dp.message(AddTaxi.time, F.text)
async def add_time(message: Message, state: FSMContext):
    m = TIME_RE.match(message.text.strip())
    if not m:
        await message.answer("Vaqt noto'g'ri. Masalan: 14:30")
        return
    now = now_local()
    dep = now.replace(hour=int(m[1]), minute=int(m[2]))
    if dep <= now:
        dep += timedelta(days=1)  # vaqt o'tib ketgan bo'lsa — ertaga
    await state.update_data(departure=dep.strftime(FMT))
    await state.set_state(AddTaxi.car)
    await message.answer(
        f"Ketish: <b>{dep.strftime('%d.%m %H:%M')}</b>\n🚗 Mashina turi (masalan: Nexia 3, Cobalt):"
    )


@dp.message(AddTaxi.car, F.text)
async def add_car(message: Message, state: FSMContext):
    car = message.text.strip()
    if not 2 <= len(car) <= 40:
        await message.answer("Mashina turini 2–40 belgi bilan yozing.")
        return
    await state.update_data(car=car)
    await state.set_state(AddTaxi.price)
    await message.answer("💵 Narxi (so'mda, masalan: 150000):")


@dp.message(AddTaxi.price, F.text)
async def add_price(message: Message, state: FSMContext):
    raw = re.sub(r"[\s,.]", "", message.text)
    if not raw.isdigit() or not 0 < int(raw) < 100_000_000:
        await message.answer("Narxni raqam bilan yozing. Masalan: 150000")
        return
    price = int(raw)
    d = await state.get_data()
    uid = message.from_user.id
    ok = await db.add_taxi_paid(uid, d["phone"], d["dest"], d["departure"], d["car"], price)
    await state.clear()
    if not ok:
        await message.answer(
            "Tanga yetarli emas. Reklama ko'rib tanga oling 👇", reply_markup=main_kb()
        )
        await message.answer("⬇️", reply_markup=watch_ad_kb())
        return
    coins = await db.get_coins(uid)
    await message.answer(
        "✅ Taksi qo'shildi!\n\n"
        f"📍 {html.escape(d['dest'])}\n🕒 {short(d['departure'])}\n"
        f"🚗 {html.escape(d['car'])}\n💵 {money(price)}\n📞 {html.escape(d['phone'])}\n\n"
        f"Qolgan tanga: <b>{coins}</b>",
        reply_markup=main_kb(),
    )


# ---------------- mening taksilarim ----------------
@dp.message(F.text == BTN_MY)
async def my_taxis(message: Message, state: FSMContext):
    await state.clear()
    rows = await db.my_taxis(message.from_user.id, now_local().strftime(FMT))
    if not rows:
        await message.answer("Sizda faol taksi yo'q.")
        return
    for tid, dest, dep, car, price, phone in rows:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"del:{tid}")]]
        )
        await message.answer(
            f"📍 {html.escape(dest)} · 🕒 {short(dep)}\n"
            f"🚗 {html.escape(car)} · 💵 {money(price)}\n📞 {html.escape(phone)}",
            reply_markup=kb,
        )


@dp.callback_query(F.data.startswith("del:"))
async def del_taxi(call: CallbackQuery):
    ok = await db.delete_taxi(int(call.data.split(":")[1]), call.from_user.id)
    await call.message.edit_text("🗑 O'chirildi." if ok else "Topilmadi.")
    await call.answer()


# ---------------- taksi qidirish (yo'lovchi) ----------------
class Search(StatesGroup):
    dest = State()
    time = State()


@dp.message(F.text == BTN_SEARCH)
async def search_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Search.dest)
    await message.answer("📍 Qayerga borasiz?", reply_markup=dest_kb())


@dp.callback_query(Search.dest, F.data.startswith("dst:"))
async def search_dest(call: CallbackQuery, state: FSMContext):
    dest = DESTINATIONS[int(call.data.split(":")[1])]
    await state.update_data(dest=dest)
    await state.set_state(Search.time)
    await call.message.answer(
        f"Manzil: <b>{dest}</b>\n🕒 Soat nechada ketmoqchisiz? (masalan 14:30)",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@dp.message(Search.time, F.text)
async def search_time(message: Message, state: FSMContext):
    m = TIME_RE.match(message.text.strip())
    if not m:
        await message.answer("Vaqt noto'g'ri. Masalan: 14:30")
        return
    d = await state.get_data()
    now = now_local()
    want = now.replace(hour=int(m[1]), minute=int(m[2]))
    if want < now - timedelta(minutes=SEARCH_WINDOW_MIN):
        want += timedelta(days=1)
    w = timedelta(minutes=SEARCH_WINDOW_MIN)
    rows = await db.search_taxis(
        d["dest"], (want - w).strftime(FMT), (want + w).strftime(FMT), now.strftime(FMT)
    )
    await state.clear()
    if not rows:
        await message.answer(
            f"😕 {html.escape(d['dest'])} yo'nalishida {want.strftime('%d.%m %H:%M')} ±{SEARCH_WINDOW_MIN} daqiqa "
            "ichida taksi topilmadi.",
            reply_markup=main_kb(),
        )
        return
    lines = [
        f"🚖 <b>{html.escape(d['dest'])}</b> · {want.strftime('%d.%m %H:%M')} ±{SEARCH_WINDOW_MIN} daq.\n"
    ]
    for phone, dep, car, price in rows:
        lines.append(
            f"🕒 <b>{short(dep)}</b> · 🚗 {html.escape(car)}\n"
            f"💵 {money(price)} · 📞 {html.escape(phone)}\n"
        )
    await message.answer("\n".join(lines), reply_markup=main_kb())


# ======================= MINI APP API =======================
_pending_ads: dict = {}


def verify_init_data(init_data: str, max_age: int = 86400):
    """Telegram Mini App initData imzosini tekshiradi. To'g'ri bo'lsa user dict qaytaradi."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", None)
        if not received:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def jerr(msg: str, status: int = 400, **extra):
    return web.json_response({"error": msg, **extra}, status=status)


def api_user(request: web.Request) -> dict:
    user = verify_init_data(request.headers.get("X-Init-Data", ""))
    if not user:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "Ilovani Telegram ichidan oching"}),
            content_type="application/json",
        )
    return user


def parse_hm(value):
    m = TIME_RE.match(str(value or "").strip())
    return (int(m[1]), int(m[2])) if m else None


async def api_me(request: web.Request):
    user = api_user(request)
    return web.json_response(
        {
            "name": user.get("first_name", ""),
            "coins": await db.get_coins(user["id"]),
            "destinations": DESTINATIONS,
            "window": SEARCH_WINDOW_MIN,
        }
    )


async def api_add_taxi(request: web.Request):
    uid = api_user(request)["id"]
    try:
        d = await request.json()
    except Exception:
        return jerr("Noto'g'ri so'rov")
    phone = re.sub(r"[\s\-()]", "", str(d.get("phone", "")))
    if not PHONE_RE.match(phone):
        return jerr("Telefon raqami noto'g'ri")
    dest = d.get("dest")
    if dest not in DESTINATIONS:
        return jerr("Manzilni tanlang")
    hm = parse_hm(d.get("time"))
    if not hm:
        return jerr("Ketish vaqti noto'g'ri")
    car = str(d.get("car", "")).strip()
    if not 2 <= len(car) <= 40:
        return jerr("Mashina turini yozing (2–40 belgi)")
    raw = re.sub(r"[\s,.]", "", str(d.get("price", "")))
    if not raw.isdigit() or not 0 < int(raw) < 100_000_000:
        return jerr("Narx noto'g'ri")
    now = now_local()
    dep = now.replace(hour=hm[0], minute=hm[1])
    if dep <= now:
        dep += timedelta(days=1)
    ok = await db.add_taxi_paid(uid, phone, dest, dep.strftime(FMT), car, int(raw))
    if not ok:
        return jerr("Tanga yetarli emas. Reklama ko'rib tanga oling", 402)
    return web.json_response(
        {"coins": await db.get_coins(uid), "departure": dep.strftime("%d.%m %H:%M")}
    )


async def api_my(request: web.Request):
    uid = api_user(request)["id"]
    rows = await db.my_taxis(uid, now_local().strftime(FMT))
    items = [
        {"id": tid, "dest": dest, "dep": short(dep), "car": car, "price": price, "phone": phone}
        for tid, dest, dep, car, price, phone in rows
    ]
    return web.json_response({"items": items})


async def api_del_taxi(request: web.Request):
    uid = api_user(request)["id"]
    ok = await db.delete_taxi(int(request.match_info["id"]), uid)
    return web.json_response({"ok": ok})


async def api_search(request: web.Request):
    api_user(request)
    dest = request.query.get("dest", "")
    hm = parse_hm(request.query.get("time"))
    if dest not in DESTINATIONS:
        return jerr("Manzilni tanlang")
    if not hm:
        return jerr("Soat noto'g'ri")
    now = now_local()
    want = now.replace(hour=hm[0], minute=hm[1])
    if want < now - timedelta(minutes=SEARCH_WINDOW_MIN):
        want += timedelta(days=1)
    w = timedelta(minutes=SEARCH_WINDOW_MIN)
    rows = await db.search_taxis(
        dest, (want - w).strftime(FMT), (want + w).strftime(FMT), now.strftime(FMT)
    )
    items = []
    for phone, dep, car, price in rows:
        d = datetime.strptime(dep, FMT)
        items.append(
            {
                "phone": phone,
                "car": car,
                "price": price,
                "time": d.strftime("%H:%M"),
                "date": d.strftime("%d.%m"),
                "today": d.date() == now.date(),
                "diff": int((d - want).total_seconds() // 60),
            }
        )
    return web.json_response(
        {"items": items, "dest": dest, "want": want.strftime("%d.%m %H:%M"), "window": SEARCH_WINDOW_MIN}
    )


async def api_ad_start(request: web.Request):
    uid = api_user(request)["id"]
    wait = await db.ad_wait_seconds(uid, AD_COOLDOWN)
    if wait > 0:
        return jerr(f"Keyingi reklamani {wait} soniyadan so'ng ko'ring", 429, wait=wait)
    ad = await db.random_ad()
    payload = None
    if ad:
        text, link, image = ad
        payload = {
            "text": text,
            "link": link,
            "image": f"/media/{os.path.basename(image)}" if image and os.path.isfile(image) else "",
        }
    if len(_pending_ads) > 10000:
        _pending_ads.clear()
    _pending_ads[uid] = time.time()
    return web.json_response({"ad": payload, "seconds": AD_WATCH_SECONDS})


async def api_ad_claim(request: web.Request):
    uid = api_user(request)["id"]
    started = _pending_ads.get(uid)
    if not started or time.time() - started < AD_WATCH_SECONDS - 0.5:
        return jerr("Reklamani oxirigacha ko'ring", 400)
    _pending_ads.pop(uid, None)
    ok, val = await db.claim_ad_coin(uid, AD_COOLDOWN)
    if not ok:
        return jerr(f"Keyingi reklamani {val} soniyadan so'ng ko'ring", 429, wait=val)
    return web.json_response({"coins": val})


# ---------------- fon vazifalari va ishga tushirish ----------------
async def cleanup_loop():
    while True:
        try:
            n = await db.cleanup_expired(now_local().strftime(FMT))
            if n:
                logging.info("Vaqti o'tgan %s ta taksi o'chirildi", n)
        except Exception:
            logging.exception("cleanup xatosi")
        await asyncio.sleep(60)


async def main():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    await db.init()

    runner = web.AppRunner(build_admin_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Admin sahifa: port %s", PORT)

    asyncio.create_task(cleanup_loop())
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    if WEBAPP_URL.startswith("https://"):
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="🚖 Taksi", web_app=WebAppInfo(url=WEBAPP_URL))
            )
        except Exception:
            logging.exception("Menu tugmasini o'rnatib bo'lmadi")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
