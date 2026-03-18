import requests
from datetime import datetime
from PIL import Image
from io import BytesIO
import tempfile
import os
from google.generativeai import GenerativeModel  # Fixed import
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

# Initialize Gemini client (fixed)
model = GenerativeModel(
    model_name="gemini-2.5-flash-exp",
    api_key=GEMINI_API_KEY,
    generation_config={
        "temperature": 0.7,
        "top_p": 0.8,
    }
)

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

    # 🔍 Check for black (missing data)
    img_small = img.resize((100, 100))
    black_pixels = sum(1 for p in img_small.getdata() if p == (0, 0, 0))
    total_pixels = 100 * 100
    
    if black_pixels / total_pixels > 0.2:
        print("⚠️ Too much black → using fallback map")
        params["LAYERS"] = "BlueMarble_ShadedRelief_Bathymetry"
        r = requests.get(NASA_GIBS_WMS, params=params, timeout=15)
        img = Image.open(BytesIO(r.content)).convert("RGB")

    # 🚀 Use Railway temp storage
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

        # 👇 Detect question type
        if "what is this satellite for" in question.lower():
            prompt = f"""
You are a satellite expert.

Answer the user's question clearly and directly.

ISS Position:
Latitude: {iss_data['latitude']:.2f}°
Longitude: {iss_data['longitude']:.2f}°

Question: {question}

Only answer the question. Do NOT describe the image.
"""
        else:
            prompt = f"""
You are a satellite expert analyzing Earth from ISS.

ISS Position:
Latitude: {iss_data['latitude']:.2f}°
Longitude: {iss_data['longitude']:.2f}°

User question: {question}

If the question is about the image, explain what is visible.
If not, just answer the question clearly and concisely.
"""

        response = model.generate_content([prompt, img])
        return response.text.strip()

    except Exception as e:
        print(f"AI error: {e}")
        return "❌ AI analysis unavailable. Try again!"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""

    # ❌ No context yet
    if user_id not in user_context:
        await update.message.reply_text("⚠️ Please use /iss first to get ISS location!")
        return

    data = user_context[user_id]

    # 🧠 Use AI with stored image
    await update.message.reply_text("🤖 Analyzing with AI...")

    answer = await asyncio.to_thread(
        ask_ai_with_image,
        text,
        data["image_path"],
        data["iss_data"]
    )

    await update.message.reply_text(answer)

# =====================================
# TELEGRAM COMMANDS
# =====================================
async def iss_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("🛰️ Fetching live ISS data...")
        
        iss = get_iss_position()
        lat = iss["latitude"]
        lon = iss["longitude"]
        alt = iss["altitude"]
        vel = iss["velocity"]
        
        await update.message.reply_text("🌍 Downloading Earth image from space...")
        file = download_earth_image(lat, lon)

        # Save context for this user (with cleanup)
        user_context[update.effective_user.id] = {
            "image_path": file,
            "iss_data": iss,
            "timestamp": datetime.utcnow()
        }

        text = f"""
🛰️ **ISS Live Telemetry**

🌐 **Position:**
- Lat: {lat:.2f}°
- Lon: {lon:.2f}°

🪂 **Orbit:**
- Altitude: {alt} km
- Velocity: {vel} km/h
        """
        
        await update.message.reply_text(text, parse_mode='Markdown')
        
        # Send photo
        try:
            with open(file, "rb") as photo:
                await update.message.reply_photo(
                    photo=photo,
                    caption="🌍 Earth view from ISS right now!"
                )
        except Exception as e:
            print(f"Photo send error: {e}")
            await update.message.reply_text("✅ Data ready! Image temporarily unavailable.")
            
    except Exception as e:
        print(f"ISS command error: {e}")
        await update.message.reply_text("❌ Failed to fetch ISS data. Try again!")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
👋 **ISS Earth Explorer Bot**

🛰️ Track the **International Space Station** in real time!

**Commands:**
/iss - Get live ISS position + Earth photo
- Then ask AI about what you see!

**Example questions:**
- "What country is this?"
- "Ocean or land?"
- "What city is visible?"
- "Weather conditions?"

**By Andrew (Zay Bhone Aung)**
🚀 Powered by NASA + Gemini AI
    """
    await update.message.reply_text(text, parse_mode='Markdown')

# =====================================
# RAILWAY MAIN (Polling - Simple & Reliable)
# =====================================
def main():
    print("🚀 Starting ISS Bot on Railway...")
    print(f"🌐 Railway Env: {os.getenv('RAILWAY_ENVIRONMENT', 'local')}")
    print("✅ Environment variables loaded")
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("iss", iss_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot handlers registered")
    print("🌍 Ready to track ISS! Starting polling...")
    
    # Run with polling (works perfectly on Railway)
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
        timeout=30,
        bootstrap_retries=-1  # Unlimited retries
    )

if __name__ == "__main__":
    main()