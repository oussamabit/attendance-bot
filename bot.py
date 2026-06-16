import os
import json
import logging
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.request import HTTPXRequest
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG — loaded from environment variables (set these in Railway dashboard)
# ─────────────────────────────────────────────────────────────────────────────
TOKEN          = os.environ["BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
ADMIN_IDS      = [int(x) for x in os.environ["ADMIN_IDS"].split(",")]
GOOGLE_CREDS   = json.loads(os.environ["GOOGLE_CREDS_JSON"])
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)

# ── Google Sheets helpers ─────────────────────────────────────────────────────

def get_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds  = ServiceAccountCredentials.from_json_keyfile_dict(GOOGLE_CREDS, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


def get_sections() -> dict:
    ws   = get_sheet().worksheet("Students")
    rows = ws.get_all_values()[1:]
    sections = {}
    for row in rows:
        if len(row) < 2:
            continue
        section, student = row[0].strip(), row[1].strip()
        if section and student:
            sections.setdefault(section, []).append(student)
    return sections


def save_attendance(teacher_name: str, section: str, absents: list):
    ws    = get_sheet().worksheet("Attendance")
    today = str(date.today())
    if absents:
        rows = [[today, teacher_name, section, student, "غائب"] for student in absents]
        ws.append_rows(rows)
    else:
        ws.append_row([today, teacher_name, section, "—", "الكل حاضر"])


def get_today_report() -> str:
    ws    = get_sheet().worksheet("Attendance")
    rows  = ws.get_all_values()[1:]
    today = str(date.today())

    today_rows = [r for r in rows if r and r[0] == today]
    if not today_rows:
        return "📭 لم يتم تسجيل أي غياب اليوم بعد."

    grouped = {}
    for r in today_rows:
        if len(r) < 5:
            continue
        _, teacher, section, student, status = r[:5]
        grouped.setdefault(teacher, {}).setdefault(section, []).append((student, status))

    lines = [f"📋 *تقرير الغياب – {today}*\n"]
    for teacher, secs in grouped.items():
        lines.append(f"👤 *الأستاذ: {teacher}*")
        for sec, entries in secs.items():
            lines.append(f"  📚 القسم: {sec}")
            for student, status in entries:
                icon = "🔴" if status == "غائب" else "✅"
                lines.append(f"    {icon} {student}")
        lines.append("")

    return "\n".join(lines)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"👋 مرحباً، {user.first_name}!\n\n"
        "📌 الأوامر المتاحة:\n"
        "  /sections — تسجيل الغياب\n"
        "  /report   — تقرير اليوم (للمسؤول فقط)"
    )


async def sections_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ جاري تحميل الأقسام…")
    try:
        sections = get_sections()
    except Exception as e:
        await msg.edit_text(f"❌ تعذّر تحميل الأقسام:\n{e}")
        return

    if not sections:
        await msg.edit_text("⚠️ لا توجد أقسام في الجدول.")
        return

    keyboard = [
        [InlineKeyboardButton(sec, callback_data=f"section|{sec}")]
        for sec in sections
    ]
    await msg.edit_text(
        "📚 اختر القسم:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول فقط.")
        return
    msg = await update.message.reply_text("⏳ جاري تحميل التقرير…")
    try:
        report = get_today_report()
    except Exception as e:
        await msg.edit_text(f"❌ خطأ في تحميل التقرير:\n{e}")
        return
    await msg.edit_text(report, parse_mode="Markdown")


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── Section chosen ────────────────────────────────────────────────────────
    if data.startswith("section|"):
        section = data.split("|", 1)[1]
        context.user_data["section"] = section
        context.user_data["absents"] = []

        try:
            sections = get_sections()
        except Exception as e:
            await query.edit_message_text(f"❌ تعذّر تحميل الطلاب:\n{e}")
            return

        students = sections.get(section, [])
        if not students:
            await query.edit_message_text(f"⚠️ لا يوجد طلاب في القسم {section}.")
            return

        context.user_data["all_students"] = students

        keyboard = [
            [InlineKeyboardButton(f"☑ {s}", callback_data=f"toggle|{s}")]
            for s in students
        ]
        keyboard.append([InlineKeyboardButton("📤 إرسال التقرير", callback_data="submit")])

        await query.edit_message_text(
            f"📌 *{section}*\nاضغط على اسم الطالب لتسجيله غائباً، واضغط مجدداً للإلغاء:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    # ── Toggle absent/present ─────────────────────────────────────────────────
    elif data.startswith("toggle|"):
        student = data.split("|", 1)[1]
        absents = context.user_data.get("absents", [])
        section = context.user_data.get("section", "")

        if student in absents:
            absents.remove(student)
        else:
            absents.append(student)
        context.user_data["absents"] = absents

        all_students = context.user_data.get("all_students", [])
        if not all_students:
            try:
                sections = get_sections()
                all_students = sections.get(section, [])
                context.user_data["all_students"] = all_students
            except Exception:
                return

        keyboard = []
        for s in all_students:
            label = f"🔴 {s} (غائب)" if s in absents else f"☑ {s}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"toggle|{s}")])
        keyboard.append([InlineKeyboardButton("📤 إرسال التقرير", callback_data="submit")])

        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ── Submit ────────────────────────────────────────────────────────────────
    elif data == "submit":
        section      = context.user_data.get("section", "غير معروف")
        absents      = context.user_data.get("absents", [])
        teacher_name = query.from_user.full_name

        await query.edit_message_text("⏳ جاري حفظ الغياب…")

        try:
            save_attendance(teacher_name, section, absents)
        except Exception as e:
            await query.edit_message_text(f"❌ فشل الحفظ:\n{e}")
            return

        if absents:
            absent_list = "\n".join(f"  🔴 {s}" for s in absents)
            text = (
                f"✅ *تم إرسال التقرير!*\n\n"
                f"📚 القسم: *{section}*\n"
                f"👤 الأستاذ: {teacher_name}\n\n"
                f"الغائبون:\n{absent_list}"
            )
        else:
            text = (
                f"✅ *تم إرسال التقرير!*\n\n"
                f"📚 القسم: *{section}*\n"
                f"👤 الأستاذ: {teacher_name}\n"
                f"🎉 جميع الطلاب حاضرون"
            )

        await query.edit_message_text(text, parse_mode="Markdown")

        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"📬 *تقرير جديد من {teacher_name}*\n\n{text}",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        context.user_data.clear()


# ── Run ───────────────────────────────────────────────────────────────────────

request = HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30)

app = ApplicationBuilder().token(TOKEN).request(request).build()
app.add_handler(CommandHandler("start",    start))
app.add_handler(CommandHandler("sections", sections_cmd))
app.add_handler(CommandHandler("report",   report_cmd))
app.add_handler(CallbackQueryHandler(handle_buttons))

print("✅ البوت يعمل…")
app.run_polling(drop_pending_updates=True)