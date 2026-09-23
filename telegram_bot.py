import os
import uuid
import base64
import tempfile
import subprocess
import asyncio
import logging
import shutil
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.error import TelegramError, NetworkError, TimedOut
import yt_dlp

# ---------- LOGGING ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)   # reduce log noise
logger = logging.getLogger(__name__)

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable is not set!")

DOWNLOAD_DIR = "./downloads"
MAX_TELEGRAM_SIZE_MB = 50
DOWNLOAD_TIMEOUT_SEC = 1800   # 30 min for large playlists
MODE_SINGLE = "single"
MODE_PLAYLIST = "playlist"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ---------- HELPERS ----------
def safe_remove(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning(f"Failed to remove {path}: {e}")


def cleanup_dir_old_files(max_age_sec: int = 3600):
    now = time.time()
    try:
        for name in os.listdir(DOWNLOAD_DIR):
            p = os.path.join(DOWNLOAD_DIR, name)
            if os.path.isfile(p) and now - os.path.getmtime(p) > max_age_sec:
                safe_remove(p)
    except Exception:
        pass


def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def is_valid_url(text: str) -> bool:
    if not text:
        return False
    text = text.strip()
    return text.startswith(("http://", "https://")) and " " not in text


def human_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


def write_cookies_file() -> str | None:
    """Decode YT_COOKIES_B64 env var into a temp cookies.txt. Returns path or None."""
    cookies_b64 = os.environ.get("YT_COOKIES_B64")
    if not cookies_b64:
        logger.warning("⚠️ YT_COOKIES_B64 not set — YouTube may block downloads.")
        return None
    try:
        cookies_bytes = base64.b64decode(cookies_b64)
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="yt_cookies_")
        with os.fdopen(fd, "wb") as f:
            f.write(cookies_bytes)
        logger.info("✅ YouTube cookies loaded.")
        return path
    except Exception as e:
        logger.warning(f"Failed to decode cookies: {e}")
        return None


# ---------- DOWNLOAD ----------
def download_audio(url: str, allow_playlist: bool) -> list:
    """Download as MP3 using original titles. Returns list of file paths."""
    session_dir = os.path.join(DOWNLOAD_DIR, str(uuid.uuid4()))
    os.makedirs(session_dir, exist_ok=True)

    cookies_path = write_cookies_file()

    ydl_opts = {
        "outtmpl": os.path.join(session_dir, "%(title)s.%(ext)s"),
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": not allow_playlist,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "ignoreerrors": allow_playlist,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android", "ios"],
            }
        },
    }

    if cookies_path:
        ydl_opts["cookiefile"] = cookies_path

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
    finally:
        if cookies_path:
            safe_remove(cookies_path)

    mp3_files = []
    for name in sorted(os.listdir(session_dir)):
        if name.lower().endswith(".mp3"):
            mp3_files.append(os.path.join(session_dir, name))

    if not mp3_files:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise RuntimeError(
            "No audio file was produced. This usually means YouTube blocked the "
            "download (cookies expired or missing) or the URL is invalid."
        )

    return mp3_files


