import os
import re
import glob
import shutil
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
import yt_dlp

# --- Render Dummy Web Server ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active!")

    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()
# -------------------------------

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

app = Client(
    "twitter_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

URL_CACHE = {}

SECRET_COOKIE_PATH = "/etc/secrets/cookies.txt"
WRITABLE_COOKIE_PATH = "/tmp/cookies.txt"
COOKIE_FILE = None

if os.path.exists(SECRET_COOKIE_PATH):
    try:
        shutil.copyfile(SECRET_COOKIE_PATH, WRITABLE_COOKIE_PATH)
        COOKIE_FILE = WRITABLE_COOKIE_PATH
        print("✅ Secret cookies copied to /tmp/cookies.txt")
    except Exception as e:
        print(f"⚠️ Cookie copy error: {e}")
elif os.path.exists("cookies.txt"):
    COOKIE_FILE = "cookies.txt"

def get_ydl_options(extra_opts=None):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    }
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        opts['cookiefile'] = COOKIE_FILE
    if extra_opts:
        opts.update(extra_opts)
    return opts

def get_best_mp4_url(formats):
    mp4_formats = []
    for f in formats:
        url = f.get("url", "")
        proto = f.get("protocol", "")
        if "m3u8" not in proto and ".m3u8" not in url:
            if f.get("ext") == "mp4" or f.get("vcodec") != "none":
                mp4_formats.append(f)
    
    if mp4_formats:
        mp4_formats.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)
        return mp4_formats[0].get("url")
    return None

def build_quality_buttons(post_id, formats, direct_url=None):
    video_formats = []
    seen_heights = set()
    for f in formats:
        height = f.get("height")
        proto = f.get("protocol", "")
        if height and height not in seen_heights:
            if "m3u8" not in proto and ".m3u8" not in f.get("url", ""):
                seen_heights.add(height)
                video_formats.append(f)

    buttons = []
    if video_formats:
        video_formats.sort(key=lambda x: x.get("height", 0), reverse=True)
        row = []
        for f in video_formats:
            h = f.get("height")
            btn_text = f"🎬 {h}p"
            cb_data = f"dl|{post_id}|{f['format_id']}"
            row.append(InlineKeyboardButton(btn_text, callback_data=cb_data))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        
        buttons.append([InlineKeyboardButton("✨ Best Quality", callback_data=f"dl|{post_id}|best")])

    if direct_url and ".m3u8" not in direct_url:
        buttons.append([InlineKeyboardButton("🚀 Direct Fast Download Link", url=direct_url)])

    return InlineKeyboardMarkup(buttons) if buttons else None

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    await message.reply_text(
        "👋 **Namaste! Main Twitter/X Media Downloader Bot hoon.**\n\n"
        "Mujhe kisi bhi Twitter/X post ka link bhejein, video ya photo download ho jayegi!"
    )

