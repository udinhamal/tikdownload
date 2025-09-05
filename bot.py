import os
import re
import time
import asyncio
import tempfile
import subprocess
import threading
from dotenv import load_dotenv
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# === Load Env ===
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))

# === Rate limit tracker ===
last_used = {}

# === Flask healthcheck server ===
health_app = Flask(__name__)

@health_app.route("/")
def home():
    return "✅ Bot is running!"

def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    health_app.run(host="0.0.0.0", port=port, threaded=True)

# === Command Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Halo! Kirim link TikTok.\n\n"
        "Pilih format setelah kirim link:\n"
        "🎬 Video (MP4)\n"
        "🎵 Audio (MP3)\n"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Cara Pakai Bot:*\n"
        "1. Kirim link TikTok.\n"
        "2. Pilih tombol 🎬 Video atau 🎵 Audio.\n\n"
        "📌 Catatan:\n"
        "- Video besar akan dikompres otomatis.\n"
        "- Tidak bisa download video private.\n",
        parse_mode="Markdown"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = time.time()

    # Admin-only check
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ Bot ini hanya untuk admin.")
        return

    # Rate limit check
    if user_id in last_used and now - last_used[user_id] < RATE_LIMIT_SECONDS:
        await update.message.reply_text("⚠️ Tunggu sebentar sebelum request lagi.")
        return
    last_used[user_id] = now

    url = extract_tiktok_url(update.message.text)
    if not url:
        await update.message.reply_text("❌ Kirim link TikTok valid.")
        return

    keyboard = [
        [
            InlineKeyboardButton("🎬 Video", callback_data=f"video|{url}"),
            InlineKeyboardButton("🎵 Audio", callback_data=f"audio|{url}")
        ]
    ]
    await update.message.reply_text("Pilih opsi:", reply_markup=InlineKeyboardMarkup(keyboard))

def extract_tiktok_url(text: str):
    match = re.search(r"https?://(www\.)?(vm\.|vt\.|m\.)?tiktok\.com/[\w\-/?=&%.]+", text)
    return match.group(0) if match else None

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode, url = query.data.split("|", 1)
    if mode == "video":
        await download_and_send(query, context, url, audio_only=False)
    else:
        await download_and_send(query, context, url, audio_only=True)

async def download_and_send(target, context: ContextTypes.DEFAULT_TYPE, url: str, audio_only=False):
    await target.message.reply_text("⏳ Sedang memproses...")

    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = os.path.join(tmpdir, "output.mp4")
        ydl_opts = {
            "outtmpl": file_path,
            "quiet": True,
            "noplaylist": True,
        }
        if audio_only:
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
            file_path = file_path.replace(".mp4", ".mp3")
            ydl_opts["outtmpl"] = file_path
        else:
            ydl_opts["format"] = "mp4/best"

        try:
            import yt_dlp
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            # Compress if too large
            MAX_SIZE = 45 * 1024 * 1024
            if os.path.getsize(file_path) > MAX_SIZE and not audio_only:
                compressed = file_path.replace(".mp4", "_compressed.mp4")
                subprocess.run([
                    "ffmpeg", "-i", file_path,
                    "-vf", "scale=720:-2", "-b:v", "800k", "-c:a", "aac", compressed
                ], check=True)
                file_path = compressed

            # Send file
            with open(file_path, "rb") as f:
                if audio_only:
                    await target.message.reply_audio(f)
                else:
                    await target.message.reply_video(f)
        except Exception as e:
            await target.message.reply_text(f"❌ Error: {e}")

# === Main ===
def main():
    # Start Flask in separate thread
    threading.Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("✅ Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
