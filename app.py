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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_ЕСЛИ_ЗАПУСКАЕТЕ_ЛОКАЛЬНО")
DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

PAGE_SIZE = 5
SEARCH_LIMIT = 20

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class BotStates(StatesGroup):
    waiting_for_query = State()
    browsing_artist = State()

def sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def build_service_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Spotify", callback_data="service:spotify")],
        [InlineKeyboardButton(text="🔴 YouTube Music", callback_data="service:youtube")],
        [InlineKeyboardButton(text="🟠 SoundCloud", callback_data="service:soundcloud")]
    ])

def build_back_keyboard(callback_data: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)]
    ])

async def download_media(url: str) -> str | None:
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

async def search_content(query: str, service: str):
    results = {"artists": [], "tracks": []}
    
    # 1. Поиск артистов (для YouTube Music и SoundCloud)
    if service in ["youtube", "soundcloud"]:
        search_prefix = "ytsearch" if service == "youtube" else "scsearch"
        try:
            ydl_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
            search_url = f"{search_prefix}5:{query} artist"
            
            def _run_artist():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(search_url, download=False).get("entries", [])
            
            entries = await asyncio.to_thread(_run_artist)
            for e in entries:
                if e:
                    results["artists"].append({
                        "title": e.get("title", "Unknown Artist"),
                        "url": e.get("url") or f"https://www.youtube.com/channel/{e.get('channel_id')}"
                    })
        except Exception:
            pass

    # 2. Поиск треков
    try:
        ydl_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
        if service == "spotify":
            # Для Spotify ищем на YouTube
            search_url = f"ytsearch{SEARCH_LIMIT}:{query} audio"
        elif service == "soundcloud":
            search_url = f"scsearch{SEARCH_LIMIT}:{query}"
        else:
            search_url = f"ytsearch{SEARCH_LIMIT}:{query} audio"

        def _run_tracks():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(search_url, download=False).get("entries", [])

        entries = await asyncio.to_thread(_run_tracks)
        for e in entries:
            if e:
                results["tracks"].append({
                    "title": e.get("title", "Unknown"),
                    "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                    "uploader": e.get("uploader") or e.get("channel") or "Unknown"
                })
    except Exception as e:
        print(f"[search error] {e}")

    return results

async def get_artist_albums(artist_url: str):
    try:
        ydl_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
        def _run():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(artist_url, download=False)
                # Если это канал/плейлист, получаем список видео/треков
                entries = info.get("entries", [])
                return [{"title": e.get("title"), "url": e.get("url")} for e in entries if e]
        return await asyncio.to_thread(_run)
    except Exception as e:
        print(f"[artist albums error] {e}")
        return []

def build_results_keyboard(data: dict, page: int, service: str):
    rows = []
    
    # Кнопки артистов (только на первой странице)
    if page == 0 and data["artists"]:
        rows.append([InlineKeyboardButton(text="👤 Артисты", callback_data="ignore")])
        for i, artist in enumerate(data["artists"]):
            rows.append([InlineKeyboardButton(text=artist["title"][:40], callback_data=f"artist:{i}")])
        rows.append([InlineKeyboardButton(text="🎵 Треки", callback_data="ignore")])

    # Кнопки треков
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_tracks = data["tracks"][start:end]

    for i, track in enumerate(page_tracks):
        global_index = start + i
        rows.append([InlineKeyboardButton(text=track["title"][:45], callback_data=f"dl:{global_index}")])

    # Навигация по трекам
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page:{page-1}"))
    if end < len(data["tracks"]):
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="🏠 В начало", callback_data="restart")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Выбери сервис для поиска или просто отправь ссылку на трек/альбом/плейлист:",
        reply_markup=build_service_keyboard()
    )

@dp.callback_query(F.data == "restart")
async def restart_bot(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "Выбери сервис для поиска или просто отправь ссылку:",
        reply_markup=build_service_keyboard()
    )

@dp.callback_query(F.data.startswith("service:"))
async def process_service(callback: types.CallbackQuery, state: FSMContext):
    service = callback.data.split(":")[1]
    await state.update_data(service=service)
    names = {"spotify": "Spotify", "youtube": "YouTube Music", "soundcloud": "SoundCloud"}
    await callback.message.edit_text(
        f"Сервис: {names.get(service)}.\n\n"
        "Отправь название трека, артиста или ссылку."
    )
    await state.set_state(BotStates.waiting_for_query)
    await callback.answer()

@dp.message(BotStates.waiting_for_query)
async def handle_query(message: types.Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    service = data.get("service", "youtube")

    # Если это ссылка — сразу качаем
    if text.startswith("http"):
        await message.answer("⬇️ Скачиваю по ссылке…")
        filepath = await download_media(text)
        if filepath:
            await message.answer_audio(FSInputFile(filepath), caption="Готово")
        else:
            await message.answer("Не удалось скачать. Проверь ссылку.")
        return

    # Иначе ищем
    await message.answer("🔍 Ищу…")
    results = await search_content(text, service)
    await state.update_data(results=results, page=0)
    
    if not results["tracks"] and not results["artists"]:
        await message.answer("Ничего не найдено.")
        return

    kb = build_results_keyboard(results, 0, service)
    await message.answer("Результаты поиска (сначала артисты, потом треки):", reply_markup=kb)

@dp.callback_query(F.data.startswith("artist:"))
async def handle_artist(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    results = data.get("results", {})
    index = int(callback.data.split(":")[1])
    
    if index >= len(results.get("artists", [])):
        await callback.answer("Ошибка.", show_alert=True)
        return
        
    artist = results["artists"][index]
    await callback.answer("Загружаю дискографию…")
    await callback.message.edit_text(f"👤 {artist['title']}\nЗагружаю треки…")
    
    # Для простоты считаем, что ссылка на артиста ведет на плейлист/канал с треками
    tracks = await get_artist_albums(artist["url"])
    
    if not tracks:
        await callback.message.edit_text("Не удалось загрузить треки артиста.")
        return

    # Показываем треки артиста как результат поиска
    new_results = {"artists": [], "tracks": tracks}
    await state.update_data(results=new_results, page=0)
    
    kb = build_results_keyboard(new_results, 0, data.get("service"))
    await callback.message.edit_text(f"Треки артиста {artist['title']}:", reply_markup=kb)

@dp.callback_query(F.data.startswith("page:"))
async def handle_page(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    results = data.get("results", {})
    page = int(callback.data.split(":")[1])
    await state.update_data(page=page)
    
    kb = build_results_keyboard(results, page, data.get("service"))
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("dl:"))
async def handle_download(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    results = data.get("results", {})
    index = int(callback.data.split(":")[1])
    
    if index >= len(results.get("tracks", [])):
        await callback.answer("Ошибка.", show_alert=True)
        return

    track = results["tracks"][index]
    await callback.answer("Начинаю скачивание…")
    status_msg = await callback.message.answer(f"⬇️ Качаю: {track['title']}")

    filepath = await download_media(track["url"])
    if filepath:
        await status_msg.edit_text("✅ Готово…")
        await callback.message.answer_audio(FSInputFile(filepath), caption=track["title"])
    else:
        await status_msg.edit_text("❌ Не удалось скачать.")

app = Flask(__name__)

@app.route('/')
def health():
    return "Bot is running", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

async def run_bot():
    await dp.start_polling(bot)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(run_bot())
