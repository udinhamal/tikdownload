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
    CallbackQueryHandler,
    ContextTypes,
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
COMPRESS_MAX_MB = int(os.getenv("COMPRESS_MAX_MB", 45))
CRF = os.getenv("CRF", "28")
MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", 720))

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN tidak diset.")

HELP_TEXT = (
    "Kirim link TikTok ke bot ini.\n\n"
    "• Klik tombol ⬇️ Video / 🎵 Audio\n"
    "• /audio – kirim audio (MP3) dari link terakhir\n"
    "Catatan: Unduh hanya konten yang kamu punya haknya."
)

# Rate limit per user (window 60s)
user_windows: dict[int, deque] = defaultdict(lambda: deque(maxlen=50))
WINDOW_SECONDS = 60
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
            await update.effective_message.reply_text("Bot ini mode admin-only. Akses ditolak.")
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

async def send_upload_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress(Exception):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)

def file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)

def ensure_yt_dlp():
    try:
        import yt_dlp  # noqa: F401
        return True
    except Exception:
        return False

# =====================
# Handlers
# =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text("Halo! Kirim link TikTok lalu pilih Video/Audio. /help untuk bantuan.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(HELP_TEXT)

def extract_tiktok_url(text: str):
    m = TIKTOK_URL_RE.search(text or "")
    return m.group(0) if m else None

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    url = extract_tiktok_url(update.message.text or "")
    if not url:
        await update.message.reply_text("Kirim tautan TikTok yang valid ya.")
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
    q = update.callback_query
    await q.answer()
    data = (q.data or "")
    if data.startswith("dl|"):
        await download_and_send(q, context, data[3:], audio_only=False)
    elif data.startswith("au|"):
        await download_and_send(q, context, data[3:], audio_only=True)

async def audio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    uid = update.effective_user.id
    url = last_url.get(uid)
    if not url:
        await update.message.reply_text("Kirim video dulu supaya aku tahu sumber audionya.")
        return
    await download_and_send(update, context, url, audio_only=True)

# =====================
# Core download/send
# =====================
async def download_and_send(update_or_query, context: ContextTypes.DEFAULT_TYPE, url: str, audio_only=False):
    if not ensure_yt_dlp():
        target = update_or_query.message if hasattr(update_or_query, "message") else update_or_query
        await target.reply_text("Server error: yt-dlp tidak tersedia.")
        return

    await send_upload_action(update_or_query, context)

    # Import di sini agar error mudah dibaca
    import yt_dlp

    # Ekstrak info
    info_opts = {
        "format": "bv*+ba/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(info_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    title = (info.get("title") or "tiktok_video").strip().replace("/", "-")
    tmpdir = tempfile.mkdtemp(prefix="ttdl_")
    base = os.path.join(tmpdir, title[:80])

    target = update_or_query.message if hasattr(update_or_query, "message") else update_or_query

    if audio_only:
        # Download audio → MP3
        audio_opts = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            ],
            "outtmpl": base + ".%(ext)s",
        }
        with yt_dlp.YoutubeDL(audio_opts) as ydl:
            ydl.extract_info(url, download=True)
        final_path = base + ".mp3"
        await target.reply_audio(audio=open(final_path, "rb"), caption=title[:990])
        return

    # Download video
    out_path = base + ".mp4"
    dl_opts = {
        "format": "bv*+ba/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "outtmpl": out_path,
    }
    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        ydl.download([url])

    # Compress jika perlu
    final_path = await compress_if_needed(out_path, COMPRESS_MAX_MB, CRF, MAX_HEIGHT)

    # Jika masih kebesaran, kirim direct link
    size_mb = file_size_mb(final_path)
    if size_mb > COMPRESS_MAX_MB:
        direct_url = info.get("url")
        if not direct_url and info.get("formats"):
            best = max(info["formats"], key=lambda f: f.get("height", 0))
            direct_url = best.get("url")
        if direct_url:
            await target.reply_text(f"Ukuran file ({size_mb:.1f} MB) masih besar. Unduh langsung:\n{direct_url}")
            return

    await target.reply_video(video=open(final_path, "rb"), caption=title[:990], supports_streaming=True)

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
        return out_path if file_size_mb(out_path) < size else in_path
    except Exception:
        return in_path

# =====================
# Bootstrap (SYNC, no asyncio.run)
# =====================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("audio", audio_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # POLLING saja. Jangan pakai asyncio.run, jangan pakai await di sini.
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
