import os, asyncio, datetime, uvicorn, random, re, subprocess
import aiohttp
from fastapi import FastAPI, Body, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bson import ObjectId
from pydantic import BaseModel
from PIL import Image # নতুন ইমেজ লাইব্রেরি
from pyrogram import Client as PyroClient # ২ জিবি ফাইল সাপোর্ট করার জন্য এড করা হলো

# --- কনফিগারেশন ---
TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URL = os.getenv("MONGO_URL", "mongodb+srv://akash:akash@cluster0.etisrpx.mongodb.net/?appName=Cluster0")
OWNER_ID = int(os.getenv("ADMIN_ID", "7525127704")) 
APP_URL = os.getenv("APP_URL", "https://adaltbot.onrender.com/")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1003275619931") # আপনার চ্যানেলের আইডি এখানে দিন

# ২ জিবি ফাইল ডাউনলোডের জন্য API ID এবং HASH (এড করা হলো)
API_ID = int(os.getenv("API_ID", "29904834"))
API_HASH = os.getenv("API_HASH", "8b4fd9ef578af114502feeafa2d31938")

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

# Pyrogram ক্লায়েন্ট সেটআপ (বড় ফাইল হ্যান্ডেল করার জন্য)
pyro_bot = PyroClient("bot_pyro_session", api_id=API_ID, api_hash=API_HASH, bot_token=TOKEN)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

client = AsyncIOMotorClient(MONGO_URL)
db = client['movie_data6base']

admin_temp = {}
admin_cache = set([OWNER_ID]) 

# --- ভিডিও ডিউরেশন ফরম্যাট করার ফাংশন ---
def get_duration_display(seconds):
    try:
        seconds = int(float(seconds))
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h > 0: return f"{h}h {m}m"
        return f"{m}m {s}s"
    except: return "0m"

