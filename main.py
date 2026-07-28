"""
Gym Membership Management Telegram Bot
========================================

Storage: MongoDB (MongoDB Atlas free tier recommended)
--------------------------------------------------------
Data is stored in MongoDB instead of a local file. This means your data
survives Railway redeploys, restarts, and even switching to a completely
different Railway account - because MongoDB Atlas is a separate, free,
persistent database that lives independently of Railway.

Features
--------
1. /add <name> <number> <dd/mm/yyyy> [duration]
   - Adds a new member. "duration" is optional: 1m, 3m, 6m, 12m, etc.
     If omitted, defaults to 1 month.
   - If a member with the same name already exists (different number),
     the new member is automatically renamed "Name 1", "Name 2", etc.
   - If the same phone number already exists, the record is updated instead
     of creating a duplicate.

2. /edit <number> <duration>
   - Changes a member's subscription plan/mode (e.g. monthly -> quarterly).
   - The member's expiry date is immediately recalculated from their
     original join date using the new duration, and this new duration
     becomes the default used every time /paid renews them going forward.

3. /due
   - Lists every member whose subscription has already expired.

4. /members
   - Shows "Total Members: N" followed by a numbered, compact list
     (name, number, expiry) of every member.

5. /paid <number>
   - Looks up the member by phone number only and automatically renews
     their subscription using their configured plan duration.

6. /delete <number>
   - Permanently removes a member using only their phone number.

7. Automatic daily expiry check
   - Runs once every day. Any member whose subscription expires "today"
     is marked as expired and a notification is sent to every configured
     admin/owner in the exact format:
         "Aditya (7992357603) - Subscription expired on 12/07/2026"

8. /backup and /restore
   - /backup exports all current MongoDB data as a downloadable JSON file
     (an extra manual safety net, on top of MongoDB's own persistence).
   - /restore lets you upload a previously downloaded backup file, which
     replaces all current data in MongoDB with the contents of that file.

Setup
-----
1. pip install -r requirements.txt
2. Create a free MongoDB Atlas cluster (see README.md for step-by-step).
3. Create a bot with @BotFather on Telegram and copy the bot token.
4. Get your Telegram numeric chat ID (and every other admin/owner's ID)
   via @userinfobot on Telegram.
5. Set the environment variables below (see README.md).
6. Run:  python main.py

All bot-facing messages are in English and written in a professional tone,
as requested.
"""

import json
import logging
import os
import re
from datetime import datetime, date
from pathlib import Path

from dateutil.relativedelta import relativedelta
from motor.motor_asyncio import AsyncIOMotorClient

from telegram import Update, Document
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #

# Set these as environment variables (recommended, especially on Railway):
#   GYM_BOT_TOKEN   - your Telegram bot token from @BotFather
#   GYM_BOT_ADMINS  - comma separated numeric Telegram chat IDs
#   MONGO_URI       - your MongoDB Atlas connection string
#   MONGO_DB_NAME   - (optional) database name, defaults to "gym_bot"
BOT_TOKEN = os.environ.get("GYM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")

ADMIN_IDS = [
    int(x) for x in os.environ.get("GYM_BOT_ADMINS", "").split(",") if x.strip()
] or [
    # Fallback: put your own numeric Telegram chat IDs here if you are not
    # using the environment variable, e.g.:
    # 111111111,
    # 222222222,
]

MONGO_URI = os.environ.get("MONGO_URI", "")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "gym_bot")

DATE_FMT = "%d/%m/%Y"
DEFAULT_PLAN_MONTHS = 1

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("gym_bot")

# In-memory flag: which admin user_ids are currently expected to send a
# backup file for /restore.
AWAITING_RESTORE = set()

# --------------------------------------------------------------------------- #
# MONGODB CONNECTION
# --------------------------------------------------------------------------- #

_mongo_client = None
_members_collection = None


def get_collection():
    """Lazily initializes and returns the MongoDB 'members' collection."""
    global _mongo_client, _members_collection
    if _members_collection is None:
        _mongo_client = AsyncIOMotorClient(MONGO_URI)
        db = _mongo_client[MONGO_DB_NAME]
        _members_collection = db["members"]
    return _members_collection


# --------------------------------------------------------------------------- #
# DATA LAYER (MongoDB, keyed by phone number as the document _id)
# --------------------------------------------------------------------------- #

