import json
import os
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def load_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


ENV = load_env(BASE_DIR / ".env")


def env_value(key: str, default: str = "") -> str:
    return os.environ.get(key, ENV.get(key, default))


def parse_int_list(value: str) -> list[int]:
    result: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if item:
            result.append(int(item))
    return result


def parse_str_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


BOT_TOKEN = env_value("BOT_TOKEN")
OWNER_CHAT_ID = int(env_value("OWNER_CHAT_ID", "0"))
SUPER_ADMINS = set(parse_int_list(env_value("SUPER_ADMINS", "")))
REQUIRED_CHANNELS = parse_str_list(env_value("REQUIRED_CHANNELS", ""))
CHANNEL_LINKS = parse_str_list(env_value("CHANNEL_LINKS", ""))
FUTURE_CHANNEL_BUTTONS = parse_str_list(env_value("FUTURE_CHANNEL_BUTTONS", ""))
DB_PATH = BASE_DIR / env_value("DB_PATH", "bot.sqlite3")
POLL_TIMEOUT = int(env_value("POLL_TIMEOUT", "35"))
PORT = int(env_value("PORT", "10000"))
GITHUB_DATA_TOKEN = env_value("GITHUB_DATA_TOKEN", "")
GITHUB_DATA_REPO = env_value("GITHUB_DATA_REPO", "")
GITHUB_DATA_FILE = env_value("GITHUB_DATA_FILE", "users.json")
BLOB_READ_WRITE_TOKEN = env_value("BLOB_READ_WRITE_TOKEN", "")
BLOB_USERS_FILE = env_value("BLOB_USERS_FILE", "users.json")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is missing in .env")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"
SUBSCRIBE_TEXT = "Сначало подпишитесь на наши канали потом можно пользоватся ботом !"


@dataclass
class UserInfo:
    chat_id: int
    username: str
    first_name: str
    last_name: str
    phone: str

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        if self.phone:
            return self.phone
        full_name = " ".join(part for part in [self.first_name, self.last_name] if part)
        if full_name:
            return f"{full_name} ({self.chat_id})"
        return str(self.chat_id)


class BotApiError(RuntimeError):
    pass


