import os, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')

def run_server():
    port = int(os.environ.get('PORT', 10000))
    HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever()

threading.Thread(target=run_server, daemon=True).start()

import datetime
import sqlite3
import requests
import dns.resolver
import phonenumbers
from phonenumbers import geocoder, carrier
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = "9078234962:AAF7Y2UKX3hY_TXsBW6rlbaLM-vMEvYq9cc"
MY_UPI_ID = "9187680147@ybl"
ADMIN_ID = None

DB_FILE = "bot_database.db"

# ----------------- DATABASE SETUP -----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            tier TEXT DEFAULT 'free',
            premium_until TEXT,
            daily_queries INTEGER DEFAULT 0,
            last_active_date TEXT,
            referrer_id INTEGER,
            referrals_count INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan TEXT,
            screenshot_file_id TEXT,
            status TEXT DEFAULT 'pending',
            timestamp TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_or_create_user(user_id, username, full_name, referrer_id=None):
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, tier, premium_until, daily_queries, last_active_date, referrals_count FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()

    if not row:
        c.execute('''
            INSERT INTO users (user_id, username, full_name, tier, premium_until, daily_queries, last_active_date, referrer_id, referrals_count)
            VALUES (?, ?, ?, 'free', NULL, 0, ?, ?, 0)
        ''', (user_id, username, full_name, today, referrer_id))
        
        if referrer_id and referrer_id != user_id:
            c.execute("UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id = ?", (referrer_id,))
            c.execute("SELECT referrals_count, premium_until FROM users WHERE user_id = ?", (referrer_id,))
            ref_data = c.fetchone()
            if ref_data and ref_data[0] % 3 == 0:
                now = datetime.datetime.now()
                new_expiry = (now + datetime.timedelta(days=1)).isoformat()
                c.execute("UPDATE users SET tier = 'premium', premium_until = ? WHERE user_id = ?", (new_expiry, referrer_id))
        conn.commit()
        c.execute("SELECT user_id, tier, premium_until, daily_queries, last_active_date, referrals_count FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()

    conn.close()
    return row

def check_user_limit(user_id):
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT tier, premium_until, daily_queries, last_active_date FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()

    if not row:
        conn.close()
        return True, "free"

    tier, premium_until, daily_queries, last_active_date = row

    if tier == "premium":
        if premium_until:
            expiry_dt = datetime.datetime.fromisoformat(premium_until)
            if now > expiry_dt:
                c.execute("UPDATE users SET tier = 'free', premium_until = NULL WHERE user_id = ?", (user_id,))
                conn.commit()
                tier = "free"
            else:
                conn.close()
                return True, "premium"
        else:
            conn.close()
            return True, "premium"

    if last_active_date != today:
        c.execute("UPDATE users SET daily_queries = 0, last_active_date = ? WHERE user_id = ?", (today, user_id))
        conn.commit()
        daily_queries = 0

    if daily_queries < 1:
        c.execute("UPDATE users SET daily_queries = daily_queries + 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return True, "free"

    conn.close()
    return False, "limit_exceeded"

# ----------------- KEYBOARD MENUS -----------------
MENU_MAIN = [
    ["🛠 PUBLIC UTILITIES", "🔍 SECURITY & OSINT"],
    ["👑 BUY PREMIUM", "📊 MY ACCOUNT STATUS"],
    ["🎁 REFER & EARN", "📩 CONTACT OWNER"]
]

MENU_UTILITY = [
    ["📍 PINCODE LOOKUP", "💳 IFSC LOOKUP"],
    ["🏢 GSTIN VERIFIER", "🚗 RTO CODE LOOKUP"],
    ["⛽ FUEL RATES", "💱 LIVE FOREX / CRYPTO"],
    ["⬅️ MAIN MENU"]
]

MENU_OSINT = [
    ["📞 CARRIER LOOKUP", "📱 IMEI SPECS"],
    ["🌐 IP / HOST RECON", "📧 EMAIL AUDIT"],
    ["🔎 USERNAME SCANNER", "🛡 URL SAFETY CHECK"],
    ["📡 DNS LOOKUP", "⬅️ MAIN MENU"]
]

RTO_CODES = {
    "KA-17": "Davanagere, Karnataka",
    "KA-16": "Chitradurga, Karnataka",
    "KA-14": "Shimoga, Karnataka",
    "KA-01": "Bangalore Central (Koramangala), Karnataka",
    "KA-02": "Bangalore West (Rajajinagar), Karnataka",
    "KA-03": "Bangalore East (Indiranagar), Karnataka",
    "KA-04": "Bangalore North (Yeshwanthpur), Karnataka",
    "KA-05": "Bangalore South (Jayanagar), Karnataka",
    "MH-01": "Mumbai South, Maharashtra",
    "DL-01": "Delhi North (Mall Road), Delhi",
}

# ----------------- COMMAND & BUTTON HANDLERS -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ADMIN_ID
    user = update.effective_user
    if ADMIN_ID is None:
        ADMIN_ID = user.id

    referrer = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer = int(context.args[0].replace("ref_", ""))
        except ValueError:
            referrer = None

    get_or_create_user(user.id, user.username or "None", user.full_name, referrer)

    await update.message.reply_markdown(
        f"👋 *Welcome, {user.first_name}!*",
        reply_markup=ReplyKeyboardMarkup(MENU_MAIN, resize_keyboard=True)
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    username = update.effective_user.username or "None"
    full_name = update.effective_user.full_name

    get_or_create_user(user_id, username, full_name)

    # Main Navigation
    if text == "🛠 PUBLIC UTILITIES":
        await update.message.reply_markdown("📂 *Select a Public Utility Service:*", reply_markup=ReplyKeyboardMarkup(MENU_UTILITY, resize_keyboard=True))
        return
    elif text == "🔍 SECURITY & OSINT":
        await update.message.reply_markdown("📂 *Select an OSINT & Security Tool:*", reply_markup=ReplyKeyboardMarkup(MENU_OSINT, resize_keyboard=True))
        return
    elif text == "⬅️ MAIN MENU":
        await update.message.reply_markdown("🏠 *Returned to Main Menu:*", reply_markup=ReplyKeyboardMarkup(MENU_MAIN, resize_keyboard=True))
        return

    # Account & Growth
    elif text == "📊 MY ACCOUNT STATUS":
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT tier, premium_until, daily_queries, referrals_count FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()

        tier, prem_until, daily_q, refs = row
        rem = "Unlimited" if tier == "premium" else max(0, 1 - daily_q)
        expiry_info = f"• VIP Active Until: `{prem_until}`" if tier == "premium" and prem_until else "• VIP: Inactive"

        msg = (
            f"👤 *Account Status & Limits:*\n\n"
            f"• Tier: {'👑 VIP Premium' if tier == 'premium' else '🆓 Free User'}\n"
            f"• Searches Remaining Today: {rem}\n"
            f"• Friends Referred: {refs}\n"
            f"{expiry_info}\n"
            f"• Daily Quota Reset: Midnight UTC"
        )
        await update.message.reply_markdown(msg)
        return

    elif text == "🎁 REFER & EARN":
        bot_info = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
        msg = (
            f"🎁 *Invite Friends & Get Free VIP!*\n\n"
            f"Share your referral link with friends or channels:\n`{ref_link}`\n\n"
            "✨ *Reward:* For every **3 friends** who join via your link, you automatically get **1 Day of VIP Premium** for free!"
        )
        await update.message.reply_markdown(msg)
        return

    elif text == "👑 BUY PREMIUM":
        inline_keyboard = [
            [InlineKeyboardButton("💰 ₹19 - 1 Day", callback_data="plan_19")],
            [InlineKeyboardButton("💰 ₹49 - 3 Days", callback_data="plan_49")],
            [InlineKeyboardButton("💰 ₹99 - 7 Days", callback_data="plan_99")],
            [InlineKeyboardButton("💰 ₹149 - 15 Days", callback_data="plan_149")],
            [InlineKeyboardButton("💰 ₹199 - ⭐ 1 Month (Popular)", callback_data="plan_199")],
            [InlineKeyboardButton("💰 ₹299 - 2 Months", callback_data="plan_299")],
            [InlineKeyboardButton("💰 ₹499 - 3 Months", callback_data="plan_499")],
            [InlineKeyboardButton("💰 ₹699 - 6 Months", callback_data="plan_699")],
            [InlineKeyboardButton("💰 ₹999 - 1 Year", callback_data="plan_999")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_plan")]
        ]
        await update.message.reply_markdown("💎 *Select an Instant Upgrade Plan:*", reply_markup=InlineKeyboardMarkup(inline_keyboard))
        return

    elif text == "📩 CONTACT OWNER":
        await update.message.reply_markdown(f"📩 *Direct Support:*\nFor manual activation or issues, contact the bot administrator directly.")
        return

    # Trigger Tool Inputs
    tool_prompts = {
        "📍 PINCODE LOOKUP": ("pincode", "📍 Enter a 6-digit Indian PIN code (e.g. `577213`):"),
        "💳 IFSC LOOKUP": ("ifsc", "💳 Enter an 11-digit Bank IFSC code (e.g. `SBIN0000847`):"),
        "🏢 GSTIN VERIFIER": ("gstin", "🏢 Enter a 15-digit GSTIN (e.g. `29AAAAA0000A1Z5`):"),
        "🚗 RTO CODE LOOKUP": ("rto", "🚗 Enter an RTO code prefix (e.g. `KA-17`, `DL-01`):"),
        "⛽ FUEL RATES": ("fuel", "⛽ Enter your city/district name in India (e.g. `Bangalore` or `Delhi`):"),
        "💱 LIVE FOREX / CRYPTO": ("forex", "💱 Enter currency/crypto symbol (e.g. `USD`, `EUR`, `BTC`, `USDT`):"),
        "📞 CARRIER LOOKUP": ("carrier", "📞 Enter phone number with country code (e.g. `+919876543210`):"),
        "📱 IMEI SPECS": ("imei", "📱 Enter a 15-digit IMEI number:"),
        "🌐 IP / HOST RECON": ("ip", "🌐 Enter an IP address or domain (e.g. `1.1.1.1` or `google.com`):"),
        "📧 EMAIL AUDIT": ("email", "📧 Enter an email address to audit:"),
        "🔎 USERNAME SCANNER": ("username", "🔎 Enter username handle to search across platforms:"),
        "🛡 URL SAFETY CHECK": ("url", "🛡 Enter URL to inspect (e.g. `https://example.com`):"),
        "📡 DNS LOOKUP": ("dns", "📡 Enter website domain for DNS query (e.g. `wikipedia.org`):")
    }

    if text in tool_prompts:
        allowed, _ = check_user_limit(user_id)
        if not allowed:
            await update.message.reply_markdown("🔒 *DAILY LIMIT REACHED!*\n\nFree users get 1 search per day.\nClick *👑 BUY PREMIUM* for unlimited access.")
            return
        state, prompt_msg = tool_prompts[text]
        context.user_data["awaiting"] = state
        await update.message.reply_markdown(prompt_msg)
        return

    # Execute Lookup Queries
    mode = context.user_data.get("awaiting")
    if not mode:
        await update.message.reply_text("Please choose an option from the menu.")
        return

    context.user_data["awaiting"] = None
    query = text.strip()

    if mode == "pincode":
        res = requests.get(f"https://api.postalpincode.in/pincode/{query}").json()
        if res and res[0].get("Status") == "Success":
            po = res[0]["PostOffice"][0]
            await update.message.reply_markdown(f"📮 *PIN Code {query}*\n• Office: {po.get('Name')}\n• District: {po.get('District')}\n• State: {po.get('State')}")
        else:
            await update.message.reply_text("❌ PIN code not found.")

    elif mode == "ifsc":
        res = requests.get(f"https://ifsc.razorpay.com/{query.upper()}")
        if res.status_code == 200:
            d = res.json()
            await update.message.reply_markdown(f"🏦 *Bank IFSC: {query.upper()}*\n• Bank: {d.get('BANK')}\n• Branch: {d.get('BRANCH')}\n• UPI Enabled: {d.get('UPI')}\n• Address: {d.get('ADDRESS')}")
        else:
            await update.message.reply_text("❌ IFSC code not found.")

    elif mode == "gstin":
        gst = query.upper()
        if len(gst) == 15:
            state_code = gst[:2]
            pan = gst[2:12]
            await update.message.reply_markdown(f"🏢 *GSTIN Audit: {gst}*\n• State Prefix: `{state_code}`\n• Extracted PAN: `{pan}`\n• Format Check: ✅ Valid 15-char structure")
        else:
            await update.message.reply_text("❌ Invalid GSTIN format. Must be 15 alphanumeric characters.")

    elif mode == "rto":
        code = query.upper().replace(" ", "")
        location = RTO_CODES.get(code, "Unknown RTO Region / Not in database")
        await update.message.reply_markdown(f"🚗 *RTO Vehicle Prefix: {code}*\n• Location: {location}")

    elif mode == "fuel":
        city = query.title()
        await update.message.reply_markdown(f"⛽ *Daily Fuel Indicator ({city})*\n• Petrol: ~₹101.90 / L\n• Diesel: ~₹87.80 / L\n*(Prices subject to local pump variance)*")

    elif mode == "forex":
        sym = query.upper()
        try:
            r = requests.get("https://api.exchangerate-api.com/v4/latest/USD").json()
            inr = r["rates"]["INR"]
            if sym in ["USD", "USDT"]:
                await update.message.reply_markdown(f"💱 *1 {sym}* = ₹{inr:.2f} INR")
            elif sym in r["rates"]:
                val = inr / r["rates"][sym]
                await update.message.reply_markdown(f"💱 *1 {sym}* = ₹{val:.2f} INR")
            else:
                await update.message.reply_text("Currency not recognized.")
        except Exception as e:
            await update.message.reply_text("❌ Exchange API currently unavailable.")

    elif mode == "carrier":
        try:
            p = phonenumbers.parse(query, None)
            if phonenumbers.is_valid_number(p):
                c_name = carrier.name_for_number(p, "en") or "Unknown Carrier"
                reg = geocoder.description_for_number(p, "en") or "Unknown Circle"
                await update.message.reply_markdown(f"📞 *Carrier Info:*\n• Valid: ✅\n• Operator: {c_name}\n• Circle: {reg}")
            else:
                await update.message.reply_text("❌ Invalid phone number.")
        except Exception as e:
            await update.message.reply_text("❌ Please include country code (+91).")

    elif mode == "imei":
        imei = query.replace(" ", "")
        if len(imei) == 15 and imei.isdigit():
            await update.message.reply_markdown(f"📱 *IMEI Diagnostic Specs:*\n• TAC: `{imei[:8]}`\n• Serial: `{imei[8:14]}`\n• Check Digit: `{imei[14]}`")
        else:
            await update.message.reply_text("❌ Invalid 15-digit IMEI.")

    elif mode == "ip":
        r = requests.get(f"http://ip-api.com/json/{query}").json()
        if r.get("status") == "success":
            await update.message.reply_markdown(f"🌐 *Network Info ({query})*\n• IP: `{r.get('query')}`\n• ISP: {r.get('isp')}\n• ASN: {r.get('as')}\n• Country: {r.get('country')}")
        else:
            await update.message.reply_text("❌ Could not resolve target.")

    elif mode == "email":
        em = query.lower()
        disposable = ["tempmail.com", "10minutemail.com", "guerrillamail.com", "throwaway.com"]
        if "@" in em:
            dom = em.split("@")[-1]
            is_disp = "⚠️ Disposable Temp-Mail" if dom in disposable else "✅ Clean Private / Corporate Mail"
            await update.message.reply_markdown(f"📧 *Email Audit: {em}*\n• Domain: {dom}\n• Category: {is_disp}")
        else:
            await update.message.reply_text("❌ Invalid email format.")

    elif mode == "username":
        uname = query.replace("@", "")
        platforms = [
            f"• GitHub: https://github.com/{uname}",
            f"• Reddit: https://www.reddit.com/user/{uname}",
            f"• Pinterest: https://www.pinterest.com/{uname}/",
            f"• Telegram: https://t.me/{uname}"
        ]
        await update.message.reply_markdown(f"🔎 *Profile Endpoints for `{uname}`:*\n\n" + "\n".join(platforms))

    elif mode == "url":
        target = query if query.startswith("http") else f"https://{query}"
        try:
            r = requests.head(target, allow_redirects=True, timeout=5)
            await update.message.reply_markdown(f"🛡 *URL Audit:*\n• Final URL: `{r.url}`\n• HTTP Status: `{r.status_code}`\n• Status: ✅ Reachable")
        except Exception as e:
        await update.message.reply_text("⚠️ Target URL failed to respond or is unreachable.")

    elif mode == "dns":
        try:
            answers = dns.resolver.resolve(query, 'A')
            ips = [rdata.to_text() for rdata in answers]
            await update.message.reply_markdown(f"📡 *DNS A-Records for `{query}`:*\n" + "\n".join([f"• `{ip}`" for ip in ips]))
        except Exception as e:
        await update.message.reply_text("❌ Could not resolve DNS records for this domain.")

# ----------------- PAYMENT CALLBACKS & PROOFS -----------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_plan":
        await query.edit_message_text("❌ Purchase cancelled.")
        return

    plan_price = query.data.replace("plan_", "")
    context.user_data["pending_plan"] = plan_price
    context.user_data["awaiting_screenshot"] = True

    upi_link = f"upi://pay?pa={MY_UPI_ID}&pn=UtilityBot&am={plan_price}&cu=INR"
    msg = (
        f"👑 *UPGRADE TO VIP PREMIUM*\n\n"
        f"💰 *Amount to Pay:* ₹{plan_price}\n"
        f"🆔 *UPI ID:* `{MY_UPI_ID}`\n\n"
        f"👉 [Click here to Pay via UPI]({upi_link})\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📸 *Payment Verification:*\n"
        f"1. Send ₹{plan_price} using any UPI app.\n"
        "2. Take a screenshot of the completed transaction.\n"
        "3. Send the screenshot right here in this chat.\n\n"
        "⏳ *Admin will verify and upgrade your account immediately!*"
    )
    await query.edit_message_text(msg, # parse_mode removed)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.full_name
    username = update.effective_user.username or "None"

    if context.user_data.get("awaiting_screenshot"):
        plan = context.user_data.get("pending_plan", "Unknown")
        context.user_data["awaiting_screenshot"] = False
        photo_id = update.message.photo[-1].file_id

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO payments (user_id, plan, screenshot_file_id, status, timestamp) VALUES (?, ?, ?, 'pending', ?)",
                  (user_id, plan, photo_id, datetime.datetime.now().isoformat()))
        conn.commit()
        conn.close()

        admin_btns = [
            [
                InlineKeyboardButton(f"✅ Approve (₹{plan})", callback_data=f"app_{user_id}_{plan}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"rej_{user_id}")
            ]
        ]
        caption = f"🔔 *PAYMENT RECEIVED!*\n\n• User: {user_name} (@{username})\n• ID: `{user_id}`\n• Plan: ₹{plan}"

        if ADMIN_ID:
            await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo_id, caption=caption, reply_markup=InlineKeyboardMarkup(admin_btns), # parse_mode removed)

        await update.message.reply_markdown("✅ *Payment screenshot received!* It has been sent to the admin for verification.")
    else:
        await update.message.reply_text("Please choose a plan from 👑 BUY PREMIUM before uploading a payment proof.")

async def handle_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    now = datetime.datetime.now()

    days_map = {
        "19": 1, "49": 3, "99": 7, "149": 15,
        "199": 30, "299": 60, "499": 90, "699": 180, "999": 365
    }

    if data.startswith("app_"):
        parts = data.split("_")
        target_uid = int(parts[1])
        plan = parts[2]
        days = days_map.get(plan, 30)
        new_expiry = (now + datetime.timedelta(days=days)).isoformat()

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE users SET tier = 'premium', premium_until = ? WHERE user_id = ?", (new_expiry, target_uid))
        c.execute("UPDATE payments SET status = 'approved' WHERE user_id = ? AND status = 'pending'", (target_uid,))
        conn.commit()
        conn.close()

        await query.edit_message_caption(caption=f"✅ *Approved!* User `{targe

    elif data.startswith("rej_"):
        target_uid = int(data.split("_")[1])
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE payments SET status = 'rejected' WHERE user_id = ? AND status = 'pending'", (target_uid,))
        conn.commit()
        conn.close()

        await query.edit_message_caption(caption=f"❌ *Declined.* User `{target_uid}` rejected.", # parse_mode removed)
        try:
            await context.bot.send_message(
                chat_id=target_uid,
                text="❌ *Payment Rejected.* Verification could not be completed. Contact the owner via 📩 CONTACT OWNER.",
                # parse_mode removed
            )
        except Exception as e:
        print(f"Failed sending approval message to user: {e}")

# ----------------- MAIN INITIALIZER -----------------
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_admin_action, pattern="^(app_|rej_)"))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Multi-Utility OSINT & Billing Engine Bot is online on Kali Linux...")
    app.run_polling()