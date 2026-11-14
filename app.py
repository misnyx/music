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
SEARCH_LIMIT = 15

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class BotStates(StatesGroup):
    waiting_for_query = State()

def sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def build_service_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Spotify", callback_data="service:spotify")],
        [InlineKeyboardButton(text="🔴 YouTube Music", callback_data="service:youtube")],
        [InlineKeyboardButton(text="🟠 SoundCloud", callback_data="service:soundcloud")]
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

async def search_all(query: str, service: str):
    prefix = "ytsearch" if service in ["youtube", "spotify"] else "scsearch"
    
    # Поиск артистов
    artists_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    artists_url = f"{prefix}3:{query} artist"
    
    # Поиск альбомов и плейлистов
    albums_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    albums_url = f"{prefix}3:{query} album playlist"
    
    # Поиск треков
    tracks_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    tracks_url = f"{prefix}10:{query}"

    def _run(url, opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                return ydl.extract_info(url, download=False).get("entries", [])
            except:
                return []

    artists = await asyncio.to_thread(_run, artists_url, artists_opts)
    albums = await asyncio.to_thread(_run, albums_url, albums_opts)
    tracks = await asyncio.to_thread(_run, tracks_url, tracks_opts)

    return {
        "artists": [{"title": e.get("title"), "url": e.get("url")} for e in artists if e],
        "albums": [{"title": e.get("title"), "url": e.get("url")} for e in albums if e],
        "tracks": [{
            "title": f"{e.get('uploader', e.get('channel', 'Артист'))} - {e.get('title', 'Unknown')}",
            "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}"
        } for e in tracks if e]
    }

def build_results_keyboard(data: dict, page: int, mode: str = "main"):
    rows = []
    
    if mode == "main":
        # 1. Артисты
        if data.get("artists"):
            rows.append([InlineKeyboardButton(text="👤 Артисты", callback_data="ignore")])
            for i, artist in enumerate(data["artists"]):
                rows.append([InlineKeyboardButton(text=artist["title"][:40], callback_data=f"artist:{i}")])

        # 2. Альбомы/Плейлисты
        if data.get("albums"):
            rows.append([InlineKeyboardButton(text="💿 Альбомы / Плейлисты", callback_data="ignore")])
            for i, album in enumerate(data["albums"]):
                rows.append([InlineKeyboardButton(text=album["title"][:40], callback_data=f"album:{i}")])

        # 3. Треки
        if data.get("tracks"):
            rows.append([InlineKeyboardButton(text="🎵 Треки", callback_data="ignore")])
            start = page * PAGE_SIZE
            end = start + PAGE_SIZE
            page_tracks = data["tracks"][start:end]
            
            for i, track in enumerate(page_tracks):
                global_index = start + i
                rows.append([InlineKeyboardButton(text=track["title"][:45], callback_data=f"dl:{global_index}")])

            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page:{page-1}"))
            if end < len(data["tracks"]):
                nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"page:{page+1}"))
            if nav:
                rows.append(nav)
    
    elif mode == "album":
        start = page * PAGE_SIZE
        end = start + PAGE_SIZE
        page_tracks = data["tracks"][start:end]
        
        for i, track in enumerate(page_tracks):
            global_index = start + i
            rows.append([InlineKeyboardButton(text=track["title"][:45], callback_data=f"dl:{global_index}")])
            
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page:{page-1}"))
        if end < len(data["tracks"]):
            nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"page:{page+1}"))
        if nav:
            rows.append(nav)
            
        rows.append([InlineKeyboardButton(text="🔙 К поиску", callback_data="restart")])

    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Выбери сервис:",
        reply_markup=build_service_keyboard()
    )

