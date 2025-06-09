#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import random
import re
from io import BytesIO
from threading import Thread
from functools import wraps

import datetime
import time

import emoji
from PIL import Image, ImageDraw, ImageFont
from PIL.Image import Dither
from aiohttp import web
import websockets

from playwright.async_api import async_playwright

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# --------------------------------------------------------------------
# Configuration & Globals
# --------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("No TELEGRAM_TOKEN set for the bot. Please set the environment variable.")

SERVER_IP = os.getenv("SERVER_IP")
if not SERVER_IP:
    # This will stop the script if the IP is not provided, preventing confusing errors later.
    raise ValueError("The SERVER_IP environment variable is not set!")


HTTP_PORT = 8000
WS_PORT = 8765

IMAGE_FILE = "image.png"   # The PNG served to M5Paper
image_available = False    # True if we have a new image to serve

# Modes – only one of friends_mode or xkcd_mode should be active.
update_duration_minutes = 30  # default update interval
friends_mode = False          # Friends mode flag
xkcd_mode = False             # XKCD mode flag
m5_battery_percent = 0        # Last reported battery percentage from M5

# M5Paper (portrait) resolution
MAX_WIDTH = 540
MAX_HEIGHT = 960

# Directory & font paths (files in the same directory as this script)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_TEXT_PATH = os.path.join(SCRIPT_DIR, "DejaVuSans.ttf")
FONT_EMOJI_PATH = os.path.join(SCRIPT_DIR, "NotoColorEmoji-Regular.ttf")

# Path to settings and Friends quotes files
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "settings.json")
FRIENDS_QUOTES_FILE = os.path.join(SCRIPT_DIR, "friends.json")

# New: XKCD preloading globals
PRELOADED_XKCD_FILE = os.path.join(SCRIPT_DIR, "xkcd_next.png")
preloaded_xkcd_image_ready = False

# Whitelist Configuration
ALLOWED_CHAT_IDS = {432856100, 1752631505}  # Replace with your allowed chat IDs

# Night Mode Configuration
NIGHT_START_HOUR = 22  # 10 PM
NIGHT_START_MINUTE = 30  # 30 minutes past the hour
MORNING_WAKE_HOUR = 6  # 6 AM
MORNING_WAKE_MINUTE = 30 # 30 minutes past the hour

