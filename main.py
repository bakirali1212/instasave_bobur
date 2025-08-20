import asyncio
import logging
import os
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile
from dotenv import load_dotenv
from yt_dlp import YoutubeDL

# =========================
# Config & constants
# =========================
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN")
IG_COOKIES_PATH = os.getenv("IG_COOKIES_PATH", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in .env")

# Target quality & limits
MAX_HEIGHT_TARGET = 480          # 360 if you want even smaller
TELEGRAM_LIMIT_MB = 2000         # hard limit
SAFE_LIMIT_MB = 1900             # compress when above this
SEND_AS_VIDEO_THRESHOLD_MB = 50  # send as video if ≤ 50MB else document

# Concurrency
CONCURRENCY = asyncio.Semaphore(2)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# URL patterns (incl. Shorts)
YOUTUBE_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)",
    re.I,
)
INSTAGRAM_RE = re.compile(r"(https?://)?(www\.)?instagram\.com/(reel|p|tv)/", re.I)


# =========================
# Helpers
# =========================
def is_supported_url(url: str) -> bool:
    return bool(YOUTUBE_RE.search(url) or INSTAGRAM_RE.search(url))

def platform_name(url: str) -> str:
    if YOUTUBE_RE.search(url):
        return "YouTube"
    if INSTAGRAM_RE.search(url):
        return "Instagram"
    return "Unknown"

def human_mb(bytes_: int) -> float:
    return round(bytes_ / (1024 * 1024), 2)

@asynccontextmanager
async def typing(chat_id: int):
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        yield
    finally:
        pass

def build_ydl_opts(tmp_dir: Path, from_instagram: bool, max_h: int = MAX_HEIGHT_TARGET) -> dict:
    """
    Flexible format chain:
     1) Try MP4 video+audio ≤ max_h
     2) Try single MP4 ≤ max_h
     3) Try best MP4
     4) Fallback to best (any)
    """
    fmt_chain = (
        f"bv*[height<={max_h}][ext=mp4]+ba[ext=m4a]"
        f"/b[height<={max_h}][ext=mp4]"
        f"/best[ext=mp4]"
        f"/best"
    )

    opts = {
        "outtmpl": str(tmp_dir / "%(title).200B.%(ext)s"),
        "format": fmt_chain,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "prefer_ffmpeg": True,
        "retries": 3,
    }
    if from_instagram and IG_COOKIES_PATH:
        opts["cookies"] = IG_COOKIES_PATH
    return opts

