import requests
from datetime import datetime
from PIL import Image
from io import BytesIO
import tempfile
import os
import google.generativeai as genai  # ✅ FIXED IMPORT
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.ext import MessageHandler, filters

# 🚀 Railway Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Safety check
if not BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("❌ BOT_TOKEN and GEMINI_API_KEY must be set!")

# ✅ FIXED: Configure Gemini (global)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash-exp')  # ✅ Correct way

# =====================================
# USER CONTEXT MEMORY (In-Memory)
# =====================================
user_context = {}

NASA_GIBS_WMS = "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"
DELTA_DEGREES = 3

# =====================================
# GET ISS POSITION
# =====================================
def get_iss_position():
    apis = [
        "https://api.wheretheiss.at/v1/satellites/25544",
        "http://api.open-notify.org/iss-now.json"
    ]
    for url in apis:
        try:
            r = requests.get(url, timeout=8)
            data = r.json()
            if "latitude" in data:
                return {
                    "latitude": float(data["latitude"]),
                    "longitude": float(data["longitude"]),
                    "altitude": float(data.get("altitude", 0)),
                    "velocity": float(data.get("velocity", 0)),
                    "timestamp": datetime.utcnow()
                }
            else:
                pos = data["iss_position"]
                return {
                    "latitude": float(pos["latitude"]),
                    "longitude": float(pos["longitude"]),
                    "altitude": None,
                    "velocity": None,
                    "timestamp": datetime.utcnow()
                }
        except Exception as e:
            print(f"API {url} failed: {e}")
            continue
    raise RuntimeError("❌ All ISS APIs failed")

def is_night(lat, lon):
    hour = datetime.utcnow().hour + (lon / 15)
    hour = hour % 24
    return hour < 6 or hour > 18

# =====================================
# DOWNLOAD EARTH IMAGE (Railway Temp Storage)
# =====================================
def download_earth_image(lat, lon):
    layer = "MODIS_Terra_CorrectedReflectance_TrueColor"
    if is_night(lat, lon):
        layer = "VIIRS_SNPP_DayNightBand_ENCC"

    min_lat = lat - DELTA_DEGREES
    max_lat = lat + DELTA_DEGREES
    min_lon = lon - DELTA_DEGREES
    max_lon = lon + DELTA_DEGREES

    bbox = f"{min_lat},{min_lon},{max_lat},{max_lon}"

    params = {
        "SERVICE": "WMS",
        "REQUEST": "GetMap",
        "VERSION": "1.3.0",
        "LAYERS": layer,
        "FORMAT": "image/png",
        "WIDTH": 1024,
        "HEIGHT": 1024,
        "CRS": "EPSG:4326",
        "BBOX": bbox,
        "TIME": "",
        "STYLES": ""
    }
    
    try:
        r = requests.get(NASA_GIBS_WMS, params=params, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"Image download failed: {e}")
        raise RuntimeError("❌ Failed to download Earth image")

    try:
        img = Image.open(BytesIO(r.content)).convert("RGB")
    except Exception as e:
        print(f"Image processing failed: {e}")
        raise RuntimeError("❌ Invalid image received")

    # 🔍 Check for black pixels
    img_small = img.resize((100, 100))
    black_pixels = sum(1 for p in img_small.getdata() if p == (0, 0, 0))
    total_pixels = 100 * 100
    
    if black_pixels / total_pixels > 0.2:
        print("⚠️ Too much black → using fallback map")
        params["LAYERS"] = "BlueMarble_ShadedRelief_Bathymetry"
        r = requests.get(NASA_GIBS_WMS, params=params, timeout=15)
        img = Image.open(BytesIO(r.content)).convert("RGB")

    # 🚀 Railway temp storage
    temp_file = tempfile.NamedTemporaryFile(
        suffix=f"_iss_{lat:.2f}_{lon:.2f}.png",
        delete=False
    )
    filename = temp_file.name
    temp_file.close()
    
    img.save(filename, optimize=True)
    print(f"✅ Image saved: {filename}")
    return filename

def ask_ai_with_image(question, image_path, iss_data):
    try:
        img = Image.open(image_path)

        prompt = f"""
You are a satellite expert analyzing Earth from ISS.

ISS Position:
Latitude: {iss_data['latitude']:.2f}°
Longitude: {iss_data['longitude']:.2f}°

User question: {question}

Answer clearly and concisely. If about the image, describe what you see.
"""

        response = model.generate_content([prompt, img])
        return response.text.strip()

    except Exception as e:
        print(f"AI error: {e}")
        return "❌ AI temporarily unavailable. Try /iss again!"

# ... [REST OF YOUR CODE REMAINS SAME - handlers, main function] ...

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""

    if user_id not in user_context:
        await update.message.reply_text("⚠️ Please use /iss first!")
        return

    data = user_context[user_id]
    await update.message.reply_text("🤖 AI analyzing...")

    answer = await asyncio.to_thread(
        ask_ai_with_image,
        text,
        data["image_path"],
        data["iss_data"]
    )

    await update.message.reply_text(answer)

async def iss_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("🛰️ Fetching ISS...")
        iss_data = get_iss_position()
        file_path = download_earth_image(iss_data["latitude"], iss_data["longitude"])

        user_context[update.effective_user.id] = {
            "image_path": file_path,
            "iss_data": iss_data
        }

        text = f"""
🛰️ **ISS Live**

🌐 Lat: {iss_data['latitude']:.2f}° | Lon: {iss_data['longitude']:.2f}°
🪂 Alt: {iss_data['altitude']}km | Speed: {iss_data['velocity']}km/h
        """
        
        await update.message.reply_text(text, parse_mode='Markdown')
        
        with open(file_path, "rb") as photo:
            await update.message.reply_photo(photo=photo, caption="🌍 From ISS now!")
            
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""
👋 **ISS Earth Bot**

/iss - Live ISS location + Earth photo
Then ask AI questions!

By Andrew 🚀
    """, parse_mode='Markdown')

def main():
    print("🚀 ISS Bot starting...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("iss", iss_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Bot ready!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
