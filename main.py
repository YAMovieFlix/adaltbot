import os, asyncio, datetime, uvicorn, random, re, subprocess, uuid
import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from PIL import Image
from pyrogram import Client as PyroClient

# ==========================================
# কনফিগারেশন সেটিংস
# ==========================================
TOKEN = os.getenv("BOT_TOKEN", "8615600822:AAGj3eUYdhRc0_uK18fpw0UzmgyGrdc9glU")
MONGO_URL = os.getenv("MONGO_URL", "mongodb+srv://akash:akash@cluster0.etisrpx.mongodb.net/?appName=Cluster0")
OWNER_ID = int(os.getenv("ADMIN_ID", "7525127704")) 
APP_URL = os.getenv("APP_URL", "https://rare-rori-yeasinvai-bf8e2c68.koyeb.app/").rstrip('/')
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1003275619931") 
MAIN_CHANNEL_ID = int(CHANNEL_ID)

API_ID = int(os.getenv("API_ID", "29904834"))
API_HASH = os.getenv("API_HASH", "8b4fd9ef578af114502feeafa2d31938")

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# বড় ফাইল (২ জিবি পর্যন্ত) ডাউনলোডের জন্য পাইরোগ্রাম ক্লায়েন্ট
pyro_bot = PyroClient("bot_pyro_store_session", api_id=API_ID, api_hash=API_HASH, bot_token=TOKEN)

client = AsyncIOMotorClient(MONGO_URL)
db = client['file_store_income_db']

admin_temp = {}
admin_cache = set([OWNER_ID])

# ==========================================
# হেল্পার ফাংশন সমূহ (Duration, Grid, Parser)
# ==========================================
def get_duration_display(seconds):
    try:
        seconds = int(float(seconds))
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h > 0: return f"{h}h {m}m"
        return f"{m}m {s}s"
    except: return "0m"

async def create_screenshot_grid(video_path, output_path):
    try:
        cmd = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
        duration = float(subprocess.check_output(cmd, shell=True).decode().strip())
        
        interval = duration / 7
        screenshots = []
        for i in range(1, 7):
            time_pos = i * interval
            temp_out = f"temp_frame_{i}_{random.randint(100,999)}.jpg"
            subprocess.run(f'ffmpeg -ss {time_pos} -i "{video_path}" -vframes 1 -q:v 2 "{temp_out}" -y', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(temp_out):
                screenshots.append(temp_out)
        
        if not screenshots: return False, 0
        
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

# ডাইনামিক শর্টনার কল
async def get_short_url(long_url: str):
    shortener = await db.shorteners.find_one({"active": True})
    if not shortener:
        return None
    
    domain = shortener.get("domain", "").strip().rstrip("/")
    api_key = shortener.get("api_key", "").strip()
    
    if not domain.startswith("http"):
        domain = f"https://{domain}"

    api_url = f"{domain}/api?api={api_key}&url={long_url}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=10) as resp:
                data = await resp.json()
                if "shortenedUrl" in data: return data["shortenedUrl"]
                if "url" in data: return data["url"]
                if "short" in data: return data["short"]
    except Exception as e:
        print(f"Shortener API Error: {e}")
    return None

# অটো-ডিলিট ব্যাকগ্রাউন্ড ওয়ার্কার
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
        await asyncio.sleep(30)

# ==========================================
# ব্লিংক ইন্ডেক্সিং ওয়ার্কার
# ==========================================
async def blink_worker(chat_id, last_id, admin_id):
    await bot.send_message(admin_id, f"🚀 <b>ব্লিংক প্রসেস শুরু হয়েছে!</b>\nআইডি 1 থেকে {last_id} পর্যন্ত ফাইল চেক করে অটো আপলোড করা হচ্ছে।", parse_mode="HTML")
    success_count = 0
    me = await bot.get_me()
    
    for current_id in range(1, last_id + 1):
        try:
            msg = await pyro_bot.get_messages(chat_id, current_id)
            if not msg or msg.empty: continue
            
            if msg.video or (msg.document and "video" in (msg.document.mime_type or "")):
                serial_res = await db.settings.find_one_and_update(
                    {"id": "auto_post_count"},
                    {"$inc": {"value": 1}},
                    upsert=True,
                    return_document=True
                )
                count_val = serial_res.get('value', 1)
                title = f"🥵 New Hot Sex Video Number🥵 হট সেক্স ভাইরাল ভিডিও নাম্বার 🥵 {count_val}"

                video_path = await pyro_bot.download_media(msg)
                grid_path = f"grid_blink_{random.randint(1000,9999)}.jpg"
                
                res, duration_sec = await create_screenshot_grid(video_path, grid_path)
                duration_str = get_duration_display(duration_sec) if res else "0m"

                tg_fid = msg.video.file_id if msg.video else msg.document.file_id
                
                ins = await db.files.insert_one({
                    "title": title, "caption": title, "tg_file_id": tg_fid,
                    "file_type": "video", "channel_id": chat_id, "msg_id": current_id,
                    "duration": duration_str, "category": "main", "created_at": datetime.datetime.utcnow()
                })
                
                get_link = f"https://t.me/{me.username}?start=getfile_{str(ins.inserted_id)}"
                kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="⚡ গেট ফাইল (ডাউনলোড)", url=get_link)]])

                if res:
                    with open(grid_path, 'rb') as f:
                        await bot.send_photo(
                            CHANNEL_ID,
                            photo=types.BufferedInputFile(f.read(), filename="grid.jpg"), 
                            caption=f"🎥 <b>নতুন ফাইল যুক্ত হয়েছে!</b>\n\n🎬 নাম: <b>{title}</b>\n⏳ সময়: <code>{duration_str}</code>",
                            parse_mode="HTML",
                            reply_markup=kb
                        )
                else:
                    await bot.send_message(
                        CHANNEL_ID,
                        f"🎥 <b>নতুন ফাইল যুক্ত হয়েছে!</b>\n\n🎬 নাম: <b>{title}</b>\n⏳ সময়: <code>{duration_str}</code>",
                        parse_mode="HTML",
                        reply_markup=kb
                    )
                    
                success_count += 1
                if os.path.exists(video_path): os.remove(video_path)
                if os.path.exists(grid_path): os.remove(grid_path)
                await asyncio.sleep(1.5)
        except Exception:
            continue

    await bot.send_message(admin_id, f"✅ <b>ব্লিংক ইন্ডেক্স সম্পন্ন!</b>\nমোট <b>{success_count}</b>টি ফাইল সফলভাবে স্টোর হয়েছে।", parse_mode="HTML")

