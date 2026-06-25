import json
import os
import threading
import time
from pathlib import Path

import requests
from flask import Flask, jsonify
from telebot import TeleBot, types
from telebot.apihelper import ApiTelegramException

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
STATE_FILE = Path("state.json")
SELF_PING_INTERVAL = 14 * 60

INTRO_TEXT = (
    "Приветствую. Этот бот предназначен для отправки файлов на фикс и подачи заявления на тестера.\n\n"
    "Выберите действие кнопкой ниже."
)

FILE_TYPES = ["document", "photo", "video", "audio", "voice", "animation", "video_note", "sticker"]
MENU_TESTER = "Подача заявления на тестера"
MENU_FIX = "Отправить файл на фикс"

DEFAULT_STATE = {
    "groups": [],
    "file_links": {},
    "applications": {},
    "sessions": {},
}

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is required")

app = Flask(__name__)
bot = TeleBot(BOT_TOKEN, threaded=True)
state_lock = threading.RLock()
state = json.loads(json.dumps(DEFAULT_STATE))


def normalize_state(data):
    result = json.loads(json.dumps(DEFAULT_STATE))
    if not isinstance(data, dict):
        return result

    groups = data.get("groups")
    if isinstance(groups, list):
        seen = set()
        for item in groups:
            try:
                gid = int(item)
            except Exception:
                continue
            if gid not in seen:
                seen.add(gid)
                result["groups"].append(gid)

    file_links = data.get("file_links")
    if isinstance(file_links, dict):
        result["file_links"] = {
            str(k): int(v) for k, v in file_links.items() if str(v).lstrip("-").isdigit()
        }
    else:
        legacy_links = data.get("links")
        if isinstance(legacy_links, dict):
            result["file_links"] = {
                str(k): int(v) for k, v in legacy_links.items() if str(v).lstrip("-").isdigit()
            }

    applications = data.get("applications")
    if isinstance(applications, dict):
        result["applications"] = applications

    sessions = data.get("sessions")
    if isinstance(sessions, dict):
        result["sessions"] = sessions

    return result


def load_state():
    global state
    if not STATE_FILE.exists():
        return
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state = normalize_state(raw)
    except Exception as exc:
        print("Failed to load state:", exc)


def save_state():
    with state_lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)


def ensure_group(chat_id: int):
    with state_lock:
        if chat_id not in state["groups"]:
            state["groups"].append(chat_id)
            save_state()


def session_key(user_id: int) -> str:
    return str(user_id)


def get_session(user_id: int):
    return state["sessions"].get(session_key(user_id))


def set_session(user_id: int, session: dict | None):
    with state_lock:
        key = session_key(user_id)
        if session is None:
            state["sessions"].pop(key, None)
        else:
            state["sessions"][key] = session
        save_state()


def display_user(message):
    user = message.from_user
    username = f"@{user.username}" if user.username else "нет username"
    full_name = " ".join(part for part in [user.first_name, user.last_name] if part)
    if not full_name:
        full_name = "без имени"
    return username, full_name


def main_menu_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton(MENU_TESTER))
    kb.row(types.KeyboardButton(MENU_FIX))
    return kb


def application_keyboard(app_id: str):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("Принять", callback_data=f"app:accept:{app_id}"),
        types.InlineKeyboardButton("Отклонить", callback_data=f"app:reject:{app_id}"),
    )
    return kb


def render_application_text(app_data: dict, final_decision: str | None = None):
    username = app_data.get("username") or "нет username"
    full_name = app_data.get("full_name") or "без имени"
    phone = app_data.get("phone", "не указано")
    cpu = app_data.get("cpu", "не указано")
    pc = app_data.get("pc", "не указано")

    lines = [
        "Новое заявление на тестера",
        "",
        f"Юзер: {username}",
        f"Ник: {full_name}",
        f"ID: {app_data.get('user_id')}",
        "",
        f"Какой у вас телефон: {phone}",
        f"Какой у вас процессор: {cpu}",
        f"Имеете ли вы ПК: {pc}",
    ]

    if final_decision:
        lines += ["", f"Решение: {final_decision}"]

    return "\n".join(lines)


def render_application_review_text(app_data: dict):
    decision = app_data.get("status")
    decision_map = {"accepted": "Принято", "rejected": "Отклонено"}
    return render_application_text(app_data, decision_map.get(decision))