def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat_id = None
        if update.effective_chat:
            chat_id = update.effective_chat.id
        elif update.callback_query and update.callback_query.message:
            chat_id = update.callback_query.message.chat.id

        if chat_id not in ALLOWED_CHAT_IDS:
            logger.warning("Unauthorized access denied for chat_id: %s", chat_id)
            if update.message:
                await update.message.reply_text("Unauthorized to use this bot.")
            elif update.callback_query and update.callback_query.message:
                await update.callback_query.message.reply_text("Unauthorized to use this bot.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped


async def render_html_to_image_bytes(html_content: str, width: int = MAX_WIDTH) -> bytes:
    """
    Renders HTML content to PNG image bytes using a headless browser.
    """
    async with async_playwright() as p:
        # Using chromium, but could be firefox or webkit
        browser = await p.chromium.launch()
        page = await browser.new_page()

        # Set the viewport to control the width of the rendered output
        await page.set_viewport_size({"width": width, "height": 100}) # Height is arbitrary

        await page.set_content(html_content)

        # Find the main container element to screenshot just that part
        element = await page.query_selector("#container")
        if not element:
            logger.error("Could not find #container element in headless browser.")
            await browser.close()
            return b'' # Return empty bytes on failure

        # Take a screenshot of just the element, returning the image data as bytes
        image_bytes = await element.screenshot(type="png")

        await browser.close()
        logger.info(f"Rendered {len(image_bytes)} bytes of PNG from HTML.")
        return image_bytes


# --------------------------------------------------------------------
# New Utility Function for Sleep Calculation
# --------------------------------------------------------------------
def get_next_sleep_duration_minutes():
    """
    Calculates the appropriate sleep duration in minutes.
    - During the day, it returns the user-configured duration.
    - During the night, it calculates the minutes until the next morning's wakeup time.
    """
    global update_duration_minutes
    now = datetime.datetime.now()

    # Define the start of night and the wake-up time for today
    night_start_time = now.replace(hour=NIGHT_START_HOUR, minute=NIGHT_START_MINUTE, second=0, microsecond=0)
    morning_wakeup_time = now.replace(hour=MORNING_WAKE_HOUR, minute=MORNING_WAKE_MINUTE, second=0, microsecond=0)

    # Check if we are in the "night" window (e.g., after 10 PM or before 6:30 AM)
    if now >= night_start_time or now < morning_wakeup_time:
        # We are in night mode, calculate sleep until the morning wakeup.

        # If it's already past the wakeup time today (e.g., it's 10 PM),
        # the target wakeup is tomorrow morning.
        if now > morning_wakeup_time:
            target_wakeup = morning_wakeup_time + datetime.timedelta(days=1)
        # Otherwise (e.g., it's 2 AM), the target wakeup is today.
        else:
            target_wakeup = morning_wakeup_time

        # Calculate the difference in seconds and convert to minutes
        delta_seconds = (target_wakeup - now).total_seconds()
        sleep_minutes = int(delta_seconds / 60) + 1  # Add 1 to ensure it wakes up just AFTER the target time

        logger.info(
            f"Night mode active. Scheduling wake-up in {sleep_minutes} minutes "
            f"(at {target_wakeup.strftime('%H:%M')})."
        )
        return sleep_minutes
    else:
        # We are in day mode, use the standard duration.
        logger.info(f"Day mode active. Using standard duration: {update_duration_minutes} minutes.")
        return update_duration_minutes


# --------------------------------------------------------------------
# Load/Save Settings
# --------------------------------------------------------------------
def load_settings():
    global update_duration_minutes, friends_mode, xkcd_mode, m5_battery_percent
    if not os.path.exists(SETTINGS_FILE):
        logger.info("No settings.json found; using defaults.")
        return
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            update_duration_minutes = data.get("update_duration_minutes", 30)
            friends_mode = data.get("friends_mode", False)
            xkcd_mode = data.get("xkcd_mode", False)
            m5_battery_percent = data.get("m5_battery_percent", 0)
        logger.info("Loaded settings: update_duration_minutes=%d, friends_mode=%s, xkcd_mode=%s, battery=%d%%",
                    update_duration_minutes, friends_mode, xkcd_mode, m5_battery_percent)
    except Exception as e:
        logger.warning("Failed to load settings.json: %s. Using defaults.", e)

def save_settings():
    data = {
        "update_duration_minutes": update_duration_minutes,
        "friends_mode": friends_mode,
        "xkcd_mode": xkcd_mode,
        "m5_battery_percent": m5_battery_percent
    }
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved settings: %s", data)
    except Exception as e:
        logger.error("Failed to save settings: %s", e)

# --------------------------------------------------------------------
# 1) Utility: Splitting text vs. emoji tokens
# --------------------------------------------------------------------
def split_text_and_emojis(line):
    tokens = []
    buffer = []
    for ch in line:
        if ch in emoji.EMOJI_DATA:
            if buffer:
                tokens.append(''.join(buffer))
                buffer = []
            tokens.append(ch)
        else:
            buffer.append(ch)
    if buffer:
        tokens.append(''.join(buffer))
    return tokens

# --------------------------------------------------------------------
# 2) Word-Wrapping & Rendering Text with Emojis
# --------------------------------------------------------------------
# def render_text_to_image(text,
#                          text_font_path=FONT_TEXT_PATH,
#                          emoji_font_path=FONT_EMOJI_PATH,
#                          img_width=MAX_WIDTH,
#                          font_size=24):
#     import io
#     img_height = 3000
#     base_img = Image.new('RGB', (img_width, img_height), (255, 255, 255))
#     draw = ImageDraw.Draw(base_img)
#
#     if os.path.exists(text_font_path):
#         text_font = ImageFont.truetype(text_font_path, font_size)
#     else:
#         text_font = ImageFont.load_default()
#     if os.path.exists(emoji_font_path):
#         emoji_font = ImageFont.truetype(emoji_font_path, font_size)
#     else:
#         emoji_font = text_font
#
#     ascent, descent = text_font.getmetrics()
#     line_height = ascent + descent
#
#     words = re.findall(r'\S+|\n|\s', text)
#     wrapped_lines = []
#     current_line = ""
#     for word in words:
#         if word == "\n":
#             wrapped_lines.append(current_line)
#             current_line = ""
#             continue
#         word_emoji = emoji.emojize(word, language='alias')
#         test_line = current_line + word_emoji
#         if draw.textlength(test_line, font=text_font) <= img_width:
#             current_line = test_line
#         else:
#             wrapped_lines.append(current_line)
#             current_line = word_emoji
#     if current_line.strip():
#         wrapped_lines.append(current_line)
#
#     total_height = line_height * len(wrapped_lines)
#     if total_height < 1:
#         total_height = line_height
#     if total_height > img_height:
#         img_height = total_height
#         base_img = Image.new('RGB', (img_width, img_height), (255, 255, 255))
#         draw = ImageDraw.Draw(base_img)
#
#     y_offset = 0
#     for line in wrapped_lines:
#         x_offset = 0
#         tokens = split_text_and_emojis(line)
#         for token in tokens:
#             use_font = emoji_font if any(ch in emoji.EMOJI_DATA for ch in token) else text_font
#             draw.text((x_offset, y_offset), token, font=use_font, fill=(0, 0, 0))
#             x_offset += draw.textlength(token, font=use_font)
#         y_offset += line_height
#
#     base_img = base_img.crop((0, 0, img_width, y_offset))
#     mono_img = base_img.convert("1", dither=Image.NONE)
#     out_stream = io.BytesIO()
#     out_stream.name = "text_image.png"
#     mono_img.save(out_stream, "PNG")
#     out_stream.seek(0)
#     return out_stream, mono_img.width, mono_img.height, mono_img


# --------------------------------------------------------------------
# 3) Photo Processing
# --------------------------------------------------------------------
def process_photo(image_data: bytes):
    global image_available
    try:
        with Image.open(BytesIO(image_data)) as img:
            # --- START: NEW FIX FOR TRANSPARENCY ---
            # If the image has an alpha (transparency) channel, like the PNGs
            # from our text renderer, we must flatten it onto a white background
            # before converting to grayscale, otherwise transparent areas become black.
            if 'A' in img.mode:
                logger.info("Image has transparency; flattening onto a white background.")
                # Create a new image with a white background (RGB mode is safe)
                background = Image.new("RGB", img.size, (255, 255, 255))
                # Paste the original image onto the background using its alpha channel as the mask
                background.paste(img, mask=img.split()[-1])
                img = background  # Replace the original with the new flattened image
            # --- END: NEW FIX FOR TRANSPARENCY ---

            w, h = img.size
            logger.info(f"Photo size: {w}x{h}")
            if w > h:
                logger.info("Rotating 90° for portrait.")
                img = img.transpose(Image.ROTATE_90)

            orig_w, orig_h = img.size
            scale = 540.0 / orig_w
            new_h = int(round(orig_h * scale))
            img = img.resize((540, new_h))

            # Convert to 8-bit grayscale first
            img = img.convert("L")

            # Quantize to a 16-color (4-bit) palette
            img = img.quantize(colors=16, dither=Dither.NONE)

            # Create the final canvas.
            final_img = Image.new("P", (540, 960))
            final_img.putpalette(img.getpalette())

            white_index = 0
            try:
                white_pixel = Image.new("L", (1, 1), color=255)
                white_pixel = white_pixel.quantize(palette=img)
                white_index = white_pixel.getpixel((0, 0))
            except Exception as e:
                logger.warning(f"Could not determine white index for palette, defaulting to 0. Error: {e}")

            final_img.paste(white_index, (0, 0, 540, 960))  # Fill background with white

            y_offset = (960 - new_h) // 2 if new_h < 960 else 0
            final_img.paste(img, (0, y_offset))

            final_img.save(IMAGE_FILE, "PNG", optimize=True, compress_level=9)

        image_available = True
        logger.info("Saved compressed photo -> %s, centered with y_offset=%d", IMAGE_FILE, y_offset)
    except Exception as e:
        logger.error("Error in process_photo: %s", e)

# --------------------------------------------------------------------
# 4) Text Processing
# --------------------------------------------------------------------
async def process_text_browser(message_text: str):
    global image_available
    # We use CSS for styling. We can embed fonts or just use system fonts.
    # Using 'pre-wrap' for whitespace preserves newlines from the user's message.
    html_template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            @font-face {{
                font-family: 'DejaVu Sans';
                src: url('file://{FONT_TEXT_PATH}');
            }}
            body {{
                margin: 0;
                font-family: 'DejaVu Sans', 'Noto Color Emoji', sans-serif;
                font-size: 28px; /* Control font size here */
                background-color: transparent; /* Transparent background for screenshot */
            }}
            #container {{
                display: inline-block; /* Shrink-wrap the content */
                padding: 20px; /* Add some margin */
                white-space: pre-wrap; /* Honor newlines and spaces */
                word-break: break-word; /* Break long words */
            }}
        </style>
    </head>
    <body>
        <div id="container">{message_text}</div>
    </body>
    </html>
    """

    try:
        # Step 1: Render HTML to an image in memory
        image_bytes = await render_html_to_image_bytes(html_template)
        if not image_bytes:
            raise Exception("Rendering HTML to bytes failed.")

        # Step 2: Process the resulting image for the M5Paper
        # We can reuse the photo processor!
        process_photo(image_bytes)

        # image_available is set by process_photo, so we don't need to set it here.

    except Exception as e:
        logger.error("Error in process_text_browser: %s", e)


# --------------------------------------------------------------------
# 5) Friends Quotes Processing
# --------------------------------------------------------------------
def process_friends_quote():
    global image_available
    try:
        with open(FRIENDS_QUOTES_FILE, "r", encoding="utf-8") as f:
            quotes = json.load(f)
        all_quotes = quotes.get("quotes", [])
        if not all_quotes:
            logger.error("No quotes in friends.json")
            return False
        quote = random.choice(all_quotes)
        dialogue = quote.get("dialogue", [])
        if not dialogue:
            logger.error("Selected quote has no dialogue")
            return False
        dialogue_lines = []
        for entry in dialogue:
            speaker = entry.get("speaker", "Unknown")
            text = entry.get("text", "")
            dialogue_lines.append(f"{speaker} - {text}")
        season = quote.get("season", "?")
        episode = quote.get("episode", "?")
        episode_title = quote.get("episode_title", "Untitled")
        footer_text = f"Season {season} - Episode {episode}: {episode_title}"
        img_width, img_height = 960, 540
        image = Image.new("L", (img_width, img_height), 255)
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.truetype(FONT_TEXT_PATH, 30)
        except Exception as e:
            logger.warning("Font not found, using default: %s", e)
            font = ImageFont.load_default()
        def wrap_text(text, font, max_width, draw):
            words = text.split()
            if not words:
                return []
            lines = []
            current_line = words[0]
            for w2 in words[1:]:
                test_line = current_line + " " + w2
                if draw.textsize(test_line, font=font)[0] <= max_width:
                    current_line = test_line
                else:
                    lines.append(current_line)
                    current_line = w2
            lines.append(current_line)
            return lines
        max_text_width = img_width - 60
        line_height = draw.textsize("Ay", font=font)[1]
        wrapped_dialogue = []
        for line in dialogue_lines:
            w_lines = wrap_text(line, font, max_text_width, draw)
            wrapped_dialogue.extend(w_lines)
        footer_w, footer_h = draw.textsize(footer_text, font=font)
        dialogue_block_height = len(wrapped_dialogue) * line_height
        footer_margin = 20
        available_height = img_height - footer_h - footer_margin
        dialogue_y_start = (available_height - dialogue_block_height) / 2
        for line in wrapped_dialogue:
            lw, _ = draw.textsize(line, font=font)
            x = (img_width - lw) / 2
            draw.text((x, dialogue_y_start), line, font=font, fill=0)
            dialogue_y_start += line_height
        footer_x = (img_width - footer_w) / 2
        footer_y = img_height - footer_h - footer_margin
        draw.text((footer_x, footer_y), footer_text, font=font, fill=0)
        final_img = image.rotate(90, expand=True)
        final_img.save(IMAGE_FILE, "PNG")
        image_available = True
        return True
    except Exception as e:
        logger.error("Error in process_friends_quote: %s", e)
        return False


# --------------------------------------------------------------------
# 6) XKCD Comic Processing - Preloading & Fallback
# --------------------------------------------------------------------
def pre_process_photo(image_data: bytes, output_file: str):
    try:
        with Image.open(BytesIO(image_data)) as img:
            # This logic is identical to the main process_photo function now
            w, h = img.size
            logger.info(f"XKCD photo size: {w}x{h}")
            if w > h:
                img = img.transpose(Image.ROTATE_90)
            orig_w, orig_h = img.size
            scale = 540.0 / orig_w
            new_h = int(round(orig_h * scale))
            img = img.resize((540, new_h))
            img = img.convert("L").quantize(colors=16, dither=Dither.NONE)

            final_img = Image.new("P", (540, 960))
            final_img.putpalette(img.getpalette())

            white_index = 0
            try:
                white_pixel = Image.new("L", (1, 1), color=255).quantize(palette=img)
                white_index = white_pixel.getpixel((0, 0))
            except Exception:
                pass

            final_img.paste(white_index, (0, 0, 540, 960))
            y_offset = (960 - new_h) // 2 if new_h < 960 else 0
            final_img.paste(img, (0, y_offset))

            final_img.save(output_file, "PNG", optimize=True, compress_level=9)
            logger.info("Preprocessed compressed XKCD image saved to %s", output_file)
        return True
    except Exception as e:
        logger.error("Error in pre_process_photo: %s", e)
        return False

def preload_xkcd_comic():
    global preloaded_xkcd_image_ready
    try:
        import urllib.request
        with urllib.request.urlopen("https://xkcd.com/info.0.json") as response:
            latest_data = json.loads(response.read().decode())
        latest_num = latest_data.get("num", 2500)
        random_num = random.randint(1, latest_num)
        url_info = f"https://xkcd.com/{random_num}/info.0.json"
        with urllib.request.urlopen(url_info) as response:
            comic_data = json.loads(response.read().decode())
        img_url = comic_data.get("img")
        if not img_url:
            logger.error("No image URL for XKCD comic %d", random_num)
            return False
        logger.info("Preloading XKCD comic %d, URL: %s", random_num, img_url)
        with urllib.request.urlopen(img_url) as response:
            img_data = response.read()
        if pre_process_photo(img_data, PRELOADED_XKCD_FILE):
            preloaded_xkcd_image_ready = True
            logger.info("Preloaded XKCD comic stored in %s", PRELOADED_XKCD_FILE)
            return True
        else:
            return False
    except Exception as e:
        logger.error("Error in preload_xkcd_comic: %s", e)
        return False

def process_xkcd_comic():
    try:
        import urllib.request
        with urllib.request.urlopen("https://xkcd.com/info.0.json") as response:
            latest_data = json.loads(response.read().decode())
        latest_num = latest_data.get("num", 2500)
        random_num = random.randint(1, latest_num)
        url_info = f"https://xkcd.com/{random_num}/info.0.json"
        with urllib.request.urlopen(url_info) as response:
            comic_data = json.loads(response.read().decode())
        img_url = comic_data.get("img")
        if not img_url:
            logger.error("No image URL for XKCD comic %d", random_num)
            return False
        logger.info("XKCD comic %d, URL: %s", random_num, img_url)
        with urllib.request.urlopen(img_url) as response:
            img_data = response.read()
        return pre_process_photo(img_data, IMAGE_FILE)
    except Exception as e:
        logger.error("Error in process_xkcd_comic: %s", e)
        return False

# --------------------------------------------------------------------
# 7) Telegram Bot Handlers
# --------------------------------------------------------------------
@restricted
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    welcome_text = (
        "Welcome!\n\n"
        "I can help you process photos and text for your M5Paper display.\n\n"
        "Commands:\n"
        "/start - Show welcome message\n"
        "/help - Show help message\n"
        "/chatid - Show your chat ID\n"
        "/settings - Set update interval / Show status\n"
        "/friends - Display a random Friends quote\n"
        "/xkcd - Display a random XKCD comic\n\n"
        f"Your chat ID: {chat_id}"
    )
    await update.message.reply_text(welcome_text)

@restricted
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Available commands:\n"
        "/start - Show welcome message\n"
        "/settings - Set update interval / Show status\n"
        "/help - Show help message\n"
        "/chatid - Show your chat ID\n"
        "/friends - Display a random Friends quote\n"
        "/xkcd - Display a random XKCD comic\n\n"
        "You may also send a photo or text message to create an image."
    )
    await update.message.reply_text(help_text)

async def chatid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"Your chat ID: {chat_id}")

@restricted
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global friends_mode, xkcd_mode
    friends_mode = False
    xkcd_mode = False
    save_settings()
    if update.message and update.message.photo:
        photo_obj = update.message.photo[-1]
        file_obj = await context.bot.get_file(photo_obj.file_id)
        bio = BytesIO()
        await file_obj.download_to_memory(bio)
        data = bio.getvalue()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, process_photo, data)
        await update.message.reply_text("Photo processed -> image.png")
    else:
        await update.message.reply_text("No photo found.")

@restricted
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global friends_mode, xkcd_mode
    friends_mode = False
    xkcd_mode = False
    save_settings()

    msg = update.message.text
    # No more run_in_executor needed since our whole chain is async now
    await process_text_browser(msg)
    await update.message.reply_text("Text rendered via browser -> image.png")

@restricted
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [
            InlineKeyboardButton("1 min",   callback_data="duration:1"),
            InlineKeyboardButton("5 min",   callback_data="duration:5"),
            InlineKeyboardButton("30 min",  callback_data="duration:30"),
        ],
        [
            InlineKeyboardButton("60 min",  callback_data="duration:60"),
            InlineKeyboardButton("120 min", callback_data="duration:120"),
        ],
        [
            InlineKeyboardButton("Show Status", callback_data="show_status")
        ]
    ]
    markup = InlineKeyboardMarkup(kb)
    await update.message.reply_text("Select update interval or Show Status:", reply_markup=markup)

@restricted
async def duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global update_duration_minutes
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("duration:"):
        try:
            val = int(data.split(":")[1])
            update_duration_minutes = val
            save_settings()
            logger.info("Update interval set to %d minutes", val)
            await query.edit_message_text(text=f"Update interval set to {val} minutes.")
        except:
            await query.edit_message_text(text="Invalid duration.")
    elif data == "show_status":
        status_text = (
            f"**Current Settings**\n"
            f"Update Interval: {update_duration_minutes} min\n"
            f"Friends Mode: {friends_mode}\n"
            f"XKCD Mode: {xkcd_mode}\n"
            f"M5 Battery: {m5_battery_percent}%\n"
        )
        await query.edit_message_text(text=status_text, parse_mode="Markdown")
    else:
        await query.edit_message_text(text="Unknown command.")

@restricted
async def friends_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global friends_mode, xkcd_mode
    friends_mode = True
    xkcd_mode = False
    save_settings()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, process_friends_quote)
    if result:
        await update.message.reply_text("Random Friends quote -> image.png")
    else:
        await update.message.reply_text("Failed to process Friends quote.")

@restricted
async def xkcd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global xkcd_mode, friends_mode, preloaded_xkcd_image_ready
    xkcd_mode = True
    friends_mode = False
    save_settings()
    loop = asyncio.get_event_loop()
    if not preloaded_xkcd_image_ready:
        result = await loop.run_in_executor(None, preload_xkcd_comic)
        if not result:
            await update.message.reply_text("Failed to preload XKCD comic.")
            return
    try:
        import shutil
        shutil.copyfile(PRELOADED_XKCD_FILE, IMAGE_FILE)
        logger.info("Copied preloaded XKCD comic to IMAGE_FILE.")
        preloaded_xkcd_image_ready = False
        loop.run_in_executor(None, preload_xkcd_comic)
    except Exception as e:
        logger.error("Error copying preloaded XKCD image: %s", e)
        await update.message.reply_text("Error using preloaded XKCD comic.")
        return
    await update.message.reply_text("Random XKCD comic -> image.png")

# --------------------------------------------------------------------
# 6) WebSocket & HTTP Handlers
# --------------------------------------------------------------------
async def ws_handler(websocket, path=None):
    global friends_mode, xkcd_mode, m5_battery_percent, preloaded_xkcd_image_ready
    logger.info("WS connected from %s", websocket.remote_address)
    try:
        async for message in websocket:
            logger.info("WS message: %s", message)
            battery_val = None
            if "|" in message:
                parts = message.split("|")
                base_cmd = parts[0]
                for p in parts[1:]:
                    if p.startswith("battery:"):
                        try:
                            battery_val = int(p.split(":")[1])
                        except:
                            pass
            else:
                base_cmd = message
            if battery_val is not None:
                m5_battery_percent = battery_val
                logger.info("Received battery: %d%%", battery_val)
                save_settings()
            if base_cmd == "checkForImage":
                current_sleep_duration = get_next_sleep_duration_minutes()
                dur_str = f"duration:{current_sleep_duration}"
                if friends_mode:
                    await asyncio.get_running_loop().run_in_executor(None, process_friends_quote)
                elif xkcd_mode:
                    if preloaded_xkcd_image_ready:
                        try:
                            import shutil
                            shutil.copyfile(PRELOADED_XKCD_FILE, IMAGE_FILE)
                            logger.info("Using preloaded XKCD comic.")
                            preloaded_xkcd_image_ready = False
                            asyncio.get_running_loop().run_in_executor(None, preload_xkcd_comic)
                        except Exception as e:
                            logger.error("Error copying preloaded XKCD image: %s", e)
                            process_xkcd_comic()
                    else:
                        process_xkcd_comic()
                if os.path.exists(IMAGE_FILE):
                    url = f"http://{SERVER_IP}:{HTTP_PORT}/{IMAGE_FILE}"
                    reply = f"update:{url}|{dur_str}"
                else:
                    reply = f"no_update|{dur_str}"
                await websocket.send(reply)
                logger.info("WS sent: %s", reply)
    except websockets.ConnectionClosed:
        logger.info("WS connection closed.")

async def start_ws_server():
    server = await websockets.serve(ws_handler, "0.0.0.0", WS_PORT)
    logger.info("WebSocket server on port %d", WS_PORT)
    return server

async def handle_image(request):
    if os.path.exists(IMAGE_FILE):
        logger.info("Serving image.png to %s", request.remote)
        return web.FileResponse(IMAGE_FILE)
    else:
        return web.Response(status=404, text="Image not found")

async def start_http_server():
    app = web.Application()
    app.router.add_get(f"/{IMAGE_FILE}", handle_image)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    logger.info("HTTP server on port %d, serving /%s", HTTP_PORT, IMAGE_FILE)

# --------------------------------------------------------------------
# 7) Telegram Bot Thread
# --------------------------------------------------------------------
def run_telegram_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    commands = [
        BotCommand("start", "Show welcome message"),
        BotCommand("help", "Show help message"),
        BotCommand("settings", "Set update interval"),
        BotCommand("friends", "Random Friends quote"),
        BotCommand("xkcd", "Random XKCD comic"),
    ]
    loop.run_until_complete(application.bot.set_my_commands(commands))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("chatid", chatid_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("friends", friends_command))
    application.add_handler(CommandHandler("xkcd", xkcd_command))
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_handler(CallbackQueryHandler(duration_callback))
    logger.info("Starting Telegram bot polling...")
    loop.run_until_complete(application.run_polling(shutdown_signals=None))

# --------------------------------------------------------------------
# 8) Main Async Entry
# --------------------------------------------------------------------
async def main():
    load_settings()
    await start_http_server()
    await start_ws_server()
    bot_thread = Thread(target=run_telegram_bot, daemon=True)
    bot_thread.start()
    logger.info("Server up. Listening for Telegram, WS, HTTP on 0.0.0.0.")
    await asyncio.Event().wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")