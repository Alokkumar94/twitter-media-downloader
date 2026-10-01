import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp

# --- Render Dummy Web Server ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running successfully!")

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

def build_quality_buttons(post_id, formats, direct_url=None):
    video_formats = []
    seen_heights = set()
    for f in formats:
        height = f.get("height")
        proto = f.get("protocol", "")
        if height and height not in seen_heights:
            if "m3u8" not in proto:
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

    # Direct Download Link Button agar URL uplabdh ho
    if direct_url:
        buttons.append([InlineKeyboardButton("🚀 Direct Fast Download Link", url=direct_url)])

    return InlineKeyboardMarkup(buttons) if buttons else None

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    text = (
        "👋 **Namaste! Twitter/X Media Downloader Bot mein aapka swagat hai.**\n\n"
        "Mujhe kisi bhi Twitter/X post ka link bhejein. Main media ke saath captions, "
        "multiple qualities aur badi files ke liye Direct Download Link provide karunga!\n\n"
        "⚡ Send your link to start."
    )
    await message.reply_text(text)

@app.on_message(filters.text & ~filters.command(["start", "help"]))
async def handle_twitter_url(client: Client, message: Message):
    url = message.text.strip()
    
    if not ("twitter.com" in url or "x.com" in url):
        await message.reply_text("❌ Kripya valid Twitter/X post ka link bhejein.")
        return

    status_msg = await message.reply_text("🔍 **Post analyze ki ja rahi hai...**")

    loop = asyncio.get_running_loop()
    
    def extract():
        ydl_opts = {'quiet': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await loop.run_in_executor(None, extract)
        post_id = info.get("id", str(message.id))
        
        # Best direct stream URL nikalna
        direct_url = info.get("url")
        if not direct_url and info.get("formats"):
            direct_url = info["formats"][-1].get("url")

        URL_CACHE[post_id] = {
            "url": url,
            "info": info,
            "direct_url": direct_url
        }

        markup = build_quality_buttons(post_id, info.get("formats", []), direct_url)

        if markup:
            await status_msg.edit_text(
                "🎬 **Media mil gaya!**\n\nTelegram par paane ke liye quality chunein ya **Direct Fast Download Link** se turant download karein:",
                reply_markup=markup
            )
        else:
            await status_msg.edit_text("⏳ **Download shuru ho raha hai...**")
            await process_download(client, message.chat.id, post_id, "best", status_msg)

    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)}")

@app.on_callback_query(filters.regex(r"^quality_menu\|"))
async def quality_menu_handler(client: Client, callback_query: CallbackQuery):
    _, post_id = callback_query.data.split("|")
    cached = URL_CACHE.get(post_id)
    if not cached:
        await callback_query.answer("⚠️ Link expire ho gaya, dubara post link bhejein.", show_alert=True)
        return

    markup = build_quality_buttons(post_id, cached["info"].get("formats", []), cached.get("direct_url"))
    if markup:
        await callback_query.message.reply_text(
            "🎬 **Quality chunein:**",
            reply_markup=markup
        )
        await callback_query.answer()
    else:
        await callback_query.answer("Koi alag quality uplabdh nahi hai.", show_alert=True)

@app.on_callback_query(filters.regex(r"^dl\|"))
async def callback_download(client: Client, callback_query: CallbackQuery):
    _, post_id, format_id = callback_query.data.split("|")
    cached = URL_CACHE.get(post_id)
    
    if not cached:
        await callback_query.answer("⚠️️ Link expire ho gaya. Kripya dubara bhejein.", show_alert=True)
        return

    await callback_query.answer("Processing...")
    status_msg = await callback_query.message.edit_text("⏳ **Downloading... thoda intezar karein.**")
    
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
    
    ydl_opts = {
        'format': f'{format_id}+bestaudio/best' if format_id != 'best' else 'best',
        'outtmpl': f'{out_dir}/%(id)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
    }

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

        # File size check (Agar file 400 MB se badi ho toh direct link provide karein)
        file_size_bytes = os.path.getsize(filename) if os.path.exists(filename) else 0
        file_size_mb = file_size_bytes / (1024 * 1024)

        if file_size_mb > 400:
            os.remove(filename)
            buttons = []
            if direct_url:
                buttons.append([InlineKeyboardButton("🚀 Direct Fast Download (Browser)", url=direct_url)])
            await status_msg.edit_text(
                f"⚠️️ **File Size Bada Hai ({file_size_mb:.1f} MB)**\n\n"
                "Server memory limit ki wajah se 400MB+ files seedhe browser se download karna behtar hai. Neeche diye gaye link par click karke direct download karein:",
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

        # Inline button jisme quality aur direct link dono ka option rahega
        btn_rows = [
            [InlineKeyboardButton("Download with different quality 📥", callback_data=f"quality_menu|{post_id}")]
        ]
        if direct_url:
            btn_rows.append([InlineKeyboardButton("🚀 Direct Fast Download Link", url=direct_url)])
        reply_markup = InlineKeyboardMarkup(btn_rows)

        if filename.endswith(('.jpg', '.jpeg', '.png', '.webp')):
            await client.send_photo(chat_id=chat_id, photo=filename, caption=final_caption, reply_markup=reply_markup)
        elif filename.endswith(('.mp4', '.mkv', '.webm', '.mov')):
            await client.send_video(chat_id=chat_id, video=filename, caption=final_caption, reply_markup=reply_markup, supports_streaming=True)
        elif filename.endswith('.gif'):
            await client.send_animation(chat_id=chat_id, animation=filename, caption=final_caption, reply_markup=reply_markup)
        else:
            await client.send_document(chat_id=chat_id, document=filename, caption=final_caption, reply_markup=reply_markup)

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ Download fail ho gaya: {str(e)}")

    finally:
        if os.path.exists(out_dir):
            for f in os.listdir(out_dir):
                try:
                    os.remove(os.path.join(out_dir, f))
                except:
                    pass
            try:
                os.rmdir(out_dir)
            except:
                pass

if __name__ == "__main__":
    print("Bot start ho raha hai...")
    app.run()
    
