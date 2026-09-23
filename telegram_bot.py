import os
import uuid
import subprocess
import asyncio
import nest_asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import yt_dlp

nest_asyncio.apply()

BOT_TOKEN = '8512021280:AAEzC_BwI6KQM8OUwE88fSVTyc-CStuiahA'  # Replace with your token

DOWNLOAD_DIR = "./downloads"
LOG_FILE = "logs.txt"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def log_download(url, filename, size_mb):
    with open(LOG_FILE, "a") as f:
        f.write(f"URL: {url}\nFile: {filename}\nSize: {size_mb:.2f} MB\n---\n")

def compress_video(file_path):
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if size_mb <= 50:
        return file_path
    compressed_path = file_path.replace(".mp4", "_compressed.mp4")
    subprocess.run([
        "ffmpeg", "-i", file_path, "-vcodec", "libx264", "-crf", "28", compressed_path
    ], check=True)
    os.remove(file_path)
    return compressed_path

def download_video(url: str) -> str:
    unique_id = str(uuid.uuid4())
    output_file = os.path.join(DOWNLOAD_DIR, f"{unique_id}.mp4")
    ydl_opts = {
        'outtmpl': output_file,
        'format': 'best',
        'merge_output_format': 'mp4',
        'quiet': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    return output_file

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send me a video link (YouTube, Instagram, etc.), and I’ll download and send it back.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📌 *How to Use:*\n"
        "1. Send a public video link (Instagram Reel/Post, YouTube, etc.)\n"
        "2. I’ll download it for you (compressed if needed)\n"
        "3. You’ll receive the video here\n\n"
        "_Note: Private Instagram content is not supported without login_"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    await update.message.reply_text("⏳ Downloading video...")

    try:
        file_path = download_video(url)
        file_path = compress_video(file_path)
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        log_download(url, os.path.basename(file_path), size_mb)

        await update.message.reply_video(video=open(file_path, 'rb'))
        os.remove(file_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    print("🤖 Instagram Video Bot running locally...")
    await app.run_polling()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if "already running" in str(e):
            loop = asyncio.get_event_loop()
            loop.create_task(main())
            loop.run_forever()
        else:
            raise