# ==========================================
# /start ও ফাইল ডেলিভারি সিস্টেম
# ==========================================
@dp.message(Command("start"))
async def start_handler(m: types.Message):
    await db.users.update_one({"user_id": m.from_user.id}, {"$set": {"first_name": m.from_user.first_name}}, upsert=True)
    args = m.text.split()
    bot_info = await bot.get_me()

    # ১. ভেরিফিকেশন লিঙ্ক পার হয়ে আসলে (verify_TOKEN)
    if len(args) > 1 and args[1].startswith("verify_"):
        token = args[1].replace("verify_", "")
        token_doc = await db.verify_tokens.find_one({"token": token, "user_id": m.from_user.id})
        
        if not token_doc:
            return await m.answer("❌ <b>ভেরিফিকেশন লিঙ্কটির মেয়াদ শেষ অথবা এটি অবৈধ!</b>\nদয়া করে ফাইলে আবার ক্লিক করুন।", parse_mode="HTML")
        
        file_id = token_doc["file_id"]
        file_data = await db.files.find_one({"_id": ObjectId(file_id)})
        if not file_data:
            return await m.answer("⚠️ দুঃখিত, ফাইলটি ডাটাবেস থেকে মুছে ফেলা হয়েছে!")

        # ফাইল পাঠানোর প্রক্রিয়া
        time_cfg = await db.settings.find_one({"id": "del_time"})
        del_minutes = time_cfg['minutes'] if time_cfg else 60
        protect_cfg = await db.settings.find_one({"id": "protect_content"})
        is_protected = protect_cfg['status'] if protect_cfg else True

        title_show = file_data.get('caption') or file_data.get('title') or "আপনার ফাইল"
        caption = f"🎬 <b>{title_show}</b>\n\n⏳ <b>সতর্কতা:</b> কপিরাইট এড়াতে ফাইলটি <b>{del_minutes} মিনিট</b> পর স্বয়ংক্রিয়ভাবে মুছে যাবে। এখনি ফরওয়ার্ড বা সেভ করে নিন!"
        
        sent = None
        if file_data.get("file_type") == "video":
            sent = await bot.send_video(m.from_user.id, file_data["tg_file_id"], caption=caption, parse_mode="HTML", protect_content=is_protected)
        else:
            sent = await bot.send_document(m.from_user.id, file_data["tg_file_id"], caption=caption, parse_mode="HTML", protect_content=is_protected)

        if sent:
            del_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=del_minutes)
            await db.auto_delete.insert_one({"chat_id": m.from_user.id, "message_id": sent.message_id, "delete_at": del_at})
        
        await db.verify_tokens.delete_one({"_id": token_doc["_id"]})
        return

    # ২. ফাইল ডাউনলোড রিকোয়েস্ট (getfile_ID)
    if len(args) > 1 and args[1].startswith("getfile_"):
        raw_id = args[1].replace("getfile_", "")
        file_data = await db.files.find_one({"_id": ObjectId(raw_id)})
        if not file_data:
            return await m.answer("⚠️ ফাইলটি খুঁজে পাওয়া যায়নি!")

        # টোকেন তৈরি
        token = f"tok_{uuid.uuid4().hex[:12]}"
        await db.verify_tokens.insert_one({
            "token": token, "user_id": m.from_user.id, "file_id": raw_id, "created_at": datetime.datetime.utcnow()
        })
        
        tg_verify_return = f"https://t.me/{bot_info.username}?start=verify_{token}"

        # শর্টনার চেক
        shortened_link = await get_short_url(tg_verify_return)
        
        if shortened_link:
            # শর্টনার থাকলে শর্টনার বাটন দিবে
            kb = InlineKeyboardBuilder()
            kb.button(text="📥 আনলক ও ডাউনলোড করুন", url=shortened_link)
            return await m.answer(
                f"🎬 <b>ফাইল:</b> {file_data.get('caption') or file_data.get('title') or 'New File'}\n\n"
                "⚠️ <b>ফাইলটি আনলক করতে নিচের বাটনে ক্লিক করে শর্টনারের পেজটি কমপ্লিট করুন:</b>",
                reply_markup=kb.as_markup(),
                parse_mode="HTML"
            )
        else:
            # শর্টনার না থাকলে মনিটেগ ও ডিরেক্ট লিঙ্ক ভেরিফিকেশন URL
            ad_verify_url = f"{APP_URL}/verify?token={token}"
            kb = InlineKeyboardBuilder()
            kb.button(text="⚡ আনলক করুন (অ্যাড ভেরিফাই)", url=ad_verify_url)
            return await m.answer(
                f"🎬 <b>ফাইল:</b> {file_data.get('caption') or file_data.get('title') or 'New File'}\n\n"
                "⚠️ <b>ফাইলটি সম্পূর্ণ ফ্রিতে পেতে নিচের বাটনে ক্লিক করে অ্যাড ভেরিফিকেশন সম্পন্ন করুন:</b>",
                reply_markup=kb.as_markup(),
                parse_mode="HTML"
            )

    # সাধারণ /start
    uid = m.from_user.id
    if uid in admin_cache:
        text = (
            "👋 <b>হ্যালো অ্যাডমিন! ফাইল স্টোর প্যানেলে স্বাগতম।</b>\n\n"
            "⚙️ <b>ফাইল ও আপলোড কমান্ড:</b>\n"
            "🔹 <code>/auto</code> - গেট ফাইল চ্যানেলের অতীত সব ফাইল স্টোর\n"
            "🔹 <code>/skip</code> - চ্যানেল অনুযায়ী ফাইল স্কিপ করুন\n"
            "🔹 <code>/filecnl</code> - নতুন ফাইল চ্যানেল ক্যাটাগরি যুক্ত\n"
            "🔹 <code>/post</code> - ম্যানুয়াল ফাইল আপলোড (পোস্টার ও নাম সহ)\n"
            "🔹 <code>/new</code> - অটো স্ক্রিনশট গ্রিড আপলোড (নাম দিয়ে)\n"
            "🔹 <code>/blink</code> - চ্যানেলের লাস্ট পোস্ট লিঙ্ক দিলে ফুল ইন্ডেক্স\n"
            "🔹 <code>/del</code> - ফাইল ডাটাবেস থেকে ডিলিট করুন\n\n"
            "🔗 <b>শর্টনার ও অ্যাড সেটিংস:</b>\n"
            "🔸 <code>/setsortner</code> - কাস্টম শর্টনার অ্যাড, লিস্ট ও ডিলিট\n"
            "🔸 <code>/setad [zone_id]</code> - মনিটেগ অ্যাড জোন আইডি\n"
            "🔸 <code>/monetag on/off</code> - মনিটেগ অ্যাড অন/অফ\n"
            "🔸 <code>/addlink [URL]</code> - ডিরেক্ট লিঙ্ক অ্যাড\n"
            "🔸 <code>/links</code> - ডিরেক্ট লিঙ্ক তালিকা | <code>/dellink ID</code>\n"
            "🔸 <code>/adlink on/off</code> - ডিরেক্ট লিঙ্ক অন/অফ\n"
            "🔸 <code>/setsteps [সংখ্যা]</code> - ভেরিফিকেশন স্টেপ সংখ্যা\n\n"
            "⚙️ <b>সাধারণ সেটিংস:</b>\n"
            "🔸 <code>/protect on/off</code> - ফরোয়ার্ড প্রোটেকশন\n"
            "🔸 <code>/settime [মিনিট]</code> - অটো-ডিলিট টাইম\n"
            "🔸 <code>/setunlocktime [সময়]</code> - আনলক টাইম (যেমন: 1h,30m)\n"
            "🔸 <code>/stats</code> - পরিসংখ্যান | <code>/cast</code> - ব্রডকাস্ট\n"
            "🔸 <code>/settg [URL]</code> | <code>/set18 [URL]</code> | <code>/setsitename [নাম]</code>\n"
            "🔸 <code>/setnotice</code> | <code>/sethead</code> | <code>/setmid</code> | <code>/setfoot</code>\n"
        )
        if uid == OWNER_ID:
            text += "\n👑 <b>ওনার কমান্ড:</b>\n🔸 অ্যাড অ্যাডমিন: <code>/addadmin ID</code>\n🔸 ডিলিট অ্যাডমিন: <code>/deladmin ID</code>\n🔸 অ্যাডমিন লিস্ট: <code>/adminlist</code>\n"
    else:
        text = f"👋 <b>স্বাগতম {m.from_user.first_name}!</b>\n\n[টেলিগ্রাম আইডি: <code>{uid}</code>]\n\nআমাদের চ্যানেলে থাকা ভিডিও লিঙ্কে ক্লিক করে আপনি সরাসরি ফাইল সংগ্রহ করতে পারবেন।"
    
    await m.answer(text, parse_mode="HTML")