@app.on_message(filters.text & ~filters.command(["start", "help"]))
async def handle_twitter_url(client: Client, message: Message):
    url = message.text.strip()
    
    if not ("twitter.com" in url or "x.com" in url):
        await message.reply_text("❌ Kripya valid Twitter/X post ka link bhejein.")
        return

    status_msg = await message.reply_text("🔍 **Post check ki ja rahi hai...**")
    loop = asyncio.get_running_loop()

    # Pehle video check karein
    def extract_video():
        opts = get_ydl_options()
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await loop.run_in_executor(None, extract_video)
        formats = info.get("formats", []) if info else []
        post_id = str(info.get("id") or message.id)
        direct_url = get_best_mp4_url(formats)

        has_video = any(f.get("vcodec") != "none" and f.get("vcodec") is not None for f in formats)

        if formats and has_video:
            URL_CACHE[post_id] = {
                "url": url,
                "info": info,
                "direct_url": direct_url
            }

            markup = build_quality_buttons(post_id, formats, direct_url)
            if markup:
                await status_msg.edit_text(
                    "🎬 **Video mil gayi!**\n\nQuality chunein ya browser download link use karein:",
                    reply_markup=markup
                )
                return
    except Exception:
        pass

    # Video nahi hai -> yt-dlp ke through thumbnail/image extract karein (authenticated)
    img_dir = f"downloads/img_{message.id}"
    os.makedirs(img_dir, exist_ok=True)

    def extract_image_via_ytdlp():
        opts = get_ydl_options({
            'skip_download': True,
            'writethumbnail': True,
            'outtmpl': f'{img_dir}/photo.%(ext)s',
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        meta = await loop.run_in_executor(None, extract_image_via_ytdlp)
        
        # Metadata se high-res photos nikaalein
        photos = []
        if meta:
            # 1. Check thumbnails list
            for t in meta.get("thumbnails", []):
                t_url = t.get("url", "")
                if "media" in t_url or "twimg.com" in t_url:
                    # Clean high-res format
                    t_url = re.sub(r"&name=\w+", "&name=large", t_url)
                    if t_url not in photos:
                        photos.append(t_url)

        description = (meta.get("description") or meta.get("title") or "")[:650]
        uploader = meta.get("uploader") or meta.get("uploader_id") or ""
        caption = f"{description}\n\n𝕏 **{uploader}**\n🤖 @MyTwitterXDownloader_bot"

        if photos:
            # Aakhri (best quality) photo bhejein
            await client.send_photo(chat_id=message.chat.id, photo=photos[-1], caption=caption)
            await status_msg.delete()
            return

    except Exception as e:
        print(f"Fallback extraction error: {e}")

    finally:
        if os.path.exists(img_dir):
            shutil.rmtree(img_dir, ignore_errors=True)

    await status_msg.edit_text("⚠️ **Notice:** Tweet media fetch nahi ho saka (Post sirf text ho sakti hai).")

@app.on_callback_query(filters.regex(r"^quality_menu\|"))
async def quality_menu_handler(client: Client, callback_query: CallbackQuery):
    _, post_id = callback_query.data.split("|")
    cached = URL_CACHE.get(post_id)
    if not cached:
        await callback_query.answer("⚠️ Link expire ho gaya.", show_alert=True)
        return

    markup = build_quality_buttons(post_id, cached["info"].get("formats", []), cached.get("direct_url"))
    if markup:
        await callback_query.message.reply_text("🎬 **Quality chunein:**", reply_markup=markup)
        await callback_query.answer()
    else:
        await callback_query.answer("Koi aur quality uplabdh nahi hai.", show_alert=True)

@app.on_callback_query(filters.regex(r"^dl\|"))
async def callback_download(client: Client, callback_query: CallbackQuery):
    _, post_id, format_id = callback_query.data.split("|")
    cached = URL_CACHE.get(post_id)
    
    if not cached:
        await callback_query.answer("⚠️ Link expire ho gaya.", show_alert=True)
        return

    await callback_query.answer("Downloading...")
    status_msg = await callback_query.message.edit_text("⏳ **Download ho raha hai...**")
    
    await process_download(
        client, 
        callback_query.message.chat.id, 
        post_id, 
        format_id, 
        status_msg
    )

async def process_download(client, chat_id, post_id, format_id, status_msg):
    cached = URL_CACHE.get(post_id)
    url = cached["url"]
    direct_url = cached.get("direct_url")
    loop = asyncio.get_running_loop()
    
    out_dir = f"downloads/{post_id}"
    os.makedirs(out_dir, exist_ok=True)
    
    ydl_opts = get_ydl_options({
        'format': f'{format_id}+bestaudio/best' if format_id != 'best' else 'best',
        'outtmpl': f'{out_dir}/%(id)s.%(ext)s',
    })

    def run_dl():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            dl_info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(dl_info)
            return filename, dl_info

    try:
        filename, dl_info = await loop.run_in_executor(None, run_dl)
        
        if not os.path.exists(filename):
            files = [os.path.join(out_dir, f) for f in os.listdir(out_dir)]
            if files:
                filename = files[0]

        file_size_bytes = os.path.getsize(filename) if os.path.exists(filename) else 0
        file_size_mb = file_size_bytes / (1024 * 1024)

        if file_size_mb > 400:
            os.remove(filename)
            buttons = []
            if direct_url and ".m3u8" not in direct_url:
                buttons.append([InlineKeyboardButton("🚀 Direct MP4 Download (Browser)", url=direct_url)])
            await status_msg.edit_text(
                f"⚠️ **File Size Bada Hai ({file_size_mb:.1f} MB)**\n\nDirect browser se download karein:",
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else None
            )
            return

        await status_msg.edit_text("⬆️ **Telegram par upload ho raha hai...**")
        
        description = dl_info.get("description") or dl_info.get("title") or ""
        uploader = dl_info.get("uploader") or dl_info.get("uploader_id") or ""
        uploader_id = dl_info.get("uploader_id", "")
        
        caption_parts = []
        if description:
            caption_parts.append(description[:700])
        
        if uploader:
            handle = f" (@{uploader_id})" if uploader_id and uploader_id != uploader else ""
            caption_parts.append(f"\n𝕏 **{uploader}{handle}**")
            
        caption_parts.append("\n🤖 @MyTwitterXDownloader_bot")
        final_caption = "\n".join(caption_parts)

        btn_rows = [
            [InlineKeyboardButton("Download with different quality 📥", callback_data=f"quality_menu|{post_id}")]
        ]
        if direct_url and ".m3u8" not in direct_url:
            btn_rows.append([InlineKeyboardButton("🚀 Direct Fast Download Link", url=direct_url)])
            
        reply_markup = InlineKeyboardMarkup(btn_rows)

        if filename.endswith(('.mp4', '.mkv', '.webm', '.mov')):
            await client.send_video(chat_id=chat_id, video=filename, caption=final_caption, reply_markup=reply_markup, supports_streaming=True)
        elif filename.endswith('.gif'):
            await client.send_animation(chat_id=chat_id, animation=filename, caption=final_caption, reply_markup=reply_markup)
        else:
            await client.send_document(chat_id=chat_id, document=filename, caption=final_caption, reply_markup=reply_markup)

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ Download fail: {str(e)[:250]}")

    finally:
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)

if __name__ == "__main__":
    print("Bot start ho raha hai...")
    app.run()
