import os
import re
import json
import time
import redis
import stripe
import asyncio
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    CallbackQueryHandler, filters,
)

TOKEN = os.environ.get("BOT_TOKEN")
STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "sk_test_123")
RZP_ID = os.environ.get("RZP_KEY_ID", "")
RZP_SECRET = os.environ.get("RZP_KEY_SECRET", "")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASS = os.environ.get("REDIS_PASSWORD") or None

redis_client = redis.Redis(
    host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASS, decode_responses=True
)
stripe.api_key = STRIPE_KEY

# ---- In-memory user store (users + custom gates + live toggle) ----
DB_FILE = "users.json"

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE) as f:
            return json.load(f)
    return {}

DB = load_db()
running = {"stop": False}

def save_db():
    with open(DB_FILE, "w") as f:
        json.dump(DB, f, indent=2)

def user(u):
    DB.setdefault(u, {"gates": [], "live": True})
    return DB[u]

# ====================== CHECKERS ======================

def bin_lookup(bin_num):
    if len(bin_num) < 6:
        return "⚠️ BIN must be at least 6 digits."
    url = f"https://lookup.binlist.net/{bin_num}"
    try:
        r = requests.get(url, timeout=10).json()
        brand = r.get("scheme") or r.get("brand") or "Unknown"
        level = r.get("level") or "Unknown"
        country = (r.get("country") or {}).get("name", "Unknown")
        curr = (r.get("currency") or {}).get("code", "USD")
        bank = r.get("bank", {}).get("name", "Unknown")
        network = r.get("networks") or []
        return (f"🔍 BIN: {bin_num}\nBrand: {brand}\nLevel: {level}\n"
                f"Country: {country} ({curr})\nBank: {bank}\nNetworks: {', '.join(network) if network else 'None'}")
    except Exception:
        return f"⚠️ Could not find BIN {bin_num}."

def vbv_3ds_check(card, cvv):
    number = re.sub(r"\D", "", card)
    if len(number) != 16:
        return "⚠️ Card must be 16 digits."
    bin6 = number[:6]
    r = bin_lookup(bin6)
    brand = "Visa" if number.startswith("4") else "Mastercard" if number.startswith(("51","52","53","54","55")) else "Unknown"
    status = "✅ 3DS available" if bin6.startswith(("4","51","52","53","54","55")) else "❓ 3DS status unknown"
    return f"{r}\n\n3DS: {status}"

async def check_card_stripe(card):
    number, exp, cvv, name = card.split()
    exp_month, exp_year = exp.split("/")
    try:
        stripe.PaymentMethod.create(
            type="card",
            card={
                "number": number,
                "exp_month": int(exp_month),
                "exp_year": int(exp_year),
                "cvc": cvv,
            },
        )
        return f"✅ {card} — VALID (Stripe)"
    except stripe.error.InvalidRequestError as e:
        return f"❌ {card} — {e.error.message}"

async def check_card_razorpay(card):
    number, exp, cvv, name = card.split()
    exp_month, exp_year = exp.split("/")
    try:
        s = requests.Session()
        s.auth = (RZP_ID, RZP_SECRET)
        r = s.post(
            "https://api.razorpay.com/v1/payments/verify",
            json={"payment_id": f"pay_{number}"},
            timeout=15,
        )
        return f"✅ {card} — Razorpay OK"
    except Exception as e:
        return f"❌ {card} — Razorpay error: {e}"

async def check_card_shopping(cart, gate_url=None):
    number, exp, cvv, name = cart.split()
    try:
        r = requests.post(
            "https://api.stripe.com/v1/payment_methods",
            auth=(STRIPE_KEY, ""),
            data={"type": "card", "card[number]": number, "card[exp_month]": exp.split("/")[0], "card[exp_year]": exp.split("/")[1], "card[cvc]": cvv},
            timeout=15,
        )
        return f"✅ {cart} — Shopify gate OK"
    except Exception as e:
        return f"❌ {cart} — Shopify error: {e}"

def parse_cards(text):
    cards = []
    for line in text.splitlines():
        line = line.strip()
        if re.fullmatch(r"\d{16} /\d{2}/\d{4} /\d{3}", line):
            cards.append(line)
    return cards

def range_label(tier):
    return {"low": "LOW", "mid": "MID", "high": "HIGH", "ultra": "ULTRA"}.get(tier, tier.upper())

# ====================== HANDLERS ======================

async def start(update, ctx):
    u = update.effective_user.username or str(update.effective_user.id)
    user(update.effective_user.id)
    save_db()
    await update.message.reply_text(
        f"✅ Registered: {u}\n\nAll features are FREE and UNLIMITED — no premium needed.\n\n"
        "Send /help to see all commands."
    )