# ---------- HANDLERS ----------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("mode", None)
    keyboard = [
        [
            InlineKeyboardButton("🎵 Single Song", callback_data="mode_single"),
            InlineKeyboardButton("📃 Playlist", callback_data="mode_playlist"),
        ]
    ]
    await update.message.reply_text(
        "👋 *Welcome!* What would you like to download?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 *How to Use:*\n"
        "1. Send /start\n"
        "2. Choose *Single Song* or *Playlist*\n"
        "3. Send the YouTube link\n"
        "4. I'll download it as MP3 with the original title\n\n"
        "_Only public content is supported._",
        parse_mode="Markdown",
    )


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "mode_single":
        context.user_data["mode"] = MODE_SINGLE
        await query.edit_message_text(
            "🎵 *Single Song mode.*\nNow send me a YouTube video link.",
            parse_mode="Markdown",
        )
    elif query.data == "mode_playlist":
        context.user_data["mode"] = MODE_PLAYLIST
        await query.edit_message_text(
            "📃 *Playlist mode.*\nNow send me a YouTube playlist link.\n"
            "_(Large playlists can take a while.)_",
            parse_mode="Markdown",
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    url = update.message.text.strip()
    mode = context.user_data.get("mode")

    if not is_valid_url(url):
        await update.message.reply_text(
            "❌ That doesn't look like a valid link. Use /start to pick a mode."
        )
        return

    if not mode:
        await update.message.reply_text(
            "ℹ️ Please use /start first to choose Single Song or Playlist."
        )
        return

    allow_playlist = (mode == MODE_PLAYLIST)
    status = await update.message.reply_text("⏳ Downloading audio...")

    files = []
    try:
        files = await asyncio.wait_for(
            asyncio.to_thread(download_audio, url, allow_playlist),
            timeout=DOWNLOAD_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        await status.edit_text(
            "❌ Download timed out. Try a shorter video or smaller playlist."
        )
        return
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        low = msg.lower()
        if "sign in to confirm" in low or "not a bot" in low:
            reply = (
                "🔐 *YouTube blocked this download.*\n\n"
                "The server's IP is being flagged by YouTube. The bot owner needs "
                "to refresh the `YT_COOKIES_B64` environment variable with fresh "
                "browser cookies."
            )
        elif "private video" in low:
            reply = "🔒 This video is private."
        elif "age" in low and "restrict" in low:
            reply = "🔞 Age-restricted content."
        elif "unsupported url" in low:
            reply = "❌ This link is not supported."
        elif "ffmpeg" in low:
            reply = "⚙️ Server is missing ffmpeg. Redeploy with the Dockerfile."
        elif "video unavailable" in low or "not available" in low:
            reply = "🚫 Video unavailable (removed or region-locked)."
        else:
            reply = f"❌ Download failed:\n`{msg[:300]}`"
        await status.edit_text(reply, parse_mode="Markdown")
        return
    except Exception as e:
        logger.exception("Download error")
        await status.edit_text(
            f"❌ Error: `{type(e).__name__}: {str(e)[:250]}`",
            parse_mode="Markdown",
        )
        return

    total = len(files)
    await status.edit_text(f"📤 Sending {total} file(s)...")

    sent = 0
    failed = 0
    for path in files:
        try:
            mb = human_mb(path)
            title = os.path.splitext(os.path.basename(path))[0]

            if mb > MAX_TELEGRAM_SIZE_MB:
                await update.message.reply_text(
                    f"⚠️ Skipping *{title}* — {mb:.1f} MB (over 50 MB limit).",
                    parse_mode="Markdown",
                )
                failed += 1
                continue

            with open(path, "rb") as f:
                await update.message.reply_audio(
                    audio=f,
                    title=title,
                    read_timeout=180,
                    write_timeout=180,
                )
            sent += 1

        except TelegramError as e:
            logger.warning(f"Telegram send failed for {path}: {e}")
            failed += 1
        except Exception:
            logger.exception(f"Send error for {path}")
            failed += 1
        finally:
            safe_remove(path)

    if files:
        session_dir = os.path.dirname(files[0])
        shutil.rmtree(session_dir, ignore_errors=True)

    try:
        await status.delete()
    except Exception:
        pass

    summary = f"✅ Sent {sent} file(s)."
    if failed:
        summary += f" ⚠️ {failed} skipped/failed."
    await update.message.reply_text(summary)

    cleanup_dir_old_files()


# ---------- GLOBAL ERROR HANDLER ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)
    if isinstance(context.error, (NetworkError, TimedOut)):
        return
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ Something went wrong. Please try again."
            )
        except Exception:
            pass


# ---------- MAIN ----------
def main():
    if not check_ffmpeg():
        logger.warning(
            "⚠️ ffmpeg not found on PATH. MP3 conversion will fail. "
            "Ensure your Dockerfile installs ffmpeg."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(mode_callback, pattern="^mode_"))
    app.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    )
    app.add_error_handler(error_handler)

    logger.info("🤖 Music Downloader Bot is running...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