# ==========================================
# /getfile কমান্ড
# ==========================================
@dp.message(Command("getfile"))
async def getfile_cmd(m: types.Message):
    try:
        fid = m.text.split()[1]
        file_data = await db.files.find_one({"_id": ObjectId(fid)})
        if not file_data: return await m.answer("⚠️ ফাইল পাওয়া যায়নি!")
        await m.answer(f"ফাইল লিঙ্ক পেতে ক্লিক করুন:\n👉 /start getfile_{fid}")
    except:
        await m.answer("⚠️ সঠিক নিয়ম: <code>/getfile [File_ID]</code>", parse_mode="HTML")

# ==========================================
# /setsortner (কাস্টম শর্টনার সিস্টেম)
# ==========================================
@dp.message(Command("setsortner"))
async def setsortner_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    parts = m.text.split()
    
    # নতুন শর্টনার অ্যাড: /setsortner domain.com apikey
    if len(parts) >= 3:
        domain = parts[1]
        api_key = parts[2]
        await db.shorteners.update_many({}, {"$set": {"active": False}})
        await db.shorteners.insert_one({
            "domain": domain, "api_key": api_key, "active": True, "created_at": datetime.datetime.utcnow()
        })
        return await m.answer(f"✅ <b>নতুন শর্টনার সফলভাবে যুক্ত এবং চালু করা হয়েছে!</b>\n🌐 ডোমেন: <code>{domain}</code>\n🔑 API Key: <code>{api_key}</code>", parse_mode="HTML")

    # শর্টনারের তালিকা ও ডিলিট অপশন
    shorteners = await db.shorteners.find().to_list(length=50)
    if not shorteners:
        return await m.answer(
            "⚠️ <b>কোনো কাস্টম শর্টনার যুক্ত করা নেই!</b>\n\n"
            "নতুন শর্টনার যুক্ত করতে এভাবে পাঠান:\n"
            "<code>/setsortner droplink.co 94857463728191</code>\n"
            "বা\n<code>/setsortner Xjndjdj.vercel.app jsndnjd</code>",
            parse_mode="HTML"
        )

    builder = InlineKeyboardBuilder()
    text = "🔗 <b>বর্তমান শর্টনার তালিকা:</b>\n\n"
    for idx, s in enumerate(shorteners):
        status = "🟢 ACTIVE" if s.get("active") else "⚪ INACTIVE"
        text += f"{idx+1}. 🌐 <code>{s['domain']}</code> [{status}]\n🔑 <code>{s['api_key']}</code>\n\n"
        builder.button(text=f"❌ ডিলিট ({s['domain']})", callback_data=f"del_short_{str(s['_id'])}")
        if not s.get("active"):
            builder.button(text=f"✅ একটিভ করুন ({s['domain']})", callback_data=f"act_short_{str(s['_id'])}")
    
    builder.adjust(1)
    text += "➕ <b>নতুন অ্যাড করতে লিখুন:</b>\n<code>/setsortner [ডোমেইন] [API_KEY]</code>"
    await m.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("del_short_"))
