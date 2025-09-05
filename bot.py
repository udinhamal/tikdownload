import asyncio
import os
import re
import tempfile
import time
import subprocess
from collections import defaultdict, deque
from contextlib import suppress

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
)

# =====================
# Config & Globals
# =====================
TIKTOK_URL_RE = re.compile(r"https?://(www\.)?(vm\.|vt\.|m\.)?tiktok\.com/[\w\-/?=&%.]+", re.IGNORECASE)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in (os.getenv("ADMIN_IDS") or "").replace(" ", "").split(",") if x}
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", 5))
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "false").lower() == "true"
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))
LISTEN_ADDR = os.getenv("LISTEN_ADDR", "0.0.0.0")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
COMPRESS_MAX_MB = int(os.getenv("COMPRESS_MAX_MB", 45))
CRF = os.getenv("CRF", "28")
MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", 720))

if not BOT_TOKEN:
    raise SystemExit("Error: BOT_TOKEN is not set. Put it in .env or environment variables.")

HELP_TEXT = (
    "Kirimkan link TikTok ke bot ini.\n\n"
    "Perintah:\n"
    "• Kirim link → pilih Video/Audio\n"
    "• /audio – kirim audio (MP3) dari link terakhir\n"
    "• /help – bantuan\n\n"
    "Catatan: Hanya unduh konten yang Anda punya haknya."
)

# Rate limiter (per user): sliding window per 60s
user_windows: dict[int, deque] = defaultdict(lambda: deque(maxlen=50))
WINDOW_SECONDS = 60

# Simpan URL terakhir per user untuk tombol/command Audio only
last_url: dict[int, str] = {}

# =====================
# Helpers
# =====================
def is_allowed(user_id: int) -> bool:
    return (not ADMIN_IDS) or (user_id in ADMIN_IDS)

async def guard(update: Update) -> bool:
    if update.effective_user is None:
        return False
    uid = update.effective_user.id
    if not is_allowed(uid):
        with suppress(Exception):
            await update.effective_message.reply_text("Bot dalam mode admin-only. Akses ditolak.")
        return False
    # rate limit
    now = time.time()
    dq = user_windows[uid]
    while dq and now - dq[0] > WINDOW_SECONDS:
        dq.popleft()
    if len(dq) >= RATE_LIMIT_PER_MIN:
        with suppress(Exception):
            await update.effective_message.reply_text("Terlalu banyak permintaan. Coba lagi sebentar.")
        return False
    dq.append(now)
    return True

async def send_typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress(Exception):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)

def file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)

# =====================
# yt-dlp wrappers
# =====================
def ensure_yt_dlp():
    try:
        import yt_dlp  # noqa: F401
        return True
    except Exception:
        return False

