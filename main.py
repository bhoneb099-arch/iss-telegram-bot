import requests
from datetime import datetime
from PIL import Image
from io import BytesIO
import google.generativeai as genai  # ✅ CHANGED: Correct import
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.ext import MessageHandler, filters
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = genai.configure(api_key=GEMINI_API_KEY)  # ✅ CHANGED: Correct client setup

# =====================================
# USER CONTEXT MEMORY
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
        except:
            pass
    raise RuntimeError("ISS API failed")

def is_night(lat, lon):
    hour = datetime.utcnow().hour + (lon / 15)
    hour = hour % 24
    return hour < 6 or hour > 18

# =====================================
# DOWNLOAD EARTH IMAGE
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
    r = requests.get(NASA_GIBS_WMS, params=params, timeout=10)

    if r.status_code != 200:
        raise RuntimeError("Failed to download image")

    try:
        img = Image.open(BytesIO(r.content)).convert("RGB")
    except Exception:
        raise RuntimeError("Invalid image received")

    # 🔍 Check for black (missing data)
    img_small = img.resize((100, 100))
    black_pixels = sum(1 for p in img_small.getdata() if p == (0, 0, 0))
    total_pixels = 100 * 100
    
    if black_pixels / total_pixels > 0.2:
        print("⚠️ Too much black → using fallback map")
        params["LAYERS"] = "BlueMarble_ShadedRelief_Bathymetry"
        r = requests.get(NASA_GIBS_WMS, params=params, timeout=10)
        img = Image.open(BytesIO(r.content)).convert("RGB")

    filename = f"iss_{lat}_{lon}.png"
    img.save(filename)
    return filename

def ask_ai_with_image(question, image_path, iss_data):  # ✅ CHANGED: Updated for new API
    try:
        img = Image.open(image_path)

        # 👇 Detect question type
        if "what is this satellite for" in question.lower():
            prompt = f"""
You are a satellite expert.

Answer the user's question clearly and directly.

ISS Position:
Latitude: {iss_data['latitude']}
Longitude: {iss_data['longitude']}

Question:
{question}

Only answer the question. Do NOT describe the image.
"""
        else:
            prompt = f"""
You are a satellite expert.

ISS Position:
Latitude: {iss_data['latitude']}
Longitude: {iss_data['longitude']}

User question:
{question}

If the question is about the image, explain what is visible.
If not, just answer the question clearly.
"""

        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content([prompt, img])

        return response.text

    except Exception as e:
        print("AI error:", e)
        return "❌ AI failed"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""

    # ❌ No context yet
    if user_id not in user_context:
        await update.message.reply_text("⚠️ Please click-> /iss <-first.")
        return

    data = user_context[user_id]

    # 🧠 Use AI with stored image

    answer = await asyncio.to_thread(
        ask_ai_with_image,
        text,
        data["image_path"],
        data["iss_data"]
    )

    await update.message.reply_text(answer)

# =====================================
# TELEGRAM COMMAND
# =====================================
async def iss_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛰️ Fetching ISS data...")
    iss = get_iss_position()
    lat = iss["latitude"]
    lon = iss["longitude"]
    alt = iss["altitude"]
    vel = iss["velocity"]
    file = download_earth_image(lat, lon)

    # Save context for this user
    user_context[update.effective_user.id] = {
        "image_path": file,
        "iss_data": iss
    }

    text = f"""
🛰️ ISS Telemetry

Latitude: {lat:.2f}
Longitude: {lon:.2f}

Altitude: {alt} km
Velocity: {vel} km/h

click for more satellite image-> /iss <-
"""
    await update.message.reply_text(text)
    with open(file, "rb") as photo:
        await update.message.reply_photo(photo=photo)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = """
👋 Hola!

🛰️ Track the International Space Station in real time.

Type or Click-> /iss <-to get:
- Live ISS location  
- Earth view from space  
- AI analysis of what you see  

Ask questions like:
"What is this satellite for?","Is this ocean or land?"

— Designed by Andrew (Zay Bhone Aung)
"""
    await update.message.reply_text(text)

# =====================================
# MAIN BOT
# =====================================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("iss", iss_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