async def load_members() -> dict:
    """Returns all members as {number: {name, number, start_date, expiry_date, plan_months, status}}."""
    collection = get_collection()
    members = {}
    async for doc in collection.find({}):
        number = doc["_id"]
        doc.pop("_id", None)
        members[number] = doc
    return members


async def save_member(number: str, data: dict) -> None:
    """Inserts or updates a single member document."""
    collection = get_collection()
    await collection.replace_one({"_id": number}, data, upsert=True)


async def delete_member_doc(number: str) -> bool:
    collection = get_collection()
    result = await collection.delete_one({"_id": number})
    return result.deleted_count > 0


async def restore_all_members(members: dict) -> None:
    """Wipes the collection and replaces it with the given data (used by /restore)."""
    collection = get_collection()
    await collection.delete_many({})
    if members:
        docs = [{"_id": number, **data} for number, data in members.items()]
        await collection.insert_many(docs)


def normalize_number(raw: str) -> str:
    """Keep digits only, e.g. '+91 79923-57603' -> '917992357603'."""
    return re.sub(r"\D", "", raw)


def unique_display_name(members: dict, base_name: str, exclude_number: str = None) -> str:
    """
    If 'base_name' is already used by a different member, append 1, 2, 3...
    Matching is case-insensitive on the base name (ignoring any numeric suffix).
    """
    base_name_clean = base_name.strip()
    used_names = {
        v["name"].lower()
        for k, v in members.items()
        if k != exclude_number
    }

    if base_name_clean.lower() not in used_names:
        return base_name_clean

    counter = 1
    while f"{base_name_clean} {counter}".lower() in used_names:
        counter += 1
    return f"{base_name_clean} {counter}"


def parse_duration_months(text: str):
    """
    Parses strings like '1m', '3m', '6month', '12 months' into an integer
    number of months. Returns None if the text does not match this format.
    """
    match = re.fullmatch(r"\s*(\d+)\s*m(?:onths?)?\s*", text, flags=re.IGNORECASE)
    if not match:
        return None
    months = int(match.group(1))
    return months if months > 0 else None


def format_plan(months: int) -> str:
    return f"{months} month" + ("s" if months != 1 else "")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def deny_access(update: Update) -> None:
    await update.message.reply_text(
        "You are not authorized to use this bot. Please contact the gym admin."
    )


# --------------------------------------------------------------------------- #
# COMMAND HANDLERS
# --------------------------------------------------------------------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Welcome to the Gym Membership Management Bot.\n\n"
        "Available commands:\n"
        "/add <name> <number> <dd/mm/yyyy> [duration] - Add a new member\n"
        "/edit <number> <duration> - Change a member's subscription plan\n"
        "/members - View all members\n"
        "/due - View expired memberships\n"
        "/paid <number> - Renew a member's subscription\n"
        "/delete <number> - Remove a member\n"
        "/backup - Download the current data as a file\n"
        "/restore - Restore data from a previously downloaded backup file\n\n"
        "Duration format: 1m, 3m, 6m, 12m (defaults to 1m if not specified)"
    )


async def add_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny_access(update)

    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Usage: /add <name> <number> <dd/mm/yyyy> [duration]\n"
            "Example: /add Aditya 7992357603 12/06/2026\n"
            "Example with plan: /add Aditya 7992357603 12/06/2026 3m"
        )
        return

    maybe_duration = parse_duration_months(args[-1])
    if maybe_duration is not None and len(args) >= 4:
        plan_months = maybe_duration
        date_raw = args[-2]
        number_raw = args[-3]
        name_parts = args[:-3]
    else:
        plan_months = DEFAULT_PLAN_MONTHS
        date_raw = args[-1]
        number_raw = args[-2]
        name_parts = args[:-2]

    name = " ".join(name_parts).strip()
    number = normalize_number(number_raw)

    if not name or not number:
        await update.message.reply_text("Please provide a valid name and phone number.")
        return

    try:
        start_dt = datetime.strptime(date_raw, DATE_FMT).date()
    except ValueError:
        await update.message.reply_text(
            "Invalid date format. Please use dd/mm/yyyy, e.g. 12/06/2026."
        )
        return

    expiry_dt = start_dt + relativedelta(months=plan_months)

    members = await load_members()
    is_update = number in members
    final_name = unique_display_name(members, name, exclude_number=number)

    new_doc = {
        "name": final_name,
        "number": number,
        "start_date": start_dt.strftime(DATE_FMT),
        "expiry_date": expiry_dt.strftime(DATE_FMT),
        "plan_months": plan_months,
        "status": "active",
    }
    await save_member(number, new_doc)

    action = "updated" if is_update else "added"
    await update.message.reply_text(
        f"Member {action} successfully.\n\n"
        f"Name: {final_name}\n"
        f"Number: {number}\n"
        f"Joined: {start_dt.strftime(DATE_FMT)}\n"
        f"Plan: {format_plan(plan_months)}\n"
        f"Expiry: {expiry_dt.strftime(DATE_FMT)}"
    )