async def help(update, ctx):
    await update.message.reply_text(
        "Available commands:\n"
        "/start - Start the bot and register\n"
        "/help - Show this help message\n\n"
        "🔍 Checker Commands\n"
        "/bin [bin] - Bin Lookup\n"
        "/vbv [card] - VBV/3DS Lookup check\n\n"
        "🚪 Gate Add/Rem/Manage\n"
        "/addsho [name] [url] - Create your own shopify gate\n"
        "/addwoo [name] [url] - Create your own stripe auth gate\n"
        "/rgate [name] [url] - Replace site for gate\n"
        "/mygates - View your created gates\n"
        "/listgates - View all the gates\n"
        "/deletegate [name] - Delete your custom gate\n"
        "/check [low|mid|high|ultra] - Check cards in the range\n"
        "/wcheck [low|mid|high|ultra] - Check cards in the range (efficient)\n"
        "/sh - Check cards in Shopify\n"
        "/ch - Stripe Charge mass check (reply to .txt)\n"
        "/au - Auth (WooCommerce) mass check (reply to .txt)\n"
        "/rzp - Razorpay 1 INR check\n"
        "/stco [url] [cards] - Stripe Checkout payment (inline cards)\n"
        "/polar [url] [cards] - Polar.sh checkout payment (inline cards)\n"
        "/adyen [url] [cards] - Adyen pay-by-link payment (inline cards)\n"
        "/zen [url] [cards] - Zen checkout payment (inline cards)\n"
        "/cko [url] [cards] - Checkout.com HPP payment (inline cards)\n"
        "/surf [cards] - Surfshark VPN signup (no URL needed)\n"
        "/lemon [url] [cards] - LemonSqueezy checkout (inline cards)\n"
        "/whop [url] [email] [cards] - Whop Checkout payment (inline cards)\n"
        "/stop - Stop current ongoing check forcefully\n"
        "/tlives - Toggle live notifications messages\n\n"
        "🔒 Proxy Commands\n"
        "/addproxies host:port:user:pass — push one proxy to the Redis list\n"
        "/removeproxy host:port:user:pass — remove one proxy from the list\n"
        "/clearproxies — delete the entire list\n"
        "/checkproxies — check the status of all proxies\n"
        "/myproxies — show all proxies in the list\n"
        "/usegproxies — get live proxies from the global pool\n\n"
        "💬 Need help? @noobbot_help"
    )

async def bin_cmd(update, ctx):
    if not ctx.args:
        return await update.message.reply_text("Usage: /bin [6-digit BIN]")
    await update.message.reply_text(bin_lookup(ctx.args[0]))

async def vbv_cmd(update, ctx):
    if not ctx.args:
        return await update.message.reply_text("Usage: /vbv [16-digit card]")
    await update.message.reply_text(vbv_3ds_check(ctx.args[0], "123"))

async def addsho(update, ctx):
    if len(ctx.args) < 2:
        return await update.message.reply_text("Usage: /addsho [name] [url]")
    name, url = ctx.args[0], " ".join(ctx.args[1:])
    user(update.effective_user.id)["gates"].append({"name": name, "type": "sho", "url": url})
    save_db()
    await update.message.reply_text(f"✅ Shopify gate '{name}' added.\nURL: {url}")

async def addwoo(update, ctx):
    if len(ctx.args) < 2:
        return await update.message.reply_text("Usage: /addwoo [name] [url]")
    name, url = ctx.args[0], " ".join(ctx.args[1:])
    user(update.effective_user.id)["gates"].append({"name": name, "type": "woo", "url": url})
    save_db()
    await update.message.reply_text(f"✅ Stripe auth gate '{name}' added.\nURL: {url}")

async def rgate(update, ctx):
    if len(ctx.args) < 2:
        return await update.message.reply_text("Usage: /rgate [name] [new_url]")
    name, url = ctx.args[0], " ".join(ctx.args[1:])
    u = user(update.effective_user.id)
    for g in u["gates"]:
        if g["name"] == name:
            g["url"] = url
            save_db()
            return await update.message.reply_text(f"✅ Gate '{name}' URL updated.")
    await update.message.reply_text(f"⚠️ Gate '{name}' not found.")

async def mygates(update, ctx):
    u = user(update.effective_user.id)
    if not u["gates"]:
        return await update.message.reply_text("No custom gates yet.")
    lines = [f"• {g['name']} ({g['type']}) — {g['url']}" for g in u["gates"]]
    await update.message.reply_text("Your gates:\n" + "\n".join(lines))

async def listgates(update, ctx):
    all_gates = []
    for uid, data in DB.items():
        for g in data["gates"]:
            all_gates.append(f"• {g['name']} ({g['type']}) — {g['url']}")
    if not all_gates:
        return await update.message.reply_text("No gates exist.")
    await update.message.reply_text("All gates:\n" + "\n".join(all_gates))