def reply_to_group_forward(message):
    if message.chat.type not in ("group", "supergroup"):
        return False
    if not message.reply_to_message:
        return False

    key = f"{message.chat.id}:{message.reply_to_message.message_id}"
    user_chat_id = state["file_links"].get(key)
    if not user_chat_id:
        return False

    try:
        bot.copy_message(user_chat_id, message.chat.id, message.message_id)
        return True
    except Exception as exc:
        print("Failed to relay reply to user:", exc)
        try:
            bot.send_message(
                user_chat_id,
                "Пришёл ответ из группы, но переслать его автоматически не удалось.",
            )
        except Exception:
            pass
        return True


def start_tester_flow(user_id: int):
    set_session(
        user_id,
        {
            "mode": "tester",
            "step": 1,
            "answers": {},
        },
    )


def start_fix_flow(user_id: int):
    set_session(user_id, {"mode": "fix_file"})


def finish_tester_application(message, answers):
    user = message.from_user
    username, full_name = display_user(message)
    app_id = f"{user.id}_{int(time.time())}"

    app_data = {
        "app_id": app_id,
        "user_id": user.id,
        "username": username,
        "full_name": full_name,
        "phone": answers.get("phone", ""),
        "cpu": answers.get("cpu", ""),
        "pc": answers.get("pc", ""),
        "status": "pending",
        "sent_messages": [],
        "created_at": int(time.time()),
    }

    with state_lock:
        state["applications"][app_id] = app_data
        save_state()

    sent_any = False
    groups = list(state["groups"])
    for group_id in groups:
        try:
            sent = bot.send_message(
                group_id,
                render_application_text(app_data),
                reply_markup=application_keyboard(app_id),
                disable_web_page_preview=True,
            )
            app_data["sent_messages"].append(
                {"chat_id": group_id, "message_id": sent.message_id}
            )
            sent_any = True
        except ApiTelegramException as exc:
            print(f"Failed to send application to group {group_id}:", exc)
        except Exception as exc:
            print(f"Unexpected error while sending application to {group_id}:", exc)

    with state_lock:
        state["applications"][app_id] = app_data
        save_state()

    set_session(user.id, None)

    if sent_any:
        bot.send_message(
            message.chat.id,
            "Спасибо. Ваш запрос отправлен на рассмотрение. Вы будете уведомлены в боте, когда вас одобрят или отклонят.",
            reply_markup=main_menu_keyboard(),
        )
    else:
        bot.send_message(
            message.chat.id,
            "Спасибо. Заявление сохранено, но бот пока не находится ни в одной группе. Как только он будет добавлен в группу, заявление можно будет отправить повторно.",
            reply_markup=main_menu_keyboard(),
        )


def handle_tester_step(message, session):
    answers = session.get("answers", {})
    step = int(session.get("step", 1))
    text = (message.text or "").strip()

    if step == 1:
        answers["phone"] = text
        session["step"] = 2
        session["answers"] = answers
        set_session(message.from_user.id, session)
        bot.send_message(message.chat.id, "Ответ сохранен. Какой у вас процессор?")
        return

    if step == 2:
        answers["cpu"] = text
        session["step"] = 3
        session["answers"] = answers
        set_session(message.from_user.id, session)
        bot.send_message(message.chat.id, "Ответ сохранен. Имеете ли вы ПК?")
        return

    if step == 3:
        answers["pc"] = text
        finish_tester_application(message, answers)
        return

    set_session(message.from_user.id, None)
    bot.send_message(
        message.chat.id,
        "Сессия сброшена. Выберите действие кнопкой ниже.",
        reply_markup=main_menu_keyboard(),
    )


@app.get("/")
def home():
    return "OK"


@app.get("/health")
def health():
    return jsonify(ok=True)