async def edit_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny_access(update)

    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "Usage: /edit <number> <duration>\n"
            "Example: /edit 7992357603 3m"
        )
        return

    number = normalize_number(args[0])
    plan_months = parse_duration_months(args[1])

    if plan_months is None:
        await update.message.reply_text(
            "Invalid duration format. Use 1m, 3m, 6m, 12m, etc."
        )
        return

    members = await load_members()
    if number not in members:
        await update.message.reply_text(f"No member found with number {number}.")
        return

    member = members[number]
    start_dt = datetime.strptime(member["start_date"], DATE_FMT).date()
    new_expiry = start_dt + relativedelta(months=plan_months)

    member["plan_months"] = plan_months
    member["expiry_date"] = new_expiry.strftime(DATE_FMT)
    member["status"] = "active" if new_expiry >= date.today() else "expired"
    await save_member(number, member)

    await update.message.reply_text(
        f"Subscription plan updated successfully.\n\n"
        f"Name: {member['name']}\n"
        f"Number: {number}\n"
        f"New Plan: {format_plan(plan_months)} (applies from every future renewal too)\n"
        f"Recalculated Expiry: {member['expiry_date']}"
    )


async def list_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny_access(update)

    members = await load_members()
    if not members:
        await update.message.reply_text("No members found.")
        return

    sorted_members = sorted(members.values(), key=lambda x: x["name"].lower())
    lines = [f"Total Members: {len(sorted_members)}\n"]
    for i, m in enumerate(sorted_members, start=1):
        lines.append(f"{i}. {m['name']} | {m['number']} | Expiry: {m['expiry_date']}")

    await update.message.reply_text("\n".join(lines))


async def due_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny_access(update)

    members = await load_members()
    today = date.today()
    due_list = []
    for m in members.values():
        expiry = datetime.strptime(m["expiry_date"], DATE_FMT).date()
        if expiry <= today:
            due_list.append(m)

    if not due_list:
        await update.message.reply_text("No members are currently due. All subscriptions are active.")
        return

    lines = ["Members with Expired Subscriptions:\n"]
    for m in sorted(due_list, key=lambda x: x["name"].lower()):
        lines.append(f"- {m['name']} ({m['number']}) - Expired on {m['expiry_date']}")
    await update.message.reply_text("\n".join(lines))


async def mark_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny_access(update)

    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Usage: /paid <number>\nExample: /paid 7992357603")
        return

    number = normalize_number(args[0])
    members = await load_members()

    if number not in members:
        await update.message.reply_text(f"No member found with number {number}.")
        return

    member = members[number]
    plan_months = member.get("plan_months", DEFAULT_PLAN_MONTHS)
    today = date.today()
    current_expiry = datetime.strptime(member["expiry_date"], DATE_FMT).date()

    base_date = current_expiry if current_expiry > today else today
    new_expiry = base_date + relativedelta(months=plan_months)

    member["expiry_date"] = new_expiry.strftime(DATE_FMT)
    member["status"] = "active"
    await save_member(number, member)

    await update.message.reply_text(
        f"Payment recorded successfully.\n\n"
        f"Name: {member['name']}\n"
        f"Number: {number}\n"
        f"Plan: {format_plan(plan_months)}\n"
        f"New Expiry Date: {member['expiry_date']}"
    )


async def delete_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny_access(update)

    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Usage: /delete <number>\nExample: /delete 7992357603")
        return

    number = normalize_number(args[0])
    members = await load_members()

    if number not in members:
        await update.message.reply_text(f"No member found with number {number}.")
        return

    removed = members[number]
    await delete_member_doc(number)

    await update.message.reply_text(
        f"Member removed successfully.\n\n"
        f"Name: {removed['name']}\n"
        f"Number: {number}"
    )


