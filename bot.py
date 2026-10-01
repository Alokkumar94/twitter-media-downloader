import os
import asyncio
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

API_ID = int(os.environ.get("API_ID", ""))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

app = Client(
    "twitter_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

url_cache = {}

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    text = (
        f"👋 Namaste {message.from_user.first_name}!\n\n"
        "Main **Twitter (X) HD Media Downloader Bot** hu.\n\n"
        "📹 Videos (Alag-alag quality: 1080p, 720p, 480p)\n"
        "🖼️ Photos (Original High Quality)\n"
        "🎞️ GIFs\n\n"
        "Bas kisi bhi Twitter/X post ka link bhejein!"
    )
    await message.reply_text(text)

@app.on_message(filters.text & filters.private)
async def process_twitter_link(client: Client, message: Message):
    url = message.text.strip()

    if not ("twitter.com" in url or "x.com" in url):
        await message.reply_text("❌ Kripya sirf valid Twitter / X post link bhejein!")
        return

    status_msg = await message.reply_text("🔍 Post check kiya ja raha hai...")

    loop = asyncio.get_event_loop()

    def get_info():
        ydl_opts = {'quiet': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await loop.run_in_executor(None, get_info)
        post_id = info.get("id", str(message.id))
        url_cache[post_id] = {
            "url": url,
            "title": info.get("title", "Twitter Media"),
            "user_id": message.from_user.id
        }

        formats = info.get("formats", [])
        video_formats = []

        seen_heights = set()
        for f in formats:
            height = f.get("height")
            format_id = f.get("format_id")
            if height and height not in seen_heights:
                seen_heights.add(height)
                video_formats.append((height, format_id))

        if video_formats:
            video_formats.sort(key=lambda x: x[0], reverse=True)
            buttons = []
            row = []
            for height, f_id in video_formats:
                row.append(InlineKeyboardButton(f"🎬 {height}p", callback_data=f"dl_{post_id}_{f_id}_{height}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            
            buttons.append([InlineKeyboardButton("✨ Best Quality (Auto)", callback_data=f"dl_{post_id}_best_auto")])

            reply_markup = InlineKeyboardMarkup(buttons)
            await status_msg.edit_text(
                f"📌 **Title:** {info.get('title', 'Twitter Post')[:80]}...\n\n"
                "👇 Kis quality me download karna chahte hain choose karein:",
                reply_markup=reply_markup
            )
        else:
            await status_msg.edit_text("🖼️ High Quality Photo detect hui hai, download ho raha hai...")
            await download_and_send(client, message.chat.id, url, "best", info.get("title", "Twitter Media"), status_msg)

    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)[:120]}")

@app.on_callback_query(filters.regex(r"^dl_"))
async def handle_download_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data.split("_")
    post_id = data[1]
    format_id = data[2]
    quality_label = data[3] if len(data) > 3 else "Best"

    post_data = url_cache.get(post_id)
    if not post_data:
        await callback_query.answer("⚠️ Link expire ho gaya, dubara link bhejein.", show_alert=True)
        return

    await callback_query.answer(f"📥 {quality_label} download start...")
    status_msg = callback_query.message

    format_str = "best" if format_id == "best" else format_id
    await status_msg.edit_text(f"⏳ **{quality_label}** download ho raha hai...")
    
    await download_and_send(
        client,
        callback_query.message.chat.id,
        post_data["url"],
        format_str,
        post_data["title"],
        status_msg
    )

async def download_and_send(client, chat_id, url, format_str, title, status_msg):
    loop = asyncio.get_event_loop()
    output_template = f"downloads/{chat_id}_%(id)s_%(format_id)s.%(ext)s"

    ydl_opts = {
        'format': f"{format_str}+bestaudio/best" if format_str != "best" else "best",
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
    }

    try:
        def execute_dl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                return filename, info.get('ext', '')

        file_path, ext = await loop.run_in_executor(None, execute_dl)

        if not os.path.exists(file_path):
            base, _ = os.path.splitext(file_path)
            for possible_ext in ['mp4', 'mkv', 'webm', 'jpg', 'jpeg', 'png', 'webp']:
                test_path = f"{base}.{possible_ext}"
                if os.path.exists(test_path):
                    file_path = test_path
                    ext = possible_ext
                    break

        if os.path.exists(file_path):
            await status_msg.edit_text("📤 Telegram par send kiya ja raha hai...")
            caption_text = f"🎬 **{title}**\n\nDownloaded via @{client.me.username}"

            if ext.lower() in ['mp4', 'mkv', 'webm', 'mov']:
                await client.send_video(
                    chat_id=chat_id,
                    video=file_path,
                    caption=caption_text,
                    supports_streaming=True
                )
            elif ext.lower() in ['jpg', 'jpeg', 'png', 'webp']:
                await client.send_photo(
                    chat_id=chat_id,
                    photo=file_path,
                    caption=caption_text
                )
            else:
                await client.send_document(
                    chat_id=chat_id,
                    document=file_path,
                    caption=caption_text
                )

            await status_msg.delete()
            os.remove(file_path)
        else:
            await status_msg.edit_text("❌ Media download fail ho gaya.")

    except Exception as e:
        await status_msg.edit_text(f"❌ Download error: {str(e)[:120]}")
        if 'file_path' in locals() and os.path.exists(file_path):
            os.remove(file_path)

if __name__ == "__main__":
    if not os.path.exists("downloads"):
        os.makedirs("downloads")
    print("HQ Bot is running...")
    app.run()
  