async def ffmpeg_compress(src: Path, dst: Path, max_h: int, crf: int) -> None:
    """
    Re-encode with H.264 (libx264) + AAC, scale to max_h, CRF controls quality/size.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-vf", f"scale=-2:{max_h}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-c:a", "aac", "-b:a", "128k",
        str(dst),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError("ffmpeg compress failed")

async def shrink_until_ok(filepath: Path, status_msg: Message) -> Path:
    """
    If file is near/over limit, try a few shrinking strategies in order.
    Returns a path that is ≤ TELEGRAM_LIMIT_MB (or the last attempt).
    """
    attempts = [
        (MAX_HEIGHT_TARGET, 28),
        (MAX_HEIGHT_TARGET, 30),
        (360, 28),
        (360, 30),
    ]
    current = filepath
    size_mb = human_mb(current.stat().st_size)
    if size_mb <= SAFE_LIMIT_MB:
        return current

    for max_h, crf in attempts:
        await status_msg.edit_text(
            f"📦 Katta video. Siqilmoqda ({max_h}p, CRF {crf})…\n"
            f"📦 Large video. Compressing ({max_h}p, CRF {crf})…"
        )
        candidate = current.with_suffix(f".{max_h}p.crf{crf}.mp4")
        await ffmpeg_compress(current, candidate, max_h=max_h, crf=crf)
        size_mb = human_mb(candidate.stat().st_size)
        logging.info(f"Compressed to ~{size_mb} MB @ {max_h}p CRF{crf}")
        current = candidate
        if size_mb <= SAFE_LIMIT_MB:
            break

    return current

async def send_file(message: Message, filepath: Path, title: str):
    size_mb = human_mb(filepath.stat().st_size)
    caption = f"<b>{title}</b>" if title else "Video"
    await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)

    if size_mb > TELEGRAM_LIMIT_MB:
        await message.answer(
            "❗️Fayl hajmi Telegram limitidan katta (>2GB), yuborib bo‘lmaydi.\n"
            "❗️File exceeds Telegram limit (>2GB)."
        )
        return

    try:
        file = FSInputFile(str(filepath))
        if size_mb <= SEND_AS_VIDEO_THRESHOLD_MB:
            await message.answer_video(file, caption=caption, parse_mode=ParseMode.HTML)
        else:
            await message.answer_document(file, caption=caption, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.exception("Sending file failed")
        await message.answer(f"❌ Yuborishda xato: {e!s}\n❌ Sending failed: {e!s}")


# =========================
# Handlers
# =========================
@dp.message(CommandStart())
async def on_start(message: Message):
    text_en = (
        "Send me a **YouTube** (incl. Shorts) or **Instagram** link.\n\n"
        "• I target ≤480p MP4 to keep size small (with smart fallbacks).\n"
        "• If still large, I auto-compress before sending.\n"
        "• Playlists not supported. Telegram max ~2GB."
    )
    text_uz = (
        "**YouTube** (Shorts ham) yoki **Instagram** havolasini yuboring.\n\n"
        "• Odatda ≤480p MP4 yuklayman (mos kelmasa moslashuvchan fallback bor).\n"
        "• Baribir katta bo‘lsa, yuborishdan oldin avtomatik siqaman.\n"
        "• Playlist qo‘llanmaydi. Telegram limiti ~2GB."
    )
    await message.answer(f"{text_en}\n\n—\n\n{text_uz}", parse_mode=ParseMode.MARKDOWN)

@dp.message(F.text)
async def handle_url(message: Message):
    url = (message.text or "").strip()
    if not is_supported_url(url):
        await message.answer(
            "Send a valid YouTube/Instagram link.\n\n"
            "YouTube/Instagram havolasini yuboring."
        )
        return

    async with CONCURRENCY:
        plat = platform_name(url)
        status_msg = await message.answer(f"⏳ Downloading from {plat}…\n\n{plat} dan yuklanmoqda…")

        with tempfile.TemporaryDirectory() as td:
            tmp_dir = Path(td)
            ydl_opts = build_ydl_opts(tmp_dir, from_instagram=(plat == "Instagram"))

            try:
                await bot.send_chat_action(message.chat.id, ChatAction.RECORD_VIDEO)

                def _extract():
                    with YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        # Resolve downloaded filepath
                        path = None
                        if isinstance(info, dict):
                            rd = info.get("requested_downloads") or []
                            if rd and isinstance(rd, list):
                                fp = rd[0].get("filepath")
                                if fp:
                                    path = Path(fp)
                            if not path:
                                path = Path(ydl.prepare_filename(info))
                                if not path.exists():
                                    path = path.with_suffix(".mp4")
                        else:
                            raise RuntimeError("Unexpected info type from yt-dlp")
                        title = info.get("title") or "video"
                        return path, title

                filepath, title = await asyncio.to_thread(_extract)
                if not filepath or not filepath.exists():
                    raise FileNotFoundError("Downloaded file not found")

                size_mb = human_mb(filepath.stat().st_size)
                logging.info(f"Downloaded ~{size_mb} MB from {plat}")

                # Compress if near/over safe threshold
                if size_mb > SAFE_LIMIT_MB:
                    filepath = await shrink_until_ok(filepath, status_msg)

                await status_msg.edit_text(
                    "✅ Downloaded. Uploading to Telegram…\n\n"
                    "✅ Yuklab olindi. Telegram’ga yuborilmoqda…"
                )
                await send_file(message, filepath, title)

            except Exception as e:
                logging.exception("Download/Process failed")
                await status_msg.edit_text(
                    "❌ Download/convert failed. Possible reasons:\n"
                    "• Private/age/region restricted content\n"
                    "• Unsupported/removed link\n"
                    "• Network/ffmpeg issues\n\n"
                    f"Error: {e!s}\n\n"
                    "❌ Yuklab/siqib bo‘lmadi. Ehtimoliy sabablari:\n"
                    "• Yopiq/yosh/region cheklovi\n"
                    "• Noto‘g‘ri yoki o‘chirilgan havola\n"
                    "• Tarmoq/ffmpeg muammosi"
                )


# =========================
# Entry point
# =========================
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
