"""
Gym Membership Management Telegram Bot
========================================

Storage: local JSON file (members.json)
--------------------------------------------------------
All data is stored in a simple members.json file next to this script.

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
   - Always extends from the member's existing expiry date (even if it
     has already passed), so the math stays simple and predictable
     regardless of how late the payment is.

6. /delete <number>
   - Permanently removes a member using only their phone number.

7. Automatic daily expiry check
   - Runs once every day. Any member whose subscription expires "today"
     is marked as expired and a notification is sent to every configured
     admin/owner in the exact format:
         "Aditya (7992357603) - Subscription expired on 12/07/2026"

8. /backup and /restore
   - /backup sends the current members.json data file to you as a
     downloadable Telegram document. Useful as a manual safety net,
     especially on platforms with ephemeral storage (like Railway),
     since a redeploy or restart can wipe the local file.
   - /restore lets you upload a previously downloaded backup file back
     to the bot, replacing all current data with it.

9. /1m, /3m, /6m, /1y (and any /<N>m you like)
   - Lists every member currently on that exact plan duration, so you
     can quickly see "who is on the 3 month plan" etc.

10. /find <query>
    - If the query is a phone number, shows that member's exact details.
    - If the query is a name (or part of a name), shows every member
      whose name contains that text - handy when you don't remember
      the exact spelling or number.

11. /permission <numeric_id_or_@username>
    - Grants another person admin access to the bot directly from
      Telegram, no need to touch Railway environment variables.
    - Prefer the numeric Telegram ID (from @userinfobot) - usernames
      only resolve if that person has a public username and Telegram
      is able to look it up, which isn't always guaranteed.

Setup
-----
1. pip install -r requirements.txt
2. Create a bot with @BotFather on Telegram and copy the bot token.
3. Get your Telegram numeric chat ID (and every other admin/owner's ID) -
   the easiest way is to message @userinfobot on Telegram.
4. Fill in BOT_TOKEN and ADMIN_IDS below (or set them as environment
   variables - see the bottom of this file).
5. Run:  python main.py

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

# You can either hardcode these two values, or (recommended) set them as
# environment variables before starting the bot:
#   export GYM_BOT_TOKEN="123456:ABC-your-bot-token"
#   export GYM_BOT_ADMINS="111111111,222222222"
BOT_TOKEN = os.environ.get("GYM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")

ADMIN_IDS = [
    int(x) for x in os.environ.get("GYM_BOT_ADMINS", "").split(",") if x.strip()
] or [
    # Fallback: put your own numeric Telegram chat IDs here if you are not
    # using the environment variable, e.g.:
    # 111111111,
    # 222222222,
]

DATA_FILE = Path(__file__).parent / "members.json"
ADMIN_FILE = Path(__file__).parent / "admins_extra.json"
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


def load_extra_admins() -> list:
    """Admins added later via /permission, stored separately from the
    original env-configured ADMIN_IDS so they survive restarts."""
    if not ADMIN_FILE.exists():
        return []
    with open(ADMIN_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_extra_admins(ids: list) -> None:
    with open(ADMIN_FILE, "w", encoding="utf-8") as f:
        json.dump(ids, f, indent=2)


# Combined set of admins: the ones from the GYM_BOT_ADMINS env variable,
# plus any added later via /permission (persisted in admins_extra.json).
ADMIN_IDS = set(ADMIN_IDS) | set(load_extra_admins())


# --------------------------------------------------------------------------- #
# DATA LAYER (simple JSON file, keyed by phone number)
# --------------------------------------------------------------------------- #

def load_members() -> dict:
    if not DATA_FILE.exists():
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_members(members: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(members, f, indent=2, ensure_ascii=False)


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
        "/find <number or name> - Look up a member\n"
        "/1m, /3m, /6m, /1y - List members on that plan\n"
        "/permission <id or @username> - Grant admin access\n"
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

    members = load_members()
    is_update = number in members
    final_name = unique_display_name(members, name, exclude_number=number)

    members[number] = {
        "name": final_name,
        "number": number,
        "start_date": start_dt.strftime(DATE_FMT),
        "expiry_date": expiry_dt.strftime(DATE_FMT),
        "plan_months": plan_months,
        "status": "active",
    }
    save_members(members)

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

    members = load_members()
    if number not in members:
        await update.message.reply_text(f"No member found with number {number}.")
        return

    member = members[number]
    start_dt = datetime.strptime(member["start_date"], DATE_FMT).date()
    new_expiry = start_dt + relativedelta(months=plan_months)

    member["plan_months"] = plan_months
    member["expiry_date"] = new_expiry.strftime(DATE_FMT)
    member["status"] = "active" if new_expiry >= date.today() else "expired"
    save_members(members)

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

    members = load_members()
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

    members = load_members()
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
    members = load_members()

    if number not in members:
        await update.message.reply_text(f"No member found with number {number}.")
        return

    member = members[number]
    plan_months = member.get("plan_months", DEFAULT_PLAN_MONTHS)
    current_expiry = datetime.strptime(member["expiry_date"], DATE_FMT).date()

    # Always extend from the existing expiry date, even if it has already
    # passed - keeps the math simple and predictable regardless of how
    # late the payment is.
    new_expiry = current_expiry + relativedelta(months=plan_months)

    member["expiry_date"] = new_expiry.strftime(DATE_FMT)
    member["status"] = "active"
    save_members(members)

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
    members = load_members()

    if number not in members:
        await update.message.reply_text(f"No member found with number {number}.")
        return

    removed = members.pop(number)
    save_members(members)

    await update.message.reply_text(
        f"Member removed successfully.\n\n"
        f"Name: {removed['name']}\n"
        f"Number: {number}"
    )


async def find_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny_access(update)

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /find <number or name>\n"
            "Example: /find 7992357603\n"
            "Example: /find Yash"
        )
        return

    query = " ".join(args).strip()
    members = load_members()

    # If the query looks like a phone number, do an exact lookup.
    digits_only = re.sub(r"\D", "", query)
    if digits_only and digits_only == query.replace(" ", "").replace("+", ""):
        number = normalize_number(query)
        if number in members:
            m = members[number]
            plan_months = m.get("plan_months", DEFAULT_PLAN_MONTHS)
            await update.message.reply_text(
                f"Name: {m['name']}\n"
                f"Number: {m['number']}\n"
                f"Joined: {m['start_date']}\n"
                f"Plan: {format_plan(plan_months)}\n"
                f"Expiry: {m['expiry_date']}\n"
                f"Status: {m['status'].capitalize()}"
            )
        else:
            await update.message.reply_text(f"No member found with number {number}.")
        return

    # Otherwise, treat it as a (partial) name search.
    matches = [m for m in members.values() if query.lower() in m["name"].lower()]
    if not matches:
        await update.message.reply_text(f"No members found matching '{query}'.")
        return

    lines = [f"Found {len(matches)} member(s) matching '{query}':\n"]
    for m in sorted(matches, key=lambda x: x["name"].lower()):
        plan_months = m.get("plan_months", DEFAULT_PLAN_MONTHS)
        lines.append(
            f"- {m['name']} | {m['number']} | Plan: {format_plan(plan_months)} | "
            f"Expiry: {m['expiry_date']} | Status: {m['status'].capitalize()}"
        )
    await update.message.reply_text("\n".join(lines))


async def members_by_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, months: int) -> None:
    if not is_admin(update.effective_user.id):
        return await deny_access(update)

    members = load_members()
    matches = [
        m for m in members.values()
        if m.get("plan_months", DEFAULT_PLAN_MONTHS) == months
    ]

    if not matches:
        await update.message.reply_text(f"No members found on the {format_plan(months)} plan.")
        return

    lines = [f"Members on the {format_plan(months)} plan - Total: {len(matches)}\n"]
    for i, m in enumerate(sorted(matches, key=lambda x: x["name"].lower()), start=1):
        lines.append(f"{i}. {m['name']} | {m['number']} | Expiry: {m['expiry_date']}")
    await update.message.reply_text("\n".join(lines))


async def plan_1m(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await members_by_plan(update, context, 1)


async def plan_3m(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await members_by_plan(update, context, 3)


async def plan_6m(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await members_by_plan(update, context, 6)


async def plan_1y(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await members_by_plan(update, context, 12)


# --------------------------------------------------------------------------- #
# ADMIN PERMISSIONS
# --------------------------------------------------------------------------- #

async def grant_permission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny_access(update)

    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "Usage: /permission <numeric_telegram_id_or_@username>\n"
            "Example: /permission 7992357603\n"
            "Example: /permission @someusername\n\n"
            "Tip: the numeric ID (from @userinfobot) is more reliable than a username."
        )
        return

    target = args[0].strip()

    if target.startswith("@"):
        try:
            chat = await context.bot.get_chat(target)
            new_id = chat.id
        except Exception:
            await update.message.reply_text(
                f"Could not resolve username {target}. This can happen if that person has "
                "never messaged this bot, or has no public username. Please ask them to "
                "message @userinfobot on Telegram and send you their numeric ID instead, "
                "then use /permission with that number."
            )
            return
    else:
        try:
            new_id = int(target)
        except ValueError:
            await update.message.reply_text(
                "Please provide a valid numeric Telegram ID or a @username."
            )
            return

    if new_id in ADMIN_IDS:
        await update.message.reply_text("This person already has admin access.")
        return

    ADMIN_IDS.add(new_id)
    extra_admins = load_extra_admins()
    if new_id not in extra_admins:
        extra_admins.append(new_id)
        save_extra_admins(extra_admins)

    await update.message.reply_text(
        f"Admin access granted successfully to ID: {new_id}\n"
        "They can now use all admin commands on this bot."
    )


# --------------------------------------------------------------------------- #
# BACKUP / RESTORE (important on platforms like Railway with ephemeral disk)
# --------------------------------------------------------------------------- #

async def backup_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return await deny_access(update)

    if not DATA_FILE.exists():
        await update.message.reply_text("No data to back up yet. Add a member first.")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    await update.message.reply_document(
        document=open(DATA_FILE, "rb"),
        filename=f"members_backup_{timestamp}.json",
        caption=(
            "Here is your current member data backup.\n"
            "Save this file safely. Use /restore to load it back into the bot later."
        ),
    )


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
        # Ignore random documents sent without /restore being requested first.
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
            "members.json backup and try /restore again."
        )
        return

    save_members(restored_data)
    AWAITING_RESTORE.discard(user_id)

    await update.message.reply_text(
        f"Data restored successfully. {len(restored_data)} member record(s) loaded."
    )


# --------------------------------------------------------------------------- #
# DAILY EXPIRY CHECK (runs automatically, notifies admins/owners)
# --------------------------------------------------------------------------- #

async def check_expiries(context: ContextTypes.DEFAULT_TYPE) -> None:
    members = load_members()
    today = date.today()
    changed = False

    for member in members.values():
        expiry = datetime.strptime(member["expiry_date"], DATE_FMT).date()
        if expiry <= today and member["status"] != "expired":
            member["status"] = "expired"
            changed = True

            message = (
                f"{member['name']} ({member['number']}) - "
                f"Subscription expired on {member['expiry_date']}"
            )
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(chat_id=admin_id, text=message)
                except Exception as e:
                    logger.error("Failed to notify admin %s: %s", admin_id, e)

    if changed:
        save_members(members)


# --------------------------------------------------------------------------- #
# APPLICATION ENTRY POINT
# --------------------------------------------------------------------------- #

def main() -> None:
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE" or not BOT_TOKEN:
        raise SystemExit(
            "Bot token not configured. Set GYM_BOT_TOKEN env variable or edit BOT_TOKEN in the script."
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
    application.add_handler(CommandHandler("find", find_member))
    application.add_handler(CommandHandler("1m", plan_1m))
    application.add_handler(CommandHandler("3m", plan_3m))
    application.add_handler(CommandHandler("6m", plan_6m))
    application.add_handler(CommandHandler("1y", plan_1y))
    application.add_handler(CommandHandler("permission", grant_permission))
    application.add_handler(CommandHandler("backup", backup_data))
    application.add_handler(CommandHandler("restore", restore_data))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Check expiries once at startup, then every 24 hours.
    application.job_queue.run_repeating(check_expiries, interval=24 * 60 * 60, first=10)

    logger.info("Gym bot started. Polling for updates...")
    application.run_polling()


if __name__ == "__main__":
    main()