@dp.callback_query(F.data == "restart")
async def restart_bot(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Выбери сервис:", reply_markup=build_service_keyboard())

@dp.callback_query(F.data.startswith("service:"))
async def process_service(callback: types.CallbackQuery, state: FSMContext):
    service = callback.data.split(":")[1]
    await state.update_data(service=service)
    names = {"spotify": "Spotify", "youtube": "YouTube Music", "soundcloud": "SoundCloud"}
    await callback.message.edit_text(
        f"Сервис: {names.get(service)}.\n\n"
        "Отправь название трека, имя артиста или ссылку:"
    )
    await state.set_state(BotStates.waiting_for_query)
    await callback.answer()

@dp.message(BotStates.waiting_for_query)
async def handle_query(message: types.Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    service = data.get("service", "youtube")

    if text.startswith("http"):
        await message.answer("⬇️ Скачиваю по ссылке…")
        filepath = await download_media(text)
        if filepath:
            await message.answer_audio(FSInputFile(filepath), caption="Готово")
        else:
            await message.answer("Не удалось скачать. Проверь ссылку.")
        return

    await message.answer("🔍 Ищу…")
    results = await search_all(text, service)
    await state.update_data(results=results, page=0, mode="main")
    
    if not any(results.values()):
        await message.answer("Ничего не найдено.")
        return

    kb = build_results_keyboard(results, 0, "main")
    await message.answer(
        f"Результаты поиска по запросу «{text}»:",
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith("artist:"))
async def open_artist(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    results = data.get("results", {})
    index = int(callback.data.split(":")[1])
    artist = results["artists"][index]
    
    await callback.answer("Загружаю дискографию…")
    await callback.message.edit_text(f"⏳ Загружаю треки артиста: {artist['title']}…")
    
    # При открытии артиста, мы ищем его треки, чтобы показать список
    # В YouTube это может быть канал, в SoundCloud - профиль
    ydl_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(artist["url"], download=False)
                if "entries" in info:
                    return [{"title": e.get("title"), "url": e.get("url")} for e in info["entries"] if e]
                else:
                    # Если это просто канал, возможно нужно использовать поиск
                    return [{"title": info.get("title"), "url": info.get("webpage_url")}]
            except:
                return []

    tracks = await asyncio.to_thread(_run)
    if not tracks:
        await callback.message.edit_text("Не удалось загрузить треки.")
        return

    new_results = {"artists": [], "albums": [], "tracks": tracks}
    await state.update_data(results=new_results, page=0, mode="album") # используем режим альбома для треков
    
    # Форматируем названия
    for t in new_results["tracks"]:
        t["title"] = f"{artist['title']} - {t['title']}"
    
    kb = build_results_keyboard(new_results, 0, "album")
    await callback.message.edit_text(f"Треки артиста {artist['title']}:", reply_markup=kb)

@dp.callback_query(F.data.startswith("album:"))
async def open_album(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    results = data.get("results", {})
    index = int(callback.data.split(":")[1])
    album = results["albums"][index]
    
    await callback.answer("Загружаю треки из альбома…")
    await callback.message.edit_text(f"⏳ Загружаю треки из: {album['title']}…")
    
    ydl_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(album["url"], download=False)
                # ВАЖНО: если это плейлист, берем "entries"
                if "entries" in info:
                    return [{"title": e.get("title"), "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}"} for e in info["entries"] if e]
                return []
            except:
                return []

    tracks = await asyncio.to_thread(_run)
    if not tracks:
        await callback.message.edit_text("Не удалось загрузить треки. Возможно, это не альбом.")
        return

    new_results = {"artists": [], "albums": [], "tracks": tracks}
    await state.update_data(results=new_results, page=0, mode="album")
    
    # Форматируем названия для альбома (если нужно)
    for t in new_results["tracks"]:
        t["title"] = f"{album['title']} - {t['title']}"

    kb = build_results_keyboard(new_results, 0, "album")
    await callback.message.edit_text(f"Треки из альбома/плейлиста {album['title']}:", reply_markup=kb)

@dp.callback_query(F.data.startswith("page:"))
async def handle_page(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    results = data.get("results", {})
    page = int(callback.data.split(":")[1])
    mode = data.get("mode", "main")
    await state.update_data(page=page)
    
    kb = build_results_keyboard(results, page, mode)
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
        await status_msg.edit_text("❌ Не удалось скачать (возможно, требуется VPN или трек заблокирован).")

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
