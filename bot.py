import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp

# --- Render ke liye Dummy Web Server (Port scan timeout fix) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running successfully!")

    def log_message(self, format, *args):
        return  # Server ke faltu logs chupane ke liye

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()
# ----------------------------------------------------------------

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

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    text = (
        "👋 **Namaste! Twitter/X Media Downloader Bot mein aapka swagat hai.**\n\n"
        "Mujhe kisi bhi Twitter/X post ka link bhejein, aur main uski Photos, GIFs ya Videos "
        "aapke pasandida resolution (Quality) mein download karke de dunga!\n\n"
        "⚡ Send your Twitter/X link to start."
    )
    await message.reply_text(text)

@app.on_message(filters.text & ~filters.command(["start", "help"]))
async def handle_twitter_url(client: Client, message: Message):
    url = message.text.strip()
    
    if not ("twitter.com" in url or "x.com" in url):
        await message.reply_text("❌ Kripya valid Twitter/X post ka link bhejein.")
        return

    status_msg = await message.reply_text("🔍 Post check ki ja rahi hai, kripya intezar karein...")

    loop = asyncio.get_running_loop()
    
    def extract():
        ydl_opts = {'quiet': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await loop.run_in_executor(None, extract)
        post_id = info.get("id", str(message.id))
        URL_CACHE[post_id] = url

        formats = info.get("formats", [])
        video_formats = []
        
        seen_heights = set()
        for f in formats:
            height = f.get("height")
            proto = f.get("protocol", "")
            if height and height not in seen_heights:
                if "m3u8" not in proto:
                    seen_heights.add(height)
                    video_formats.append(f)

        if video_formats:
            video_formats.sort(key=lambda x: x.get("height", 0), reverse=True)
            buttons = []
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

            await status_msg.edit_text(
                "🎬 **Video mil gayi!**\n\nKripya apni pasand ki quality chunein:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        else:
            await status_msg.edit_text("⏳ Download ho raha hai...")
            await process_download(client, message.chat.id, post_id, "best", status_msg)

    except Exception as e:
        await status_msg.edit_text(f"❌ Error aaya post fetch karte waqt: {str(e)}")

@app.on_callback_query(filters.regex(r"^dl\|"))
async def callback_download(client: Client, callback_query: CallbackQuery):
    _, post_id, format_id = callback_query.data.split("|")
    url = URL_CACHE.get(post_id)
    
    if not url:
        await callback_query.answer("⚠️ Link expire ho gaya. Kripya link dobara bhejein.", show_alert=True)
        return

    await callback_query.answer("Download shuru ho raha hai...")
    status_msg = await callback_query.message.edit_text("⏳ **Downloading media... kripya thoda wait karein.**")
    
    await process_download(
        client, 
        callback_query.message.chat.id, 
        post_id, 
        format_id, 
        status_msg
    )

async def process_download(client, chat_id, post_id, format_id, status_msg):
    url = URL_CACHE.get(post_id)
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
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return filename, info

    try:
        filename, info = await loop.run_in_executor(None, run_dl)
        
        if not os.path.exists(filename):
            files = [os.path.join(out_dir, f) for f in os.listdir(out_dir)]
            if files:
                filename = files[0]

        await status_msg.edit_text("⬆️ **Telegram par upload ho raha hai...**")
        
        caption = "✅ **Downloaded by @MyTwitterXDownloader_bot**"

        if filename.endswith(('.jpg', '.jpeg', '.png', '.webp')):
            await client.send_photo(chat_id=chat_id, photo=filename, caption=caption)
        elif filename.endswith(('.mp4', '.mkv', '.webm', '.mov')):
            await client.send_video(chat_id=chat_id, video=filename, caption=caption, supports_streaming=True)
        elif filename.endswith('.gif'):
            await client.send_animation(chat_id=chat_id, animation=filename, caption=caption)
        else:
            await client.send_document(chat_id=chat_id, document=filename, caption=caption)

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
        