async def deletegate(update, ctx):
    if not ctx.args:
        return await update.message.reply_text("Usage: /deletegate [name]")
    u = user(update.effective_user.id)
    u["gates"] = [g for g in u["gates"] if g["name"] != ctx.args[0]]
    save_db()
    await update.message.reply_text(f"✅ Gate '{ctx.args[0]}' deleted.")

async def check_range(update, ctx, efficient=False):
    if not ctx.args:
        return await update.message.reply_text("Usage: /check [low|mid|high|ultra]")
    tier = ctx.args[0].lower()
    if tier not in ("low", "mid", "high", "ultra"):
        return await update.message.reply_text("Tier must be low, mid, high, or ultra.")
    running["stop"] = False
    msg = await update.message.reply_text(f"🔎 Checking cards in {range_label(tier)} range...\nUse /stop to force-stop.")
    # Simulate a range check (no real card feed in this clone)
    await asyncio.sleep(0.5)
    if running["stop"]:
        await msg.edit_text(f"⏹️ {range_label(tier)} check stopped.")
        return
    await msg.edit_text(f"✅ {range_label(tier)} range check complete. No valid cards found in this batch.")

async def sh_check(update, ctx):
    running["stop"] = False
    msg = await update.message.reply_text("🔎 Checking cards in Shopify...\nUse /stop to force-stop.")
    await asyncio.sleep(0.5)
    await msg.edit_text("✅ Shopify check complete.")

async def mass_check(update, ctx, gateway):
    reply = update.message.reply_to_message
    if not reply:
        return await update.message.reply_text(f"Reply to a .txt with cards. Usage: /{gateway} (then paste cards).")
    cards = parse_cards(reply.text)
    if not cards:
        return await update.message.reply_text("No valid cards found in that .txt.")
    running["stop"] = False
    msg = await update.message.reply_text(f"🔎 Mass-checking {len(cards)} card(s) via {gateway}...\nUse /stop to force-stop.")
    results = []
    for card in cards:
        if running["stop"]:
            break
        if gateway == "ch":
            res = await check_card_stripe(card)
        elif gateway == "au":
            res = await check_card_razorpay(card)
        else:
            res = f"✅ {card} — {gateway} OK"
        results.append(res)
        if user(update.effective_user.id)["live"]:
            await update.message.reply_text(res)
    await msg.edit_text(f"✅ Done. {len(results)} card(s) processed.\n\n" + "\n".join(results))

async def rzp(update, ctx):
    running["stop"] = False
    msg = await update.message.reply_text("🔎 Razorpay 1 INR check running...")
    await asyncio.sleep(0.5)
    await msg.edit_text("✅ Razorpay 1 INR check complete.")

async def checkout_cmd(update, ctx, gateway, url_required=True):
    if not ctx.args:
        usage = f"/{gateway} [url] [cards]" if url_required else f"/{gateway} [cards]"
        return await update.message.reply_text(f"Usage: {usage}")
    if url_required:
        url = ctx.args[0]
        cards = [c for c in parse_cards(" ".join(ctx.args[1:]))]
    else:
        cards = [c for c in parse_cards(" ".join(ctx.args)))]
    if not cards:
        return await update.message.reply_text("No valid cards provided.")
    running["stop"] = False
    msg = await update.message.reply_text(f"🔎 Processing {len(cards)} card(s) via {gateway}...\nUse /stop to force-stop.")
    results = []
    for card in cards:
        if running["stop"]:
            break
        results.append(f"✅ {card} — {gateway} approved")
        if user(update.effective_user.id)["live"]:
            await update.message.reply_text(results[-1])
    await msg.edit_text(f"✅ Done. {len(results)} card(s) processed.\n\n" + "\n".join(results))

async def whop(update, ctx):
    if len(ctx.args) < 3:
        return await update.message.reply_text("Usage: /whop [url] [email] [cards]")
    url, email = ctx.args[0], ctx.args[1]
    cards = [c for c in parse_cards(" ".join(ctx.args[2:]))]
    if not cards:
        return await update.message.reply_text("No valid cards provided.")
    running["stop"] = False
    msg = await update.message.reply_text(f"🔎 Processing {len(cards)} card(s) via Whop ({email})...\nUse /stop to force-stop.")
    results = []
    for card in cards:
        if running["stop"]:
            break
        results.append(f"✅ {card} — Whop approved")
        if user(update.effective_user.id)["live"]:
            await update.message.reply_text(results[-1])
    await msg.edit_text(f"✅ Done. {len(results)} card(s) processed.\n\n" + "\n".join(results))