# --------------------------------------------------------------------------- #
# BACKUP / RESTORE (extra manual safety net on top of MongoDB persistence)
# --------------------------------------------------------------------------- #

async def backup_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny_access(update)

    members = await load_members()
    if not members:
        await update.message.reply_text("No data to back up yet. Add a member first.")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_path = Path(f"/tmp/members_backup_{timestamp}.json")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(members, f, indent=2, ensure_ascii=False)

    await update.message.reply_document(
        document=open(temp_path, "rb"),
        filename=temp_path.name,
        caption=(
            "Here is your current member data backup.\n"
            "Save this file safely. Use /restore to load it back into the bot later."
        ),
    )
    temp_path.unlink(missing_ok=True)


async def restore_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny_access(update)

    AWAITING_RESTORE.add(update.effective_user.id)
    await update.message.reply_text(
        "Please send the backup file (.json) now as a document to restore your data.\n"
        "This will overwrite the current member data."
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not is_admin(user_id):
        return await deny_access(update)

    if user_id not in AWAITING_RESTORE:
        return

    document: Document = update.message.document
    if not document.file_name.lower().endswith(".json"):
        await update.message.reply_text(
            "That does not look like a valid backup file (expected a .json file). "
            "Please send the correct backup file, or send /restore again to cancel and retry."
        )
        return

    file = await document.get_file()
    file_bytes = await file.download_as_bytearray()

    try:
        restored_data = json.loads(file_bytes.decode("utf-8"))
        if not isinstance(restored_data, dict):
            raise ValueError("Backup file format is invalid.")
    except Exception:
        await update.message.reply_text(
            "Failed to read this backup file. Please make sure it is a valid, unmodified "
            "backup and try /restore again."
        )
        return

    await restore_all_members(restored_data)
    AWAITING_RESTORE.discard(user_id)

    await update.message.reply_text(
        f"Data restored successfully. {len(restored_data)} member record(s) loaded."
    )


# --------------------------------------------------------------------------- #
# DAILY EXPIRY CHECK (runs automatically, notifies admins/owners)
# --------------------------------------------------------------------------- #

async def check_expiries(context: ContextTypes.DEFAULT_TYPE) -> None:
    members = await load_members()
    today = date.today()

    for number, member in members.items():
        expiry = datetime.strptime(member["expiry_date"], DATE_FMT).date()
        if expiry <= today and member["status"] != "expired":
            member["status"] = "expired"
            await save_member(number, member)

            message = (
                f"{member['name']} ({member['number']}) - "
                f"Subscription expired on {member['expiry_date']}"
            )
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(chat_id=admin_id, text=message)
                except Exception as e:
                    logger.error("Failed to notify admin %s: %s", admin_id, e)


# --------------------------------------------------------------------------- #
# APPLICATION ENTRY POINT
# --------------------------------------------------------------------------- #

def main() -> None:
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE" or not BOT_TOKEN:
        raise SystemExit(
            "Bot token not configured. Set the GYM_BOT_TOKEN environment variable."
        )
    if not MONGO_URI:
        raise SystemExit(
            "MongoDB not configured. Set the MONGO_URI environment variable "
            "to your MongoDB Atlas connection string. See README.md for setup steps."
        )
    if not ADMIN_IDS:
        logger.warning(
            "No ADMIN_IDS configured - expiry notifications will not be sent to anyone. "
            "Set the GYM_BOT_ADMINS environment variable."
        )

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_member))
    application.add_handler(CommandHandler("edit", edit_member))
    application.add_handler(CommandHandler("members", list_members))
    application.add_handler(CommandHandler("due", due_members))
    application.add_handler(CommandHandler("paid", mark_paid))
    application.add_handler(CommandHandler("delete", delete_member))
    application.add_handler(CommandHandler("backup", backup_data))
    application.add_handler(CommandHandler("restore", restore_data))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Check expiries once at startup, then every 24 hours.
    application.job_queue.run_repeating(check_expiries, interval=24 * 60 * 60, first=10)

    logger.info("Gym bot started. Polling for updates...")
    application.run_polling()


if __name__ == "__main__":
    main()
