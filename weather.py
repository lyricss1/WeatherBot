import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import aiohttp

TG_TOK = "..."
OWM_KEY = "..."

users = {}
running_tasks = {}


class InitSt(StatesGroup):
    w_name = State()
    w_city = State()


dp = Dispatcher()


async def fetch_weather_raw(city: str, endpoint: str = "weather"):
    url = f"https://api.openweathermap.org/data/2.5/{endpoint}"
    params = dict(q=city, appid=OWM_KEY, units="metric", lang="en")

    async with aiohttp.ClientSession() as sess:
        resp = await sess.get(url, params=params)
        if resp.status != 200:
            return
        return await resp.json()


async def send_weather_message(bot: Bot, chat_id: int):
    u = users.get(chat_id)
    if not u or not u.get("city"):
        await bot.send_message(chat_id, "No city set. Use /setcity")
        return

    data = await fetch_weather_raw(u["city"])
    if not data:
        return

    txt = (
        "Weather for " + data["name"] + " (" + data["sys"]["country"] + ")\n"
        "Hello, " + u.get("name", "friend") + ", here is your weather:\n"
        "Temp: {}°C\n".format(data["main"]["temp"]) +
        "Humidity: " + str(data["main"]["humidity"]) + "%\n" +
        "Wind: {} m/s".format(data["wind"]["speed"])
    )

    await bot.send_message(chat_id, txt)
    return True


async def scheduler_loop(bot: Bot, chat_id: int, interval: float):
    try:
        while True:
            await asyncio.sleep(interval * 3600)
            await send_weather_message(bot, chat_id)
    except asyncio.CancelledError:
        return


@dp.message(Command("start"))
async def start_cmd(msg: types.Message, state: FSMContext):
    uid = msg.from_user.id

    if uid in users and users[uid].get("name"):
        await msg.answer("Welcome back, " + users[uid]["name"])
        await show_menu(msg)
        return

    await msg.answer("Welcome. Let's get started.\nEnter your name:")
    await state.set_state(InitSt.w_name)


async def show_menu(msg: types.Message):
    text = (
        "Commands:\n"
        "/weather - now\n"
        "/days - select date\n"
        "/forecast - next hours\n"
        "/monitor h - auto\n"
        "/stop - stop auto\n"
        "/setcity - change city\n"
        "/reset - clear data"
    )
    await msg.answer(text)


@dp.message(StateFilter(InitSt.w_name))
async def save_name(msg: types.Message, state: FSMContext):
    nm = msg.text.strip()
    users[msg.from_user.id] = dict(name=nm)

    await msg.answer("Ok {}, now send your city:".format(nm))
    await state.set_state(InitSt.w_city)


@dp.message(StateFilter(InitSt.w_city))
async def save_city(msg: types.Message, state: FSMContext):
    city = msg.text.strip()
    if (await fetch_weather_raw(city)) is None:
        await msg.answer("City not found. Try again:")
        return

    users[msg.from_user.id]["city"] = city
    await msg.answer("Saved: " + city)
    await state.clear()
    await show_menu(msg)


@dp.message(Command("weather"))
async def cmd_weather(msg: types.Message):
    await send_weather_message(msg.bot, msg.chat.id)


@dp.message(Command("forecast"))
async def cmd_forecast(msg: types.Message):
    uid = msg.from_user.id
    u = users.get(uid)

    if not u:
        await msg.answer("Set city first.")
        return

    res = await fetch_weather_raw(u["city"], "forecast")
    if not res:
        return

    out = ["Forecast for " + u["city"]]

    for itm in res["list"][:3]:
        out.append(itm["dt_txt"][11:16] + "  " +
                   str(itm["main"]["temp"]) + "°C  " +
                   "(" + itm["weather"][0]["main"] + ")")

    await msg.answer("\n".join(out))


@dp.message(Command("days"))
async def cmd_days(msg: types.Message):
    uid = msg.from_user.id
    u = users.get(uid)

    if not u:
        await msg.answer("Set city first.")
        return

    res = await fetch_weather_raw(u["city"], "forecast")
    if not res:
        return

    dates = sorted({i["dt_txt"].split()[0] for i in res["list"]})

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=d, callback_data="day_" + d)]
            for d in dates
        ]
    )

    await msg.answer("Select date:", reply_markup=kb)


@dp.callback_query(F.data.startswith("day_"))
async def process_day_click(call: CallbackQuery):
    dt = call.data.split("_")[1]
    uid = call.from_user.id

    city = users[uid]["city"]
    res = await fetch_weather_raw(city, "forecast")
    if not res:
        return

    items = [x for x in res["list"] if x["dt_txt"].startswith(dt)]

    out = ["Weather " + dt + " (" + city + ")"]
    for i in items:
        tm = i["dt_txt"][11:16]
        if tm in ("09:00", "12:00", "15:00", "18:00", "21:00"):
            out.append(tm + ": " + str(i["main"]["temp"]) +
                       "°C, " + i["weather"][0]["main"])

    await call.message.edit_text("\n".join(out))
    await call.answer()


@dp.message(Command("setcity"))
async def cmd_setcity(msg: types.Message, state: FSMContext):
    parts = msg.text.split(maxsplit=1)

    if len(parts) == 2:
        ct = parts[1]
        if await fetch_weather_raw(ct):
            if msg.from_user.id not in users:
                users[msg.from_user.id] = {"name": "User"}
            users[msg.from_user.id]["city"] = ct
            await msg.answer("Updated: " + ct)
        else:
            await msg.answer("City not found.")
        return

    await msg.answer("Enter city:")
    await state.set_state(InitSt.w_city)


@dp.message(Command("monitor"))
async def cmd_monitor(msg: types.Message):
    uid = msg.from_user.id
    args = msg.text.split()

    if uid not in users:
        return

    if len(args) != 2:
        await msg.answer("Usage: /monitor hours")
        return

    try:
        hours = float(args[1])

        if uid in running_tasks:
            running_tasks[uid].cancel()

        running_tasks[uid] = asyncio.create_task(
            scheduler_loop(msg.bot, uid, hours)
        )

        await msg.answer("Auto-update every " + args[1] + "h")
    except:
        await msg.answer("Bad number.")


@dp.message(Command("stop"))
async def cmd_stop(msg: types.Message):
    uid = msg.from_user.id

    if uid in running_tasks:
        running_tasks[uid].cancel()
        del running_tasks[uid]
        await msg.answer("Stopped.")
    else:
        await msg.answer("No tasks.")


@dp.message(Command("reset"))
async def cmd_reset(msg: types.Message, state: FSMContext):
    uid = msg.from_user.id
    if uid in running_tasks:
        running_tasks[uid].cancel()
        del running_tasks[uid]
    users.pop(uid, None)
    await msg.answer("Data cleared. Restarting…")
    await start_cmd(msg, state)



async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=TG_TOK)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
