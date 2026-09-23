import os
import uuid
import subprocess
import asyncio
import logging
import shutil
import time
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.error import TelegramError, NetworkError, TimedOut
import yt_dlp

# ---------- LOGGING ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable is not set!")

DOWNLOAD_DIR = "./downloads"
LOG_FILE = "logs.txt"
MAX_TELEGRAM_SIZE_MB = 50          # Telegram bot upload limit
MAX_DOWNLOAD_SIZE_MB = 500         # Hard cap: refuse to download larger files
DOWNLOAD_TIMEOUT_SEC = 300         # 5 minutes per download
COMPRESS_TIMEOUT_SEC = 300         # 5 minutes per compression attempt
ALLOWED_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ---------- HELPERS ----------
def log_download(url: str, filename: str, size_mb: float):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"URL: {url}\nFile: {filename}\nSize: {size_mb:.2f} MB\n---\n")
    except Exception as e:
        logger.warning(f"Could not write to log file: {e}")


def safe_remove(path: str):
    """Silently remove a file if it exists."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning(f"Failed to remove {path}: {e}")


def cleanup_dir_old_files(max_age_sec: int = 3600):
    """Remove leftover files older than 1 hour (safety net)."""
    now = time.time()
    try:
        for name in os.listdir(DOWNLOAD_DIR):
            p = os.path.join(DOWNLOAD_DIR, name)
            if os.path.isfile(p) and now - os.path.getmtime(p) > max_age_sec:
                safe_remove(p)
    except Exception as e:
        logger.warning(f"Cleanup failed: {e}")


def check_ffmpeg() -> bool:
    """Check if ffmpeg is available on PATH."""
    return shutil.which("ffmpeg") is not None


def is_valid_url(text: str) -> bool:
    if not text:
        return False
    text = text.strip()
    return text.startswith(("http://", "https://")) and " " not in text


# ---------- DOWNLOAD ----------
def download_video(url: str) -> str:
    """Download a video from URL. Returns path to file. Raises on failure."""
    unique_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f"{unique_id}.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,            # refuse playlists; only single video
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "max_filesize": MAX_DOWNLOAD_SIZE_MB * 1024 * 1024,
        "restrictfilenames": True,
        "nocheckcertificate": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError("Could not extract any information from the link.")

        # Find the actual downloaded file
        file_path = None
        if "requested_downloads" in info and info["requested_downloads"]:
            file_path = info["requested_downloads"][0].get("filepath")
        if not file_path or not os.path.exists(file_path):
            for name in os.listdir(DOWNLOAD_DIR):
                if name.startswith(unique_id):
                    file_path = os.path.join(DOWNLOAD_DIR, name)
                    break

    if not file_path or not os.path.exists(file_path):
        raise FileNotFoundError("Downloaded file could not be located.")

    # Verify extension
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        if check_ffmpeg():
            converted = os.path.join(DOWNLOAD_DIR, f"{unique_id}.mp4")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", file_path, "-c", "copy", converted],
                    check=True, timeout=COMPRESS_TIMEOUT_SEC,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                safe_remove(file_path)
                file_path = converted
            except Exception as e:
                logger.warning(f"Container conversion failed: {e}")
        else:
            safe_remove(file_path)
            raise RuntimeError("Unsupported file format and ffmpeg is not installed.")

    # Size check before returning
    size_bytes = os.path.getsize(file_path)
    if size_bytes > MAX_DOWNLOAD_SIZE_MB * 1024 * 1024:
        safe_remove(file_path)
        raise RuntimeError(
            f"File is too large to handle (max {MAX_DOWNLOAD_SIZE_MB} MB)."
        )

    return file_path


# ---------- COMPRESS ----------
def compress_video(file_path: str) -> str:
    """Compress to fit Telegram's 50 MB limit. Returns path (may be same file)."""
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if size_mb <= MAX_TELEGRAM_SIZE_MB:
        return file_path

    if not check_ffmpeg():
        return file_path  # caller will handle "still too big" case

    compressed_path = file_path.rsplit(".", 1)[0] + "_compressed.mp4"

    # Escalating compression attempts
    for crf, scale in [("28", None), ("32", "1280:-2"), ("36", "854:-2")]:
        try:
            cmd = ["ffmpeg", "-y", "-i", file_path, "-vcodec", "libx264", "-crf", crf]
            if scale:
                cmd += ["-vf", f"scale={scale}"]
            cmd += ["-preset", "veryfast", "-movflags", "+faststart", compressed_path]

            subprocess.run(
                cmd,
                check=True,
                timeout=COMPRESS_TIMEOUT_SEC,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            new_mb = os.path.getsize(compressed_path) / (1024 * 1024)
            logger.info(f"Compression attempt (crf={crf}) -> {new_mb:.2f} MB")

            if new_mb <= MAX_TELEGRAM_SIZE_MB:
                safe_remove(file_path)
                return compressed_path

            safe_remove(compressed_path)

        except subprocess.TimeoutExpired:
            logger.warning(f"Compression timed out (crf={crf})")
            safe_remove(compressed_path)
        except subprocess.CalledProcessError as e:
            logger.warning(f"Compression failed (crf={crf}): {e}")
            safe_remove(compressed_path)
        except Exception as e:
            logger.warning(f"Unexpected compression error: {e}")
            safe_remove(compressed_path)

    return file_path  # nothing worked


# ---------- HANDLERS ----------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Send me a video link (YouTube, Instagram, etc.), "
        "and I’ll download and send it back as MP4."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📌 *How to Use:*\n"
        "1. Send a single video link (YouTube, Instagram Reel, etc.)\n"
        "2. I’ll download it (MP4) and send it back\n"
        "3. If it's over 50 MB, I'll try to compress it\n\n"
        "*Limits:*\n"
        f"• Max original size: {MAX_DOWNLOAD_SIZE_MB} MB\n"
        f"• Max upload size: {MAX_TELEGRAM_SIZE_MB} MB (Telegram limit)\n"
        "• Playlists are not supported\n"
        "• Private/age-restricted videos may fail\n\n"
        "_Note: Only public content is supported._"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    url = update.message.text.strip()

    # --- Validate URL ---
    if not is_valid_url(url):
        await update.message.reply_text(
            "❌ That doesn't look like a valid link.\n"
            "Please send a URL starting with http:// or https://"
        )
        return

    status_msg = await update.message.reply_text("⏳ Downloading video...")

    file_path = None
    try:
        # --- Download (run in thread so bot stays responsive) ---
        file_path = await asyncio.wait_for(
            asyncio.to_thread(download_video, url),
            timeout=DOWNLOAD_TIMEOUT_SEC,
        )

        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        await status_msg.edit_text(f"📦 Downloaded ({size_mb:.2f} MB). Checking size...")

        # --- Compress if needed ---
        if size_mb > MAX_TELEGRAM_SIZE_MB:
            await status_msg.edit_text(
                f"🗜️ File is {size_mb:.2f} MB — compressing to fit Telegram's 50 MB limit..."
            )
            file_path = await asyncio.wait_for(
                asyncio.to_thread(compress_video, file_path),
                timeout=COMPRESS_TIMEOUT_SEC * 4,
            )

        final_mb = os.path.getsize(file_path) / (1024 * 1024)

        if final_mb > MAX_TELEGRAM_SIZE_MB:
            await status_msg.edit_text(
                f"❌ Sorry, even after compression the file is {final_mb:.1f} MB, "
                f"which exceeds Telegram's 50 MB bot limit. Try a shorter video."
            )
            return

        # --- Send ---
        await status_msg.edit_text(f"📤 Uploading ({final_mb:.2f} MB)...")
        with open(file_path, "rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
            )

        log_download(url, os.path.basename(file_path), final_mb)
        try:
            await status_msg.delete()
        except Exception:
            pass

    except asyncio.TimeoutError:
        await status_msg.edit_text(
            "❌ The download took too long and timed out. Please try a shorter video or a different link."
        )

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Private video" in msg or "private" in msg.lower():
            user_msg = "🔒 This video is private. I can only download public content."
        elif "age" in msg.lower() and "restrict" in msg.lower():
            user_msg = "🔞 This video is age-restricted and can't be downloaded."
        elif "not available" in msg.lower() or "unavailable" in msg.lower():
            user_msg = "🚫 This video is unavailable (removed or region-locked)."
        elif "Unsupported URL" in msg:
            user_msg = "❌ This website/link is not supported."
        elif "max-filesize" in msg.lower() or "too large" in msg.lower():
            user_msg = f"📦 The file is too large (limit: {MAX_DOWNLOAD_SIZE_MB} MB)."
        else:
            user_msg = "❌ Download failed. The link may be invalid or unsupported."
        await status_msg.edit_text(user_msg)

    except subprocess.CalledProcessError:
        await status_msg.edit_text(
            "❌ Video processing (ffmpeg) failed. The file may be corrupted."
        )

    except FileNotFoundError as e:
        await status_msg.edit_text(f"❌ {str(e)}")

    except TelegramError as e:
        logger.error(f"Telegram API error: {e}")
        try:
            await status_msg.edit_text(
                "❌ Telegram refused the upload. The file may be too large or in an unsupported format."
            )
        except Exception:
            pass

    except Exception as e:
        logger.exception("Unexpected error in handle_message")
        try:
            await status_msg.edit_text(f"❌ Unexpected error: {type(e).__name__}")
        except Exception:
            pass

    finally:
        if file_path:
            safe_remove(file_path)
        cleanup_dir_old_files()


# ---------- GLOBAL ERROR HANDLER ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)
    if isinstance(context.error, (NetworkError, TimedOut)):
        return  # transient, ignore
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ Something went wrong while processing your request. Please try again."
            )
        except Exception:
            pass


# ---------- MAIN ----------
def main():
    if not check_ffmpeg():
        logger.warning(
            "⚠️ ffmpeg not found on PATH. Compression and merging will fail."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    )
    app.add_error_handler(error_handler)

    logger.info("🤖 Video Downloader Bot is running...")
    # run_polling is SYNCHRONOUS — do NOT await it, do NOT wrap in asyncio.run
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