# --- নতুন স্ক্রিনশট গ্রিড ফাংশন (৬টি স্ক্রিনশট ২ সারি ৩ কলাম ল্যান্ডস্কেপ গ্রিড) ---
async def create_screenshot_grid(video_path, output_path):
    try:
        # ভিডিওর ডিউরেশন বের করা
        cmd = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
        duration = float(subprocess.check_output(cmd, shell=True).decode().strip())
        
        interval = duration / 7 # ৬টি গ্রিডের জন্য গ্যাপ
        screenshots = []
        for i in range(1, 7):
            time_pos = i * interval
            temp_out = f"temp_frame_{i}_{random.randint(100,999)}.jpg"
            # FFMPEG দিয়ে ফ্রেম নেওয়া
            subprocess.run(f'ffmpeg -ss {time_pos} -i "{video_path}" -vframes 1 -q:v 2 "{temp_out}" -y', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(temp_out):
                screenshots.append(temp_out)
        
        if not screenshots: return False, 0
        
        # PIL দিয়ে গ্রিড তৈরি (৩ কলাম, ২ সারি)
        images = [Image.open(x) for x in screenshots]
        w, h = images[0].size
        grid = Image.new('RGB', (w*3, h*2))
        
        for idx, img in enumerate(images):
            x = (idx % 3) * w
            y = (idx // 3) * h
            grid.paste(img, (x, y))
            img.close()
            os.remove(screenshots[idx])
            
        grid.save(output_path)
        return True, duration
    except Exception:
        return False, 0

# --- নতুন টাইম পার্সার (Flexible Time: 1h,1m,1s ফরম্যাট সাপোর্ট করার জন্য) ---
def parse_duration(time_str):
    total_seconds = 0
    hours = re.search(r'(\d+)h', time_str)
    minutes = re.search(r'(\d+)m', time_str)
    seconds = re.search(r'(\d+)s', time_str)
    if hours: total_seconds += int(hours.group(1)) * 3600
    if minutes: total_seconds += int(minutes.group(1)) * 60
    if seconds: total_seconds += int(seconds.group(1))
    if not any([hours, minutes, seconds]) and time_str.isdigit():
        total_seconds = int(time_str) * 3600 # যদি শুধু সংখ্যা দেয় তবে ঘন্টা হিসেবে ধরবে
    return total_seconds if total_seconds > 0 else 86400

# --- নতুন ব্লিংক লিঙ্ক পার্সার (চ্যানেল আইডি ও লাস্ট মেসেজ আইডি বের করার জন্য) ---
def parse_blink_link(link):
    pattern = r"t\.me/(?:c/)?([^/]+)/(\d+)"
    match = re.search(pattern, link)
    if match:
        chat_id = match.group(1)
        if chat_id.isdigit(): chat_id = int("-100" + chat_id)
        msg_id = int(match.group(2))
        return chat_id, msg_id
    return None, None

async def load_admins():
    admin_cache.clear()
    admin_cache.add(OWNER_ID)
    try:
        async for admin in db.admins.find():
            admin_cache.add(admin["user_id"])
    except Exception: pass

# --- ব্যাকগ্রাউন্ড অটো-ডিলিট ওয়ার্কার ---
async def auto_delete_worker():
    while True:
        try:
            now = datetime.datetime.utcnow()
            expired_msgs = db.auto_delete.find({"delete_at": {"$lte": now}})
            async for msg in expired_msgs:
                try:
                    await bot.delete_message(chat_id=msg["chat_id"], message_id=msg["message_id"])
                except Exception: pass
                await db.auto_delete.delete_one({"_id": msg["_id"]})
        except Exception: pass
        await asyncio.sleep(60)

# --- নতুন ব্লিংক ইন্ডেক্সিং ওয়ার্কার (১ থেকে লাস্ট আইডি পর্যন্ত) ---
async def blink_worker(chat_id, last_id, admin_id):
    await bot.send_message(admin_id, f"🚀 <b>ব্লিংক প্রসেস শুরু হয়েছে!</b>\nআইডি 1 থেকে {last_id} পর্যন্ত ফাইল চেক করে অটো আপলোড করা হচ্ছে।", parse_mode="HTML")
    
    success_count = 0
    for current_id in range(1, last_id + 1):
        try:
            # Pyrogram দিয়ে মেসেজ গেট করা
            msg = await pyro_bot.get_messages(chat_id, current_id)
            if not msg or msg.empty: continue
            
            # শুধুমাত্র ভিডিও বা ভিডিও ডকুমেন্ট থাকলে প্রসেস করবে
            if msg.video or (msg.document and "video" in msg.document.mime_type):
                # সিরিয়াল কাউন্টার আপডেট
                serial_res = await db.settings.find_one_and_update(
                    {"id": "auto_post_count"},
                    {"$inc": {"value": 1}},
                    upsert=True,
                    return_document=True
                )
                count_val = serial_res.get('value', 1)
                title = f"🥵 New Hot Sex Video Number🥵 হট সেক্স ভাইরাল ভিডিও নাম্বার 🥵 {count_val}"

                # ভিডিও ডাউনলোড ও গ্রিড তৈরি
                video_path = await pyro_bot.download_media(msg)
                grid_path = f"grid_blink_{random.randint(1000,9999)}.jpg"
                
                res, duration_sec = await create_screenshot_grid(video_path, grid_path)
                duration_str = get_duration_display(duration_sec) if res else "0m"

                if res:
                    # গ্রিড ফটো আপলোড
                    with open(grid_path, 'rb') as f:
                        photo_msg = await bot.send_photo(CHANNEL_ID, photo=types.BufferedInputFile(f.read(), filename="grid.jpg"), 
                        caption=f"🎥 <b>নতুন মুভি যুক্ত হয়েছে!</b>\n\n🎬 নাম: <b>{title}</b>", parse_mode="HTML",
                        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🎬 মুভিটি দেখুন", url=f"https://t.me/{(await bot.get_me()).username}?start=new")]]))
                        photo_id = photo_msg.photo[-1].file_id

                    # ডাটাবেসে মুভি সেভ
                    await db.movies.insert_one({
                        "title": title, "photo_id": photo_id, "file_id": (msg.video.file_id if msg.video else msg.document.file_id), 
                        "file_type": "video", "clicks": 0, "duration": duration_str, "created_at": datetime.datetime.utcnow()
                    })
                    success_count += 1
                
                # ক্লিনআপ
                if os.path.exists(video_path): os.remove(video_path)
                if os.path.exists(grid_path): os.remove(grid_path)
                
                # রেট লিমিট এড়াতে সামান্য বিরতি
                await asyncio.sleep(1.5)
                
        except Exception:
            continue

    await bot.send_message(admin_id, f"✅ <b>ব্লিংক ইন্ডেক্স সম্পন্ন!</b>\nমোট <b>{success_count}</b>টি ভিডিও সফলভাবে আপলোড হয়েছে।", parse_mode="HTML")

# ==========================================
# ১. মেইন ওনার (Owner) ও অ্যাডমিন কমান্ড সমূহ (নতুন সহ)
# ==========================================

@dp.message(Command("addadmin"))
async def add_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return
    try:
        new_admin = int(m.text.split()[1])
        if new_admin in admin_cache:
            return await m.answer("⚠️ এই ইউজারটি আগে থেকেই অ্যাডমিন!")
        await db.admins.insert_one({"user_id": new_admin})
        admin_cache.add(new_admin)
        await m.answer(f"✅ নতুন অ্যাডমিন যুক্ত করা হয়েছে: <code>{new_admin}</code>", parse_mode="HTML")
        try: await bot.send_message(new_admin, "🎉 <b>অভিনন্দন!</b> আপনাকে এই বটের অ্যাডমিন বানানো হয়েছে। আপনি এখন মুভি আপলোড করতে পারবেন।", parse_mode="HTML")
        except: pass
    except: await m.answer("⚠️ সঠিক নিয়ম: <code>/addadmin ইউজার_আইডি</code>", parse_mode="HTML")

@dp.message(Command("deladmin"))
async def del_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return
    try:
        del_admin = int(m.text.split()[1])
        if del_admin == OWNER_ID: return await m.answer("⚠️ আপনি নিজেকে (Owner) ডিলিট করতে পারবেন না!")
        await db.admins.delete_one({"user_id": del_admin})
        admin_cache.discard(del_admin)
        await m.answer(f"✅ অ্যাডমিন রিমুভ করা হয়েছে: <code>{del_admin}</code>", parse_mode="HTML")
    except: await m.answer("⚠️ সঠিক নিয়ম: <code>/deladmin ইউজার_আইডি</code>", parse_mode="HTML")

@dp.message(Command("adminlist"))
async def list_admins_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return
    text = "👥 <b>বর্তমান অ্যাডমিন লিস্ট:</b>\n"
    text += f"👑 Owner: <code>{OWNER_ID}</code>\n"
    for ad in admin_cache:
        if ad != OWNER_ID: text += f"👮 Admin: <code>{ad}</code>\n"
    await m.answer(text, parse_mode="HTML")

# --- নতুন আনলিমিটেড ডিরেক্ট লিঙ্ক সিস্টেম কমান্ড ---
@dp.message(Command("addlink"))
async def add_ad_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1]
        await db.ad_links.insert_one({"url": url, "created_at": datetime.datetime.utcnow()})
        await m.answer(f"✅ নতুন অ্যাড লিঙ্ক যুক্ত হয়েছে:\n<code>{url}</code>", parse_mode="HTML")
    except: await m.answer("⚠️ নিয়ম: `/addlink https://directlink.com`")

@dp.message(Command("links"))
async def list_ad_links(m: types.Message):
    if m.from_user.id not in admin_cache: return
    links = await db.ad_links.find().to_list(length=100)
    if not links: return await m.answer("কোনো অ্যাড লিঙ্ক নেই।")
    text = "🔗 <b>বর্তমান অ্যাড লিঙ্ক সমূহ:</b>\n\n"
    for i, l in enumerate(links):
        text += f"{i+1}. <code>{l['url']}</code>\nID: <code>{l['_id']}</code>\n\n"
    text += "ডিলিট করতে: `/dellink ID` লিখুন।"
    await m.answer(text, parse_mode="HTML")

@dp.message(Command("dellink"))
async def del_ad_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link_id = m.text.split(" ", 1)[1]
        await db.ad_links.delete_one({"_id": ObjectId(link_id)})
        await m.answer("✅ লিঙ্কটি ডিলিট করা হয়েছে।")
    except: await m.answer("⚠️ নিয়ম: `/dellink ID` (ID পাবেন /links কমান্ডে)")

# --- নতুন অ্যাড সুইচ কমান্ড ---
@dp.message(Command("monetag"))
async def toggle_monetag(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        status = m.text.split(" ")[1].lower() == "on"
        await db.settings.update_one({"id": "monetag_status"}, {"$set": {"status": status}}, upsert=True)
        await m.answer(f"✅ Monetag Ads এখন {'চালু (ON)' if status else 'বন্ধ (OFF)'}")
    except: await m.answer("⚠️ নিয়ম: `/monetag on` বা `/monetag off`")

@dp.message(Command("adlink"))
async def toggle_adlink(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        status = m.text.split(" ")[1].lower() == "on"
        await db.settings.update_one({"id": "adlink_status"}, {"$set": {"status": status}}, upsert=True)
        await m.answer(f"✅ Direct Link Ads এখন {'চালু (ON)' if status else 'বন্ধ (OFF)'}")
    except: await m.answer("⚠️ নিয়ম: `/adlink on` বা `/adlink off`")

# --- মূল কোডের বাকি কমান্ডসমূহ ---
@dp.message(Command("setsteps"))
async def set_steps_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        steps = int(m.text.split(" ")[1])
        await db.settings.update_one({"id": "step_config"}, {"$set": {"count": steps}}, upsert=True)
        await m.answer(f"✅ অ্যাড স্টেপ সংখ্যা এখন: <b>{steps}</b>", parse_mode="HTML")
    except: await m.answer("⚠️ নিয়ম: `/setsteps 2`")

# --- আপডেট করা সেট আনলক টাইম কমান্ড (Flexible Time Support) ---
@dp.message(Command("setunlocktime"))
async def set_unlock_time_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        raw_val = m.text.split(" ", 1)[1]
        seconds = parse_duration(raw_val)
        await db.settings.update_one({"id": "unlock_config"}, {"$set": {"seconds": seconds, "raw": raw_val}}, upsert=True)
        await m.answer(f"✅ মুভি আনলক থাকার সময় সেট করা হয়েছে: <b>{raw_val}</b>", parse_mode="HTML")
    except: await m.answer("⚠️ নিয়ম: <code>/setunlocktime 1h,1m,10s</code>")

@dp.message(Command("setnotice"))
async def set_notice_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        notice = m.text.split(" ", 1)[1]
        await db.settings.update_one({"id": "site_notice"}, {"$set": {"text": notice}}, upsert=True)
        await m.answer("✅ নোটিশ আপডেট করা হয়েছে।")
    except: await m.answer("⚠️ নিয়ম: `/setnotice মুভি না চললে ভিপিএন অন করুন`")

@dp.message(Command("sethead"))
async def set_head_ad(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        code = m.text.split(" ", 1)[1]
        await db.settings.update_one({"id": "header_ad"}, {"$set": {"code": code}}, upsert=True)
        await m.answer("✅ হেডার ব্যানার অ্যাড কোড আপডেট হয়েছে।")
    except: await m.answer("⚠️ নিয়ম: `/sethead [অ্যাড কোড]`")

@dp.message(Command("setmid"))
async def set_mid_ad(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        code = m.text.split(" ", 1)[1]
        await db.settings.update_one({"id": "middle_ad"}, {"$set": {"code": code}}, upsert=True)
        await m.answer("✅ মিডেল ব্যানার অ্যাড কোড আপডেট হয়েছে।")
    except: await m.answer("⚠️ নিয়ম: `/setmid [অ্যাড কোড]`")

@dp.message(Command("setfoot"))
async def set_foot_ad(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        code = m.text.split(" ", 1)[1]
        await db.settings.update_one({"id": "footer_ad"}, {"$set": {"code": code}}, upsert=True)
        await m.answer("✅ ফুটার ব্যানার অ্যাড কোড আপডেট হয়েছে।")
    except: await m.answer("⚠️ নিয়ম: `/setfoot [অ্যাড কোড]`")

# --- নতুন ব্লিংক কমান্ড (চ্যানেলের লাস্ট পোস্ট লিঙ্ক দিলে ১ থেকে ইন্ডেক্স করবে) ---
@dp.message(Command("blink"))
async def blink_cmd_handler(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ", 1)[1]
        chat_id, last_id = parse_blink_link(link)
        if not chat_id or not last_id:
            return await m.answer("⚠️ <b>ভুল লিঙ্ক!</b> সঠিক উদাহরণ: <code>/blink https://t.me/your_channel/1234</code>", parse_mode="HTML")
        
        # ব্যাকগ্রাউন্ডে প্রসেস শুরু করা
        asyncio.create_task(blink_worker(chat_id, last_id, m.from_user.id))
        await m.answer("⏳ চ্যানেলের আইডি 1 থেকে শুরু করে সব ভিডিও ইন্ডেক্স করা হচ্ছে। সম্পন্ন হলে জানানো হবে।")
    except:
        await m.answer("⚠️ নিয়ম: <code>/blink [চ্যানেলের শেষ পোস্ট লিঙ্ক]</code>", parse_mode="HTML")

# ==========================================
# ২. বটের সাধারণ অ্যাডমিন কমান্ড (আপনার আগের কোড)
# ==========================================

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await db.users.update_one({"user_id": message.from_user.id}, {"$set": {"first_name": message.from_user.first_name}}, upsert=True)
    kb = [[types.InlineKeyboardButton(text="🎬 ওপেন মুভি অ্যাপ", web_app=types.WebAppInfo(url=APP_URL))]]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    uid = message.from_user.id
    if uid in admin_cache:
        text = (
            "👋 <b>হ্যালো অ্যাডমিন!</b>\n\n"
            "⚙️ <b>পোস্ট কমান্ড:</b>\n"
            "🔹 <code>/post</code> - ম্যানুয়াল আপলোড (আগের মত)\n"
            "🔹 <code>/new</code> - অটো স্ক্রিনশট আপলোড (নাম দিয়ে)\n"
            "🔹 <code>/auto</code> - ফুল অটো (সিরিয়াল নাম + স্ক্রিনশট)\n"
            "🔹 <code>/blink</code> - চ্যানেল ফুল ইন্ডেক্স (আইডি ১ থেকে)\n\n"
            "⚙️ <b>সাধারণ কমান্ড:</b>\n"
            "🔸 জোন: <code>/setad</code> | টেলিগ্রাম: <code>/settg</code> | 18+: <code>/set18</code>\n"
            "🔸 সাইট নেম: <code>/setsitename [নাম]</code>\n"
            "🔸 প্রোটেকশন: <code>/protect on</code> বা <code>/protect off</code>\n"
            "🔸 অটো-ডিলিট টাইম: <code>/settime [মিনিট]</code>\n"
            "🔸 আনলক টাইম: <code>/setunlocktime [সময়]</code>\n"
            "🔸 ডিলিট: <code>/del</code> | স্ট্যাটাস: <code>/stats</code> | ব্রডকাস্ট: <code>/cast</code>\n"
            "\n🆕 <b>অ্যাড সেটিংস:</b>\n"
            "🔸 লিঙ্ক অ্যাড: <code>/addlink [URL]</code>\n"
            "🔸 লিঙ্ক লিস্ট: <code>/links</code>\n"
            "🔸 লিঙ্ক সুইচ: <code>/adlink on/off</code>\n"
            "🔸 মনিটেগ সুইচ: <code>/monetag on/off</code>\n"
            "🔸 স্টেপ: <code>/setsteps [সংখ্যা]</code>\n"
            "🔸 নোটিশ: <code>/setnotice [টেক্সট]</code>\n"
            "🔸 হেডার অ্যাড: <code>/sethead [কোড]</code>\n"
            "🔸 মিডেল অ্যাড: <code>/setmid [কোড]</code>\n"
            "🔸 ফুটার অ্যাড: <code>/setfoot [কোড]</code>\n"
        )
        if uid == OWNER_ID:
            text += "\n👑 <b>ওনার কমান্ড:</b>\n🔸 অ্যাড অ্যাডমিন: <code>/addadmin ID</code>\n🔸 ডিলিট অ্যাডমিন: <code>/deladmin ID</code>\n🔸 অ্যাডমিন লিস্ট: <code>/adminlist</code>\n"
            
        text += "\n📥 <b>মুভি আপলোড করতে /post, /new বা /auto কমান্ড ব্যবহার করুন।</b>"
    else:
        text = f"👋 <b>স্বাগতম {message.from_user.first_name}!</b>\n\n[আপনার টেলিগ্রাম আইডি: <code>{uid}</code>]\n\nমুভি দেখতে নিচের বাটনে ক্লিক করুন।"
    await message.answer(text, reply_markup=markup, parse_mode="HTML")

# --- নতুন আপলোড কমান্ডসমূহ ---
@dp.message(Command("post"))
async def post_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    admin_temp[m.from_user.id] = {"step": "manual_file"}
    await m.answer("📥 <b>ম্যানুয়াল আপলোড:</b> মুভি ফাইলটি (Video/Doc) পাঠান।")

@dp.message(Command("new"))
async def new_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    admin_temp[m.from_user.id] = {"step": "auto_name"}
    await m.answer("🆕 <b>অটো আপলোড:</b> মুভির নাম লিখে পাঠান।")

@dp.message(Command("auto"))
async def auto_post_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    admin_temp[m.from_user.id] = {"step": "auto_serial_mode"}
    await m.answer("🤖 <b>ফুল অটো মোড:</b> শুধু ভিডিও ফাইলটি পাঠান।\nবট অটোমেটিক সিরিয়াল নাম্বার ও স্ক্রিনশট দিয়ে পোস্ট করে দিবে।")

@dp.message(Command("setsitename"))
async def set_site_name(m: types.Message):
    if m.from_user.id in admin_cache:
        try:
            new_name = m.text.split(" ", 1)[1]
            await db.settings.update_one({"id": "site_name"}, {"$set": {"name": new_name}}, upsert=True)
            await m.answer(f"✅ সাইটের নাম পরিবর্তন করে <b>{new_name}</b> রাখা হয়েছে।", parse_mode="HTML")
        except: await m.answer("⚠️ সঠিক নিয়ম: <code>/setsitename My Movie Site</code>", parse_mode="HTML")

@dp.message(Command("protect"))
async def protect_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        state = m.text.split(" ")[1].lower()
        if state == "on":
            await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": True}}, upsert=True)
            await m.answer("✅ ফরোয়ার্ড প্রোটেকশন <b>চালু (ON)</b> করা হয়েছে। এখন কেউ মুভি ফরোয়ার্ড বা সেভ করতে পারবে না।", parse_mode="HTML")
        elif state == "off":
            await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": False}}, upsert=True)
            await m.answer("✅ ফরোয়ার্ড প্রোটেকশন <b>বন্ধ (OFF)</b> করা হয়েছে। এখন সবাই মুভি ফরোয়ার্ড করতে পারবে।", parse_mode="HTML")
        else: await m.answer("⚠️ সঠিক নিয়ম: <code>/protect on</code> অথবা <code>/protect off</code>", parse_mode="HTML")
    except: await m.answer("⚠️ সঠিক নিয়ম: <code>/protect on</code> অথবা <code>/protect off</code>", parse_mode="HTML")

@dp.message(Command("stats"))
async def stats_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    uc = await db.users.count_documents({})
    mc = await db.movies.count_documents({})
    time_cfg = await db.settings.find_one({"id": "del_time"})
    del_m = time_cfg['minutes'] if time_cfg else 60
    protect_cfg = await db.settings.find_one({"id": "protect_content"})
    prot_status = "ON 🔒" if protect_cfg and protect_cfg.get('status', True) else "OFF 🔓"
    
    await m.answer(f"📊 <b>স্ট্যাটাস:</b>\n👥 মোট ইউজার: <code>{uc}</code>\n🎬 মোট মুভি: <code>{mc}</code>\n⏳ অটো-ডিলিট: <code>{del_m} মিনিট</code>\n🛡️ প্রোটেকশন: <b>{prot_status}</b>", parse_mode="HTML")

@dp.message(Command("del"))
async def del_movie_list(m: types.Message):
    if m.from_user.id not in admin_cache: return
    movies = await db.movies.find().sort("created_at", -1).limit(20).to_list(length=20)
    if not movies: return await m.answer("কোনো মুভি নেই।")
    builder = InlineKeyboardBuilder()
    for mv in movies: builder.button(text=f"❌ {mv['title']}", callback_data=f"del_{str(mv['_id'])}")
    builder.adjust(1)
    await m.answer("⚠️ ডিলিট করতে ক্লিক করুন:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("del_"))
async def del_movie_callback(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    try:
        await db.movies.delete_one({"_id": ObjectId(c.data.split("_")[1])})
        await c.answer("✅ ডিলিট হয়েছে!", show_alert=True)
        await c.message.edit_text("✅ মুভিটি ডাটাবেস থেকে মুছে ফেলা হয়েছে।", reply_markup=None)
    except: pass

@dp.message(Command("settime"))
async def set_del_time(m: types.Message):
    if m.from_user.id in admin_cache:
        try:
            await db.settings.update_one({"id": "del_time"}, {"$set": {"minutes": int(m.text.split(" ")[1])}}, upsert=True)
            await m.answer(f"✅ অটো-ডিলিট টাইম সেট করা হয়েছে।")
        except: await m.answer("⚠️ সঠিক নিয়ম: <code>/settime 60</code>", parse_mode="HTML")

@dp.message(Command("setad"))
async def set_ad(m: types.Message):
    if m.from_user.id in admin_cache:
        try:
            await db.settings.update_one({"id": "ad_config"}, {"$set": {"zone_id": m.text.split(" ")[1]}}, upsert=True)
            await m.answer("✅ জোন আপডেট হয়েছে।")
        except: await m.answer("⚠️ সঠিক নিয়ম: <code>/setad 1234567</code>", parse_mode="HTML")

@dp.message(Command("settg"))
async def set_tg(m: types.Message):
    if m.from_user.id in admin_cache:
        try:
            await db.settings.update_one({"id": "link_tg"}, {"$set": {"url": m.text.split(" ")[1]}}, upsert=True)
            await m.answer("✅ টেলিগ্রাম লিংক আপডেট হয়েছে।")
        except: await m.answer("⚠️ সঠিক নিয়ম: <code>/settg https://t.me/...</code>", parse_mode="HTML")

@dp.message(Command("set18"))
async def set_18(m: types.Message):
    if m.from_user.id in admin_cache:
        try:
            await db.settings.update_one({"id": "link_18"}, {"$set": {"url": m.text.split(" ")[1]}}, upsert=True)
            await m.answer("✅ 18+ লিংক আপডেট হয়েছে।")
        except: await m.answer("⚠️ সঠিক নিয়ম: <code>/set18 https://t.me/...</code>", parse_mode="HTML")

# ==========================================
# ৩. ইনপুট প্রসেসিং (আপলোড, ব্রডকাস্ট, রিপ্লাই) (আপনার আগের কোড)
# ==========================================

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message):
    if m.from_user.id not in admin_cache: return
    admin_temp[m.from_user.id] = {"step": "bcast_wait"}
    await m.answer("📢 <b>অ্যাডভান্সড ব্রডকাস্ট:</b>\nযে মেসেজটি ব্রডকাস্ট করতে চান সেটি পাঠান।\n<i>নোট: বট অটোমেটিক মেসেজের নিচে '🎬 ওপেন মুভি অ্যাপ' বাটন লাগিয়ে দিবে।</i>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("reply_"))
async def process_reply_cb(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    user_id = int(c.data.split("_")[1])
    admin_temp[c.from_user.id] = {"step": "reply_user", "target_uid": user_id}
    await c.message.reply("✍️ <b>ইউজারকে কী রিপ্লাই দিতে চান তা লিখে পাঠান:</b>\n(টেক্সট, ছবি বা ভয়েস মেসেজ পাঠাতে পারেন)", parse_mode="HTML")
    await c.answer()

@dp.message(F.content_type.in_({'text', 'photo', 'video', 'document', 'voice'}))
async def catch_all_inputs(m: types.Message):
    uid = m.from_user.id
    state = admin_temp.get(uid, {}).get("step")
    
    # ইউজারকে রিপ্লাই দেওয়ার ফ্লো
    if uid in admin_cache and state == "reply_user":
        target_uid = admin_temp[uid]["target_uid"]
        del admin_temp[uid]
        try:
            if m.text: await bot.send_message(target_uid, f"📩 <b>অ্যাডমিন রিপ্লাই:</b>\n\n{m.text}", parse_mode="HTML")
            else: await m.copy_to(target_uid, caption=f"📩 <b>অ্যাডমিন রিপ্লাই:</b>\n\n{m.caption or ''}", parse_mode="HTML")
            await m.answer("✅ ইউজারকে সফলভাবে রিপ্লাই পাঠানো হয়েছে!")
        except Exception:
            await m.answer("⚠️ রিপ্লাই পাঠানো যায়নি! ইউজার হয়তো বট ব্লক করেছে।")
        return

    # ব্রডকাস্ট ফ্লো
    if uid in admin_cache and state == "bcast_wait":
        del admin_temp[uid]
        await m.answer("⏳ ব্রডকাস্ট শুরু হয়েছে...")
        kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🎬 ওপেন মুভি অ্যাপ", web_app=types.WebAppInfo(url=APP_URL))]])
        success = 0
        async for u in db.users.find():
            try:
                await m.copy_to(chat_id=u['user_id'], reply_markup=kb)
                success += 1
                await asyncio.sleep(0.05)
            except: pass
        await m.answer(f"✅ সম্পন্ন! সর্বমোট <b>{success}</b> জনকে মেসেজ পাঠানো হয়েছে।", parse_mode="HTML")
        return

    # --- ১. ম্যানুয়াল আপলোড ফ্লো (/post) ---
    if uid in admin_cache and (m.document or m.video) and state == "manual_file":
        fid = m.video.file_id if m.video else m.document.file_id
        ftype = "video" if m.video else "document"
        admin_temp[uid] = {"step": "manual_photo", "file_id": fid, "type": ftype}
        await m.answer("✅ ফাইল পেয়েছি! এবার মুভির <b>পোস্টার (Photo)</b> সেন্ড করুন।", parse_mode="HTML")
        return

    if uid in admin_cache and m.photo and state == "manual_photo":
        admin_temp[uid]["photo_id"] = m.photo[-1].file_id
        admin_temp[uid]["step"] = "manual_title"
        await m.answer("✅ পোস্টার পেয়েছি! এবার মুভির <b>নাম</b> লিখে পাঠান।", parse_mode="HTML")
        return

    if uid in admin_cache and m.text and state == "manual_title":
        title = m.text.strip()
        await db.movies.insert_one({
            "title": title, "photo_id": admin_temp[uid]["photo_id"], 
            "file_id": admin_temp[uid]["file_id"], "file_type": admin_temp[uid]["type"], 
            "clicks": 0, "duration": "Unknown", "created_at": datetime.datetime.utcnow()
        })
        try:
            me = await bot.get_me()
            kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🎬 মুভিটি দেখুন", url=f"https://t.me/{me.username}?start=new")]])
            await bot.send_photo(CHANNEL_ID, photo=admin_temp[uid]["photo_id"], caption=f"🎥 <b>নতুন মুভি যুক্ত করা হয়েছে!</b>\n\n🎬 নাম: <b>{title}</b>", parse_mode="HTML", reply_markup=kb)
        except: pass
        del admin_temp[uid]
        await m.answer(f"🎉 <b>{title}</b> ম্যানুয়ালি যুক্ত করা হয়েছে!")
        return

    # --- ২. অটো আপলোড ফ্লো (/new) এবং /auto প্রসেসিং ---
    if uid in admin_cache and m.text and state == "auto_name":
        admin_temp[uid] = {"step": "auto_file", "title": m.text.strip()}
        await m.answer(f"✅ মুভি: <b>{m.text}</b>\nএবার ভিডিও ফাইলটি পাঠান (Max 2GB)।", parse_mode="HTML")
        return

    if uid in admin_cache and (m.document or m.video) and (state == "auto_file" or state == "auto_serial_mode"):
        file = m.video or m.document
        
        status_msg = await m.answer("⏳ প্রসেসিং শুরু হয়েছে... ভিডিও থেকে ৬টি ল্যান্ডস্কেপ স্ক্রিনশট নেওয়া হচ্ছে। দয়া করে অপেক্ষা করুন।")
        
        try:
            # --- /auto কমান্ডের জন্য নাম তৈরি ---
            if state == "auto_serial_mode":
                # ডাটাবেস থেকে সিরিয়াল কাউন্টার আপডেট করা
                serial_res = await db.settings.find_one_and_update(
                    {"id": "auto_post_count"},
                    {"$inc": {"value": 1}},
                    upsert=True,
                    return_document=True
                )
                count_val = serial_res.get('value', 1)
                title = f"🥵 New Hot Sex Video Number🥵 হট সেক্স ভাইরাল ভিডিও নাম্বার 🥵 {count_val}"
            else:
                title = admin_temp[uid]["title"]

            # --- Pyrogram দিয়ে ২ জিবি ফাইল ডাউনলোড লজিক (নিচে আপডেট করা হলো) ---
            # ২ জিবি এর ফাইল Bot API দিয়ে ডাউনলোড হয় না, তাই Pyrogram ব্যবহার করা হলো:
            video_path = await pyro_bot.download_media(m.video or m.document)
            grid_path = f"grid_poster_{uid}_{random.randint(100,999)}.jpg"
            
            # স্ক্রিনশট গ্রিড তৈরি (৬টি স্ক্রিনশট ২x৩ গ্রিড)
            res, duration_sec = await create_screenshot_grid(video_path, grid_path)
            duration_str = get_duration_display(duration_sec) if res else "0m"
            
            if res:
                # গ্রিড ফটো আপলোড
                with open(grid_path, 'rb') as f:
                    photo_msg = await bot.send_photo(m.chat.id, photo=types.BufferedInputFile(f.read(), filename="grid.jpg"), caption=f"🎬 {title}\n⏳ Duration: {duration_str}")
                    photo_id = photo_msg.photo[-1].file_id
                
                ftype = "video" if m.video else "document"
                
                await db.movies.insert_one({
                    "title": title, "photo_id": photo_id, "file_id": file.file_id, 
                    "file_type": ftype, "clicks": 0, "duration": duration_str, "created_at": datetime.datetime.utcnow()
                })
                
                me = await bot.get_me()
                kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🎬 মুভিটি দেখুন", url=f"https://t.me/{me.username}?start=new")]])
                await bot.send_photo(CHANNEL_ID, photo=photo_id, caption=f"🎥 <b>নতুন মুভি যুক্ত হয়েছে!</b>\n\n🎬 নাম: <b>{title}</b>", parse_mode="HTML", reply_markup=kb)
                
                await status_msg.edit_text(f"🎉 <b>{title}</b> স্ক্রিনশট সহ সফলভাবে যুক্ত হয়েছে!")
            else:
                await status_msg.edit_text("❌ স্ক্রিনশট নিতে ব্যর্থ হয়েছে। দয়া করে /post ব্যবহার করুন।")
            
            # ক্লিনআপ
            if os.path.exists(video_path): os.remove(video_path)
            if os.path.exists(grid_path): os.remove(grid_path)
            if uid in admin_temp: del admin_temp[uid]

        except Exception as e:
            await status_msg.edit_text(f"❌ এরর: {str(e)}")

# ==========================================
# ৪. ওয়েব অ্যাপ UI এবং APIs (আপনার আগের কোড)
# ==========================================

@app.get("/", response_class=HTMLResponse)
async def web_ui():
    ad_cfg = await db.settings.find_one({"id": "ad_config"})
    tg_cfg = await db.settings.find_one({"id": "link_tg"})
    b18_cfg = await db.settings.find_one({"id": "link_18"})
    sn_cfg = await db.settings.find_one({"id": "site_name"})
    nt_cfg = await db.settings.find_one({"id": "site_notice"})
    st_cfg = await db.settings.find_one({"id": "step_config"})
    h_ad = await db.settings.find_one({"id": "header_ad"})
    m_ad = await db.settings.find_one({"id": "middle_ad"})
    f_ad = await db.settings.find_one({"id": "footer_ad"})
    
    # নতুন সেটিংস ফেচ করা
    monetag_cfg = await db.settings.find_one({"id": "monetag_status"})
    adlink_cfg = await db.settings.find_one({"id": "adlink_status"})
    all_links = await db.ad_links.find().to_list(length=100)
    direct_links = [l['url'] for l in all_links] if all_links else []
    
    zone_id = ad_cfg['zone_id'] if ad_cfg else "10916755"
    tg_url = tg_cfg['url'] if tg_cfg else "https://t.me/MovieeBD"
    link_18 = b18_cfg['url'] if b18_cfg else "https://t.me/MovieeBD"
    site_name = sn_cfg['name'] if sn_cfg else "MovieZone"
    notice_text = nt_cfg['text'] if nt_cfg else "মুভি অ্যাপে স্বাগতম! মুভি ডাউনলোড করতে এড এর স্টেপ গুলো কমপ্লিট করুন।"
    total_steps = st_cfg['count'] if st_cfg else 2
    header_code = h_ad['code'] if h_ad else ""
    middle_code = m_ad['code'] if m_ad else ""
    footer_code = f_ad['code'] if f_ad else ""
    monetag_on = monetag_cfg['status'] if monetag_cfg else True
    adlink_on = adlink_cfg['status'] if adlink_cfg else False

    html_code = r"""
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{{SITE_NAME}}</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        <style>
            * { margin:0; padding:0; box-sizing:border-box; }
            body { background:#0f172a; font-family: sans-serif; color:#fff; overflow-x:hidden; } 
            
            /* সাইট বক্স/কন্টেইনার */
            .main-container { max-width: 600px; margin: 0 auto; background: #0f172a; min-height: 100vh; }
            
            header { display:flex; justify-content:space-between; align-items:center; padding:15px; border-bottom:1px solid #1e293b; background:#0f172a; position:sticky; top:0; z-index:1000; }
            .logo { font-size:22px; font-weight:bold; }
            .logo span { background:red; color:#fff; padding:2px 5px; border-radius:5px; margin-left:5px; font-size:16px; }
            .user-info { display:flex; align-items:center; gap:8px; background:#1e293b; padding:5px 12px; border-radius:20px; font-weight:bold; font-size:14px; }
            .user-info img { width:26px; height:26px; border-radius:50%; object-fit:cover; }
            
            /* নোটিশ বার */
            .notice-bar { background: linear-gradient(90deg, #f87171, #ef4444); color: white; padding: 10px; font-size: 14px; font-weight: bold; text-align: center; overflow: hidden; white-space: nowrap; }
            .notice-content { display: inline-block; animation: scroll-text 15s linear infinite; }
            @keyframes scroll-text { 0% { transform: translateX(100%); } 100% { transform: translateX(-100%); } }

            /* ব্যানার অ্যাড স্লট */
            .ad-slot { width: 100%; display: flex; justify-content: center; align-items: center; padding: 10px 0; overflow: hidden; min-height: 50px; background: rgba(30, 41, 59, 0.5); margin: 5px 0; }

            .search-box { padding:15px; display: flex; align-items: center; gap: 10px; }
            .back-btn { background: #1e293b; color: white; border: none; padding: 10px 15px; border-radius: 50%; cursor: pointer; display: none; }
            .search-input { flex: 1; padding:14px; border-radius:25px; border:none; outline:none; text-align:center; background:#1e293b; color:#fff; font-size:16px; transition: 0.3s; }
            .search-input:focus { box-shadow: 0 0 10px rgba(248,113,113,0.5); }
            
            .section-title { padding: 5px 15px 10px; font-size: 18px; font-weight: bold; color: #f87171; display:flex; align-items:center; gap:8px;}
            
            .trending-container { display: flex; overflow-x: auto; gap: 12px; padding: 0 15px 20px; scroll-behavior: smooth; }
            .trending-container::-webkit-scrollbar { display: none; }
            .trending-card { min-width: 80%; background: #1e293b; border-radius: 12px; overflow: hidden; cursor: pointer; flex-shrink: 0; position:relative;}
            .trending-card img { height: 180px; object-fit:cover; width:100%; border-radius:10px; display:block; }
            .trending-card .post-content { padding:3px; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00); border-radius: 12px; }
            
            /* ১ কলাম গ্রিড ও ল্যান্ডস্কেপ কার্ড (Desktop & Mobile) */
            .grid { padding:0 15px 20px; display: grid; grid-template-columns: 1fr; gap: 20px; }
            .card { background:#1e293b; border-radius:12px; overflow:hidden; cursor:pointer; transition: transform 0.2s; border: 1px solid #334155; position: relative;}
            .card:active { transform: scale(0.98); }
            
            .post-content { 
                position:relative; padding: 3px; border-radius: 12px;
                background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000);
                background-size: 400%; animation: glowing 8s linear infinite;
            }
            @keyframes glowing { 0% { background-position: 0 0; } 50% { background-position: 400% 0; } 100% { background-position: 0 0; } }

            /* ল্যান্ডস্কেপ ইমেজ হাইট */
            .post-content img { width:100%; height:200px; object-fit:cover; display:block; border-radius: 10px; }
            
            /* ভিডিও ডিউরেশন ব্যাজ */
            .duration-badge { position: absolute; top: 10px; left: 10px; background: rgba(0,0,0,0.8); color: #fff; padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; z-index: 105; border: 1px solid rgba(255,255,255,0.2); }

            .tag { position:absolute; top:8px; right:8px; padding:4px 6px; border-radius:6px; font-weight:bold; font-size:10px; display:flex; align-items:center; gap:4px; box-shadow: 0 2px 5px rgba(0,0,0,0.5); z-index: 100;}
            .tag-locked { background:rgba(0,0,0,0.85); color:#f87171; border: 1px solid #f87171; }
            .tag-unlocked { background:rgba(0,0,0,0.85); color:#10b981; border: 1px solid #10b981; }
            
            /* পোস্টারের উপরে স্টেপ ওভারলে */
            .step-overlay { position: absolute; top: 10px; left: 10px; background: rgba(0,0,0,0.7); color: #f87171; padding: 4px 8px; border-radius: 6px; font-size: 12px; font-weight: bold; border: 1px solid #f87171; z-index: 100; display:none; }
            
            /* আনলক টাইমার ওভারলে */
            .unlock-timer { position: absolute; top: 10px; right: 10px; background: rgba(16, 185, 129, 0.9); color: white; padding: 4px 8px; border-radius: 6px; font-size: 12px; font-weight: bold; z-index: 101; display: none; }

            .top-badge { position:absolute; top:8px; left:8px; background:red; color:white; padding:3px 6px; border-radius:6px; font-size:10px; font-weight:bold; box-shadow: 0 2px 5px rgba(0,0,0,0.5); z-index:10; display:none;}
            .view-badge { position:absolute; bottom:8px; left:8px; background:rgba(0,0,0,0.7); color:#fff; padding:3px 6px; border-radius:6px; font-size:11px; font-weight:bold; display:flex; align-items:center; gap:4px; box-shadow: 0 2px 5px rgba(0,0,0,0.5); }

            .card-footer { padding:12px; font-size:16px; font-weight:bold; text-align:center; word-wrap: break-word; color:#e2e8f0; line-height:1.4; }
            
            .skeleton { background: #1e293b; border-radius: 12px; height: 180px; overflow: hidden; position: relative; }
            .skeleton::after { content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.05), transparent); animation: shimmer 1.5s infinite; }
            @keyframes shimmer { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }

            .pagination { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 10px 15px 120px; flex-wrap: wrap; }
            .page-btn { background: #1e293b; color: #fff; border: 1px solid #334155; padding: 10px 15px; border-radius: 8px; cursor: pointer; font-weight: bold; transition: 0.2s; outline: none; }
            .page-btn.active { background: #f87171; border-color: #f87171; color: white; }
            .page-btn:disabled { opacity: 0.4; cursor: not-allowed; }

            .floating-btn { position:fixed; right:20px; color:white; width:50px; height:50px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:20px; font-weight:bold; z-index:500; cursor:pointer; box-shadow: 0 4px 10px rgba(0,0,0,0.5); }
            .btn-18 { bottom:155px; background:red; border:2px solid #fff; }
            .btn-tg { bottom:95px; background:#24A1DE; }
            .btn-req { bottom:35px; background:#10b981; }

            /* অ্যাড স্ক্রিন নতুন ডিজাইন (ক্লিক সিস্টেমের জন্য আপডেট করা) */
            .ad-screen { position:fixed; top:0; left:0; width:100%; height:100%; background:#0f172a; display:none; flex-direction:column; align-items:center; justify-content:center; z-index:2000; padding: 20px; }
            .screen-back { position: absolute; top: 20px; left: 20px; background: #1e293b; color: #f87171; padding: 10px 15px; border-radius: 20px; cursor: pointer; font-weight: bold; display: flex; align-items: center; gap: 5px; border: 1px solid #f87171; }
            
            .ad-poster-img { width: 100%; max-width: 300px; height: 180px; object-fit: cover; border-radius: 15px; margin-bottom: 20px; border: 2px solid #f87171; }
            .progress-container { width: 100%; max-width: 300px; background: #1e293b; height: 10px; border-radius: 5px; margin-bottom: 10px; overflow: hidden; }
            .progress-bar { height: 100%; background: #f87171; width: 0%; transition: width 0.3s; }
            .step-text { font-size: 18px; font-weight: bold; color: #f87171; margin-bottom: 5px; }
            .percent-text { font-size: 14px; color: #94a3b8; margin-bottom: 15px; }
            .timer { width:80px; height:80px; border-radius:50%; border:5px solid red; display:flex; align-items:center; justify-content:center; font-size:30px; margin-bottom:20px; color:red; font-weight:bold; }
            
            /* নতুন বাটন ডিজাইন */
            .btn-visit { background:#f87171; color:white; border:none; padding:15px 30px; border-radius:30px; font-size:18px; font-weight:bold; cursor:pointer; width:80%; text-align:center; text-decoration:none; display:block; margin-top:10px;}
            .btn-next { background:#10b981; color:white; border:none; padding:15px 30px; border-radius:30px; font-size:18px; font-weight:bold; cursor:pointer; display:none; width:80%; margin-top:10px; }

            .modal { position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); display:none; align-items:center; justify-content:center; z-index:3000; }
            .modal-content { background:#1e293b; width:90%; padding:30px; border-radius:15px; text-align:center; }
            .req-input { width: 100%; padding: 12px; margin: 15px 0; border-radius: 8px; border: none; background: #0f172a; color: white; outline:none; }
            .btn-submit { background: #10b981; color: white; border: none; padding: 10px 20px; border-radius: 8px; font-weight: bold; width:100%; font-size:16px;}
        </style>
    </head>
    <body>
        <div class="main-container">
            <header>
                <div class="logo">{{SITE_NAME}} <span>BD</span></div>
                <div class="user-info"><span id="uName">Guest</span><img id="uPic" src="https://cdn-icons-png.flaticon.com/512/3135/3135715.png"></div>
            </header>

            <div class="notice-bar">
                <div class="notice-content">{{NOTICE_TEXT}}</div>
            </div>

            <div class="ad-slot">{{HEADER_AD}}</div>

            <div class="search-box">
                <button class="back-btn" id="searchBack" onclick="clearSearch()"><i class="fa-solid fa-arrow-left"></i></button>
                <input type="text" id="searchInput" class="search-input" placeholder="মুভি বা ওয়েব সিরিজ খুঁজুন...">
            </div>

            <div class="ad-slot">{{MIDDLE_AD}}</div>

            <div id="trendingWrapper">
                <div class="section-title"><i class="fa-solid fa-fire"></i> ট্রেন্ডিং মুভি</div>
                <div class="trending-container" id="trendingGrid">
                    <div class="skeleton" style="min-width:80%; height:180px;"></div>
                </div>
            </div>

            <div class="section-title"><i class="fa-solid fa-film"></i> নতুন সব মুভি</div>
            <div class="grid" id="movieGrid"></div>
            
            <div class="ad-slot">{{FOOTER_AD}}</div>

            <div class="pagination" id="paginationBox"></div>
        </div>

        <div id="adScreen" class="ad-screen">
            <div class="screen-back" onclick="closeAdScreen()"><i class="fa-solid fa-arrow-left"></i> ফিরে যান</div>
            <img id="adMoviePoster" class="ad-poster-img" src="">
            <div class="step-text" id="stepLabel">Step 1/2</div>
            <div class="percent-text" id="percentLabel">0% Completed</div>
            <div id="adsRemaining" style="font-size: 14px; color: #f87171; margin-bottom: 10px; font-weight: bold;"></div>
            <div class="progress-container"><div class="progress-bar" id="progressBar"></div></div>
            <div class="timer" id="timer">0</div>
            <p id="adMsg" style="text-align:center; margin-bottom:15px;">নিচের বাটনে ক্লিক করে অ্যাড দেখুন এবং মুভি আনলক করুন।</p>
            
            <a href="#" target="_blank" class="btn-visit" id="btnVisit" onclick="startTimer()">Visit Ad & Unlock</a>
            <button class="btn-next" id="btnNext" onclick="handleNextStep()">Next Step <i class="fa-solid fa-arrow-right"></i></button>
        </div>

        <div id="successModal" class="modal">
            <div class="modal-content">
                <i class="fa-solid fa-circle-check" style="font-size:60px; color:#10b981;"></i>
                <h2 style="margin:15px 0;">সম্পন্ন হয়েছে!</h2>
                <p style="margin-bottom: 20px; color:gray; font-size:14px;">বটের ইনবক্স চেক করুন। <br><span style="color:#f87171;">সতর্কতা: মুভিটি কিছুক্ষণ পর অটোমেটিক ডিলিট হয়ে যাবে।</span></p>
                <button class="btn-submit" onclick="tg.close()">বটে ফিরে যান</button>
            </div>
        </div>

        <div id="reqModal" class="modal">
            <div class="modal-content">
                <h2>মুভি রিকোয়েস্ট করুন</h2>
                <input type="text" id="reqText" class="req-input" placeholder="মুভির নাম ও রিলিজ সাল লিখুন...">
                <button class="btn-submit" onclick="sendReq()">সাবমিট করুন</button>
                <p style="margin-top:15px; color:gray; cursor:pointer;" onclick="document.getElementById('reqModal').style.display='none'">বাতিল করুন</p>
            </div>
        </div>

        <div class="floating-btn btn-18" onclick="window.open('{{LINK_18}}')">18+</div>
        <div class="floating-btn btn-tg" onclick="window.open('{{TG_LINK}}')"><i class="fa-brands fa-telegram"></i></div>
        <div class="floating-btn btn-req" onclick="openReqModal()"><i class="fa-solid fa-code-pull-request"></i></div>

        <script>
            let tg = window.Telegram.WebApp; tg.expand();
            const ZONE_ID = "{{ZONE_ID}}";
            const MONETAG_ON = {{MONETAG_ON}};
            const ADLINK_ON = {{ADLINK_ON}};
            const DIRECT_LINKS = {{DIRECT_LINKS}};
            const TOTAL_STEPS = {{TOTAL_STEPS}};
            
            let currentPage = 1; let isLoading = false; let searchQuery = "";
            let uid = tg.initDataUnsafe.user?.id || 0;
            let currentAdStep = 1;
            let currentMovieId = "";

            // ইংলিশ ভয়েস ফাংশন
            function playVoice(text) {
                const msg = new SpeechSynthesisUtterance();
                msg.text = text;
                msg.lang = 'en-US';
                window.speechSynthesis.speak(msg);
            }

            if(tg.initDataUnsafe && tg.initDataUnsafe.user) {
                document.getElementById('uName').innerText = tg.initDataUnsafe.user.first_name;
                if(tg.initDataUnsafe.user.photo_url) document.getElementById('uPic').src = tg.initDataUnsafe.user.photo_url;
            }

            // Monetag লোড (যদি ON থাকে)
            if(MONETAG_ON) {
                const s = document.createElement('script');
                s.src = '//libtl.com/sdk.js'; s.setAttribute('data-zone', ZONE_ID); s.setAttribute('data-sdk', 'show_' + ZONE_ID);
                document.head.appendChild(s);
            }

            function drawSkeletons(count) {
                let html = ""; for(let i=0; i<count; i++) html += `<div class="skeleton"></div>`; return html;
            }

            function startAutoScroll() {
                setInterval(() => {
                    let grid = document.getElementById('trendingGrid');
                    if(grid) {
                        let cardWidth = 300;
                        if (grid.scrollLeft >= (grid.scrollWidth - grid.clientWidth - 10)) {
                            grid.scrollTo({ left: 0, behavior: 'smooth' });
                        } else {
                            grid.scrollBy({ left: cardWidth, behavior: 'smooth' });
                        }
                    }
                }, 3000);
            }

            function formatTime(seconds) {
                const h = Math.floor(seconds / 3600);
                const m = Math.floor((seconds % 3600) / 60);
                const s = Math.floor(seconds % 60);
                let res = "";
                if(h > 0) res += h + "h ";
                if(m > 0) res += m + "m ";
                if(s > 0 || res==="") res += s + "s";
                return res;
            }

            function startLiveCountdown(elementId, seconds) {
                let timeLeft = seconds;
                const element = document.getElementById(elementId);
                if (!element) return;
                
                const interval = setInterval(() => {
                    timeLeft--;
                    if (timeLeft <= 0) {
                        element.style.display = 'none';
                        clearInterval(interval);
                        loadMovies(currentPage);
                    } else {
                        element.innerText = formatTime(timeLeft);
                        element.style.display = 'block';
                    }
                }, 1000);
            }

            async function loadTrending() {
                try {
                    const r = await fetch(`/api/trending?uid=${uid}`);
                    const data = await r.json();
                    const grid = document.getElementById('trendingGrid');
                    if(data.length === 0) {
                        document.getElementById('trendingWrapper').style.display = 'none';
                        return;
                    }
                    grid.innerHTML = data.map(m => {
                        let tagHtml = m.is_unlocked ? `<div class="tag tag-unlocked"><i class="fa-solid fa-unlock"></i></div>` : `<div class="tag tag-locked"><i class="fa-solid fa-lock"></i></div>`;
                        let timerHtml = m.is_unlocked ? `<div class="unlock-timer" id="trend_timer_${m._id}"></div>` : `<div class="step-overlay">Steps: 0/${TOTAL_STEPS}</div>`;
                        
                        const html = `
                        <div class="trending-card" onclick="handleMovieClick('${m._id}', ${m.is_unlocked}, '${m.photo_id}')">
                            <div class="post-content">
                                <div class="duration-badge">${m.duration || '0m'}</div>
                                <div class="top-badge">🔥 TOP</div>
                                ${timerHtml}
                                <img src="/api/image/${m.photo_id}" onerror="this.src='https://via.placeholder.com/400x200?text=No+Image'">
                                ${tagHtml}
                                <div class="view-badge"><i class="fa-solid fa-eye"></i> ${m.clicks}</div>
                            </div>
                            <div class="card-footer" style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${m.title}</div>
                        </div>`;
                        
                        return html;
                    }).join('');
                    
                    data.forEach(m => {
                        if(m.is_unlocked && m.rem_sec > 0) startLiveCountdown(`trend_timer_${m._id}`, m.rem_sec);
                    });

                    setTimeout(startAutoScroll, 2000);
                } catch(e) {}
            }

            async function loadMovies(page = 1) {
                if(isLoading) return;
                isLoading = true;
                currentPage = page;
                const grid = document.getElementById('movieGrid');
                const pBox = document.getElementById('paginationBox');
                grid.innerHTML = drawSkeletons(10);
                pBox.innerHTML = "";

                try {
                    const r = await fetch(`/api/list?page=${currentPage}&q=${searchQuery}&uid=${uid}`);
                    const data = await r.json();
                    if(data.movies.length === 0) {
                        grid.innerHTML = "<p style='grid-column: span 1; text-align:center; color:gray; padding:20px;'>কোনো মুভি পাওয়া যায়নি!</p>";
                    } else {
                        grid.innerHTML = data.movies.map(m => {
                            let tagHtml = m.is_unlocked ? `<div class="tag tag-unlocked"><i class="fa-solid fa-unlock"></i></div>` : `<div class="tag tag-locked"><i class="fa-solid fa-lock"></i></div>`;
                            let timerHtml = m.is_unlocked ? `<div class="unlock-timer" id="list_timer_${m._id}"></div>` : `<div class="step-overlay">Steps: 0/${TOTAL_STEPS}</div>`;
                            
                            return `
                            <div class="card" onclick="handleMovieClick('${m._id}', ${m.is_unlocked}, '${m.photo_id}')">
                                <div class="post-content">
                                    <div class="duration-badge">${m.duration || '0m'}</div>
                                    ${timerHtml}
                                    <img src="/api/image/${m.photo_id}" onerror="this.src='https://via.placeholder.com/400x200?text=No+Image'">
                                    ${tagHtml}
                                    <div class="view-badge"><i class="fa-solid fa-eye"></i> ${m.clicks}</div>
                                </div>
                                <div class="card-footer">${m.title}</div>
                            </div>`;
                        }).join('');
                        
                        data.movies.forEach(m => {
                            if(m.is_unlocked && m.rem_sec > 0) startLiveCountdown(`list_timer_${m._id}`, m.rem_sec);
                        });
                        
                        renderPagination(data.total_pages);
                    }
                } catch(e) {}
                isLoading = false;
            }

            function renderPagination(totalPages) {
                if (totalPages <= 1) return;
                let html = "";
                html += `<button class="page-btn" ${currentPage === 1 ? 'disabled' : ''} onclick="goToPage(${currentPage - 1})"><i class="fa-solid fa-angle-left"></i></button>`;
                let start = Math.max(1, currentPage - 1);
                let end = Math.min(totalPages, currentPage + 1);
                for (let i = start; i <= end; i++) { html += `<button class="page-btn ${i === currentPage ? 'active' : ''}" onclick="goToPage(${i})">${i}</button>`; }
                html += `<button class="page-btn" ${currentPage === totalPages ? 'disabled' : ''} onclick="goToPage(${currentPage + 1})"><i class="fa-solid fa-angle-right"></i></button>`;
                document.getElementById('paginationBox').innerHTML = html;
            }

            function goToPage(p) {
                if (p < 1) return;
                loadMovies(p);
                window.scrollTo({ top: document.getElementById('movieGrid').offsetTop - 100, behavior: 'smooth' });
            }

            let timeout = null;
            document.getElementById('searchInput').addEventListener('input', function(e) {
                clearTimeout(timeout); searchQuery = e.target.value.trim();
                if(searchQuery !== "") {
                    document.getElementById('trendingWrapper').style.display = 'none';
                    document.getElementById('searchBack').style.display = 'block';
                }
                else { 
                    clearSearch();
                }
                timeout = setTimeout(() => { loadMovies(1); }, 500); 
            });

            function clearSearch() {
                searchQuery = "";
                document.getElementById('searchInput').value = "";
                document.getElementById('searchBack').style.display = 'none';
                document.getElementById('trendingWrapper').style.display = 'block';
                loadTrending();
                loadMovies(1);
            }

            function handleMovieClick(id, isUnlocked, photoId) {
                if(isUnlocked) {
                    sendFile(id);
                } else {
                    currentMovieId = id;
                    currentAdStep = 1;
                    document.getElementById('adMoviePoster').src = `/api/image/${photoId}`;
                    showAdScreen();
                }
            }

            function showAdScreen() {
                document.getElementById('adScreen').style.display = 'flex';
                document.getElementById('stepLabel').innerText = `Step ${currentAdStep}/${TOTAL_STEPS}`;
                let progress = Math.round(((currentAdStep - 1) / TOTAL_STEPS) * 100);
                document.getElementById('percentLabel').innerText = `${progress}% Completed`;
                document.getElementById('progressBar').style.width = `${progress}%`;
                
                let remaining = TOTAL_STEPS - currentAdStep + 1;
                document.getElementById('adsRemaining').innerText = `বাকি আছে: ${remaining}টি অ্যাড`;
                
                document.getElementById('btnVisit').style.display = 'block';
                document.getElementById('btnVisit').style.pointerEvents = 'auto';
                document.getElementById('btnVisit').style.opacity = '1';
                document.getElementById('btnNext').style.display = 'none';
                document.getElementById('timer').innerText = "15";
                
                let useMonetag = (MONETAG_ON && (currentAdStep % 2 === 0 || !ADLINK_ON));
                window.currentUseMonetag = useMonetag;

                if(useMonetag) {
                    document.getElementById('adMsg').innerText = "নিচের আনলক বাটনে ক্লিক করুন এবং অ্যাড লোড হওয়া পর্যন্ত অপেক্ষা করুন।";
                    document.getElementById('btnVisit').innerText = "Unlock Movie (Click)";
                    document.getElementById('btnVisit').href = "javascript:void(0)";
                } else {
                    document.getElementById('adMsg').innerText = "নিচের বাটনে ক্লিক করে অ্যাড ভিজিট করুন এবং ১৫ সেকেন্ড অপেক্ষা করুন।";
                    document.getElementById('btnVisit').innerText = "Visit Ad & Unlock";
                    let linkToOpen = DIRECT_LINKS[Math.floor(Math.random() * DIRECT_LINKS.length)] || "#";
                    document.getElementById('btnVisit').href = linkToOpen;
                }
            }

            function startTimer() {
                if(window.currentUseMonetag && MONETAG_ON && typeof window['show_' + ZONE_ID] === 'function') {
                    window['show_' + ZONE_ID]();
                }

                let t = 15;
                document.getElementById('btnVisit').style.pointerEvents = 'none';
                document.getElementById('btnVisit').style.opacity = '0.5';
                document.getElementById('adMsg').innerText = "অ্যাড দেখা হচ্ছে, দয়া করে কিছুক্ষণ অপেক্ষা করুন...";
                
                let iv = setInterval(() => {
                    t--;
                    document.getElementById('timer').innerText = t;
                    if(t <= 0) {
                        clearInterval(iv);
                        document.getElementById('btnVisit').style.display = 'none';
                        document.getElementById('btnNext').style.display = 'block';
                        document.getElementById('adMsg').innerText = "এই ধাপটি সফলভাবে সম্পন্ন হয়েছে! Next বাটনে ক্লিক করুন।";
                        playVoice("Verification complete, please click next step.");
                    }
                }, 1000);
            }

            function handleNextStep() {
                if(currentAdStep < TOTAL_STEPS) {
                    currentAdStep++;
                    showAdScreen();
                } else {
                    document.getElementById('progressBar').style.width = `100%`;
                    document.getElementById('percentLabel').innerText = `100% Completed`;
                    sendFile(currentMovieId);
                }
            }

            function closeAdScreen() { document.getElementById('adScreen').style.display = 'none'; }

            async function sendFile(id) {
                await fetch('/api/send', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({userId: uid, movieId: id})});
                document.getElementById('adScreen').style.display = 'none';
                document.getElementById('successModal').style.display = 'flex';
                playVoice("Movie sent to your inbox, check it now.");
                setTimeout(() => { loadTrending(); loadMovies(currentPage); }, 1000); 
            }

            function openReqModal() { document.getElementById('reqModal').style.display = 'flex'; }
            
            async function sendReq() {
                const text = document.getElementById('reqText').value;
                if(!text) return alert('মুভির নাম লিখুন!');
                await fetch('/api/request', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({uid: uid, uname: tg.initDataUnsafe.user?.first_name || 'Guest', movie: text})});
                document.getElementById('reqModal').style.display = 'none';
                document.getElementById('reqText').value = '';
                alert('রিকোয়েস্ট সফলভাবে পাঠানো হয়েছে!');
            }

            loadTrending();
            loadMovies(1); 
        </script>
    </body>
    </html>
    """
    final_html = html_code.replace("{{ZONE_ID}}", zone_id).replace("{{TG_LINK}}", tg_url).replace("{{LINK_18}}", link_18).replace("{{SITE_NAME}}", site_name)
    final_html = final_html.replace("{{NOTICE_TEXT}}", notice_text).replace("{{TOTAL_STEPS}}", str(total_steps))
    final_html = final_html.replace("{{HEADER_AD}}", header_code).replace("{{MIDDLE_AD}}", middle_code).replace("{{FOOTER_AD}}", footer_code)
    
    # নতুন সুইচ এবং লিঙ্কের ভ্যালু রিপ্লেস
    final_html = final_html.replace("{{MONETAG_ON}}", "true" if monetag_on else "false")
    final_html = final_html.replace("{{ADLINK_ON}}", "true" if adlink_on else "false")
    final_html = final_html.replace("{{DIRECT_LINKS}}", str(direct_links))
    
    return final_html

# ==========================================
# ৫. এপিআই সেকশন (আপনার আগের কোড)
# ==========================================

# নতুন টাইম পার্সার ফাংশন
def parse_duration(time_str):
    total_seconds = 0
    hours = re.search(r'(\d+)h', time_str)
    minutes = re.search(r'(\d+)m', time_str)
    seconds = re.search(r'(\d+)s', time_str)
    if hours: total_seconds += int(hours.group(1)) * 3600
    if minutes: total_seconds += int(minutes.group(1)) * 60
    if seconds: total_seconds += int(seconds.group(1))
    if not any([hours, minutes, seconds]) and time_str.isdigit():
        total_seconds = int(time_str) * 3600
    return total_seconds if total_seconds > 0 else 86400

import re

@app.get("/api/trending")
async def trending_movies(uid: int = 0):
    unlock_cfg = await db.settings.find_one({"id": "unlock_config"})
    if unlock_cfg:
        if 'seconds' in unlock_cfg: u_seconds = unlock_cfg['seconds']
        else: u_seconds = unlock_cfg.get('hours', 24) * 3600
    else: u_seconds = 86400

    unlocked_map = {}
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(seconds=u_seconds)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            expires_at = u["unlocked_at"] + datetime.timedelta(seconds=u_seconds)
            unlocked_map[u["movie_id"]] = int((expires_at - datetime.datetime.utcnow()).total_seconds())

    movies = []
    async for m in db.movies.find().sort("clicks", -1).limit(10):
        m_id = str(m["_id"])
        m["_id"] = m_id
        m["clicks"] = m.get("clicks", 0)
        m["duration"] = m.get("duration", "0m")
        m["is_unlocked"] = m_id in unlocked_map 
        m["rem_sec"] = unlocked_map.get(m_id, 0)
        movies.append(m)
    return movies

@app.get("/api/list")
async def list_movies(page: int = 1, q: str = "", uid: int = 0):
    limit = 16
    skip = (page - 1) * limit
    query = {"title": {"$regex": q, "$options": "i"}} if q else {}
    
    unlock_cfg = await db.settings.find_one({"id": "unlock_config"})
    if unlock_cfg:
        if 'seconds' in unlock_cfg: u_seconds = unlock_cfg['seconds']
        else: u_seconds = unlock_cfg.get('hours', 24) * 3600
    else: u_seconds = 86400

    unlocked_map = {}
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(seconds=u_seconds)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            expires_at = u["unlocked_at"] + datetime.timedelta(seconds=u_seconds)
            unlocked_map[u["movie_id"]] = int((expires_at - datetime.datetime.utcnow()).total_seconds())

    movies = []
    async for m in db.movies.find(query).sort("created_at", -1).skip(skip).limit(limit):
        m_id = str(m["_id"])
        m["_id"] = m_id
        m["clicks"] = m.get("clicks", 0)
        m["duration"] = m.get("duration", "0m")
        m["is_unlocked"] = m_id in unlocked_map 
        m["rem_sec"] = unlocked_map.get(m_id, 0)
        movies.append(m)
        
    return {"movies": movies}

# কমান্ড প্রসেসিং এ টাইম আপডেট করার জন্য
@dp.message(Command("setunlocktime"))
async def set_unlock_time_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        raw_val = m.text.split(" ", 1)[1]
        seconds = parse_duration(raw_val)
        await db.settings.update_one({"id": "unlock_config"}, {"$set": {"seconds": seconds, "raw": raw_val}}, upsert=True)
        await m.answer(f"✅ মুভি আনলক থাকার সময় সেট করা হয়েছে: <b>{raw_val}</b>", parse_mode="HTML")
    except: await m.answer("⚠️ নিয়ম: `/setunlocktime 1h,10m,30s`", parse_mode="HTML")

@app.get("/api/image/{photo_id}")
async def get_image(photo_id: str):
    try:
        file_info = await bot.get_file(photo_id)
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
        async def stream_image():
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url) as resp:
                    async for chunk in resp.content.iter_chunked(1024): yield chunk
        return StreamingResponse(stream_image(), media_type="image/jpeg")
    except: return {"error": "not found"}

@app.post("/api/send")
async def send_file(d: dict = Body(...)):
    uid = d['userId']
    mid = d['movieId']
    if uid == 0: return {"ok": False}
    try:
        m = await db.movies.find_one({"_id": ObjectId(mid)})
        if m:
            time_cfg = await db.settings.find_one({"id": "del_time"})
            del_minutes = time_cfg['minutes'] if time_cfg else 60
            
            protect_cfg = await db.settings.find_one({"id": "protect_content"})
            is_protected = protect_cfg['status'] if protect_cfg else True
            
            caption = f"🎥 <b>{m['title']}</b>\n\n⏳ <b>সতর্কতা:</b> কপিরাইট এড়াতে মুভিটি <b>{del_minutes} মিনিট</b> পর অটো-ডিলিট হয়ে যাবে। দয়া করে এখনই ফরওয়ার্ড বা সেভ করে নিন!\n\n📥 Join: @PronoHub_1"
            
            sent_msg = None
            if m.get("file_type") == "video": 
                sent_msg = await bot.send_video(uid, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            else: 
                sent_msg = await bot.send_document(uid, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            
            await db.movies.update_one({"_id": ObjectId(mid)}, {"$inc": {"clicks": 1}})
            await db.user_unlocks.update_one({"user_id": uid, "movie_id": mid}, {"$set": {"unlocked_at": datetime.datetime.utcnow()}}, upsert=True)
            
            if sent_msg:
                delete_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=del_minutes)
                await db.auto_delete.insert_one({"chat_id": uid, "message_id": sent_msg.message_id, "delete_at": delete_at})
    except Exception: pass
    return {"ok": True}

class ReqModel(BaseModel):
    uid: int; uname: str; movie: str

@app.post("/api/request")
async def handle_request(data: ReqModel):
    try: 
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ রিপ্লাই দিন", callback_data=f"reply_{data.uid}")
        
        await bot.send_message(
            OWNER_ID, 
            f"🔔 <b>নতুন মুভি রিকোয়েস্ট!</b>\n\n👤 ইউজার: {data.uname} (<code>{data.uid}</code>)\n🎬 মুভির নাম: <b>{data.movie}</b>", 
            parse_mode="HTML", 
            reply_markup=builder.as_markup()
        )
    except: pass
    return {"ok": True}

async def start():
    await load_admins()
    await pyro_bot.start() # Pyrogram ক্লায়েন্ট স্টার্ট করা হলো (২ জিবি ফাইল সাপোর্ট করার জন্য)
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    
    asyncio.create_task(auto_delete_worker())
    
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(server.serve(), dp.start_polling(bot))

if __name__ == "__main__": asyncio.run(start())
