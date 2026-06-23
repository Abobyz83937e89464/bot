import json
import os
import threading
import time
from pathlib import Path

import requests
from flask import Flask, jsonify
from telebot import TeleBot
from telebot.apihelper import ApiTelegramException

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
STATE_FILE = Path("state.json")
SELF_PING_INTERVAL = 14 * 60  # 14 minutes

INTRO_TEXT = (
    "Приветствую. Этот бот предназначен для отправки файлов на фикс. "
    "Вы можете прислать файл, и мы рассмотрим его для публикации в нашем Telegram-канале: "
    "https://t.me/+jjoQNSQKpZ02OGYy"
)

FILE_TYPES = [
    "document",
    "photo",
    "video",
    "audio",
    "voice",
    "animation",
    "video_note",
    "sticker",
]

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is required")

app = Flask(__name__)
bot = TeleBot(BOT_TOKEN, threaded=True)

state_lock = threading.Lock()
state = {
    "groups": [],
    "links": {}
}

def load_state():
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        groups = data.get("groups", [])
        links = data.get("links", {})
        if isinstance(groups, list):
            state["groups"] = list(dict.fromkeys(int(x) for x in groups))
        if isinstance(links, dict):
            state["links"] = {str(k): int(v) for k, v in links.items()}
    except Exception as e:
        print("Failed to load state:", e)

def save_state():
    tmp = STATE_FILE.with_suffix(".tmp")
    payload = {
        "groups": state["groups"],
        "links": state["links"],
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)

def add_group(chat_id: int):
    with state_lock:
        if chat_id not in state["groups"]:
            state["groups"].append(chat_id)
            save_state()

def add_link(group_chat_id: int, group_message_id: int, user_chat_id: int):
    key = f"{group_chat_id}:{group_message_id}"
    with state_lock:
        state["links"][key] = user_chat_id
        save_state()

def get_user_for_group_reply(group_chat_id: int, replied_message_id: int):
    key = f"{group_chat_id}:{replied_message_id}"
    with state_lock:
        return state["links"].get(key)

def is_private(message):
    return message.chat.type == "private"

def is_group(message):
    return message.chat.type in ("group", "supergroup")

def is_file_message(message):
    return any(getattr(message, attr, None) for attr in FILE_TYPES)

@app.get("/health")
def health():
    return jsonify(ok=True)

@bot.message_handler(commands=["start"])
def start_handler(message):
    if not is_private(message):
        return
    bot.send_message(
        message.chat.id,
        INTRO_TEXT,
        disable_web_page_preview=True
    )

@bot.message_handler(content_types=FILE_TYPES)
def private_file_handler(message):
    if not is_private(message):
        return

    groups = list(state["groups"])
    if not groups:
        bot.send_message(
            message.chat.id,
            "Пока я не знаю ни одной группы. Добавь меня в группу, потом отправь файл ещё раз."
        )
        return

    sent_count = 0
    for group_id in groups:
        try:
            forwarded = bot.forward_message(
                chat_id=group_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            add_link(group_id, forwarded.message_id, message.chat.id)
            sent_count += 1
        except ApiTelegramException as e:
            print(f"Forward failed to {group_id}:", e)
        except Exception as e:
            print(f"Unexpected forward error to {group_id}:", e)

    bot.send_message(
        message.chat.id,
        f"Файл отправлен в группу. Успешно: {sent_count}/{len(groups)}"
    )

@bot.message_handler(content_types=["new_chat_members"])
def new_chat_members_handler(message):
    if not is_group(message):
        return

    add_group(message.chat.id)

    for member in message.new_chat_members:
        if member.id == bot.get_me().id:
            try:
                bot.send_message(message.chat.id, "Бот подключён и готов.")
            except Exception:
                pass
            break

@bot.message_handler(func=lambda m: m.chat.type in ("group", "supergroup"), content_types=None)
def group_reply_handler(message):
    if not is_group(message):
        return

    add_group(message.chat.id)

    if not message.reply_to_message:
        return

    user_chat_id = get_user_for_group_reply(message.chat.id, message.reply_to_message.message_id)
    if not user_chat_id:
        return

    try:
        bot.copy_message(
            chat_id=user_chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
    except Exception as e:
        print("Reply copy failed:", e)
        try:
            bot.send_message(user_chat_id, "Пришёл ответ из группы, но переслать его автоматически не удалось.")
        except Exception:
            pass

def bot_loop():
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
        except Exception as e:
            print("Polling crashed:", e)
            time.sleep(5)

def self_ping_loop():
    base_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("BASE_URL")
    if not base_url:
        print("No BASE_URL / RENDER_EXTERNAL_URL, self-ping disabled.")
        return

    health_url = base_url.rstrip("/") + "/health"
    time.sleep(20)

    while True:
        try:
            r = requests.get(health_url, timeout=20)
            print("Ping", r.status_code, health_url)
        except Exception as e:
            print("Ping failed:", e)
        time.sleep(SELF_PING_INTERVAL)

if __name__ == "__main__":
    load_state()

    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=self_ping_loop, daemon=True).start()

    app.run(host="0.0.0.0", port=PORT)