class TelegramApi:
    def request(self, method: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            API_URL + method,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=POLL_TIMEOUT + 10) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise BotApiError(f"{method}: HTTP {exc.code}: {details}") from exc
        except urllib.error.URLError as exc:
            raise BotApiError(f"{method}: {exc}") from exc

        if not body.get("ok"):
            raise BotApiError(f"{method}: {body}")
        return body["result"]

    def get_updates(self, offset: int | None) -> list[dict]:
        payload: dict = {
            "timeout": POLL_TIMEOUT,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.request("getUpdates", payload)

    def send_message(self, chat_id: int, text: str, **extra) -> dict:
        payload = {"chat_id": chat_id, "text": text, **extra}
        return self.request("sendMessage", payload)

    def copy_message(self, chat_id: int, from_chat_id: int, message_id: int, **extra) -> dict:
        payload = {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
            **extra,
        }
        return self.request("copyMessage", payload)

    def answer_callback_query(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> None:
        self.request(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert},
        )

    def get_chat_member(self, chat_id: int, user_id: int) -> dict:
        return self.request("getChatMember", {"chat_id": chat_id, "user_id": user_id})


api = TelegramApi()


class GithubUserStore:
    def __init__(self, token: str, repo: str, file_path: str):
        self.enabled = bool(token and repo and file_path)
        self.token = token
        self.repo = repo
        self.file_path = file_path
        self.api_url = f"https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(file_path)}"

    def request(self, method: str, payload: dict | None = None) -> dict | None:
        if not self.enabled:
            return None
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.api_url,
            data=data,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            print(f"GitHub data store error: HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
            return None
        except Exception as exc:
            print(f"GitHub data store error: {exc}")
            return None

    def load(self) -> tuple[dict, str | None]:
        response = self.request("GET")
        if not response:
            return {"users": {}}, None
        try:
            import base64

            raw = base64.b64decode(response.get("content", "")).decode("utf-8")
            data = json.loads(raw) if raw.strip() else {"users": {}}
            if "users" not in data:
                data["users"] = {}
            return data, response.get("sha")
        except Exception as exc:
            print(f"GitHub data parse error: {exc}")
            return {"users": {}}, response.get("sha")

    def save(self, data: dict, sha: str | None) -> None:
        if not self.enabled:
            return
        import base64

        payload = {
            "message": "Update bot users",
            "content": base64.b64encode(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii"),
        }
        if sha:
            payload["sha"] = sha
        self.request("PUT", payload)

    def upsert_user(self, user: UserInfo, started: bool = False) -> None:
        if not self.enabled:
            return
        data, sha = self.load()
        users = data.setdefault("users", {})
        current = users.get(str(user.chat_id), {})
        users[str(user.chat_id)] = {
            **current,
            "chat_id": user.chat_id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone or current.get("phone", ""),
            "started": bool(started or current.get("started")),
            "last_seen": int(time.time()),
        }
        self.save(data, sha)

    def all_user_ids(self) -> list[int]:
        if not self.enabled:
            return []
        data, _ = self.load()
        result: list[int] = []
        for chat_id, item in data.get("users", {}).items():
            try:
                value = int(item.get("chat_id", chat_id) if isinstance(item, dict) else chat_id)
            except (TypeError, ValueError):
                continue
            if value not in SUPER_ADMINS:
                result.append(value)
        return sorted(set(result))


github_store = GithubUserStore(GITHUB_DATA_TOKEN, GITHUB_DATA_REPO, GITHUB_DATA_FILE)


class BlobUserStore:
    def __init__(self, token: str, file_path: str):
        self.enabled = bool(token and file_path)
        self.token = token
        self.file_path = file_path

    def client(self):
        from vercel.blob import BlobClient

        return BlobClient(token=self.token)

    def load(self) -> dict:
        if not self.enabled:
            return {"users": {}}
        try:
            result = self.client().get(self.file_path, access="private")
            if result is None or result.status_code != 200 or result.stream is None:
                return {"users": {}}
            raw = b"".join(result.stream).decode("utf-8")
            data = json.loads(raw) if raw.strip() else {"users": {}}
            if "users" not in data:
                data["users"] = {}
            return data
        except Exception as exc:
            message = str(exc).lower()
            if "not found" not in message and "blobnotfound" not in message:
                print(f"Vercel Blob load error: {exc}")
            return {"users": {}}

    def save(self, data: dict) -> None:
        if not self.enabled:
            return
        try:
            self.client().put(
                self.file_path,
                json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                access="private",
                content_type="application/json",
                add_random_suffix=False,
                overwrite=True,
            )
        except Exception as exc:
            print(f"Vercel Blob save error: {exc}")

    def upsert_user(self, user: UserInfo, started: bool = False) -> None:
        if not self.enabled:
            return
        data = self.load()
        users = data.setdefault("users", {})
        current = users.get(str(user.chat_id), {})
        users[str(user.chat_id)] = {
            **current,
            "chat_id": user.chat_id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone or current.get("phone", ""),
            "started": bool(started or current.get("started")),
            "last_seen": int(time.time()),
        }
        self.save(data)

    def all_user_ids(self) -> list[int]:
        if not self.enabled:
            return []
        data = self.load()
        result: list[int] = []
        for chat_id, item in data.get("users", {}).items():
            try:
                value = int(item.get("chat_id", chat_id) if isinstance(item, dict) else chat_id)
            except (TypeError, ValueError):
                continue
            if value not in SUPER_ADMINS:
                result.append(value)
        return sorted(set(result))


blob_store = BlobUserStore(BLOB_READ_WRITE_TOKEN, BLOB_USERS_FILE)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


def start_health_server() -> None:
    def run() -> None:
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        server.serve_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()


def start_keep_alive() -> None:
    host = env_value("RENDER_EXTERNAL_HOSTNAME", "")
    if not host:
        return
    url = f"https://{host}/"

    def run() -> None:
        while True:
            time.sleep(600)
            try:
                urllib.request.urlopen(url, timeout=20).read()
            except Exception as exc:
                print(f"Keep-alive ping failed: {exc}")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init()

    def init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                started INTEGER NOT NULL DEFAULT 0,
                messages_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS admins (
                chat_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS blocked_users (
                chat_id INTEGER PRIMARY KEY,
                reason TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS owner_messages (
                owner_chat_id INTEGER NOT NULL,
                owner_message_id INTEGER NOT NULL,
                user_chat_id INTEGER NOT NULL,
                user_message_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (owner_chat_id, owner_message_id)
            );

            CREATE TABLE IF NOT EXISTS admin_states (
                chat_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                payload TEXT,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS spam_events (
                chat_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('antispam_enabled', '1')"
        )
        self.conn.commit()

    def upsert_user(self, user: UserInfo, started: bool = False, count_message: bool = False) -> None:
        now = int(time.time())
        current = self.conn.execute(
            "SELECT chat_id, phone, started, messages_count FROM users WHERE chat_id = ?",
            (user.chat_id,),
        ).fetchone()
        phone = user.phone or (current["phone"] if current else "")
        started_value = 1 if started else (current["started"] if current else 0)
        message_count = (current["messages_count"] if current else 0) + (1 if count_message else 0)
        self.conn.execute(
            """
            INSERT INTO users(chat_id, username, first_name, last_name, phone, first_seen, last_seen, started, messages_count)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                phone = excluded.phone,
                last_seen = excluded.last_seen,
                started = excluded.started,
                messages_count = excluded.messages_count
            """,
            (
                user.chat_id,
                user.username,
                user.first_name,
                user.last_name,
                phone,
                now,
                now,
                started_value,
                message_count,
            ),
        )
        self.conn.commit()
        blob_store.upsert_user(user, started=started)
        github_store.upsert_user(user, started=started)

    def save_owner_mapping(self, owner_chat_id: int, owner_message_id: int, user_chat_id: int, user_message_id: int) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO owner_messages(owner_chat_id, owner_message_id, user_chat_id, user_message_id, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (owner_chat_id, owner_message_id, user_chat_id, user_message_id, int(time.time())),
        )
        self.conn.commit()

    def get_mapping(self, owner_chat_id: int, owner_message_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM owner_messages WHERE owner_chat_id = ? AND owner_message_id = ?",
            (owner_chat_id, owner_message_id),
        ).fetchone()

    def is_admin(self, chat_id: int) -> bool:
        if chat_id in SUPER_ADMINS:
            return True
        row = self.conn.execute("SELECT 1 FROM admins WHERE chat_id = ?", (chat_id,)).fetchone()
        return row is not None

    def add_admin(self, chat_id: int, added_by: int) -> bool:
        if chat_id in SUPER_ADMINS:
            return False
        self.conn.execute(
            "INSERT OR IGNORE INTO admins(chat_id, added_by, created_at) VALUES(?, ?, ?)",
            (chat_id, added_by, int(time.time())),
        )
        self.conn.commit()
        return True

    def list_admins(self) -> list[int]:
        rows = self.conn.execute("SELECT chat_id FROM admins ORDER BY created_at DESC").fetchall()
        return [int(row["chat_id"]) for row in rows]

    def all_admin_ids(self) -> list[int]:
        return sorted(SUPER_ADMINS) + self.list_admins()

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def block_user(self, chat_id: int, reason: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO blocked_users(chat_id, reason, created_at) VALUES(?, ?, ?)",
            (chat_id, reason, int(time.time())),
        )
        self.conn.commit()

    def unblock_user(self, chat_id: int) -> None:
        self.conn.execute("DELETE FROM blocked_users WHERE chat_id = ?", (chat_id,))
        self.conn.commit()

    def is_blocked(self, chat_id: int) -> bool:
        row = self.conn.execute("SELECT 1 FROM blocked_users WHERE chat_id = ?", (chat_id,)).fetchone()
        return row is not None

    def stats(self) -> dict:
        total_started = self.conn.execute(
            f"SELECT COUNT(*) AS value FROM users WHERE started = 1 AND chat_id NOT IN ({placeholders(SUPER_ADMINS)})",
            tuple(SUPER_ADMINS),
        ).fetchone()["value"] if SUPER_ADMINS else self.conn.execute(
            "SELECT COUNT(*) AS value FROM users WHERE started = 1"
        ).fetchone()["value"]
        total_users = self.conn.execute(
            f"SELECT COUNT(*) AS value FROM users WHERE chat_id NOT IN ({placeholders(SUPER_ADMINS)})",
            tuple(SUPER_ADMINS),
        ).fetchone()["value"] if SUPER_ADMINS else self.conn.execute(
            "SELECT COUNT(*) AS value FROM users"
        ).fetchone()["value"]
        blocked = self.conn.execute("SELECT COUNT(*) AS value FROM blocked_users").fetchone()["value"]
        messages = self.conn.execute(
            f"SELECT COALESCE(SUM(messages_count), 0) AS value FROM users WHERE chat_id NOT IN ({placeholders(SUPER_ADMINS)})",
            tuple(SUPER_ADMINS),
        ).fetchone()["value"] if SUPER_ADMINS else self.conn.execute(
            "SELECT COALESCE(SUM(messages_count), 0) AS value FROM users"
        ).fetchone()["value"]
        recent_rows = self.conn.execute(
            f"""
            SELECT chat_id, username, first_name, last_name, phone, messages_count
            FROM users
            WHERE chat_id NOT IN ({placeholders(SUPER_ADMINS)})
            ORDER BY last_seen DESC
            LIMIT 20
            """,
            tuple(SUPER_ADMINS),
        ).fetchall() if SUPER_ADMINS else self.conn.execute(
            """
            SELECT chat_id, username, first_name, last_name, phone, messages_count
            FROM users
            ORDER BY last_seen DESC
            LIMIT 20
            """
        ).fetchall()
        return {
            "total_started": total_started,
            "total_users": total_users,
            "blocked": blocked,
            "messages": messages,
            "recent": recent_rows,
        }

    def all_user_ids(self) -> list[int]:
        rows = self.conn.execute(
            f"SELECT chat_id FROM users WHERE chat_id NOT IN ({placeholders(SUPER_ADMINS)}) ORDER BY first_seen",
            tuple(SUPER_ADMINS),
        ).fetchall() if SUPER_ADMINS else self.conn.execute("SELECT chat_id FROM users ORDER BY first_seen").fetchall()
        ids = {int(row["chat_id"]) for row in rows}
        ids.update(blob_store.all_user_ids())
        ids.update(github_store.all_user_ids())
        ids.difference_update(SUPER_ADMINS)
        return sorted(ids)

    def set_state(self, chat_id: int, state: str, payload: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO admin_states(chat_id, state, payload, updated_at) VALUES(?, ?, ?, ?)",
            (chat_id, state, payload, int(time.time())),
        )
        self.conn.commit()

    def get_state(self, chat_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM admin_states WHERE chat_id = ?", (chat_id,)).fetchone()

    def clear_state(self, chat_id: int) -> None:
        self.conn.execute("DELETE FROM admin_states WHERE chat_id = ?", (chat_id,))
        self.conn.commit()

    def register_spam_event(self, chat_id: int) -> int:
        now = int(time.time())
        self.conn.execute("INSERT INTO spam_events(chat_id, created_at) VALUES(?, ?)", (chat_id, now))
        self.conn.execute("DELETE FROM spam_events WHERE created_at < ?", (now - 10,))
        self.conn.commit()
        return self.conn.execute(
            "SELECT COUNT(*) AS value FROM spam_events WHERE chat_id = ? AND created_at >= ?",
            (chat_id, now - 10),
        ).fetchone()["value"]


def placeholders(values: set[int]) -> str:
    return ",".join("?" for _ in values) or "NULL"


db = Storage(DB_PATH)


def inline_keyboard(rows: list[list[dict]]) -> str:
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def admin_keyboard() -> str:
    antispam = "Вкл" if db.get_setting("antispam_enabled", "1") == "1" else "Выкл"
    return inline_keyboard(
        [
            [{"text": f"Antispam: {antispam}", "callback_data": "admin:antispam"}],
            [{"text": "Добавить админа", "callback_data": "admin:add_admin"}],
            [{"text": "Статистика", "callback_data": "admin:stats"}],
            [{"text": "Заблокировать", "callback_data": "admin:block"}, {"text": "Разблокировать", "callback_data": "admin:unblock"}],
            [{"text": "Запостить новость", "callback_data": "admin:post"}],
            [{"text": "Рассылка", "callback_data": "admin:broadcast"}],
        ]
    )


def subscribe_keyboard() -> str:
    rows: list[list[dict]] = []
    for idx, channel_id in enumerate(REQUIRED_CHANNELS):
        if idx < len(CHANNEL_LINKS):
            link = CHANNEL_LINKS[idx]
        else:
            link = f"https://t.me/{str(channel_id).lstrip('@')}"
        rows.append([{"text": f"Канал {idx + 1}", "url": link}])
    for button_text in FUTURE_CHANNEL_BUTTONS:
        rows.append([{"text": button_text, "callback_data": "placeholder_channel"}])
    rows.append([{"text": "Проверить подписку", "callback_data": "check_subscription"}])
    return inline_keyboard(rows)


def user_from_message(message: dict) -> UserInfo:
    sender = message.get("from", {})
    contact = message.get("contact", {})
    return UserInfo(
        chat_id=int(sender.get("id", message["chat"]["id"])),
        username=sender.get("username", "") or "",
        first_name=sender.get("first_name", "") or "",
        last_name=sender.get("last_name", "") or "",
        phone=contact.get("phone_number", "") if contact.get("user_id") == sender.get("id") else "",
    )


def is_private_message(message: dict) -> bool:
    return message.get("chat", {}).get("type") == "private"


def check_subscription(user_id: int) -> tuple[bool, str]:
    for channel_id in REQUIRED_CHANNELS:
        try:
            member = api.get_chat_member(channel_id, user_id)
        except Exception as exc:
            return False, f"Не могу проверить канал {channel_id}. Проверьте, что бот добавлен в канал. Ошибка: {exc}"
        status = member.get("status")
        if status in {"left", "kicked"}:
            return False, ""
    return True, ""


def send_subscribe_prompt(chat_id: int, reason: str = "") -> None:
    text = SUBSCRIBE_TEXT
    if reason:
        text += f"\n\n{reason}"
    api.send_message(chat_id, text, reply_markup=subscribe_keyboard())


def send_admin_panel(chat_id: int) -> None:
    api.send_message(chat_id, "Админ панель", reply_markup=admin_keyboard())


def format_user_row(row: sqlite3.Row) -> str:
    if row["username"]:
        name = f"@{row['username']}"
    elif row["phone"]:
        name = row["phone"]
    else:
        full_name = " ".join(part for part in [row["first_name"], row["last_name"]] if part)
        name = f"{full_name} ({row['chat_id']})" if full_name else str(row["chat_id"])
    return f"{name} | id: {row['chat_id']} | сообщений: {row['messages_count']}"


def send_stats(chat_id: int) -> None:
    data = db.stats()
    recent = "\n".join(format_user_row(row) for row in data["recent"]) or "Пока пусто."
    admins = db.list_admins()
    admin_lines = "\n".join(str(admin_id) for admin_id in admins) or "Нет добавленных админов."
    text = (
        "Статистика\n\n"
        f"Запускали /start: {data['total_started']}\n"
        f"Всего пользователей: {data['total_users']}\n"
        f"Сообщений от пользователей: {data['messages']}\n"
        f"Заблокировано: {data['blocked']}\n\n"
        f"Последние пользователи:\n{recent}\n\n"
        f"Добавленные админы:\n{admin_lines}"
    )
    api.send_message(chat_id, text)


def send_to_all_users(source_chat_id: int, message_id: int, repeat: int = 1) -> tuple[int, int]:
    sent = 0
    failed = 0
    recipients = db.all_user_ids()
    if not recipients:
        return sent, failed
    for user_id in recipients:
        if db.is_blocked(user_id):
            continue
        for _ in range(repeat):
            try:
                api.copy_message(user_id, source_chat_id, message_id)
                sent += 1
                time.sleep(0.04)
            except Exception:
                failed += 1
                time.sleep(0.15)
    return sent, failed


def command_name(text: str) -> str:
    first = (text.strip().split() or [""])[0].lower()
    return first.split("@", 1)[0]


def handle_direct_admin_command(message: dict, admin_id: int, text: str, is_super_admin: bool) -> bool:
    command = command_name(text)
    if command not in {"/post", "/broadcast", "/рассылка"}:
        return False

    reply = message.get("reply_to_message")
    if not reply:
        if command == "/post":
            api.send_message(admin_id, "Для поста ответьте командой /post на сообщение, которое надо отправить всем.")
        else:
            api.send_message(admin_id, "Для рассылки ответьте командой /broadcast 5 на сообщение, которое надо отправить всем.")
        return True

    if command == "/post":
        sent, failed = send_to_all_users(admin_id, reply["message_id"], repeat=1)
        api.send_message(admin_id, f"Пост отправлен. Успешно: {sent}, ошибок: {failed}.")
        return True

    parts = text.strip().split()
    if len(parts) < 2:
        api.send_message(admin_id, "Укажите количество повторов. Например: /broadcast 5")
        return True

    try:
        repeat = int(parts[1])
    except ValueError:
        api.send_message(admin_id, "Количество повторов должно быть числом. Например: /broadcast 5")
        return True

    if repeat < 1:
        api.send_message(admin_id, "Количество повторов должно быть 1 или больше.")
        return True

    if not is_super_admin and repeat > 20:
        repeat = 20
        api.send_message(admin_id, "Для обычных админов лимит 20 повторов. Запускаю 20.")

    sent, failed = send_to_all_users(admin_id, reply["message_id"], repeat=repeat)
    api.send_message(admin_id, f"Рассылка завершена. Успешно: {sent}, ошибок: {failed}. Повторов: {repeat}.")
    return True


def forward_user_message(message: dict, user: UserInfo) -> None:
    for admin_id in db.all_admin_ids():
        try:
            copied = api.copy_message(admin_id, user.chat_id, message["message_id"])
            db.save_owner_mapping(admin_id, copied["message_id"], user.chat_id, message["message_id"])
        except Exception as exc:
            print(f"Failed to forward message to admin {admin_id}: {exc}")


def reply_to_user_from_owner(message: dict, admin_id: int) -> bool:
    reply = message.get("reply_to_message")
    if not reply:
        return False
    mapping = db.get_mapping(admin_id, reply["message_id"])
    if not mapping:
        return False
    target_id = int(mapping["user_chat_id"])
    if db.is_blocked(target_id):
        api.send_message(admin_id, "Пользователь сейчас в блоке, ответ не отправлен.")
        return True
    api.copy_message(target_id, admin_id, message["message_id"])
    api.send_message(admin_id, "Ответ отправлен.")
    return True


def is_spam(user_id: int) -> bool:
    if db.get_setting("antispam_enabled", "1") != "1":
        return False
    events = db.register_spam_event(user_id)
    if events >= 7:
        db.block_user(user_id, "antispam")
        return True
    return False


def handle_admin_state(message: dict, admin_id: int, text: str) -> bool:
    state_row = db.get_state(admin_id)
    if not state_row:
        return False
    state = state_row["state"]
    payload = state_row["payload"] or ""

    if text == "/cancel":
        db.clear_state(admin_id)
        api.send_message(admin_id, "Действие отменено.")
        return True

    if state == "add_admin":
        try:
            new_admin_id = int(text.strip())
        except ValueError:
            api.send_message(admin_id, "Отправьте числовой chat_id админа или /cancel.")
            return True
        if db.add_admin(new_admin_id, admin_id):
            api.send_message(admin_id, f"Админ {new_admin_id} добавлен.")
        else:
            api.send_message(admin_id, "Этот пользователь уже супер-админ или его нельзя добавить.")
        db.clear_state(admin_id)
        return True

    if state == "block":
        try:
            target_id = int(text.strip())
        except ValueError:
            api.send_message(admin_id, "Отправьте числовой chat_id пользователя или /cancel.")
            return True
        if target_id in SUPER_ADMINS:
            api.send_message(admin_id, "Супер-админа нельзя заблокировать через панель.")
        else:
            db.block_user(target_id, "admin")
            api.send_message(admin_id, f"Пользователь {target_id} заблокирован.")
        db.clear_state(admin_id)
        return True

    if state == "unblock":
        try:
            target_id = int(text.strip())
        except ValueError:
            api.send_message(admin_id, "Отправьте числовой chat_id пользователя или /cancel.")
            return True
        db.unblock_user(target_id)
        api.send_message(admin_id, f"Пользователь {target_id} разблокирован.")
        db.clear_state(admin_id)
        return True

    if state == "post":
        if not text.strip():
            api.send_message(admin_id, "Для новости нужен заголовок. Отправьте текст или сообщение с подписью.")
            return True
        sent, failed = send_to_all_users(admin_id, message["message_id"], repeat=1)
        api.send_message(admin_id, f"Новость отправлена. Успешно: {sent}, ошибок: {failed}.")
        db.clear_state(admin_id)
        return True

    if state == "broadcast_count":
        try:
            repeat = int(text.strip())
        except ValueError:
            api.send_message(admin_id, "Отправьте число повторов или /cancel.")
            return True
        repeat = max(1, repeat)
        if admin_id not in SUPER_ADMINS and repeat > 20:
            repeat = 20
            api.send_message(admin_id, "Для обычных админов лимит 20 повторов. Запускаю 20.")
        db.set_state(admin_id, "broadcast_message", str(repeat))
        api.send_message(admin_id, "Теперь отправьте сообщение для рассылки.")
        return True

    if state == "broadcast_message":
        repeat = int(payload or "1")
        sent, failed = send_to_all_users(admin_id, message["message_id"], repeat=repeat)
        api.send_message(admin_id, f"Рассылка завершена. Успешно: {sent}, ошибок: {failed}. Повторов: {repeat}.")
        db.clear_state(admin_id)
        return True

    return False


def handle_callback(update: dict) -> None:
    callback = update["callback_query"]
    data = callback.get("data", "")
    from_user = callback.get("from", {})
    chat_id = int(from_user["id"])

    if data == "check_subscription":
        ok, reason = check_subscription(chat_id)
        if ok:
            api.answer_callback_query(callback["id"], "Подписка проверена.")
            api.send_message(chat_id, "Готово, теперь можно пользоваться ботом.")
        else:
            api.answer_callback_query(callback["id"], "Подписка не найдена.", show_alert=True)
            send_subscribe_prompt(chat_id, reason)
        return

    if data == "placeholder_channel":
        api.answer_callback_query(callback["id"], "Этот канал пока заглушка, подписка на него не проверяется.", show_alert=True)
        return

    if not data.startswith("admin:"):
        api.answer_callback_query(callback["id"])
        return

    if not db.is_admin(chat_id):
        api.answer_callback_query(callback["id"], "Нет доступа.", show_alert=True)
        return

    action = data.split(":", 1)[1]
    api.answer_callback_query(callback["id"])

    if action == "antispam":
        current = db.get_setting("antispam_enabled", "1")
        db.set_setting("antispam_enabled", "0" if current == "1" else "1")
        send_admin_panel(chat_id)
    elif action == "add_admin":
        db.set_state(chat_id, "add_admin")
        api.send_message(chat_id, "Отправьте chat_id нового админа. /cancel для отмены.")
    elif action == "stats":
        send_stats(chat_id)
    elif action == "block":
        db.set_state(chat_id, "block")
        api.send_message(chat_id, "Отправьте chat_id пользователя для блокировки. /cancel для отмены.")
    elif action == "unblock":
        db.set_state(chat_id, "unblock")
        api.send_message(chat_id, "Отправьте chat_id пользователя для разблокировки. /cancel для отмены.")
    elif action == "post":
        db.clear_state(chat_id)
        api.send_message(chat_id, "Чтобы пост стабильно отправился: отправьте нужное сообщение в этот чат, потом ответьте на него командой /post.")
    elif action == "broadcast":
        db.clear_state(chat_id)
        if chat_id in SUPER_ADMINS:
            api.send_message(chat_id, "Чтобы сделать рассылку: отправьте нужное сообщение в этот чат, потом ответьте на него командой /broadcast 5. Для супер-админа лимита 20 нет.")
        else:
            api.send_message(chat_id, "Чтобы сделать рассылку: отправьте нужное сообщение в этот чат, потом ответьте на него командой /broadcast 5. Лимит для обычного админа: 20.")


def handle_message(update: dict) -> None:
    message = update["message"]
    if not is_private_message(message):
        return

    user = user_from_message(message)
    text = message.get("text", "") or message.get("caption", "") or ""
    is_start = text.startswith("/start")
    is_admin_command = text.startswith("/admin")
    is_super_admin = user.chat_id in SUPER_ADMINS

    db.upsert_user(user, started=is_start, count_message=not is_start and user.chat_id not in SUPER_ADMINS)

    if db.is_blocked(user.chat_id):
        return

    if not is_super_admin:
        ok, reason = check_subscription(user.chat_id)
        if not ok:
            send_subscribe_prompt(user.chat_id, reason)
            return

    if db.is_admin(user.chat_id):
        if is_admin_command:
            send_admin_panel(user.chat_id)
            return
        if handle_direct_admin_command(message, user.chat_id, text, is_super_admin):
            return
        if reply_to_user_from_owner(message, user.chat_id):
            return
        if handle_admin_state(message, user.chat_id, text):
            return

    if is_start:
        if is_super_admin:
            api.send_message(user.chat_id, "Готово, можно пользоваться ботом.")
        else:
            send_subscribe_prompt(user.chat_id)
        return

    if is_spam(user.chat_id):
        api.send_message(user.chat_id, "Слишком много сообщений. Вы временно заблокированы.")
        api.send_message(OWNER_CHAT_ID, f"Antispam заблокировал пользователя {user.chat_id}.")
        return

    forward_user_message(message, user)


def main() -> None:
    start_health_server()
    start_keep_alive()
    print("Bot started. Press Ctrl+C to stop.")
    offset: int | None = None
    while True:
        try:
            updates = api.get_updates(offset)
            for update in updates:
                offset = int(update["update_id"]) + 1
                if "callback_query" in update:
                    handle_callback(update)
                elif "message" in update:
                    handle_message(update)
        except KeyboardInterrupt:
            print("Stopped.")
            break
        except Exception:
            traceback.print_exc()
            time.sleep(3)


if __name__ == "__main__":
    main()