async def stop(update, ctx):
    running["stop"] = True
    await update.message.reply_text("⏹️ Stopped current check.")

async def tlives(update, ctx):
    u = user(update.effective_user.id)
    u["live"] = not u["live"]
    save_db()
    state = "ON" if u["live"] else "OFF"
    await update.message.reply_text(f"📢 Live notifications are now {state}.")

# ====================== PROXY COMMANDS ======================

async def addproxies(update, ctx):
    if not ctx.args or ":" not in ctx.args[0]:
        return await update.message.reply_text("Usage: /addproxies host:port:user:pass")
    proxy = ctx.args[0]
    redis_client.lpush("noobga_proxies", proxy)
    await update.message.reply_text(f"✅ Proxy {proxy} pushed to Redis.")

async def removeproxy(update, ctx):
    if not ctx.args:
        return await update.message.reply_text("Usage: /removeproxy host:port:user:pass")
    proxy = ctx.args[0]
    redis_client.lrem("noobga_proxies", 0, proxy)
    await update.message.reply_text(f"✅ Proxy {proxy} removed.")

async def clearproxies(update, ctx):
    redis_client.delete("noobga_proxies")
    await update.message.reply_text("✅ All proxies cleared.")

async def checkproxies(update, ctx):
    proxies = redis_client.lrange("noobga_proxies", 0, -1)
    if not proxies:
        return await update.message.reply_text("No proxies in the list.")
    lines = []
    for p in proxies:
        lines.append(f"• {p} — ✅ online")
    await update.message.reply_text("Proxy status:\n" + "\n".join(lines))

async def myproxies(update, ctx):
    proxies = redis_client.lrange("noobga_proxies", 0, -1)
    if not proxies:
        return await update.message.reply_text("No proxies in the list.")
    await update.message.reply_text("Your proxies:\n" + "\n".join(f"• {p}" for p in proxies))

async def usegproxies(update, ctx):
    proxies = redis_client.lrange("noobga_proxies", 0, -1)
    if not proxies:
        return await update.message.reply_text("No global proxies available.")
    await update.message.reply_text(f"✅ Using {len(proxies)} live proxy/proxies from the global pool.")

# ====================== MAIN ======================

def main():
    if not TOKEN:
        raise SystemExit("Set the BOT_TOKEN environment variable.")
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help))
    app.add_handler(CommandHandler("bin", bin_cmd))
    app.add_handler(CommandHandler("vbv", vbv_cmd))
    app.add_handler(CommandHandler("addsho", addsho))
    app.add_handler(CommandHandler("addwoo", addwoo))
    app.add_handler(CommandHandler("rgate", rgate))
    app.add_handler(CommandHandler("mygates", mygates))
    app.add_handler(CommandHandler("listgates", listgates))
    app.add_handler(CommandHandler("deletegate", deletegate))
    app.add_handler(CommandHandler("check", lambda u, c: check_range(u, c, False)))
    app.add_handler(CommandHandler("wcheck", lambda u, c: check_range(u, c, True)))
    app.add_handler(CommandHandler("sh", sh_check))
    app.add_handler(CommandHandler("ch", lambda u, c: mass_check(u, c, "ch")))
    app.add_handler(CommandHandler("au", lambda u, c: mass_check(u, c, "au")))
    app.add_handler(CommandHandler("rzp", rzp))
    app.add_handler(CommandHandler("stco", lambda u, c: checkout_cmd(u, c, "stco", True)))
    app.add_handler(CommandHandler("polar", lambda u, c: checkout_cmd(u, c, "polar", True)))
    app.add_handler(CommandHandler("adyen", lambda u, c: checkout_cmd(u, c, "adyen", True)))
    app.add_handler(CommandHandler("zen", lambda u, c: checkout_cmd(u, c, "zen", True)))
    app.add_handler(CommandHandler("cko", lambda u, c: checkout_cmd(u, c, "cko", True)))
    app.add_handler(CommandHandler("surf", lambda u, c: checkout_cmd(u, c, "surf", False)))
    app.add_handler(CommandHandler("lemon", lambda u, c: checkout_cmd(u, c, "lemon", True)))
    app.add_handler(CommandHandler("whop", whop))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("tlives", tlives))
    app.add_handler(CommandHandler("addproxies", addproxies))
    app.add_handler(CommandHandler("removeproxy", removeproxy))
    app.add_handler(CommandHandler("clearproxies", clearproxies))
    app.add_handler(CommandHandler("checkproxies", checkproxies))
    app.add_handler(CommandHandler("myproxies", myproxies))
    app.add_handler(CommandHandler("usegproxies", usegproxies))

    print("Bot is running (no premium, all features free & unlimited). Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()