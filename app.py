import os
import asyncio
import re
from pathlib import Path
from flask import Flask
import threading

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
import yt_dlp

BOT_TOKEN = os.environ.get("9168240043:AAGxy7DopH0z08u-VL1qSWG_ZcMnKRM14gE")
DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

PAGE_SIZE = 5
SEARCH_LIMIT = 30

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class BotStates(StatesGroup):
    waiting_for_query = State()
    waiting_for_type = State()

def sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def build_service_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Spotify", callback_data="service:spotify")],
        [InlineKeyboardButton(text="🔴 YouTube Music", callback_data="service:youtube")],
        [InlineKeyboardButton(text="🟠 SoundCloud", callback_data="service:soundcloud")]
    ])

def build_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Трек", callback_data="type:track")],
        [InlineKeyboardButton(text="💿 Альбом", callback_data="type:album")],
        [InlineKeyboardButton(text="📜 Плейлист", callback_data="type:playlist")]
    ])

async def search_tracks(query: str, search_type: str) -> list[dict]:
    suffix = ""
    if search_type == "album":
        suffix = " full album"
    elif search_type == "playlist":
        suffix = " playlist"
    
    search_url = f"ytsearch{SEARCH_LIMIT}:{query}{suffix}"
    
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }

    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
            return info.get("entries", [])

    try:
        entries = await asyncio.to_thread(_run)
    except Exception as e:
        print(f"[search error] {e}")
        return []

    results = []
    for e in entries:
        if not e: continue
        results.append({
            "id": e.get("id"),
            "title": e.get("title", "Unknown"),
            "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
            "view_count": e.get("view_count") or 0,
        })
    
    results.sort(key=lambda x: x["view_count"], reverse=True)
    return results

async def download_audio(url: str) -> str | None:
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }

    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True)

    try:
        info = await asyncio.to_thread(_run)
    except Exception as e:
        print(f"[download error] {e}")
        return None

    title = sanitize(info.get("title", "audio"))
    candidate = DOWNLOAD_DIR / f"{title}.mp3"
    if candidate.exists():
        return str(candidate)
    mp3s = sorted(DOWNLOAD_DIR.glob("*.mp3"), key=os.path.getmtime, reverse=True)
    return str(mp3s[0]) if mp3s else None

def build_results_keyboard(results: list[dict], page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = results[start:end]

    rows = []
    for i, item in enumerate(page_items):
        global_index = start + i
        title = item["title"][:45]
        rows.append([InlineKeyboardButton(text=title, callback_data=f"dl:{global_index}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page:{page-1}"))
    if end < len(results):
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Выбери сервис, с которого хочешь искать музыку:",
        reply_markup=build_service_keyboard()
    )

@dp.callback_query(F.data.startswith("service:"))
async def process_service(callback: types.CallbackQuery, state: FSMContext):
    service = callback.data.split(":")[1]
    await state.update_data(service=service)
    
    names = {"spotify": "Spotify", "youtube": "YouTube Music", "soundcloud": "SoundCloud"}
    await callback.message.edit_text(
        f"Выбран сервис: {names.get(service, service)}.\n\n"
        "Отправь мне название трека, альбома или плейлиста."
    )
    await state.set_state(BotStates.waiting_for_query)
    await callback.answer()

@dp.message(BotStates.waiting_for_query)
async def process_query(message: types.Message, state: FSMContext):
    query = message.text.strip()
    await state.update_data(query=query)
    
    await message.answer(
        f"Запрос: «{query}».\n\n"
        "Уточни, это трек, альбом или плейлист?",
        reply_markup=build_type_keyboard()
    )
    await state.set_state(BotStates.waiting_for_type)

@dp.callback_query(BotStates.waiting_for_type, F.data.startswith("type:"))
async def process_type(callback: types.CallbackQuery, state: FSMContext):
    search_type = callback.data.split(":")[1]
    data = await state.get_data()
    query = data.get("query")
    
    names = {"track": "Трек", "album": "Альбом", "playlist": "Плейлист"}
    await callback.message.edit_text(f"Ищу {names[search_type].lower()}: «{query}»… 🔍")
    
    results = await search_tracks(query, search_type)
    
    if not results:
        await callback.message.answer("Ничего не найдено. Попробуй изменить запрос.")
        await state.clear()
        return

    await state.update_data(results=results)
    
    kb = build_results_keyboard(results, page=0)
    total_pages = (len(results) + PAGE_SIZE - 1) // PAGE_SIZE
    
    await callback.message.edit_text(
        f"Результаты для «{query}» (по популярности). Страница 1/{total_pages}:",
        reply_markup=kb
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("page:"))
async def handle_page(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    results = data.get("results")
    if not results:
        await callback.answer("Сессия поиска истекла. Начните заново /start", show_alert=True)
        return

    page = int(callback.data.split(":")[1])
    kb = build_results_keyboard(results, page=page)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("dl:"))
async def handle_download(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    results = data.get("results")
    if not results:
        await callback.answer("Сессия истекла.", show_alert=True)
        return

    index = int(callback.data.split(":")[1])
    if index >= len(results):
        await callback.answer("Ошибка выбора.", show_alert=True)
        return

    track = results[index]
    await callback.answer("Начинаю скачивание…")
    status_msg = await callback.message.answer(f"⬇️ Качаю: {track['title']}")

    filepath = await download_audio(track["url"])
    if filepath:
        await status_msg.edit_text("✅ Готово, отправляю файл…")
        await callback.message.answer_audio(FSInputFile(filepath), caption=track["title"])
    else:
        await status_msg.edit_text("❌ Не удалось скачать (возможно, требуется VPN или трек заблокирован).")

app = Flask(__name__)

@app.route('/')
def health():
    return "Bot is running", 200

def run_bot():
    asyncio.run(dp.start_polling(bot))

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
