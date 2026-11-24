import os
import re
import shutil
import asyncio
import threading
import json
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
import yt_dlp
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# --- Render / Back4app Dummy Web Server ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active!")

    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()
# ------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")

URL_CACHE = {}
ACTIVE_TASKS = {}  # {user_id: {"task": asyncio.Task, "status_msg": Message}}

# Standard Telegram Bot API upload limit is 50 MB
MAX_UPLOAD_MB = 50

SECRET_COOKIE_PATH = "/etc/secrets/cookies.txt"
WRITABLE_COOKIE_PATH = "/tmp/cookies.txt"
COOKIE_FILE = None

if os.path.exists(SECRET_COOKIE_PATH):
    try:
        shutil.copyfile(SECRET_COOKIE_PATH, WRITABLE_COOKIE_PATH)
        COOKIE_FILE = WRITABLE_COOKIE_PATH
        print("✅ Secret cookies copied to /tmp/cookies.txt", flush=True)
    except Exception as e:
        print(f"⚠️ Cookie copy error: {e}", flush=True)
elif os.path.exists("cookies.txt"):
    COOKIE_FILE = "cookies.txt"


def get_ydl_options(extra_opts=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
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


def extract_tweet_details(url):
    match = re.search(r"(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)", url)
    if match:
        return match.group(1), match.group(2)
    return None, None


def fetch_photos_via_fxtwitter(username, tweet_id):
    try:
        api_url = f"https://api.fxtwitter.com/{username}/status/{tweet_id}"
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

        tweet = data.get("tweet", {})
        media_list = tweet.get("media", {}).get("photos", [])
        photos = [p.get("url") for p in media_list if p.get("url")]
        text = tweet.get("text", "")
        author = tweet.get("author", {})

        return {
            "photos": photos,
            "text": text,
            "author_name": author.get("name", username),
            "author_handle": author.get("screen_name", username),
        }
    except Exception as e:
        print(f"FxTwitter API error: {e}", flush=True)
        return None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Namaste! Main Twitter/X Media Downloader Bot hoon.*\n\n"
        "Mujhe kisi bhi Twitter/X post ka link bhejein, video ya photo download ho jayegi!\n\n"
        "Agar koi download beech me cancel karni ho, toh `/cancel` use karein.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cancel_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ACTIVE_TASKS:
        task_info = ACTIVE_TASKS[user_id]
        task = task_info.get("task")
        if task and not task.done():
            task.cancel()
        status_msg = task_info.get("status_msg")
        if status_msg:
            try:
                await status_msg.edit_text("❌ *Task cancel kar diya gaya hai.*", parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
        ACTIVE_TASKS.pop(user_id, None)
        await update.message.reply_text("✅ Aapka chal raha task cancel ho gaya hai. Ab aap naya link bhej sakte hain.")
    else:
        await update.message.reply_text("⚠️ Aapka koi active task nahi chal raha hai.")


async def handle_twitter_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    if user_id in ACTIVE_TASKS:
        await update.message.reply_text(
            "⚠️ *One Task Is Already Processing.*\n"
            "Wait for complete it. If you want to cancel this task then use - /cancel",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    url = update.message.text.strip()
    if not ("twitter.com" in url or "x.com" in url):
        await update.message.reply_text("❌ Kripya valid Twitter/X post ka link bhejein.")
        return

    task = asyncio.create_task(process_url(context.bot, update.message, url, user_id))
    ACTIVE_TASKS[user_id] = {"task": task, "status_msg": None}


async def process_url(bot, message, url: str, user_id: int):
    status_msg = await message.reply_text("🔍 *Post check ki ja rahi hai...*", parse_mode=ParseMode.MARKDOWN)
    if user_id in ACTIVE_TASKS:
        ACTIVE_TASKS[user_id]["status_msg"] = status_msg

    username, tweet_id = extract_tweet_details(url)
    loop = asyncio.get_running_loop()

    try:
        def extract_video():
            opts = get_ydl_options()
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = None
        try:
            info = await loop.run_in_executor(None, extract_video)
        except Exception:
            pass

        formats = info.get("formats", []) if info else []
        post_id = str(info.get("id") or tweet_id or message.message_id) if info else str(tweet_id or message.message_id)
        direct_url = get_best_mp4_url(formats) if formats else None
        has_video = any(f.get("vcodec") != "none" and f.get("vcodec") is not None for f in formats)

        if formats and has_video:
            URL_CACHE[post_id] = {
                "url": url,
                "info": info,
                "direct_url": direct_url,
            }
            markup = build_quality_buttons(post_id, formats, direct_url)
            if markup:
                await status_msg.edit_text(
                    "🎬 *Video mil gayi!*\n\nQuality chunein ya browser download link use karein:",
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                )
                ACTIVE_TASKS.pop(user_id, None)
                return

        # Photos fetch karein
        if username and tweet_id:
            img_res = await loop.run_in_executor(None, lambda: fetch_photos_via_fxtwitter(username, tweet_id))
            if img_res and img_res["photos"]:
                photos = img_res["photos"]
                author_handle = img_res["author_handle"]
                caption = (
                    f"{img_res['text'][:650]}\n\n"
                    f"𝕏 {img_res['author_name']} (@{author_handle})\n"
                    f"{url}\n"
                    f"🤖 @MyTwitterX_DL_bot"
                )

                if len(photos) == 1:
                    await bot.send_photo(chat_id=message.chat_id, photo=photos[0], caption=caption)
                else:
                    media_group = [
                        InputMediaPhoto(media=p, caption=caption if i == 0 else "")
                        for i, p in enumerate(photos[:10])
                    ]
                    await bot.send_media_group(chat_id=message.chat_id, media=media_group)

                await status_msg.delete()
                ACTIVE_TASKS.pop(user_id, None)
                return

        await status_msg.edit_text(
            "⚠️ *Notice:* Is tweet me koi video ya photo nahi mili (sirf text post ho sakti hai).",
            parse_mode=ParseMode.MARKDOWN,
        )

    except asyncio.CancelledError:
        pass
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)[:200]}")
    finally:
        ACTIVE_TASKS.pop(user_id, None)


async def quality_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, post_id = query.data.split("|")
    cached = URL_CACHE.get(post_id)
    if not cached:
        await query.answer("⚠️ Link expire ho gaya.", show_alert=True)
        return

    markup = build_quality_buttons(post_id, cached["info"].get("formats", []), cached.get("direct_url"))
    if markup:
        await query.message.reply_text("🎬 *Quality chunein:*", reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
        await query.answer()
    else:
        await query.answer("Koi aur quality uplabdh nahi hai.", show_alert=True)


async def callback_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if user_id in ACTIVE_TASKS:
        await query.answer("⚠️ Pehle chal raha download complete hone dein!", show_alert=True)
        return

    _, post_id, format_id = query.data.split("|")
    cached = URL_CACHE.get(post_id)

    if not cached:
        await query.answer("⚠️ Link expire ho gaya.", show_alert=True)
        return

    await query.answer("Processing start ho rahi hai...")

    menu_msg = query.message
    status_msg = await query.message.reply_text("⏳ *Check kiya ja raha hai...*", parse_mode=ParseMode.MARKDOWN)

    task = asyncio.create_task(
        process_download(context.bot, query.message.chat_id, post_id, format_id, status_msg, user_id, menu_msg)
    )
    ACTIVE_TASKS[user_id] = {"task": task, "status_msg": status_msg}


async def process_download(bot, chat_id, post_id, format_id, status_msg, user_id, menu_msg=None):
    cached = URL_CACHE.get(post_id)
    url = cached["url"]
    info = cached.get("info", {})
    formats = info.get("formats", [])
    default_direct_url = cached.get("direct_url")
    loop = asyncio.get_running_loop()

    out_dir = f"downloads/{post_id}_{user_id}"
    os.makedirs(out_dir, exist_ok=True)

    selected_format = None
    target_direct_url = default_direct_url

    if format_id != "best":
        for f in formats:
            if str(f.get("format_id")) == str(format_id):
                selected_format = f
                if f.get("url") and ".m3u8" not in f.get("url", ""):
                    target_direct_url = f.get("url")
                break
    else:
        mp4_formats = [f for f in formats if f.get("ext") == "mp4" or f.get("vcodec") != "none"]
        if mp4_formats:
            mp4_formats.sort(key=lambda x: (x.get("height") or 0), reverse=True)
            selected_format = mp4_formats[0]
            if selected_format.get("url") and ".m3u8" not in selected_format.get("url", ""):
                target_direct_url = selected_format.get("url")

    # PRE-CHECK: Download karne se pehle hi size check karo
    if selected_format:
        est_size = selected_format.get("filesize") or selected_format.get("filesize_approx") or 0
        est_mb = est_size / (1024 * 1024) if est_size else 0
        height = selected_format.get("height") or 0

        if (est_mb > MAX_UPLOAD_MB) or (height >= 1080 and (est_mb > MAX_UPLOAD_MB or est_mb == 0)):
            display_size = f"{est_mb:.1f} MB" if est_mb > 0 else f"{height}p High Quality"
            buttons = []
            if target_direct_url and ".m3u8" not in target_direct_url:
                buttons.append([InlineKeyboardButton("🚀 Direct MP4 Download (Browser)", url=target_direct_url)])

            await status_msg.edit_text(
                f"⚠️ *File Size Bada Hai ({display_size})*\n\n"
                f"Bot API limit ({MAX_UPLOAD_MB} MB) se badi files direct upload nahi hoti.\n"
                f"Kripya direct high-speed browser se download karein:",
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
                parse_mode=ParseMode.MARKDOWN,
            )
            shutil.rmtree(out_dir, ignore_errors=True)
            ACTIVE_TASKS.pop(user_id, None)
            return

    ydl_opts = get_ydl_options({
        "format": f"{format_id}+bestaudio/best" if format_id != "best" else "best",
        "outtmpl": f"{out_dir}/%(id)s.%(ext)s",
    })

    def run_dl():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            dl_info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(dl_info)
            return filename, dl_info

    try:
        await status_msg.edit_text("⏳ *Twitter se download ho raha hai...*", parse_mode=ParseMode.MARKDOWN)
        filename, dl_info = await loop.run_in_executor(None, run_dl)

        if not os.path.exists(filename):
            files = [os.path.join(out_dir, f) for f in os.listdir(out_dir)]
            if files:
                filename = files[0]

        file_size_bytes = os.path.getsize(filename) if os.path.exists(filename) else 0
        file_size_mb = file_size_bytes / (1024 * 1024)

        if file_size_mb > MAX_UPLOAD_MB:
            if os.path.exists(filename):
                os.remove(filename)
            buttons = []
            if target_direct_url and ".m3u8" not in target_direct_url:
                buttons.append([InlineKeyboardButton("🚀 Direct MP4 Download (Browser)", url=target_direct_url)])
            await status_msg.edit_text(
                f"⚠️ *File Size Bada Hai ({file_size_mb:.1f} MB)*\n\n"
                f"{MAX_UPLOAD_MB} MB se badi file hai. Direct browser se download karein:",
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await status_msg.edit_text("⬆️ *Telegram par upload ho raha hai...*", parse_mode=ParseMode.MARKDOWN)

        description = dl_info.get("description") or dl_info.get("title") or ""
        uploader = dl_info.get("uploader") or dl_info.get("uploader_id") or ""
        uploader_id = dl_info.get("uploader_id", "")

        caption_parts = []
        if description:
            caption_parts.append(description[:700])

        if uploader:
            handle = f" (@{uploader_id})" if uploader_id and uploader_id != uploader else f" (@{uploader})"
            caption_parts.append(f"\n𝕏 {uploader}{handle}\n{url}")

        caption_parts.append("\n🤖 @MyTwitterX_DL_bot")
        final_caption = "\n".join(caption_parts)

        btn_rows = [
            [InlineKeyboardButton("Download with different quality 📥", callback_data=f"quality_menu|{post_id}")]
        ]
        if target_direct_url and ".m3u8" not in target_direct_url:
            btn_rows.append([InlineKeyboardButton("🚀 Direct Fast Download Link", url=target_direct_url)])

        reply_markup = InlineKeyboardMarkup(btn_rows)

        with open(filename, "rb") as f_obj:
            if filename.endswith((".mp4", ".mkv", ".webm", ".mov")):
                await bot.send_video(
                    chat_id=chat_id,
                    video=f_obj,
                    caption=final_caption,
                    reply_markup=reply_markup,
                    supports_streaming=True,
                )
            elif filename.endswith(".gif"):
                await bot.send_animation(
                    chat_id=chat_id,
                    animation=f_obj,
                    caption=final_caption,
                    reply_markup=reply_markup,
                )
            else:
                await bot.send_document(
                    chat_id=chat_id,
                    document=f_obj,
                    caption=final_caption,
                    reply_markup=reply_markup,
                )

        await status_msg.delete()

        if menu_msg:
            try:
                await menu_msg.delete()
            except Exception:
                pass

    except asyncio.CancelledError:
        await status_msg.edit_text("❌ Download cancel kar diya gaya.")
    except Exception as e:
        await status_msg.edit_text(f"❌ Download fail: {str(e)[:250]}")
    finally:
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)
        ACTIVE_TASKS.pop(user_id, None)


import time

def main():
    while True:
        try:
            print("Bot start ho raha hai (HTTP Bot API Mode)...", flush=True)
            application = (
                Application.builder()
                .token(BOT_TOKEN)
                .connect_timeout(30.0)
                .read_timeout(30.0)
                .write_timeout(30.0)
                .pool_timeout(30.0)
                .build()
            )

            application.add_handler(CommandHandler("start", start_command))
            application.add_handler(CommandHandler("cancel", cancel_task))
            application.add_handler(CallbackQueryHandler(quality_menu_handler, pattern=r"^quality_menu\|"))
            application.add_handler(CallbackQueryHandler(callback_download, pattern=r"^dl\|"))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_twitter_url))

            application.run_polling(drop_pending_updates=True)
            break
        except Exception as e:
            print(f"⚠️ Connection/Telegram Error: {e} — 5 second me retry kar raha hu...", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
    
