"""
Telegram bot (aiogram 3.x)
- Kirish bosqichi (hamkor havola) + ixtiyoriy kanal obunasi tekshiruvi
- Mini App (quiz, namoz vaqti, ob-havo) tugmasi
- Kunlik avtomatik xabar: foydalanuvchi shahar va vaqt tanlaydi

O'rnatish:   pip install aiogram
Ishga tushirish:
    export BOT_TOKEN="yangi_token"                (Windows: set BOT_TOKEN=...)
    export WEBAPP_URL="https://sizning-sayt.vercel.app"   (index.html joylangan manzil)
    python bot.py
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

# ================== SOZLAMALAR ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")          # tokenni kodga yozmang!
WEBAPP_URL = os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL", "")  # Mini App manzili

# Hamkor havola (kirish bosqichi). Telegram buni tekshira olmaydi.
INVITE_URL = "https://t.me/memehashbotbot/app?startapp=invite_5184762513"

# Ixtiyoriy: tekshiriladigan kanallar. Bo'sh qoldirsangiz, kanal tekshiruvi o'chiq.
# Bot kanalda ADMIN bo'lishi shart.
CHANNELS = [
    # {"chat": "@kanal_username", "link": "https://t.me/kanal_username"},
]
DB_FILE = "data.json"
# ================================================

logging.basicConfig(level=logging.INFO)

# ---------- Oddiy fayl bazasi ----------
DB = {"passed": [], "daily": {}}
if os.path.exists(DB_FILE):
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            DB.update(json.load(f))
    except Exception:
        logging.warning("data.json o'qilmadi, yangisi boshlanadi")


def save_db():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False)


# ---------- Holatlar ----------
class Form(StatesGroup):
    prayer_city = State()
    weather_city = State()
    daily_city = State()
    daily_time = State()


BTN_APP = "📱 Ilovani ochish"
BTN_PRAYER = "🕌 Namoz vaqti"
BTN_WEATHER = "🌤 Ob-havo"
BTN_DAILY = "⏰ Kunlik xabar"
BTN_DAILY_OFF = "🛑 Kunlikni o'chirish"


def menu_kb() -> ReplyKeyboardMarkup:
    rows = []
    if WEBAPP_URL:
        rows.append([KeyboardButton(text=BTN_APP, web_app=WebAppInfo(url=WEBAPP_URL))])
    rows.append([KeyboardButton(text=BTN_PRAYER), KeyboardButton(text=BTN_WEATHER)])
    rows.append([KeyboardButton(text=BTN_DAILY), KeyboardButton(text=BTN_DAILY_OFF)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ---------- Tashqi API'lar (kalitsiz, bepul) ----------
async def get_json(url: str, params: dict) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(url, params=params) as r:
            return await r.json()


async def geocode(name: str):
    data = await get_json(
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": name, "count": 1, "language": "en"},
    )
    res = data.get("results")
    if not res:
        return None
    p = res[0]
    return {
        "name": p["name"],
        "country": p.get("country", ""),
        "lat": p["latitude"],
        "lon": p["longitude"],
        "tz": p.get("timezone", "UTC"),
    }


PRAYER_NAMES = [
    ("Fajr", "🌙 Bomdod"),
    ("Sunrise", "🌅 Quyosh"),
    ("Dhuhr", "☀️ Peshin"),
    ("Asr", "🌤 Asr"),
    ("Maghrib", "🌇 Shom"),
    ("Isha", "🌌 Xufton"),
]


async def prayer_text(place: dict) -> str:
    today = datetime.now(ZoneInfo(place["tz"])).strftime("%d-%m-%Y")
    data = await get_json(
        f"https://api.aladhan.com/v1/timings/{today}",
        {"latitude": place["lat"], "longitude": place["lon"], "method": 3,
         "school": 1, "timezonestring": place["tz"]},
    )
    if data.get("code") != 200:
        raise ValueError("prayer api")
    t = data["data"]["timings"]
    lines = [f"🕌 {place['name']} — namoz vaqtlari\n📅 {today}\n"]
    lines += [f"{label}: {t[key][:5]}" for key, label in PRAYER_NAMES]
    return "\n".join(lines)


WEATHER_CODES = {
    0: "☀️ Ochiq", 1: "🌤 Asosan ochiq", 2: "⛅ Qisman bulutli", 3: "☁️ Bulutli",
    45: "🌫 Tuman", 48: "🌫 Tuman", 51: "🌦 Yengil shivalash", 53: "🌦 Shivalash",
    55: "🌧 Kuchli shivalash", 61: "🌧 Yengil yomg'ir", 63: "🌧 Yomg'ir", 65: "🌧 Kuchli yomg'ir",
    71: "🌨 Yengil qor", 73: "🌨 Qor", 75: "❄️ Kuchli qor", 80: "🌦 Jala", 81: "🌧 Jala",
    82: "⛈ Kuchli jala", 95: "⛈ Momaqaldiroq", 96: "⛈ Do'l bilan momaqaldiroq",
    99: "⛈ Do'l bilan momaqaldiroq",
}


async def weather_text(place: dict) -> str:
    data = await get_json(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": place["lat"], "longitude": place["lon"], "timezone": "auto",
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min",
            "forecast_days": 1,
        },
    )
    w, d = data["current"], data["daily"]
    return (
        f"📍 {place['name']}, {place['country']}\n\n"
        f"{WEATHER_CODES.get(w['weather_code'], '—')}\n"
        f"🌡 Hozir: {w['temperature_2m']}°C (his etilishi {w['apparent_temperature']}°C)\n"
        f"📉 Bugun: {d['temperature_2m_min'][0]}° … {d['temperature_2m_max'][0]}°C\n"
        f"💧 Namlik: {w['relative_humidity_2m']}%\n"
        f"💨 Shamol: {w['wind_speed_10m']} km/soat"
    )


# ---------- Kirish bosqichi va kanal tekshiruvi ----------
async def is_subscribed(bot: Bot, user_id: int) -> bool:
    for ch in CHANNELS:
        try:
            m = await bot.get_chat_member(ch["chat"], user_id)
            if m.status not in ("member", "administrator", "creator"):
                return False
        except Exception as e:
            logging.warning("Obuna tekshirishda xato (%s): %s", ch["chat"], e)
            return False
    return True


def sub_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📢 Kanal {i}", url=ch["link"])]
            for i, ch in enumerate(CHANNELS, 1)]
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Hamkor ilovani ochish", url=INVITE_URL)],
        [InlineKeyboardButton(text="✅ Davom etish", callback_data="pass_gate")],
    ])


GATE_TEXT = ("Botdan foydalanishdan oldin hamkor ilovamizni ochib ko'ring, "
             "so'ng «Davom etish» tugmasini bosing 👇")


class GateMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data in ("check_sub", "pass_gate"):
            return await handler(event, data)

        bot = data["bot"]
        target = event if isinstance(event, Message) else event.message

        if CHANNELS and not await is_subscribed(bot, user.id):
            await target.answer("Avval kanal(lar)ga obuna bo'ling, so'ng «Tekshirish» ni bosing 👇",
                                reply_markup=sub_keyboard())
        elif user.id not in DB["passed"]:
            await target.answer(GATE_TEXT, reply_markup=gate_keyboard())
        else:
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer()


dp = Dispatcher()
dp.message.outer_middleware(GateMiddleware())
dp.callback_query.outer_middleware(GateMiddleware())


@dp.callback_query(F.data == "check_sub")
async def check_sub(call: CallbackQuery, bot: Bot):
    if not await is_subscribed(bot, call.from_user.id):
        await call.answer("Hali hamma kanalga obuna bo'lmadingiz ❌", show_alert=True)
        return
    await call.answer()
    if call.from_user.id in DB["passed"]:
        await call.message.answer("✅ Obuna tasdiqlandi!", reply_markup=menu_kb())
    else:
        await call.message.answer(GATE_TEXT, reply_markup=gate_keyboard())


@dp.callback_query(F.data == "pass_gate")
async def pass_gate(call: CallbackQuery):
    if call.from_user.id not in DB["passed"]:
        DB["passed"].append(call.from_user.id)
        save_db()
    await call.answer()
    await call.message.answer("Xush kelibsiz! Kerakli bo'limni tanlang 👇", reply_markup=menu_kb())


# ---------- /start ----------
@dp.message(CommandStart())
async def start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Assalomu alaykum! Quiz, namoz vaqti va ob-havo ilovasini pastdagi tugmadan oching 👇",
                     reply_markup=menu_kb())


# ---------- Namoz vaqti (chatda) ----------
@dp.message(F.text == BTN_PRAYER)
async def prayer_ask(msg: Message, state: FSMContext):
    await state.set_state(Form.prayer_city)
    await msg.answer("Shahar nomini yozing (masalan: Tashkent, Samarkand, Istanbul).")


@dp.message(Form.prayer_city)
async def prayer_result(msg: Message, state: FSMContext):
    try:
        place = await geocode(msg.text.strip())
        if not place:
            await msg.answer("Shahar topilmadi. Lotin harflarida yozib ko'ring (masalan: Tashkent).")
            return
        await msg.answer(await prayer_text(place), reply_markup=menu_kb())
        await state.clear()
    except Exception:
        logging.exception("prayer")
        await msg.answer("Xizmat javob bermadi. Birozdan keyin qayta urinib ko'ring.")


# ---------- Ob-havo (chatda) ----------
@dp.message(F.text == BTN_WEATHER)
async def weather_ask(msg: Message, state: FSMContext):
    await state.set_state(Form.weather_city)
    await msg.answer("Shahar nomini yozing (masalan: Tashkent, Moscow).")


@dp.message(Form.weather_city)
async def weather_result(msg: Message, state: FSMContext):
    try:
        place = await geocode(msg.text.strip())
        if not place:
            await msg.answer("Shahar topilmadi. Lotin harflarida yozib ko'ring (masalan: Tashkent).")
            return
        await msg.answer(await weather_text(place), reply_markup=menu_kb())
        await state.clear()
    except Exception:
        logging.exception("weather")
        await msg.answer("Xizmat javob bermadi. Birozdan keyin qayta urinib ko'ring.")


# ---------- Kunlik xabar ----------
@dp.message(F.text == BTN_DAILY)
async def daily_ask(msg: Message, state: FSMContext):
    await state.set_state(Form.daily_city)
    await msg.answer("Kunlik xabar uchun shahar nomini yozing (masalan: Tashkent).")


@dp.message(Form.daily_city)
async def daily_city(msg: Message, state: FSMContext):
    try:
        place = await geocode(msg.text.strip())
    except Exception:
        await msg.answer("Xizmat javob bermadi. Qayta urinib ko'ring.")
        return
    if not place:
        await msg.answer("Shahar topilmadi. Lotin harflarida yozib ko'ring.")
        return
    await state.update_data(place=place)
    await state.set_state(Form.daily_time)
    await msg.answer(f"{place['name']} tanlandi. Xabar har kuni soat nechada kelsin? Format: 06:30")


@dp.message(Form.daily_time)
async def daily_time(msg: Message, state: FSMContext):
    t = msg.text.strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
        await msg.answer("Format noto'g'ri. Masalan: 06:30")
        return
    place = (await state.get_data())["place"]
    DB["daily"][str(msg.from_user.id)] = {**place, "time": t, "last": ""}
    save_db()
    await state.clear()
    await msg.answer(f"✅ Tayyor! Har kuni {t} da ({place['name']} vaqti bilan) "
                     f"namoz vaqti va ob-havo yuboraman.", reply_markup=menu_kb())


@dp.message(F.text == BTN_DAILY_OFF)
async def daily_off(msg: Message, state: FSMContext):
    await state.clear()
    if DB["daily"].pop(str(msg.from_user.id), None):
        save_db()
        await msg.answer("Kunlik xabar o'chirildi.", reply_markup=menu_kb())
    else:
        await msg.answer("Sizda kunlik xabar yoqilmagan.", reply_markup=menu_kb())


async def daily_loop(bot: Bot):
    while True:
        await asyncio.sleep(30)
        for uid, s in list(DB["daily"].items()):
            try:
                now = datetime.now(ZoneInfo(s["tz"]))
                if now.strftime("%H:%M") != s["time"] or s.get("last") == now.date().isoformat():
                    continue
                s["last"] = now.date().isoformat()
                save_db()
                text = await prayer_text(s) + "\n\n" + await weather_text(s)
                await bot.send_message(int(uid), "☀️ Xayrli tong!\n\n" + text)
            except Exception as e:
                logging.warning("Kunlik xabar xatosi (%s): %s", uid, e)
                if "blocked" in str(e).lower():
                    DB["daily"].pop(uid, None)
                    save_db()


async def run_web():
    """index.html ni shu botning o'zi ham tarqatadi (alohida hosting kerak emas)."""
    base = os.path.dirname(os.path.abspath(__file__))
    app = web.Application()

    async def index(_):
        return web.FileResponse(os.path.join(base, "index.html"))

    async def health(_):
        return web.Response(text="ok")

    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()


async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN o'rnatilmagan. Tokenni muhit o'zgaruvchisiga qo'ying.")
    await run_web()
    bot = Bot(BOT_TOKEN)
    asyncio.create_task(daily_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