async def del_shortener_cb(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    sid = c.data.replace("del_short_", "")
    await db.shorteners.delete_one({"_id": ObjectId(sid)})
    await c.answer("✅ শর্টনার ডিলিট হয়েছে!", show_alert=True)
    await c.message.delete()

@dp.callback_query(F.data.startswith("act_short_"))
async def act_shortener_cb(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    sid = c.data.replace("act_short_", "")
    await db.shorteners.update_many({}, {"$set": {"active": False}})
    await db.shorteners.update_one({"_id": ObjectId(sid)}, {"$set": {"active": True}})
    await c.answer("✅ এই শর্টনারটি সক্রিয় করা হয়েছে!", show_alert=True)
    await c.message.delete()

# ==========================================
# /filecnl (ক্যাটাগরি হিসেবে ফাইল চ্যানেল যুক্ত করা)
# ==========================================
@dp.message(Command("filecnl"))
async def file_channel_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    parts = m.text.split(maxsplit=2)
    
    if len(parts) >= 3:
        try:
            cnl_id = int(parts[1])
            category_name = parts[2].strip()
            await db.channels.update_one(
                {"channel_id": cnl_id},
                {"$set": {"name": category_name, "category_tag": category_name.lower().replace(" ", "_"), "skip_count": 0}},
                upsert=True
            )
            return await m.answer(f"✅ <b>ফাইল চ্যানেল ক্যাটাগরি যুক্ত হয়েছে!</b>\n🆔 Channel ID: <code>{cnl_id}</code>\n📂 ক্যাটাগরি: <b>{category_name}</b>", parse_mode="HTML")
        except:
            return await m.answer("⚠️ সঠিক নিয়ম: <code>/filecnl [চ্যানেল_আইডি] [ক্যাটাগরি_নাম]</code>\nযেমন: <code>/filecnl -100123456789 Natok</code>", parse_mode="HTML")

    channels = await db.channels.find().to_list(length=50)
    builder = InlineKeyboardBuilder()
    text = "📁 <b>ক্যাটাগরি ফাইল চ্যানেল তালিকা:</b>\n\n"
    text += f"⭐ মেইন চ্যানেল: <code>{MAIN_CHANNEL_ID}</code> (Default Getfile)\n\n"
    for cnl in channels:
        text += f"▫️ <b>{cnl['name']}</b>: <code>{cnl['channel_id']}</code> (Skip: {cnl.get('skip_count', 0)})\n"
        builder.button(text=f"❌ ডিলিট {cnl['name']}", callback_data=f"del_cnl_{cnl['channel_id']}")
    
    builder.adjust(1)
    text += "\n➕ <b>নতুন ক্যাটাগরি চ্যানেল যুক্ত করতে:</b>\n<code>/filecnl [চ্যানেল_আইডি] [ক্যাটাগরির নাম]</code>"
    await m.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("del_cnl_"))
async def del_cnl_callback(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    cid = int(c.data.replace("del_cnl_", ""))
    await db.channels.delete_one({"channel_id": cid})
    await c.answer("✅ চ্যানেল ক্যাটাগরি মুছে ফেলা হয়েছে!", show_alert=True)
    await c.message.delete()

# ==========================================
# /skip (চ্যানেল সিলেক্ট করে স্কিপ সিস্টেম)
# ==========================================
@dp.message(Command("skip"))
async def skip_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐ মেইন গেট-ফাইল চ্যানেল", callback_data=f"set_skip_{MAIN_CHANNEL_ID}")
    
    channels = await db.channels.find().to_list(length=50)
    for cnl in channels:
        builder.button(text=f"📂 {cnl['name']}", callback_data=f"set_skip_{cnl['channel_id']}")
    
    builder.adjust(1)
    await m.answer("🎯 <b>কোন চ্যানেলের ফাইল স্কিপ করতে চান? চ্যানেল বেছে নিন:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("set_skip_"))
async def select_skip_channel(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    cnl_id = int(c.data.replace("set_skip_", ""))
    admin_temp[c.from_user.id] = {"step": "waiting_skip_num", "target_cnl": cnl_id}
    await c.message.edit_text(
        f"🔢 চ্যানেল আইডি <code>{cnl_id}</code> এর জন্য কয়টি ফাইল স্কিপ করতে চান সংখ্যা লিখে পাঠান:\n"
        "(যেমন <code>1</code> লিখলে ১টি ফাইল স্কিপ হয়ে বাকিগুলো সিরিয়ালে আসবে)",
        parse_mode="HTML"
    )

# ==========================================
# /auto (গেট ফাইল চ্যানেলের ফাইল স্টোর)
# ==========================================
@dp.message(Command("auto"))
async def auto_post_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    
    # আর্গুমেন্ট ছাড়া দিলে চ্যানেলের সব ফাইল স্টোর শুরু করবে
    status_msg = await m.answer("⏳ <b>গেট ফাইল চ্যানেলের হিস্ট্রি স্ক্যান করা হচ্ছে...</b>\nফাইলগুলো ডাটাবেসে স্টোর হতে কিছুটা সময় লাগবে। দয়া করে অপেক্ষা করুন।", parse_mode="HTML")
    try:
        success = 0
        bot_info = await bot.get_me()

        async for msg in pyro_bot.get_chat_history(MAIN_CHANNEL_ID):
            if msg.video or (msg.document and "video" in (msg.document.mime_type or "")):
                tg_fid = msg.video.file_id if msg.video else msg.document.file_id
                exists = await db.files.find_one({"tg_file_id": tg_fid})
                if not exists:
                    cap = msg.caption or f"Video File {msg.id}"
                    await db.files.insert_one({
                        "channel_id": MAIN_CHANNEL_ID,
                        "msg_id": msg.id,
                        "tg_file_id": tg_fid,
                        "file_type": "video" if msg.video else "document",
                        "caption": cap,
                        "category": "main",
                        "created_at": datetime.datetime.utcnow()
                    })
                    success += 1
        
        await status_msg.edit_text(f"✅ <b>অটো ইন্ডেক্সিং সম্পন্ন!</b>\nগেট ফাইল চ্যানেল থেকে মোট <b>{success}</b>টি ফাইল সফলভাবে স্টোর করা হয়েছে।", parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(f"❌ এরর: {str(e)}")

# ==========================================
# চ্যানেলে নতুন ভিডিও দিলে অটো স্টোর লিসেনার
# ==========================================
@dp.channel_post(F.video | F.document)
async def auto_channel_listener(m: types.Message):
    cnl_id = m.chat.id
    bot_info = await bot.get_me()
    
    category = "main"
    if cnl_id != MAIN_CHANNEL_ID:
        cat_cnl = await db.channels.find_one({"channel_id": cnl_id})
        if not cat_cnl: return 
        category = cat_cnl["category_tag"]

    # স্কিপ ফিল্টার
    if cnl_id == MAIN_CHANNEL_ID:
        skip_cfg = await db.settings.find_one({"id": f"skip_{cnl_id}"})
    else:
        skip_cfg = await db.channels.find_one({"channel_id": cnl_id})
        
    skip_count = skip_cfg.get("skip_count", 0) if skip_cfg else 0
    
    if skip_count > 0:
        if cnl_id == MAIN_CHANNEL_ID:
            await db.settings.update_one({"id": f"skip_{cnl_id}"}, {"$inc": {"skip_count": -1}})
        else:
            await db.channels.update_one({"channel_id": cnl_id}, {"$inc": {"skip_count": -1}})
        return # স্কিপ হয়ে গেল

    tg_fid = m.video.file_id if m.video else m.document.file_id
    cap = m.caption or f"New Video {m.message_id}"
    
    res = await db.files.insert_one({
        "channel_id": cnl_id,
        "msg_id": m.message_id,
        "tg_file_id": tg_fid,
        "file_type": "video" if m.video else "document",
        "caption": cap,
        "category": category,
        "created_at": datetime.datetime.utcnow()
    })
    
    get_link = f"https://t.me/{bot_info.username}?start=getfile_{str(res.inserted_id)}"
    kb = InlineKeyboardBuilder()
    kb.button(text="⚡ গেট ফাইল (ডাউনলোড)", url=get_link)
    try:
        await bot.edit_message_reply_markup(chat_id=cnl_id, message_id=m.message_id, reply_markup=kb.as_markup())
    except: pass

# ==========================================
# অন্যান্য সকল অ্যাডমিন ও ওনার কমান্ড সমূহ
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

@dp.message(Command("addlink"))
async def add_ad_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1]
        await db.ad_links.insert_one({"url": url, "created_at": datetime.datetime.utcnow()})
        await m.answer(f"✅ নতুন ডিরেক্ট লিঙ্ক যুক্ত হয়েছে:\n<code>{url}</code>", parse_mode="HTML")
    except: await m.answer("⚠️ নিয়ম: `/addlink https://directlink.com`")

@dp.message(Command("links"))
async def list_ad_links(m: types.Message):
    if m.from_user.id not in admin_cache: return
    links = await db.ad_links.find().to_list(length=100)
    if not links: return await m.answer("কোনো লিঙ্ক নেই।")
    text = "🔗 <b>বর্তমান ডিরেক্ট লিঙ্ক সমূহ:</b>\n\n"
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
    except: await m.answer("⚠️ নিয়ম: `/dellink ID`")

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

@dp.message(Command("setsteps"))
async def set_steps_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        steps = int(m.text.split(" ")[1])
        await db.settings.update_one({"id": "step_config"}, {"$set": {"count": steps}}, upsert=True)
        await m.answer(f"✅ ভেরিফিকেশন স্টেপ সংখ্যা এখন: <b>{steps}</b>", parse_mode="HTML")
    except: await m.answer("⚠️ নিয়ম: `/setsteps 2`")

@dp.message(Command("setunlocktime"))
async def set_unlock_time_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        raw_val = m.text.split(" ", 1)[1]
        seconds = parse_duration(raw_val)
        await db.settings.update_one({"id": "unlock_config"}, {"$set": {"seconds": seconds, "raw": raw_val}}, upsert=True)
        await m.answer(f"✅ আনলক থাকার সময় সেট করা হয়েছে: <b>{raw_val}</b>", parse_mode="HTML")
    except: await m.answer("⚠️ নিয়ম: <code>/setunlocktime 1h,1m,10s</code>")

@dp.message(Command("setnotice"))
async def set_notice_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        notice = m.text.split(" ", 1)[1]
        await db.settings.update_one({"id": "site_notice"}, {"$set": {"text": notice}}, upsert=True)
        await m.answer("✅ নোটিশ আপডেট করা হয়েছে।")
    except: await m.answer("⚠️ নিয়ম: `/setnotice নোটিশের টেক্সট`")

@dp.message(Command("sethead"))
async def set_head_ad(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        code = m.text.split(" ", 1)[1]
        await db.settings.update_one({"id": "header_ad"}, {"$set": {"code": code}}, upsert=True)
        await m.answer("✅ হেডার অ্যাড কোড আপডেট হয়েছে।")
    except: await m.answer("⚠️ নিয়ম: `/sethead [কোড]`")

@dp.message(Command("setmid"))
async def set_mid_ad(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        code = m.text.split(" ", 1)[1]
        await db.settings.update_one({"id": "middle_ad"}, {"$set": {"code": code}}, upsert=True)
        await m.answer("✅ মিডেল অ্যাড কোড আপডেট হয়েছে।")
    except: await m.answer("⚠️ নিয়ম: `/setmid [কোড]`")

@dp.message(Command("setfoot"))
async def set_foot_ad(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        code = m.text.split(" ", 1)[1]
        await db.settings.update_one({"id": "footer_ad"}, {"$set": {"code": code}}, upsert=True)
        await m.answer("✅ ফুটার অ্যাড কোড আপডেট হয়েছে।")
    except: await m.answer("⚠️ নিয়ম: `/setfoot [কোড]`")

@dp.message(Command("blink"))
async def blink_cmd_handler(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ", 1)[1]
        chat_id, last_id = parse_blink_link(link)
        if not chat_id or not last_id:
            return await m.answer("⚠️ <b>ভুল লিঙ্ক!</b> সঠিক উদাহরণ: <code>/blink https://t.me/c/123456789/500</code>", parse_mode="HTML")
        asyncio.create_task(blink_worker(chat_id, last_id, m.from_user.id))
        await m.answer("⏳ চ্যানেলের আইডি 1 থেকে শুরু করে সব ভিডিও ইন্ডেক্স করা হচ্ছে। সম্পন্ন হলে জানানো হবে।")
    except:
        await m.answer("⚠️ নিয়ম: <code>/blink [চ্যানেলের শেষ পোস্ট লিঙ্ক]</code>", parse_mode="HTML")

@dp.message(Command("post"))
async def post_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    admin_temp[m.from_user.id] = {"step": "manual_file"}
    await m.answer("📥 <b>ম্যানুয়াল আপলোড:</b> মুভি বা ভিডিও ফাইলটি পাঠান।")

@dp.message(Command("new"))
async def new_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    admin_temp[m.from_user.id] = {"step": "auto_name"}
    await m.answer("🆕 <b>অটো আপলোড:</b> মুভির নাম লিখে পাঠান।")

@dp.message(Command("setsitename"))
async def set_site_name(m: types.Message):
    if m.from_user.id in admin_cache:
        try:
            new_name = m.text.split(" ", 1)[1]
            await db.settings.update_one({"id": "site_name"}, {"$set": {"name": new_name}}, upsert=True)
            await m.answer(f"✅ নাম পরিবর্তন করে <b>{new_name}</b> রাখা হয়েছে।", parse_mode="HTML")
        except: await m.answer("⚠️ সঠিক নিয়ম: <code>/setsitename My Movie Site</code>", parse_mode="HTML")

@dp.message(Command("protect"))
async def protect_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        state = m.text.split(" ")[1].lower()
        if state == "on":
            await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": True}}, upsert=True)
            await m.answer("✅ ফরোয়ার্ড প্রোটেকশন <b>চালু (ON)</b> করা হয়েছে।", parse_mode="HTML")
        elif state == "off":
            await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": False}}, upsert=True)
            await m.answer("✅ ফরোয়ার্ড প্রোটেকশন <b>বন্ধ (OFF)</b> করা হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ সঠিক নিয়ম: <code>/protect on</code> অথবা <code>/protect off</code>", parse_mode="HTML")

@dp.message(Command("stats"))
async def stats_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    uc = await db.users.count_documents({})
    mc = await db.files.count_documents({})
    time_cfg = await db.settings.find_one({"id": "del_time"})
    del_m = time_cfg['minutes'] if time_cfg else 60
    protect_cfg = await db.settings.find_one({"id": "protect_content"})
    prot_status = "ON 🔒" if protect_cfg and protect_cfg.get('status', True) else "OFF 🔓"
    
    await m.answer(f"📊 <b>পরিসংখ্যান:</b>\n👥 মোট ইউজার: <code>{uc}</code>\n🎬 মোট স্টোর ফাইল: <code>{mc}</code>\n⏳ অটো-ডিলিট: <code>{del_m} মিনিট</code>\n🛡️ প্রোটেকশন: <b>{prot_status}</b>", parse_mode="HTML")

@dp.message(Command("del"))
async def del_movie_list(m: types.Message):
    if m.from_user.id not in admin_cache: return
    files = await db.files.find().sort("created_at", -1).limit(20).to_list(length=20)
    if not files: return await m.answer("কোনো ফাইল নেই।")
    builder = InlineKeyboardBuilder()
    for f in files: 
        t = f.get('caption') or f.get('title') or "File"
        builder.button(text=f"❌ {t[:30]}", callback_data=f"del_{str(f['_id'])}")
    builder.adjust(1)
    await m.answer("⚠️ ডিলিট করতে ফাইলে ক্লিক করুন:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("del_"))
async def del_movie_callback(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    try:
        await db.files.delete_one({"_id": ObjectId(c.data.split("_")[1])})
        await c.answer("✅ ডিলিট হয়েছে!", show_alert=True)
        await c.message.edit_text("✅ ফাইলটি ডাটাবেস থেকে মুছে ফেলা হয়েছে।", reply_markup=None)
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
            await m.answer("✅ মনিটেগ জোন আইডি আপডেট হয়েছে।")
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

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message):
    if m.from_user.id not in admin_cache: return
    admin_temp[m.from_user.id] = {"step": "bcast_wait"}
    await m.answer("📢 <b>ব্রডকাস্ট মেসেজ:</b> যে মেসেজটি ব্রডকাস্ট করতে চান তা ফরওয়ার্ড বা সেন্ড করুন।", parse_mode="HTML")

@dp.callback_query(F.data.startswith("reply_"))
async def process_reply_cb(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    user_id = int(c.data.split("_")[1])
    admin_temp[c.from_user.id] = {"step": "reply_user", "target_uid": user_id}
    await c.message.reply("✍️ <b>ইউজারকে কী রিপ্লাই দিতে চান তা লিখে পাঠান:</b>", parse_mode="HTML")
    await c.answer()

# ==========================================
# সব ইনপুট হ্যান্ডলার (ফাইল, ছবি, টেক্সট প্রসেসিং)
# ==========================================
@dp.message(F.content_type.in_({'text', 'photo', 'video', 'document', 'voice'}))
async def catch_all_inputs(m: types.Message):
    uid = m.from_user.id
    state = admin_temp.get(uid, {}).get("step")
    me = await bot.get_me()

    # স্কিপ ইনপুট
    if uid in admin_cache and state == "waiting_skip_num" and m.text:
        try:
            num = int(m.text.strip())
            cnl_id = admin_temp[uid]["target_cnl"]
            if cnl_id == MAIN_CHANNEL_ID:
                await db.settings.update_one({"id": f"skip_{cnl_id}"}, {"$set": {"skip_count": num}}, upsert=True)
            else:
                await db.channels.update_one({"channel_id": cnl_id}, {"$set": {"skip_count": num}})
            del admin_temp[uid]
            await m.answer(f"✅ সফল হয়েছে! চ্যানেল <code>{cnl_id}</code> এর পরবর্তী <b>{num}</b> টি ফাইল স্কিপ করা হবে।", parse_mode="HTML")
        except:
            await m.answer("⚠️ দয়া করে একটি সঠিক পূর্ণসংখ্যা দিন (যেমন 1, 2, 3)।")
        return

    # ইউজারকে রিপ্লাই দেওয়া
    if uid in admin_cache and state == "reply_user":
        target_uid = admin_temp[uid]["target_uid"]
        del admin_temp[uid]
        try:
            if m.text: await bot.send_message(target_uid, f"📩 <b>অ্যাডমিন রিপ্লাই:</b>\n\n{m.text}", parse_mode="HTML")
            else: await m.copy_to(target_uid, caption=f"📩 <b>অ্যাডমিন রিপ্লাই:</b>\n\n{m.caption or ''}", parse_mode="HTML")
            await m.answer("✅ ইউজারকে সফলভাবে রিপ্লাই পাঠানো হয়েছে!")
        except Exception:
            await m.answer("⚠️ রিপ্লাই পাঠানো যায়নি! ইউজার বট ব্লক করেছে।")
        return

    # ব্রডকাস্ট
    if uid in admin_cache and state == "bcast_wait":
        del admin_temp[uid]
        await m.answer("⏳ ব্রডকাস্ট শুরু হয়েছে...")
        success = 0
        async for u in db.users.find():
            try:
                await m.copy_to(chat_id=u['user_id'])
                success += 1
                await asyncio.sleep(0.05)
            except: pass
        await m.answer(f"✅ সম্পন্ন! সর্বমোট <b>{success}</b> জনকে মেসেজ পাঠানো হয়েছে।", parse_mode="HTML")
        return

    # ম্যানুয়াল আপলোড (/post)
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
        ins = await db.files.insert_one({
            "title": title, "caption": title, "photo_id": admin_temp[uid]["photo_id"], 
            "tg_file_id": admin_temp[uid]["file_id"], "file_type": admin_temp[uid]["type"], 
            "duration": "Unknown", "channel_id": MAIN_CHANNEL_ID, "created_at": datetime.datetime.utcnow()
        })
        try:
            get_link = f"https://t.me/{me.username}?start=getfile_{str(ins.inserted_id)}"
            kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="⚡ গেট ফাইল (ডাউনলোড)", url=get_link)]])
            await bot.send_photo(CHANNEL_ID, photo=admin_temp[uid]["photo_id"], caption=f"🎥 <b>নতুন ফাইল যুক্ত করা হয়েছে!</b>\n\n🎬 নাম: <b>{title}</b>", parse_mode="HTML", reply_markup=kb)
        except: pass
        del admin_temp[uid]
        await m.answer(f"🎉 <b>{title}</b> ম্যানুয়ালি যুক্ত করা হয়েছে!")
        return

    # অটো আপলোড (/new)
    if uid in admin_cache and m.text and state == "auto_name":
        admin_temp[uid] = {"step": "auto_file", "title": m.text.strip()}
        await m.answer(f"✅ নাম: <b>{m.text}</b>\nএবার ভিডিও ফাইলটি পাঠান (Max 2GB)।", parse_mode="HTML")
        return

    if uid in admin_cache and (m.document or m.video) and state == "auto_file":
        file = m.video or m.document
        title = admin_temp[uid]["title"]
        status_msg = await m.answer("⏳ প্রসেসিং শুরু হয়েছে... ভিডিও থেকে ৬টি ল্যান্ডস্কেপ স্ক্রিনশট নেওয়া হচ্ছে।")
        
        try:
            video_path = await pyro_bot.download_media(m.video or m.document)
            grid_path = f"grid_poster_{uid}_{random.randint(100,999)}.jpg"
            
            res, duration_sec = await create_screenshot_grid(video_path, grid_path)
            duration_str = get_duration_display(duration_sec) if res else "0m"
            
            ins = await db.files.insert_one({
                "title": title, "caption": title, "tg_file_id": file.file_id, 
                "file_type": "video" if m.video else "document", "duration": duration_str,
                "channel_id": MAIN_CHANNEL_ID, "created_at": datetime.datetime.utcnow()
            })
            
            get_link = f"https://t.me/{me.username}?start=getfile_{str(ins.inserted_id)}"
            kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="⚡ গেট ফাইল (ডাউনলোড)", url=get_link)]])
            
            if res:
                with open(grid_path, 'rb') as f:
                    await bot.send_photo(CHANNEL_ID, photo=types.BufferedInputFile(f.read(), filename="grid.jpg"), caption=f"🎥 <b>নতুন ভিডিও যুক্ত হয়েছে!</b>\n\n🎬 নাম: <b>{title}</b>\n⏳ সময়: {duration_str}", parse_mode="HTML", reply_markup=kb)
                await status_msg.edit_text(f"🎉 <b>{title}</b> স্ক্রিনশট সহ চ্যানেলে পোস্ট ও স্টোর করা হয়েছে!")
            else:
                await bot.send_message(CHANNEL_ID, f"🎥 <b>নতুন ভিডিও যুক্ত হয়েছে!</b>\n\n🎬 নাম: <b>{title}</b>\n⏳ সময়: {duration_str}", parse_mode="HTML", reply_markup=kb)
                await status_msg.edit_text(f"🎉 <b>{title}</b> সফলভাবে চ্যানেলে পোস্ট ও স্টোর করা হয়েছে!")
            
            if os.path.exists(video_path): os.remove(video_path)
            if os.path.exists(grid_path): os.remove(grid_path)
            del admin_temp[uid]

        except Exception as e:
            await status_msg.edit_text(f"❌ এরর: {str(e)}")

# ==========================================
# মনিটেগ / ডিরেক্ট লিঙ্ক অ্যাড ভেরিফিকেশন ওয়েব রুট
# ==========================================
@app.get("/verify", response_class=HTMLResponse)
async def verify_page(token: str = ""):
    token_doc = await db.verify_tokens.find_one({"token": token})
    if not token_doc:
        return HTMLResponse("<h3>❌ ভেরিফিকেশন লিঙ্কটির মেয়াদ শেষ হয়ে গেছে। দয়া করে বটে আবার চেষ্টা করুন।</h3>", status_code=400)

    # সেটিংস ডাটা ফেচ
    ad_cfg = await db.settings.find_one({"id": "ad_config"})
    zone_id = ad_cfg['zone_id'] if ad_cfg else "10916755"
    
    monetag_cfg = await db.settings.find_one({"id": "monetag_status"})
    monetag_on = monetag_cfg['status'] if monetag_cfg else True
    
    all_links = await db.ad_links.find().to_list(length=100)
    direct_link = random.choice([l['url'] for l in all_links]) if all_links else "https://google.com"

    bot_info = await bot.get_me()
    final_tg_redirect = f"https://t.me/{bot_info.username}?start=verify_{token}"

    html = f"""
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ফাইল ভেরিফিকেশন</title>
        {f'<script src="//libtl.com/sdk.js" data-zone="{zone_id}" data-sdk="show_{zone_id}"></script>' if monetag_on else ''}
        <style>
            body {{ background:#0f172a; color:#fff; font-family: sans-serif; display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; margin:0; padding:20px; text-align:center; }}
            .card {{ background:#1e293b; padding:30px; border-radius:16px; max-width:400px; width:100%; box-shadow:0 10px 25px rgba(0,0,0,0.5); }}
            h2 {{ color:#f87171; margin-bottom:10px; }}
            p {{ color:#94a3b8; font-size:14px; margin-bottom:20px; }}
            .timer {{ font-size:40px; color:#10b981; font-weight:bold; margin-bottom:20px; }}
            .btn {{ display:block; width:100%; padding:15px; background:#f87171; color:white; border:none; border-radius:8px; font-size:16px; font-weight:bold; cursor:pointer; text-decoration:none; margin-top:10px; }}
            .btn-done {{ background:#10b981; display:none; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>ফাইল ভেরিফিকেশন</h2>
            <p>নিচের বাটনে ক্লিক করে অ্যাড পেজটি ভিজিট করুন এবং ১৫ সেকেন্ড অপেক্ষা করুন।</p>
            <div class="timer" id="timer">15</div>
            <a href="{direct_link}" target="_blank" class="btn" id="btnVisit" onclick="startTimer()">অ্যাড দেখুন ও আনলক করুন</a>
            <a href="{final_tg_redirect}" class="btn btn-done" id="btnGet">✅ ফাইল ডাউনলোড করুন</a>
        </div>

        <script>
            function startTimer() {{
                {f'if(typeof show_{zone_id} === "function") {{ show_{zone_id}(); }}' if monetag_on else ''}
                let t = 15;
                let btnV = document.getElementById('btnVisit');
                let btnG = document.getElementById('btnGet');
                btnV.style.pointerEvents = 'none';
                btnV.style.opacity = '0.5';
                
                let iv = setInterval(() => {{
                    t--;
                    document.getElementById('timer').innerText = t;
                    if(t <= 0) {{
                        clearInterval(iv);
                        btnV.style.display = 'none';
                        btnG.style.display = 'block';
                        document.getElementById('timer').innerText = "সম্পন্ন!";
                    }}
                }}, 1000);
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

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

# ==========================================
# মেইন স্টার্ট ফাংশন
# ==========================================
async def start():
    await load_admins()
    await pyro_bot.start()
    
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    
    asyncio.create_task(auto_delete_worker())
    
    await bot.delete_webhook(drop_pending_updates=True)
    print("🚀 টেলিগ্রাম ফাইল স্টোর ও ইনকাম বট সফলভাবে চালু হয়েছে!")
    await asyncio.gather(server.serve(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(start())