async def extract_info(url: str):
    import yt_dlp
    opts = {
        "format": "bv*+ba/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

async def download_best(url: str, outpath: str):
    import yt_dlp
    opts = {
        "format": "bv*+ba/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "skip_download": False,
        "outtmpl": outpath,
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

async def download_audio(url: str, outpath_no_ext: str) -> str:
    import yt_dlp
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
        ],
        "outtmpl": outpath_no_ext + ".%(ext)s",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return outpath_no_ext + ".mp3"

# =====================
# Compression
# =====================
async def compress_if_needed(in_path: str, max_mb: int, crf: str, max_h: int) -> str:
    size = file_size_mb(in_path)
    if size <= max_mb:
        return in_path
    out_path = in_path.rsplit(".", 1)[0] + f".crf{crf}.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", in_path,
        "-vf", f"scale='min(iw,iw*{max_h}/ih)':min({max_h},ih):force_original_aspect_ratio=decrease",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if file_size_mb(out_path) < size:
            return out_path
        return in_path
    except Exception:
        return in_path

# =====================
# Handlers
# =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(
        "Halo! Kirim link TikTok lalu pilih Video/Audio. /help untuk info.\n"
        "Tips: jangan spam, ada rate limit."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(HELP_TEXT)

def extract_tiktok_url(text: str):
    match = re.search(r"https?://(www\.)?(vm\.|vt\.|m\.)?tiktok\.com/[\w\-/?=&%.]+", text)
    return match.group(0) if match else None

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    text = update.message.text.strip()
    url = extract_tiktok_url(text)
    if not url:
        await update.message.reply_text("Kirim tautan TikTok yang valid ya. /help untuk contoh.")
        return
    last_url[update.effective_user.id] = url
    kb = InlineKeyboardMarkup.from_row([
        InlineKeyboardButton("⬇️ Video", callback_data=f"dl|{url}"),
        InlineKeyboardButton("🎵 Audio", callback_data=f"au|{url}"),
    ])
    await update.message.reply_text("Pilih opsi:", reply_markup=kb)

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("dl|"):
        url = data[3:]
        await download_and_send(update, context, url, audio_only=False)
    elif data.startswith("au|"):
        url = data[3:]
        await download_and_send(update, context, url, audio_only=True)

async def audio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    uid = update.effective_user.id
    url = last_url.get(uid)
    if not url:
        await update.message.reply_text("Kirim video TikTok dulu agar aku tahu sumber audionya.")
        return
    await download_and_send(update, context, url, audio_only=True)

async def download_and_send(update_or_query, context: ContextTypes.DEFAULT_TYPE, url: str, audio_only: bool = False):
    if not ensure_yt_dlp():
        await (update_or_query.message if hasattr(update_or_query, "message") else update_or_query).reply_text(
            "Server error: yt-dlp tidak tersedia."
        )
        return

    # Route convenience for Query vs Message
    reply_target = update_or_query.message if hasattr(update_or_query, "message") else update_or_query
    await send_typing(update_or_query, context)

    try:
        info = await extract_info(url)
        title = (info.get("title") or "tiktok_video").strip().replace("/", "-")
        tmpdir = tempfile.mkdtemp(prefix="ttdl_")
        base = os.path.join(tmpdir, title[:80])

        if audio_only:
            final_path = await download_audio(url, base)
            await reply_target.reply_audio(audio=open(final_path, "rb"), caption=title[:990])
            return

        out = base + ".mp4"
        await download_best(url, out)
        final_path = await compress_if_needed(out, COMPRESS_MAX_MB, CRF, MAX_HEIGHT)
        size_mb = file_size_mb(final_path)

        if size_mb > COMPRESS_MAX_MB:
            # fallback: send direct URL (best we can) if still too large
            direct_url = info.get("url")
            if not direct_url and info.get("formats"):
                best = max(info["formats"], key=lambda f: f.get("height", 0))
                direct_url = best.get("url")
            if direct_url:
                await reply_target.reply_text(
                    f"Ukuran file ({size_mb:.1f} MB) masih besar. Unduh langsung:\n{direct_url}"
                )
                return

        await reply_target.reply_video(
            video=open(final_path, "rb"),
            caption=title[:990],
            supports_streaming=True,
        )

    except Exception as e:
        msg = str(e)
        if "private" in msg.lower() or "403" in msg or "410" in msg:
            await reply_target.reply_text("Video tidak tersedia (private/geo/age restricted).")
        else:
            await reply_target.reply_text(f"Gagal: {msg[:400]}")

# =====================
# App bootstrap (Polling/Webhook)
# =====================
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("audio", audio_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if USE_WEBHOOK:
        if not WEBHOOK_URL:
            raise SystemExit("USE_WEBHOOK=true tapi WEBHOOK_URL kosong")
        await app.initialize()
        await app.start()
        await app.bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        await app.start_webhook(
            listen=LISTEN_ADDR,
            port=PORT,
            secret_token=WEBHOOK_SECRET,
            url_path=WEBHOOK_URL.split("/")[-1] if WEBHOOK_URL else None,
        )
        await asyncio.Event().wait()
    else:
        await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    import asyncio
    asyncio.get_event_loop().run_until_complete(main())