@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    if message.chat.type != "private":
        return
    bot.send_message(
        message.chat.id,
        INTRO_TEXT,
        reply_markup=main_menu_keyboard(),
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    if message.chat.type != "private":
        return
    set_session(message.from_user.id, None)
    bot.send_message(message.chat.id, "Сессия отменена.", reply_markup=main_menu_keyboard())


@bot.message_handler(content_types=["new_chat_members"])
def on_new_chat_members(message):
    if message.chat.type not in ("group", "supergroup"):
        return
    ensure_group(message.chat.id)
    try:
        me = bot.get_me()
        if any(member.id == me.id for member in message.new_chat_members):
            bot.send_message(message.chat.id, "Бот подключён и готов.")
    except Exception:
        pass


@bot.message_handler(content_types=FILE_TYPES)
def on_file(message):
    if message.chat.type in ("group", "supergroup"):
        ensure_group(message.chat.id)
        if message.reply_to_message:
            reply_to_group_forward(message)
        return

    if message.chat.type != "private":
        return

    session = get_session(message.from_user.id)
    if session and session.get("mode") == "tester":
        bot.send_message(
            message.chat.id,
            "Для заявления нужны только текстовые ответы. Ответьте на вопрос текстом.",
        )
        return

    if session and session.get("mode") == "fix_file":
        set_session(message.from_user.id, None)

    groups = list(state["groups"])
    if not groups:
        bot.send_message(
            message.chat.id,
            "Пока я не добавлен ни в одну группу. Сначала добавьте меня в группу.",
        )
        return

    sent_count = 0
    for group_id in groups:
        try:
            forwarded = bot.forward_message(
                group_id, message.chat.id, message.message_id
            )
            key = f"{group_id}:{forwarded.message_id}"
            with state_lock:
                state["file_links"][key] = message.chat.id
                save_state()
            sent_count += 1
        except Exception as exc:
            print(f"Forward failed to {group_id}:", exc)

    bot.send_message(
        message.chat.id,
        f"Файл отправлен в группу. Успешно: {sent_count}/{len(groups)}",
        reply_markup=main_menu_keyboard(),
    )


@bot.message_handler(content_types=["text"])
def on_text(message):
    text = (message.text or "").strip()

    if message.chat.type in ("group", "supergroup"):
        ensure_group(message.chat.id)
        reply_to_group_forward(message)
        return

    if message.chat.type != "private":
        return

    if text in ("/start", "/menu"):
        cmd_start(message)
        return

    if text == "/cancel":
        cmd_cancel(message)
        return

    if text == MENU_TESTER:
        start_tester_flow(message.from_user.id)
        bot.send_message(message.chat.id, "Какой у вас телефон?")
        return

    if text == MENU_FIX:
        start_fix_flow(message.from_user.id)
        bot.send_message(message.chat.id, "Пришлите файл одним сообщением.")
        return

    session = get_session(message.from_user.id)
    if session:
        if session.get("mode") == "tester":
            handle_tester_step(message, session)
            return
        if session.get("mode") == "fix_file":
            bot.send_message(message.chat.id, "Нужно прислать файл, а не текст.")
            return

    bot.send_message(
        message.chat.id,
        "Выберите действие кнопкой ниже.",
        reply_markup=main_menu_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: isinstance(call.data, str) and call.data.startswith("app:"))
def on_application_callback(call):
    try:
        _, action, app_id = call.data.split(":", 2)
    except ValueError:
        bot.answer_callback_query(call.id, "Некорректная кнопка.")
        return

    with state_lock:
        app_data = state["applications"].get(app_id)

    if not app_data:
        bot.answer_callback_query(call.id, "Заявка не найдена.")
        return

    if app_data.get("status") != "pending":
        current = {
            "accepted": "уже принята",
            "rejected": "уже отклонена",
        }.get(app_data.get("status"), "уже обработана")
        bot.answer_callback_query(call.id, f"Заявка {current}.")
        return

    if action == "accept":
        decision_text = "Принято"
        new_status = "accepted"
        user_notice = "Ваше заявление на тестера одобрено."
    elif action == "reject":
        decision_text = "Отклонено"
        new_status = "rejected"
        user_notice = "Ваше заявление на тестера отклонено."
    else:
        bot.answer_callback_query(call.id, "Некорректное действие.")
        return

    with state_lock:
        app_data["status"] = new_status
        app_data["decision"] = decision_text
        state["applications"][app_id] = app_data
        save_state()

    try:
        bot.send_message(app_data["user_id"], user_notice, reply_markup=main_menu_keyboard())
    except Exception as exc:
        print("Failed to notify user:", exc)

    review_text = render_application_review_text(app_data)

    for item in app_data.get("sent_messages", []):
        try:
            bot.edit_message_text(
                review_text,
                chat_id=item["chat_id"],
                message_id=item["message_id"],
                reply_markup=None,
            )
        except Exception as exc:
            print("Failed to edit application message:", exc)

    bot.answer_callback_query(call.id, f"Заявка {decision_text.lower()}.")


def bot_loop():
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
        except Exception as exc:
            print("Polling crashed:", exc)
            time.sleep(5)


def self_ping_loop():
    base_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("BASE_URL")
    if not base_url:
        print("Self-ping disabled: no RENDER_EXTERNAL_URL / BASE_URL")
        return

    health_url = base_url.rstrip("/") + "/health"
    time.sleep(20)

    while True:
        try:
            r = requests.get(health_url, timeout=20)
            print("Ping", r.status_code, health_url)
        except Exception as exc:
            print("Ping failed:", exc)
        time.sleep(SELF_PING_INTERVAL)


if __name__ == "__main__":
    load_state()
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
