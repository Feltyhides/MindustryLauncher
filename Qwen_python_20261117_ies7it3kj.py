"""
Telegram Glass: клиент в стиле Telegram Desktop с эффектом жидкого стекла,
режимом «отправить и удалить», поиском GIF и автоподбором обхода блокировок.
pip install telethon PySide6 "python-socks[asyncio]" qrcode opencv-python pillow
Рядом должен лежать bypass.py
"""
import asyncio
import atexit
import html
import json
import math
import os
import re
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QImage,
QLinearGradient, QMovie, QPainter, QPainterPath,
QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import (QApplication, QCheckBox, QDateTimeEdit, QDialog, QDoubleSpinBox,
QFileDialog, QFrame, QFormLayout, QHBoxLayout, QInputDialog,
QLabel, QLineEdit, QListWidget, QListWidgetItem,
QMainWindow, QPlainTextEdit, QProgressBar,
QPushButton, QScrollArea, QSizePolicy, QSpinBox,
QStackedWidget, QVBoxLayout, QWidget)
from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.functions.help import GetNearestDcRequest
from telethon.tl.functions.messages import GetSavedGifsRequest
from telethon.tl.types import DocumentAttributeAnimated
import bypass
import tdata_import
try:
    import cv2
except ImportError:
    cv2 = None

# Все данные для форка (api_id, api_hash, имя сессии) вводятся прямо в GUI
# на экране входа и сохраняются в settings.json. Значения по умолчанию можно
# так же задать через переменные окружения TELEGRAM_API_ID / TELEGRAM_API_HASH.
DEFAULT_SESSION = "burst_session"
GIF_BOT = "gif"
BASE = Path(__file__).resolve().parent
CACHE_DIR = BASE / "gif_cache"
CACHE_DIR.mkdir(exist_ok=True)
SETTINGS = BASE / "settings.json"

def load_settings():
    try:
        s = json.loads(SETTINGS.read_text(encoding="utf-8"))
        if not isinstance(s, dict):
            raise ValueError
    except Exception:
        s = {}
    s.setdefault("bypass", True)
    s.setdefault("session_name", os.environ.get("TELEGRAM_SESSION", DEFAULT_SESSION))
    s.setdefault("api_id", os.environ.get("TELEGRAM_API_ID", ""))
    s.setdefault("api_hash", os.environ.get("TELEGRAM_API_HASH", ""))
    return s

def save_settings(s):
    try:
        SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

# ============================== Мост worker -> GUI ==============================
class Bridge(QObject):
    log = Signal(str)
    status = Signal(str)
    toast = Signal(str)
    qr = Signal(str)
    need_password = Signal()
    logged_in = Signal(str)
    dialogs = Signal(list)
    avatar = Signal(object, bytes)
    messages = Signal(object, list)
    opened = Signal(dict)
    proxy = Signal(str, str)
    tdata_found = Signal(list)
    tdata_accounts = Signal(list)
    tdata_need_passcode = Signal()
    tdata_done = Signal(bool, str)
    phone_code_sent = Signal()
    phone_code_failed = Signal()
    gif_items = Signal(object, list)
    gif_thumb = Signal(object, int, bytes)
    gif_preview = Signal(object, int, str, str)
    progress = Signal(float)
    burst_done = Signal()
    pdf_done = Signal(str, int)

# ============================== Telegram worker ==============================
class TG:
    def __init__(self, bridge: Bridge):
        self.b = bridge
        self.settings = load_settings()
        self.loop = asyncio.new_event_loop()
        self.client = None
        self.me = None
        self.hunter = None
        self.active = None          # {"proc","port","name","ms"}
        self._pw = None
        self.gif_store, self.gif_token, self.cur_gif = [], None, None
        self.cur_proxy = None
        self._tdata_pw = None
        self._tdata_pending = None   # путь к выбранной tdata, ждём пароль/выбор аккаунта
        self.phone = None
        self._phone_code_hash = None
        threading.Thread(target=self._run, daemon=True).start()
        atexit.register(self.shutdown)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ---------- подключение ----------
    def _client_params(self):
        """Актуальные api_id / api_hash / имя сессии (можно менять прямо из GUI)."""
        try:
            api_id = int(str(self.settings.get("api_id", "")).strip())
        except (ValueError, TypeError):
            api_id = 0
        api_hash = str(self.settings.get("api_hash", "")).strip()
        name = str(self.settings.get("session_name", "") or "").strip() or DEFAULT_SESSION
        return name, api_id, api_hash

    def apply_credentials(self, name, api_id, api_hash):
        """Сохраняет данные для форка в settings.json и переподключается с ними."""
        self.settings["session_name"] = name
        self.settings["api_id"] = api_id
        self.settings["api_hash"] = api_hash
        save_settings(self.settings)
        self.submit(self.startup())

    async def try_connect(self, proxy, timeout=10):
        old = self.client
        if old:
            try:
                await asyncio.wait_for(old.disconnect(), 5)
            except Exception:
                pass
            self.client = None
        name, api_id, api_hash = self._client_params()
        if not api_id or not api_hash:
            self.b.status.emit("Заполни API ID и API Hash в блоке «Данные для форка» слева и нажми «Сохранить и подключиться».")
            return False
        c = TelegramClient(name, api_id, api_hash, proxy=proxy,
                           connection_retries=1, retry_delay=1, timeout=8)
        try:
            await asyncio.wait_for(c.connect(), timeout)
            await asyncio.wait_for(c(GetNearestDcRequest()), timeout)
        except Exception:
            try:
                await asyncio.wait_for(c.disconnect(), 3)
            except Exception:
                pass
            return False
        self.client = c
        self.cur_proxy = proxy
        return True

    def _verify(self, port):
        fut = asyncio.run_coroutine_threadsafe(
            self.try_connect(("socks5", "127.0.0.1", port), 14), self.loop)
        return fut.result(45)

    def stop_active(self):
        if self.active:
            bypass.kill(self.active.get("proc"))
            self.active = None

    async def start_bypass(self):
        self.b.proxy.emit("search", "Ищу обход…")
        self.b.status.emit("Подбираю рабочий обход блокировок…")
        h = bypass.Hunter(
            log=self.b.log.emit,
            progress=lambda d, t: self.b.proxy.emit("search", f"Проверено {d}/{t}"),
            verify=self._verify)
        self.hunter = h
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, h.run)
        except Exception as e:
            self.b.log.emit(f"Ошибка подбора обхода: {e}")
            res = None
        stopped = h._stop.is_set()
        self.hunter = None
        if res:
            self.active = res
            self.b.proxy.emit("on", f"{res['name'][:34]} · {res['ms']} мс")
            self.b.log.emit(f"Обход найден: {res['name']}")
            return True
        if not stopped:
            self.b.proxy.emit("fail", "Рабочий обход не найден")
            self.b.status.emit("Рабочий обход не найден. Нажми переключатель справа сверху, чтобы повторить.")
        return False

    async def startup(self):
        self.b.status.emit("Подключаюсь к Telegram…")
        if await self.try_connect(None, 7):
            self.b.proxy.emit("direct", "Прямое подключение")
            await self.after_connect()
            return
        self.b.log.emit("Напрямую Telegram недоступен")
        if self.settings.get("bypass", True):
            if await self.start_bypass():
                await self.after_connect()
        else:
            self.b.proxy.emit("off", "Обход выключен")
            self.b.status.emit("Нет соединения. Включи обход справа сверху.")

    async def set_bypass(self, on):
        self.settings["bypass"] = on
        save_settings(self.settings)
        if on:
            if self.hunter:
                return
            self.stop_active()
            if await self.start_bypass():
                await self.after_connect()
            elif not await self.try_connect(None, 6):
                self.b.log.emit("Нет соединения")
        else:
            if self.hunter:
                self.hunter.stop()
            self.stop_active()
            self.b.proxy.emit("off", "Обход выключен")
            if await self.try_connect(None, 8):
                self.b.proxy.emit("direct", "Прямое подключение")
                await self.after_connect()
            else:
                self.b.status.emit("Нет соединения. Включи обход справа сверху.")

    def shutdown(self):
        try:
            if self.hunter:
                self.hunter.stop()
            self.stop_active()
        except Exception:
            pass

    # ---------- вход ----------
    async def ask_password(self):
        self._pw = self.loop.create_future()
        self.b.need_password.emit()
        return await self._pw

    def give_password(self, text):
        def _s():
            if self._pw and not self._pw.done():
                self._pw.set_result(text)
        self.loop.call_soon_threadsafe(_s)

    async def start_qr(self):
        """Запускается явно по нажатию кнопки «Войти по QR», а не автоматически."""
        try:
            await self.qr_flow()
            await self.finish_login()
        except Exception as e:
            self.b.toast.emit(f"Ошибка входа по QR: {e}")

    async def qr_flow(self):
        self.b.status.emit("Отсканируй QR: Telegram на телефоне → Настройки → Устройства → Подключить устройство")
        qr = await self.client.qr_login()
        while True:
            self.b.qr.emit(qr.url)
            try:
                await qr.wait(20)
                return
            except asyncio.TimeoutError:
                await qr.recreate()
            except SessionPasswordNeededError:
                while True:
                    pw = await self.ask_password()
                    try:
                        await self.client.sign_in(password=pw)
                        return
                    except Exception as e:
                        self.b.toast.emit(f"Пароль не подошёл: {e}")

    # ---------- вход по номеру телефона и коду ----------
    async def phone_send_code(self, phone):
        self.phone = phone.strip()
        if not self.phone.startswith("+"):
            self.phone = "+" + self.phone.lstrip("+")
        try:
            sent = await self.client.send_code_request(self.phone)
            self._phone_code_hash = sent.phone_code_hash
            kind = type(sent.type).__name__
            where = {
                "SentCodeTypeApp": "сообщением в самом Telegram (чат «Telegram»)",
                "SentCodeTypeSms": "по SMS",
                "SentCodeTypeCall": "голосовым звонком-роботом, код продиктует",
                "SentCodeTypeFlashCall": "коротким звонком — код это последние цифры номера звонившего",
                "SentCodeTypeMissedCall": "пропущенным звонком — код это последние цифры номера",
            }.get(kind, kind)
            self.b.log.emit(f"Способ доставки кода: {kind}")
            self.b.status.emit(f"Telegram ответил, что код придёт: {where}. "
                              "Если за 2-3 минуты ничего нет, смотри следующее сообщение.")
            self.b.phone_code_sent.emit()
        except Exception as e:
            msg = str(e)
            if "already used" in msg or "AUTH_RESTART" in msg:
                self.b.toast.emit(
                    "Telegram попросил начать заново (обычно из-за повторных нажатий). "
                    "Закрой программу, удали файл "
                    f"{self._client_params()[0]}.session, подожди минуту и попробуй снова.")
            else:
                self.b.toast.emit(f"Не удалось отправить код: {e}")
            self.b.phone_code_failed.emit()

    async def phone_sign_in(self, code):
        try:
            await self.client.sign_in(self.phone, code, phone_code_hash=self._phone_code_hash)
        except SessionPasswordNeededError:
            while True:
                pw = await self.ask_password()
                try:
                    await self.client.sign_in(password=pw)
                    break
                except Exception as e:
                    self.b.toast.emit(f"Пароль не подошёл: {e}")
                    return
        except Exception as e:
            self.b.toast.emit(f"Код не подошёл: {e}")
            return
        await self.finish_login()

    async def finish_login(self):
        self.me = await self.client.get_me()
        self.b.logged_in.emit(utils.get_display_name(self.me))
        await self.load_dialogs()

    # ---------- импорт сессии из Telegram Desktop (tdata) ----------
    async def scan_tdata(self):
        self.b.status.emit("Ищу Telegram Desktop на компьютере…")
        try:
            found = await asyncio.get_running_loop().run_in_executor(None, tdata_import.find_tdata)
        except Exception as e:
            self.b.toast.emit(f"Ошибка поиска: {e}")
            found = []
        if not found:
            self.b.toast.emit("Telegram Desktop на этом компьютере не найден")
            self.b.status.emit("Не нашёл tdata. Можно войти по QR или указать папку вручную.")
        self.b.tdata_found.emit([str(p) for p in found])

    async def ask_tdata_passcode(self):
        self._tdata_pw = self.loop.create_future()
        self.b.tdata_need_passcode.emit()
        return await self._tdata_pw

    def give_tdata_passcode(self, text):
        def _s():
            if self._tdata_pw and not self._tdata_pw.done():
                self._tdata_pw.set_result(text)
        self.loop.call_soon_threadsafe(_s)

    async def tdata_accounts(self, path):
        accs = await asyncio.get_running_loop().run_in_executor(
            None, tdata_import.list_accounts, path)
        self.b.tdata_accounts.emit(accs)

    async def tdata_login(self, path, account_index=None):
        self.b.status.emit("Переношу авторизацию из Telegram Desktop…")
        passcode = ""
        sess_name = self._client_params()[0]
        try:
            me, phone = await tdata_import.import_session(
                path, sess_name, passcode=passcode, account_index=account_index,
                log=self.b.log.emit)
        except RuntimeError as e:
            msg = str(e)
            if "пароль" in msg.lower() or "passcode" in msg.lower():
                passcode = await self.ask_tdata_passcode()
                if passcode is None:
                    self.b.tdata_done.emit(False, "Отменено")
                    return
                try:
                    me, phone = await tdata_import.import_session(
                        path, sess_name, passcode=passcode, account_index=account_index,
                        log=self.b.log.emit)
                except Exception as e2:
                    self.b.tdata_done.emit(False, str(e2))
                    return
            else:
                self.b.tdata_done.emit(False, msg)
                return
        except Exception as e:
            self.b.tdata_done.emit(False, str(e))
            return
        self.b.log.emit(f"tdata: авторизован как {utils.get_display_name(me)} ({phone or '—'})")
        self.b.tdata_done.emit(True, utils.get_display_name(me))
        # подключаемся штатным клиентом к уже готовой сессии и продолжаем как обычно
        if await self.try_connect(self.cur_proxy, 10):
            await self.after_connect()
        else:
            self.b.toast.emit("Сессия сохранена, но подключиться не удалось. Проверь обход/сеть.")

    async def after_connect(self):
        try:
            if await self.client.is_user_authorized():
                await self.finish_login()
            else:
                self.b.status.emit(
                    "Выбери способ входа: отсканируй QR, войди по номеру телефона "
                    "или перенеси сессию из Telegram Desktop.")
        except Exception as e:
            self.b.log.emit(f"Ошибка после подключения: {e}")

    # ---------- чаты и сообщения ----------
    async def load_dialogs(self):
        items = []
        async for d in self.client.iter_dialogs(limit=200):
            kind = "user" if d.is_user else ("group" if d.is_group else "channel")
            items.append({"name": d.name or "Без имени", "id": d.id, "entity": d.entity,
                          "unread": d.unread_count, "kind": kind,
                          "username": getattr(d.entity, "username", None)})
        self.b.dialogs.emit(items)
        self.b.log.emit(f"Загружено чатов: {len(items)}")
        asyncio.create_task(self._avatars(items[:60]))

    async def _avatars(self, items):
        sem = asyncio.Semaphore(4)
        async def one(d):
            async with sem:
                try:
                    data = await self.client.download_profile_photo(
                        d["entity"], file=bytes, download_big=False)
                except Exception:
                    data = None
            if data:
                self.b.avatar.emit(d["id"], data)
        await asyncio.gather(*(one(d) for d in items))

    @staticmethod
    def _media_label(m):
        for attr, label in (("gif", "[GIF]"), ("sticker", "[стикер]"), ("photo", "[фото]"),
                            ("voice", "[голосовое]"), ("video", "[видео]"), ("media", "[медиа]")):
            if getattr(m, attr, None):
                return label
        return ""

    async def load_messages(self, chat_id, entity):
        try:
            msgs = await self.client.get_messages(entity, limit=50)
        except Exception as e:
            self.b.log.emit(f"Не удалось загрузить сообщения: {e}")
            return
        rows = []
        for m in reversed(msgs):
            who = "Я" if m.out else (utils.get_display_name(m.sender) if m.sender else "?")
            body = " ".join(x for x in (self._media_label(m), m.text or "") if x) or "[служебное]"
            rows.append({"out": bool(m.out), "who": who, "sid": m.sender_id, "body": body,
                         "when": m.date.astimezone().strftime("%d.%m %H:%M"), "mid": m.id})
        self.b.messages.emit(chat_id, rows)

    async def resolve_open(self, raw):
        raw = raw.strip()
        if raw.lstrip("-").isdigit():
            raw = int(raw)
        try:
            e = await self.client.get_entity(raw)
        except Exception as ex:
            self.b.toast.emit(f"Не нашёл «{raw}»: {ex}")
            return
        self.b.opened.emit({"name": utils.get_display_name(e), "id": e.id, "entity": e,
                            "unread": 0, "kind": "user", "username": getattr(e, "username", None)})

    # ---------- GIF ----------
    async def fetch_gifs(self, token, query):
        self.gif_token = token
        try:
            if query is None:
                res = await self.client(GetSavedGifsRequest(hash=0))
                items = [{"doc": d, "result": None} for d in getattr(res, "gifs", [])]
            else:
                res = await self.client.inline_query(GIF_BOT, query)
                items = [{"doc": r.document, "result": r} for r in res if r.document]
        except Exception as e:
            self.b.log.emit(f"GIF: ошибка {e}")
            self.b.gif_items.emit(token, [])
            return
        if token != self.gif_token:
            return
        self.gif_store = items
        self.b.gif_items.emit(token, [getattr(i["doc"], "size", 0) for i in items])
        sem = asyncio.Semaphore(6)
        async def one(i, it):
            async with sem:
                if token != self.gif_token:
                    return
                try:
                    data = await self.client.download_media(it["doc"], file=bytes, thumb=-1)
                except Exception:
                    data = None
            if data and token == self.gif_token:
                self.b.gif_thumb.emit(token, i, data)
        await asyncio.gather(*(one(i, it) for i, it in enumerate(items)))

    async def preview_gif(self, token, idx):
        try:
            doc = self.gif_store[idx]["doc"]
            path = await self.client.download_media(doc, file=str(CACHE_DIR / str(doc.id)))
        except Exception as e:
            self.b.log.emit(f"Превью GIF: {e}")
            return
        self.b.gif_preview.emit(token, idx, str(path), getattr(doc, "mime_type", "") or "")

    def pick_gif(self, idx):
        self.cur_gif = self.gif_store[idx] if 0 <= idx < len(self.gif_store) else None

    # ---------- отправка ----------
    async def _send_gif(self, entity, text, cache):
        item = self.cur_gif
        if cache.get("media") is not None:
            try:
                return await self.client.send_file(entity, cache["media"], caption=text or None)
            except FloodWaitError:
                raise
            except Exception:
                cache["media"] = None
        if item["result"] is not None:
            msg = await item["result"].click(entity)
        else:
            msg = await self.client.send_file(entity, item["doc"], caption=text or None)
        cache["media"] = msg.media
        return msg

    async def _send_one(self, entity, text, file_path, use_gif, cache, fallback):
        while True:
            try:
                if use_gif and self.cur_gif:
                    return await self._send_gif(entity, text, cache)
                if file_path:
                    kw = {}
                    if file_path.lower().endswith((".gif", ".mp4", ".webm")):
                        kw = {"attributes": [DocumentAttributeAnimated()], "supports_streaming": True}
                    return await self.client.send_file(entity, file_path, caption=text or None, **kw)
                return await self.client.send_message(entity, text or fallback)
            except FloodWaitError as e:
                self.b.log.emit(f"FloodWait: жду {e.seconds} сек.")
                await asyncio.sleep(e.seconds + 1)

    async def send_once(self, entity, text, file_path, use_gif):
        try:
            await self._send_one(entity, text, file_path, use_gif, {}, " ")
        except Exception as e:
            self.b.toast.emit(f"Ошибка отправки: {e}")

    async def burst(self, entity, count, text, file_path, use_gif, pause, del_delay, revoke):
        b = self.b
        b.log.emit(f"Собеседник: {utils.get_display_name(entity)} | ID: {entity.id}")
        counters, tasks, cache = {"sent": 0, "del": 0}, [], {}
        def upd():
            b.progress.emit((counters["sent"] + counters["del"]) / (2 * count))
        async def delete_later(mid):
            if del_delay > 0:
                await asyncio.sleep(del_delay)
            while True:
                try:
                    await self.client.delete_messages(entity, [mid], revoke=revoke)
                    counters["del"] += 1
                    b.log.emit(f"Удалено #{mid} ({counters['del']}/{count})")
                    upd()
                    return
                except FloodWaitError as e:
                    b.log.emit(f"FloodWait при удалении: {e.seconds} сек.")
                    await asyncio.sleep(e.seconds + 1)
                except Exception as e:
                    b.log.emit(f"Ошибка удаления #{mid}: {e}")
                    return
        try:
            for i in range(count):
                msg = await self._send_one(entity, text, file_path, use_gif, cache, str(i + 1))
                counters["sent"] += 1
                b.log.emit(f"Отправлено {i + 1}/{count} (#{msg.id})")
                upd()
                tasks.append(asyncio.create_task(delete_later(msg.id)))
                if pause and i < count - 1:
                    await asyncio.sleep(pause)
        except Exception as e:
            b.log.emit(f"Ошибка отправки: {e}")
        if tasks:
            await asyncio.gather(*tasks)
        b.progress.emit(1.0)
        b.log.emit(f"Готово. Отправлено {counters['sent']}, удалено {counters['del']}")
        b.burst_done.emit()

    # ---------- фото из промежутка -> PDF ----------
    async def photos_to_pdf(self, entity, dt_from, dt_to, out_path, max_photos=0):
        """Собирает фотографии чата за выбранный промежуток времени и формирует PDF."""
        b = self.b
        try:
            from PIL import Image
        except ImportError:
            b.toast.emit("Для PDF установи Pillow: pip install pillow")
            b.pdf_done.emit("", 0)
            return
        name = utils.get_display_name(entity)
        b.log.emit(f"PDF: ищу фото в «{name}» с {dt_from:%d.%m.%Y %H:%M} по {dt_to:%d.%m.%Y %H:%M}…")
        imgs, seen = [], set()
        total, done = 0, 0
        try:
            async for m in self.client.iter_messages(entity, offset_date=dt_to + timedelta(seconds=1),
                                                     reverse=True):
                if m.date < dt_from:
                    break
                media = m.photo
                if media is None or getattr(media, "id", None) in seen:
                    continue
                seen.add(media.id)
                total += 1
                if max_photos and total > max_photos:
                    total -= 1
                    break
                try:
                    data = await self.client.download_media(media, file=bytes)
                except FloodWaitError as e:
                    b.log.emit(f"PDF FloodWait: жду {e.seconds} сек.")
                    await asyncio.sleep(e.seconds + 1)
                    try:
                        data = await self.client.download_media(media, file=bytes)
                    except Exception:
                        data = None
                except Exception:
                    data = None
                done += 1
                b.progress.emit(done / max(total, 1) * 0.95)
                if not data:
                    continue
                try:
                    import io

                    def _open():
                        im = Image.open(io.BytesIO(data))
                        im.load()
                        return im.convert("RGB")

                    img = await asyncio.get_running_loop().run_in_executor(None, _open)
                except Exception as e:
                    b.log.emit(f"PDF: не удалось открыть фото #{m.id}: {e}")
                    continue
                imgs.append(img)
        except Exception as e:
            b.log.emit(f"PDF: ошибка чтения чата: {e}")
        if not imgs:
            b.toast.emit("В выбранном промежутке фотографий нет")
            b.progress.emit(0)
            b.pdf_done.emit("", 0)
            return
        try:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)

            def _save():
                first, rest = imgs[0], imgs[1:]
                first.save(out_path, "PDF", save_all=True, append_images=rest, resolution=120.0)

            await asyncio.get_running_loop().run_in_executor(None, _save)
        except Exception as e:
            b.log.emit(f"PDF: ошибка записи файла: {e}")
            b.toast.emit(f"Не удалось сохранить PDF: {e}")
            b.pdf_done.emit("", 0)
            return
        b.progress.emit(1.0)
        b.log.emit(f"PDF готов: {out_path} ({len(imgs)} стр.)")
        b.toast.emit(f"PDF сохранён: {out_path}")
        b.pdf_done.emit(out_path, len(imgs))

# ============================== Визуальные компоненты ==============================
QSS = """
* { font-family:  "Segoe UI ",  "SF Pro Display ",  "Inter ",  "Helvetica Neue ", sans-serif; color: #f2f6ff; font-size: 14px; }
QMainWindow, QDialog { background: #0b1020; }
QLabel { background: transparent; }
QLineEdit, QSpinBox, QDoubleSpinBox {
background : rgba(255,255,255,26); border: 1px solid rgba(255,255,255,60);
border-radius: 16px; padding: 8px 14px; selection-background-color: #4c8dff; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid rgba(140,190,255,210); background: rgba(255,255,255,38); }
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 0; }
QPushButton { background: rgba(255,255,255,30); border: 1px solid rgba(255,255,255,62); border-radius: 16px; padding: 8px 16px; }
QPushButton:hover { background: rgba(255,255,255,56); }
QPushButton:pressed { background: rgba(255,255,255,22); }
QPushButton:disabled { color: rgba(255,255,255,90); background: rgba(255,255,255,14); } 
QPushButton:checked { background: rgba(90,160,255,120); border: 1px solid rgba(160,210,255,170); }
QPushButton#accent { border: 1px solid rgba(255,255,255,130); font-weight: 600;
 background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(70,160,255,235), stop:1 rgba(130,90,255,235)); }
QPushButton#accent:hover { background: qlineargradient(x1:0,y1:0,x2:1, y2:1, stop:0 rgba(100,180,255,245), stop:1 rgba(150,110,255,245)); }
QPushButton#accent:disabled { background: rgba(255,255,255,20); }
QPushButton#round { border-radius: 22px; min-width: 44px; max-width: 44px; min-height: 44px; max-height: 44px; padding: 0; font-size: 18px; }
QListWidget { background: transparent; border: none; outline: none; }
QListWidget::item { border-radius: 18px; padding: 6px 8px; margin: 2px 6px; }
QListWidget::item:hover { background: rgba(255,255,255,26); }
QListWidget::item:selected { background: rgba(80,150, 255,115); color: white; }
QScrollArea { background: transparent; border: none; }
QScrollArea  > QWidget  > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 6px 2px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,70);  border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: rgba(255,255,255,120); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical, QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { height: 0; background: transparent; }
QFrame#bubOut { border-radius: 18px; border: 1px solid rgba(255,255,255,80);
background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 rgba(70,150,255,165), stop:1 rgba(120,100,255,150)); }
QFrame#bubIn { border-radius: 18px; border: 1px solid rgba(255,255,255,55);  background: rgba(255,255,255,34); }
QProgressBar { background: rgba(255,255,255,25); border: none; border-radius: 4px; max-height: 8px; min-height: 8px; }
QProgressBar::chunk { border-radius: 4px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #4aa3ff, stop:1 #9b6bff); }
QPlainTextEdit { background: rgba(0,0,0,55); border-radius: 12px; border: 1px  solid rgba(255,255,255,28); color: #b8c7dc; font-size: 12px; padding: 4px; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 6px; border: 1px solid rgba(255,255,255,90); background: rgba(255,255,255,22); }
QCheckBox::indicator:checked { background: #4c8dff; border: 1px solid rgba(255,255,255,160); }
QToolTip { background: #1b2540; color: white; border: 1px solid rgba(255,255,255,70); padding: 6px; border-radius: 8px; }
"""

PALETTE = [("#ff885e", "#ff516a"), ("#ffcd6a", "#ffa85c"), ("#82b1ff", "#665fff"),
           ("#a0de7e", "#54cb68"), ("#53edd6", "#28c9b7"), ("#72d5fd", "#2a9ef1"),
           ("#e0a2f3", "#d669ed")]

def make_avatar(name, key, size=48, data=None):
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0, 0, size, size)
    p.setClipPath(path)
    img = QPixmap()
    if data and img.loadFromData(data):
        p.drawPixmap(0, 0, img.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                      Qt.TransformationMode.SmoothTransformation))
    else:
        c1, c2 = PALETTE[abs(int(key)) % len(PALETTE)]
        g = QLinearGradient(0, 0, 0, size)
        g.setColorAt(0, QColor(c1))
        g.setColorAt(1, QColor(c2))
        p.fillRect(0, 0, size, size, g)
        p.setPen(QColor("white"))
        f = QFont()
        f.setPixelSize(int(size * 0.4))
        f.setBold(True)
        p.setFont(f)
        letters = "".join(w[0] for w in (name or "?").split()[:2]).upper() or "?"
        p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, letters)
    p.end()
    return pm

class Backdrop(QWidget):
    """Мягкий анимированный фон: цветные «капли» под стеклом."""
    BLOBS = [(0.18, 0.22, 0.55, "#3b82f6"), (0.82, 0.18, 0.5, "#a855f7"),
             (0.72, 0.86, 0.6, "#06b6d4"), (0.12, 0.88, 0.45, "#ec4899")]

    def __init__(self, animated=True, parent=None):
        super().__init__(parent)
        self.t = 0.0
        if animated:
            self.timer = QTimer(self)
            self.timer.timeout.connect(self._tick)
            self.timer.start(45)

    def _tick(self):
        self.t += 0.045
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        g = QLinearGradient(0, 0, w, h)
        g.setColorAt(0, QColor("#0a0f1f"))
        g.setColorAt(1, QColor("#160c2c"))
        p.fillRect(self.rect(), g)
        p.setPen(Qt.PenStyle.NoPen)
        for i, (x, y, r, c) in enumerate(self.BLOBS):
            cx = w * (x + 0.07 * math.sin(self.t * 0.7 + i * 1.7))
            cy = h * (y + 0.07 * math.cos(self.t * 0.6 + i * 2.3))
            rad = max(w, h) * r
            rg = QRadialGradient(cx, cy, rad)
            col = QColor(c)
            col.setAlpha(150)
            rg.setColorAt(0, col)
            col.setAlpha(0)
            rg.setColorAt(1, col)
            p.setBrush(rg)
            p.drawEllipse(QPointF(cx, cy), rad, rad)

class Glass(QFrame):
    """Панель «жидкое стекло»: полупрозрачная заливка, световая кромка, блик сверху."""
    def __init__(self, radius=24, alpha=34, parent=None):
        super().__init__(parent)
        self.radius, self.alpha = radius, alpha

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.8, 0.8, -0.8, -0.8)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, QColor(255, 255, 255, self.alpha + 26))
        g.setColorAt(1, QColor(255, 255, 255, max(self.alpha - 16, 6)))
        p.setBrush(g)
        bd = QLinearGradient(r.topLeft(), r.bottomRight())
        bd.setColorAt(0, QColor(255, 255, 255, 170))
        bd.setColorAt(0.5, QColor(255, 255, 255, 40))
        bd.setColorAt(1, QColor(255, 255, 255, 90))
        p.setPen(QPen(bd, 1.2))
        p.drawRoundedRect(r, self.radius, self.radius)
        # блик сверху
        hl = QLinearGradient(r.left(), r.top(), r.left(), r.top() + r.height() * 0.5)
        hl.setColorAt(0, QColor(255, 255, 255, 36))
        hl.setColorAt(1, QColor(255, 255, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(hl)
        p.drawRoundedRect(r.adjusted(1, 1, -1, -r.height() * 0.5), self.radius, self.radius)

class BypassPill(QPushButton):
    COLORS = {"off": (150, 160, 182), "search": (255, 190, 70), "on": (80, 230, 150),
              "direct": (100, 175, 255), "fail": (255, 100, 112)}

    def __init__(self):
        super().__init__()
        self.state, self.text_ = "off", "Обход: выключен"
        self.phase = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(42)
        self.setMinimumWidth(270)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    @property
    def enabled_(self):
        return self.state in ("search", "on")

    def set_state(self, state, text):
        self.state, self.text_ = state, text
        self.setToolTip(text)
        if state == "search":
            if not self.timer.isActive():
                self.timer.start(40)
        else:
            self.timer.stop()
        self.update()

    def _tick(self):
        self.phase += 0.2
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(*self.COLORS.get(self.state, self.COLORS["off"]))
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        rad = r.height() / 2
        bg = QLinearGradient(r.topLeft(), r.bottomRight())
        a = QColor(c)
        a.setAlpha(70 if self.underMouse() else 46)
        bg.setColorAt(0, QColor(255, 255, 255, 62))
        bg.setColorAt(1, a)
        p.setBrush(bg)
        b = QColor(c)
        b.setAlpha(170)
        p.setPen(QPen(b, 1.3))
        p.drawRoundedRect(r, rad, rad)
        # индикатор
        cx, cy = r.left() + 24, r.center().y()
        glow = QRadialGradient(cx, cy, 14)
        g1 = QColor(c)
        g1.setAlpha(150)
        g2 = QColor(c)
        g2.setAlpha(0)
        glow.setColorAt(0, g1)
        glow.setColorAt(1, g2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QPointF(cx, cy), 14, 14)
        if self.state == "search":
            p.setPen(QPen(c, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(QRectF(cx - 8, cy - 8, 16, 16), int(-self.phase * 360 * 16 / 6.28),
                      int(260 * 16))
        else:
            p.setBrush(c)
            p.drawEllipse(QPointF(cx, cy), 5.5, 5.5)
        # переключатель справа
        sw_w, sw_h = 40, 22
        sx, sy = r.right() - sw_w - 12, r.center().y() - sw_h / 2
        on = self.enabled_
        track = QColor(c) if on else QColor(255, 255, 255, 60)
        if on:
            track.setAlpha(210)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(sx, sy, sw_w, sw_h), sw_h / 2, sw_h / 2)
        kx = sx + (sw_w - sw_h + 3 if on else 3)
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(kx, sy + 3, sw_h - 6, sw_h - 6))
        # текст
        f = self.font()
        f.setPixelSize(13)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(240, 246, 255))
        tx = cx + 20
        avail = int(sx - tx - 8)
        txt = QFontMetrics(f).elidedText(self.text_, Qt.TextElideMode.ElideRight, avail)
        p.drawText(QRectF(tx, r.top(), avail, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, txt)

class QRView(QWidget):
    def __init__(self):
        super().__init__()
        self.m = None
        self.setFixedSize(260, 260)

    def set_url(self, url):
        import qrcode
        q = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(url)
        q.make(fit=True)
        self.m = q.get_matrix()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("white"))
        p.drawRoundedRect(self.rect(), 22, 22)
        if not self.m:
            p.setPen(QColor("#667"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Жду QR…")
            return
        n = len(self.m)
        cell = (self.width() - 24) / n
        p.setBrush(QColor("#0b1020"))
        for y, row in enumerate(self.m):
            for x, v in enumerate(row):
                if v:
                    p.drawRect(QRectF(12 + x * cell, 12 + y * cell, cell + 0.4, cell + 0.4))

def esc(s):
    return html.escape(str(s)).replace("\n", "<br>")

# ============================== Окно выбора GIF ==============================
class GifPicker(QDialog):
    def __init__(self, parent, tg: TG, bridge: Bridge, on_pick):
        super().__init__(parent)
        self.setWindowTitle("Выбор GIF")
        self.resize(1000, 650)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.tg, self.on_pick = tg, on_pick
        self.token, self.sel, self.thumbs = 0, None, {}
        self.movie, self.cap = None, None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        back = Backdrop(animated=False)
        outer.addWidget(back)
        root = QVBoxLayout(back)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        top = Glass(22, 30)
        tl = QHBoxLayout(top)
        tl.setContentsMargins(12, 10, 12, 10)
        self.b_search = QPushButton("Поиск")
        self.b_saved = QPushButton("Сохранённые")
        for b_ in (self.b_search, self.b_saved):
            b_.setCheckable(True)
        self.b_search.setChecked(True)
        self.b_search.clicked.connect(lambda: self._mode("search"))
        self.b_saved.clicked.connect(lambda: self._mode("saved"))
        self.q = QLineEdit()
        self.q.setPlaceholderText("Например: cat, facepalm, thumbs up")
        self.q.returnPressed.connect(self.search)
        go = QPushButton("Искать")
        go.setObjectName("accent")
        go.clicked.connect(self.search)
        for w_ in (self.b_search, self.b_saved):
            tl.addWidget(w_)
        tl.addWidget(self.q, 1)
        tl.addWidget(go)
        root.addWidget(top)
        mid = QHBoxLayout()
        mid.setSpacing(12)
        gl = Glass(22, 26)
        gll = QVBoxLayout(gl)
        gll.setContentsMargins(8, 8, 8, 8)
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setIconSize(QSize(160, 120))
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setSpacing(6)
        self.list.setUniformItemSizes(True)
        self.list.itemClicked.connect(self._clicked)
        gll.addWidget(self.list)
        mid.addWidget(gl, 1)
        side = Glass(22, 30)
        side.setFixedWidth(380)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 16, 16, 16)
        t = QLabel("Предпросмотр")
        t.setStyleSheet("font-size:16px;font-weight:600;")
        self.preview = QLabel("Выбери GIF слева")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedSize(348, 300)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#9fb4cc;")
        self.use = QPushButton("Использовать этот GIF")
        self.use.setObjectName("accent")
        self.use.setEnabled(False)
        self.use.setMinimumHeight(42)
        self.use.clicked.connect(self._confirm)
        for w_ in (t, self.preview, self.hint):
            sl.addWidget(w_)
        sl.addStretch(1)
        sl.addWidget(self.use)
        mid.addWidget(side)
        root.addLayout(mid, 1)
        self.status = QLabel("")
        self.status.setStyleSheet("color:#9fb4cc;")
        root.addWidget(self.status)
        bridge.gif_items.connect(self._on_items)
        bridge.gif_thumb.connect(self._on_thumb)
        bridge.gif_preview.connect(self._on_preview)
        QTimer.singleShot(200, self.q.setFocus)

    # --- управление ---
    def _mode(self, m):
        self.b_search.setChecked(m == "search")
        self.b_saved.setChecked(m == "saved")
        if m == "saved":
            self._load(None)
        else:
            self.search()

    def search(self):
        q = self.q.text().strip()
        if not q:
            self.status.setText("Введи запрос для поиска")
            return
        self.b_search.setChecked(True)
        self.b_saved.setChecked(False)
        self._load(q)

    def _load(self, query):
        self.token += 1
        self.list.clear()
        self.thumbs.clear()
        self._stop_anim()
        self.sel = None
        self.use.setEnabled(False)
        self.status.setText("Загрузка…")
        self.tg.submit(self.tg.fetch_gifs(self.token, query))

    def _on_items(self, token, sizes):
        if token != self.token:
            return
        self.status.setText(f"Найдено GIF: {len(sizes)}" if sizes else "Ничего не найдено")
        for i, sz in enumerate(sizes):
            it = QListWidgetItem("GIF")
            it.setSizeHint(QSize(170, 130))
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.list.addItem(it)

    def _on_thumb(self, token, idx, data):
        if token != self.token or idx >= self.list.count():
            return
        pm = QPixmap()
        if pm.loadFromData(data):
            self.thumbs[idx] = pm
            it = self.list.item(idx)
            it.setText("")
            it.setIcon(QIcon(pm.scaled(160, 120, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)))

    def _clicked(self, item):
        idx = self.list.row(item)
        self.sel = idx
        self.use.setEnabled(True)
        self._stop_anim()
        if idx in self.thumbs:
            self.preview.setPixmap(self.thumbs[idx].scaled(
                348, 300, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        self.hint.setText("Загружаю анимацию…")
        self.tg.submit(self.tg.preview_gif(self.token, idx))

    # --- анимация ---
    def _stop_anim(self):
        self.timer.stop()
        if self.movie:
            self.movie.stop()
            self.movie = None
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def _on_preview(self, token, idx, path, mime):
        if token != self.token or idx != self.sel:
            return
        self._stop_anim()
        if path.lower().endswith(".gif") or mime == "image/gif":
            mv = QMovie(path)
            mv.jumpToFrame(0)
            sz = mv.currentImage().size()
            if sz.isValid() and sz.width() and sz.height():
                k = min(348 / sz.width(), 300 / sz.height())
                mv.setScaledSize(QSize(int(sz.width() * k), int(sz.height() * k)))
            self.movie = mv
            self.preview.setMovie(mv)
            mv.start()
            self.hint.setText("")
        elif cv2 is not None:
            self.cap = cv2.VideoCapture(path)
            fps = self.cap.get(cv2.CAP_PROP_FPS) or 15
            self.timer.start(int(1000 / min(max(fps, 5), 30)))
            self.hint.setText("")
        else:
            self.hint.setText("Для анимации: pip install opencv-python (пока статичная картинка)")

    def _tick(self):
        if self.cap is None:
            return
        ok, fr = self.cap.read()
        if not ok:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            return
        fr = cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)
        h, w, _ = fr.shape
        img = QImage(fr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        self.preview.setPixmap(QPixmap.fromImage(img).scaled(
            348, 300, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _confirm(self):
        if self.sel is not None:
            self.tg.pick_gif(self.sel)
            self.on_pick(self.thumbs.get(self.sel))
        self.close()

    def closeEvent(self, e):
        self.token += 1
        self._stop_anim()
        super().closeEvent(e)

# ============================== Диалог: фото из чата за промежуток -> PDF =====
class PdfPicker(QDialog):
    """Выбор промежутка времени; клиент соберёт фото из чата и сформирует PDF."""

    def __init__(self, parent, tg: TG, bridge: Bridge, chat_name: str, entity):
        super().__init__(parent)
        self.setWindowTitle("Фото из чата в PDF")
        self.resize(560, 470)
        self.tg, self.entity = tg, entity
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        back = Backdrop(animated=False)
        outer.addWidget(back)
        root = QVBoxLayout(back)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)
        card = Glass(26, 34)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(22, 18, 22, 18)
        cl.setSpacing(10)
        t = QLabel("📄  Фото → PDF")
        t.setStyleSheet("font-size:19px;font-weight:700;")
        sub = QLabel(f"Чат: {chat_name}\nВыбери промежуток — клиент возьмёт из него фотографии "
                     "и сформирует PDF (по одной странице на фото).")
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#b8c9de;")
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        now = datetime.now().replace(microsecond=0)
        self.dt_from = QDateTimeEdit(now - timedelta(days=30))
        self.dt_to = QDateTimeEdit(now)
        for w_ in (self.dt_from, self.dt_to):
            w_.setDisplayFormat("dd.MM.yyyy HH:mm:ss")
            w_.setCalendarPopup(True)
        self.sp_max = QSpinBox()
        self.sp_max.setRange(0, 5000)
        self.sp_max.setValue(0)
        self.sp_max.setSpecialValueText("все")
        self.sp_max.setToolTip("Сколько максимум фотографий взять из промежутка (0 — все)")
        form.addRow("С:", self.dt_from)
        form.addRow("По:", self.dt_to)
        form.addRow("Максимум фото:", self.sp_max)
        # быстрые пресеты промежутка
        pr = QHBoxLayout()
        pr.setSpacing(8)
        for label, days in (("сутки", 1), ("неделя", 7), ("месяц", 30), ("год", 365)):
            b_ = QPushButton(label)
            b_.clicked.connect(lambda _=False, d=days: self._preset(d))
            pr.addWidget(b_)
        pr.addStretch(1)
        self.out_edit = QLineEdit(str(BASE / f"photos_{re.sub(r'[^A-Za-z0-9_-]+', '_', chat_name)[:32]}_"
                                          f"{now:%Y%m%d_%H%M}.pdf"))
        self.out_edit.setPlaceholderText("Куда сохранить PDF")
        b_out = QPushButton("…")
        b_out.setFixedWidth(44)
        b_out.clicked.connect(self._pick_out)
        h_out = QHBoxLayout()
        h_out.setSpacing(8)
        h_out.addWidget(self.out_edit, 1)
        h_out.addWidget(b_out)
        self.run_btn = QPushButton("Собрать фото и создать PDF")
        self.run_btn.setObjectName("accent")
        self.run_btn.setMinimumHeight(44)
        self.run_btn.clicked.connect(self._go)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#9fb4cc;")
        cl.addWidget(t)
        cl.addWidget(sub)
        cl.addLayout(form)
        cl.addLayout(pr)
        cl.addWidget(QLabel("Файл PDF:"))
        cl.addLayout(h_out)
        cl.addWidget(self.run_btn)
        cl.addWidget(self.hint)
        root.addWidget(card)
        root.addStretch(1)
        bridge.progress.connect(self._on_progress)
        bridge.pdf_done.connect(self._on_done)

    def _preset(self, days):
        end = datetime.now().replace(microsecond=0)
        self.dt_to.setDateTime(end)
        self.dt_from.setDateTime(end - timedelta(days=days))

    def _pick_out(self):
        p, _ = QFileDialog.getSaveFileName(self, "Сохранить PDF как", self.out_edit.text(),
                                           "PDF (*.pdf)")
        if p:
            if not p.lower().endswith(".pdf"):
                p += ".pdf"
            self.out_edit.setText(p)

    def _go(self):
        dt_from = self.dt_from.dateTime().toPython()
        dt_to = self.dt_to.dateTime().toPython()
        if dt_from >= dt_to:
            self.hint.setText("Начало промежутка должно быть раньше конца.")
            return
        out = self.out_edit.text().strip()
        if not out:
            self.hint.setText("Укажи, куда сохранить PDF.")
            return
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
        self.run_btn.setEnabled(False)
        self.hint.setText("Собираю фотографии… не закрывай окно.")
        self.tg.submit(self.tg.photos_to_pdf(self.entity, dt_from, dt_to, out,
                                             self.sp_max.value()))

    def _on_progress(self, v):
        if self.isVisible():
            self.hint.setText(f"Прогресс: {int(v * 100)}%")

    def _on_done(self, path, n):
        self.run_btn.setEnabled(True)
        if path and n:
            self.hint.setText(f"✅ Готово: {path} ({n} стр.)")
            self.accept()
        else:
            self.hint.setText("Завершено. Фотографий в промежутке не нашлось или произошла ошибка — "
                              "подробности в журнале.")

# ============================== Диалог: данные для форка (настройки) ==========
class SettingsDialog(QDialog):
    """Ввод и сохранение api_id / api_hash / имени сессии прямо из GUI."""

    def __init__(self, parent, tg: TG):
        super().__init__(parent)
        self.setWindowTitle("Данные для форка")
        self.resize(460, 330)
        self.tg = tg
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        back = Backdrop(animated=False)
        outer.addWidget(back)
        root = QVBoxLayout(back)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)
        card = Glass(26, 34)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(22, 18, 22, 18)
        cl.setSpacing(10)
        t = QLabel("⚙️ Данные для форка")
        t.setStyleSheet("font-size:19px;font-weight:700;")
        hint = QLabel("Получить на my.telegram.org. Всё сохраняется в settings.json рядом с программой.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#b8c9de;")
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        s_ = tg.settings
        self.ed_id = QLineEdit(str(s_.get("api_id", "") or ""))
        self.ed_id.setPlaceholderText("API ID — число")
        self.ed_hash = QLineEdit(str(s_.get("api_hash", "") or ""))
        self.ed_hash.setPlaceholderText("API HASH — 32 hex-символа")
        self.ed_sess = QLineEdit(str(s_.get("session_name", "") or DEFAULT_SESSION))
        self.ed_sess.setPlaceholderText("Имя файла сессии (без .session)")
        form.addRow("API ID:", self.ed_id)
        form.addRow("API Hash:", self.ed_hash)
        form.addRow("Сессия:", self.ed_sess)
        btns = QHBoxLayout()
        b_ok = QPushButton("💾 Сохранить и переподключиться")
        b_ok.setObjectName("accent")
        b_ok.clicked.connect(self._save)
        b_cancel = QPushButton("Отмена")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_ok)
        btns.addWidget(b_cancel)
        cl.addWidget(t)
        cl.addWidget(hint)
        cl.addLayout(form)
        cl.addLayout(btns)
        root.addWidget(card)
        root.addStretch(1)

    def _save(self):
        raw_id = self.ed_id.text().strip()
        api_hash = self.ed_hash.text().strip()
        name = re.sub(r"[^\w\-\.]", "_", self.ed_sess.text().strip()) or DEFAULT_SESSION
        if not raw_id.isdigit():
            self.tg.b.toast.emit("API ID должен быть числом")
            return
        if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            self.tg.b.toast.emit("API Hash — 32 hex-символа")
            return
        self.ed_sess.setText(name)
        # сохраняем и переподключаемся уже с новыми данными
        self.tg.apply_credentials(name, raw_id, api_hash.lower())
        self.tg.b.toast.emit(f"Данные сохранены, сессия: {name}")
        self.accept()

# ============================== Главное окно ==============================
class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Telegram Glass")
        self.resize(1240, 800)
        self.setMinimumSize(1000, 660)
        self.b = Bridge()
        self.tg = TG(self.b)
        self.cur = None
        self.items = {}           # id -> QListWidgetItem
        self.file_path = None
        self.use_gif = False
        self.running = False

        back = Backdrop()
        self.setCentralWidget(back)
        root = QVBoxLayout(back)
        root.setContentsMargins(16, 14, 16, 16)
        root.setSpacing(12)
        # ---- верхняя панель с кнопкой обхода справа ----
        top = Glass(22, 30)
        top.setFixedHeight(62)
        tl = QHBoxLayout(top)
        tl.setContentsMargins(18, 10, 14, 10)
        logo = QLabel("✈")
        logo.setStyleSheet("font-size:22px;color:#7cc4ff;")
        title = QLabel("Telegram Glass")
        title.setStyleSheet("font-size:17px;font-weight:700;")
        self.me_lbl = QLabel("")
        self.me_lbl.setStyleSheet("color:#9fb4cc;")
        self.pill = BypassPill()
        self.pill.clicked.connect(self._toggle_bypass)
        b_set = QPushButton("⚙")
        b_set.setObjectName("round")
        b_set.setToolTip("Данные для форка: API ID / API Hash / сессия (сохраняются)")
        b_set.clicked.connect(self._open_settings)
        tl.addWidget(logo)
        tl.addWidget(title)
        tl.addSpacing(10)
        tl.addWidget(self.me_lbl)
        tl.addStretch(1)
        tl.addWidget(b_set)
        tl.addWidget(self.pill)
        root.addWidget(top)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_login())
        self.stack.addWidget(self._build_main())
        # ---- сигналы ----
        b = self.b
        b.log.connect(self._log)
        b.status.connect(self.login_status.setText)
        b.toast.connect(self._toast)
        b.qr.connect(self._on_qr)
        b.need_password.connect(self._ask_password)
        b.logged_in.connect(self._on_logged)
        b.dialogs.connect(self._on_dialogs)
        b.avatar.connect(self._on_avatar)
        b.messages.connect(self._on_messages)
        b.opened.connect(self._select)
        b.proxy.connect(self.pill.set_state)
        b.progress.connect(lambda v: self.bar.setValue(int(v * 100)))
        b.burst_done.connect(self._burst_done)
        b.tdata_found.connect(self._on_tdata_found)
        b.tdata_accounts.connect(self._on_tdata_accounts)
        b.tdata_need_passcode.connect(self._ask_tdata_passcode)
        b.tdata_done.connect(self._on_tdata_done)
        b.phone_code_sent.connect(self._on_phone_code_sent)
        b.phone_code_failed.connect(self._on_phone_code_failed)
        # GUI уже показан, теперь стартуем подключение и подбор обхода
        QTimer.singleShot(400, lambda: self.tg.submit(self.tg.startup()))
        # QR теперь всегда виден слева — генерируем сразу после подключения
        QTimer.singleShot(1500, lambda: self.tg.submit(self.tg.start_qr()))

    # ---------- построение экранов ----------
    def _build_login(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card = Glass(32, 36)
        # Увеличил карточку: теперь QR и форма номера помещаются в горизонтальном layout
        card.setFixedSize(800, 720)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(30, 30, 30, 30)
        cl.setSpacing(14)
        t = QLabel("Вход в Telegram")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setStyleSheet("font-size:24px;font-weight:700;")
        self.login_status = QLabel("Запускаюсь…")
        self.login_status.setWordWrap(True)
        self.login_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.login_status.setStyleSheet("color:#b8c9de;")

        # --- Горизонтальный layout: слева QR, справа — вход по номеру ---
        h_lay = QHBoxLayout()
        h_lay.setSpacing(24)

        # Левая часть — QR-код (всегда виден, генерируется при старте)
        qr_col = QVBoxLayout()
        qr_col.setSpacing(10)
        qr_lbl = QLabel("Войди по QR-коду")
        qr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr_lbl.setStyleSheet("font-size:14px;font-weight:600;color:#b8c9de;")
        self.qr = QRView()
        qr_col.addWidget(qr_lbl)
        qr_col.addWidget(self.qr, 0, Qt.AlignmentFlag.AlignCenter)
        qr_hint = QLabel("Telegram → Настройки →\nУстройства → Подключить устройство")
        qr_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr_hint.setStyleSheet("color:#9fb4cc;font-size:11px;")
        qr_hint.setWordWrap(True)
        qr_col.addWidget(qr_hint)
        qr_col.addStretch(1)

        # Правая часть — вход по номеру телефона
        phone_col = QVBoxLayout()
        phone_col.setSpacing(8)
        phone_lbl = QLabel("Или вход по номеру")
        phone_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        phone_lbl.setStyleSheet("font-size:14px;font-weight:600;color:#b8c9de;")
        self.ph_input = QLineEdit()
        self.ph_input.setPlaceholderText("+79991234567")
        self.ph_input.returnPressed.connect(self._send_code)
        b_code = QPushButton("Отправить код")
        b_code.clicked.connect(self._send_code)
        self._b_code = b_code
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("Код из Telegram/SMS")
        self.code_input.hide()
        self.code_input.returnPressed.connect(self._confirm_code)
        b_confirm = QPushButton("Войти")
        b_confirm.setObjectName("accent")
        b_confirm.hide()
        b_confirm.clicked.connect(self._confirm_code)
        self._b_confirm = b_confirm

        phone_col.addWidget(phone_lbl)
        phone_col.addWidget(self.ph_input)
        phone_col.addWidget(b_code)
        phone_col.addWidget(self.code_input)
        phone_col.addWidget(b_confirm)
        phone_col.addStretch(1)

        h_lay.addLayout(qr_col, 1)
        h_lay.addLayout(phone_col, 1)

        # Кнопка tdata
        b_td = QPushButton("💻  Войти через Telegram Desktop на этом компьютере")
        b_td.clicked.connect(self._tdata_scan)
        b_td.setToolTip("Найдёт установленный Telegram Desktop и перенесёт вход без QR")

        # --- данные для форка: api_id / api_hash / имя сессии (сохраняются в settings.json) ---
        s_ = self.tg.settings
        cred_box = Glass(20, 26)
        cl.addWidget(cred_box)
        cform = QFormLayout()
        cform.setSpacing(8)
        cform.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        cap = QLabel("⚙️ Данные для форка")
        cap.setStyleSheet("font-size:14px;font-weight:600;color:#b8c9de;")
        self.ed_api_id = QLineEdit(str(s_.get("api_id", "") or ""))
        self.ed_api_id.setPlaceholderText("API ID — число с my.telegram.org")
        self.ed_api_hash = QLineEdit(str(s_.get("api_hash", "") or ""))
        self.ed_api_hash.setPlaceholderText("API HASH — hex-строка с my.telegram.org")
        self.ed_session = QLineEdit(str(s_.get("session_name", "") or DEFAULT_SESSION))
        self.ed_session.setPlaceholderText("Имя файла сессии (без .session)")
        self.chk_show_cred = QCheckBox("Показывать")
        self.ed_api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.chk_show_cred.toggled.connect(
            lambda on: self.ed_api_hash.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        cform.addRow("API ID:", self.ed_api_id)
        cform.addRow("API Hash:", self.ed_api_hash)
        cform.addRow("Сессия:", self.ed_session)
        b_cred = QPushButton("💾 Сохранить и подключиться")
        b_cred.setObjectName("accent")
        b_cred.clicked.connect(self._save_credentials)
        cred_in = QVBoxLayout(cred_box)
        cred_in.setContentsMargins(16, 12, 16, 12)
        cred_in.setSpacing(6)
        cred_in.insertWidget(0, cap)
        cred_in.addLayout(cform)
        row_show = QHBoxLayout()
        row_show.addWidget(self.chk_show_cred)
        row_show.addStretch(1)
        cred_in.addLayout(row_show)
        cred_in.addWidget(b_cred)

        cl.addWidget(t)
        cl.addLayout(h_lay, 1)
        cl.addWidget(self.login_status)
        cl.addWidget(b_td)
        lay.addWidget(card)
        return page

    # ---------- данные для форка (api_id / api_hash / сессия) ----------
    def _save_credentials(self):
        raw_id = self.ed_api_id.text().strip()
        api_hash = self.ed_api_hash.text().strip()
        name = re.sub(r"[^\w\-\.]", "_", self.ed_session.text().strip()) or DEFAULT_SESSION
        if not raw_id.isdigit():
            self._toast("API ID должен быть числом (смотри my.telegram.org)")
            return
        if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            self._toast("API Hash — 32 hex-символа с my.telegram.org")
            return
        self.tg.settings["session_name"] = name
        self.tg.settings["api_id"] = raw_id
        self.tg.settings["api_hash"] = api_hash.lower()
        save_settings(self.tg.settings)
        self.ed_session.setText(name)
        self.login_status.setText(f"Данные сохранены в settings.json. Подключаюсь, сессия «{name}»…")
        self._b_code.setEnabled(True)
        self._b_code.setText("Отправить код")
        self.tg.submit(self.tg.startup())

    # ---------- вход по телефону ----------
    def _send_code(self):
        if not self._b_code.isEnabled():
            return  # защита от повторных кликов, пока запрос ещё летит
        phone = self.ph_input.text().strip()
        if not phone:
            self._toast("Введи номер телефона")
            return
        self._b_code.setEnabled(False)
        self._b_code.setText("Отправляю…")
        self.login_status.setText("Отправляю код…")
        self.tg.submit(self.tg.phone_send_code(phone))

    def _on_phone_code_sent(self):
        self._b_code.setText("Код отправлен")
        self.code_input.show()
        self._b_confirm.show()
        self.code_input.setFocus()

    def _on_phone_code_failed(self):
        self._b_code.setEnabled(True)
        self._b_code.setText("Отправить код")

    def _confirm_code(self):
        code = self.code_input.text().strip()
        if not code:
            self._toast("Введи код")
            return
        self.login_status.setText("Проверяю код…")
        self.tg.submit(self.tg.phone_sign_in(code))

    # ---------- вход через Telegram Desktop (tdata) ----------
    def _tdata_scan(self):
        self.login_status.setText("Ищу Telegram Desktop на компьютере…")
        self.tg.submit(self.tg.scan_tdata())

    def _on_tdata_found(self, paths):
        if not paths:
            p = QFileDialog.getExistingDirectory(
                self, "Папка tdata не найдена автоматически — укажи её вручную (если есть)")
            if p and (Path(p) / "key_datas").exists():
                paths = [p]
            elif p:
                self._toast("В этой папке нет файла key_datas — это не tdata")
                return
            else:
                return
        if len(paths) == 1:
            self._tdata_pick(paths[0])
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Найдено несколько установок Telegram Desktop")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("Выбери, откуда перенести вход:"))
        lst = QListWidget()
        for p in paths:
            lst.addItem(p)
        v.addWidget(lst)
        ok = QPushButton("Выбрать")
        ok.setObjectName("accent")
        v.addWidget(ok)
        def _go():
            it = lst.currentItem()
            if it:
                dlg.accept()
                self._tdata_pick(it.text())
        ok.clicked.connect(_go)
        lst.itemDoubleClicked.connect(lambda _: _go())
        dlg.resize(560, 320)
        dlg.exec()

    def _tdata_pick(self, path):
        self._tdata_path = path
        self.login_status.setText("Проверяю аккаунты в этой папке…")
        self.tg.submit(self.tg.tdata_accounts(path))

    def _on_tdata_accounts(self, phones):
        if len(phones) <= 1:
            self._tdata_go(0 if phones else None)
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("В Telegram Desktop несколько аккаунтов")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("Какой перенести?"))
        lst = QListWidget()
        for ph in phones:
            lst.addItem(ph or "Без номера")
        lst.setCurrentRow(0)
        v.addWidget(lst)
        ok = QPushButton("Перенести этот аккаунт")
        ok.setObjectName("accent")
        v.addWidget(ok)
        def _go():
            idx = lst.currentRow()
            dlg.accept()
            self._tdata_go(idx)
        ok.clicked.connect(_go)
        dlg.resize(420, 300)
        dlg.exec()

    def _tdata_go(self, idx):
        self.login_status.setText("Переношу авторизацию, подожди…")
        self.tg.submit(self.tg.tdata_login(self._tdata_path, idx))

    def _ask_tdata_passcode(self):
        pw, ok = QInputDialog.getText(
            self, "Пароль локальной блокировки",
            "В Telegram Desktop включён код-пароль на вход.\nВведи его:",
            QLineEdit.EchoMode.Password)
        self.tg.give_tdata_passcode(pw if ok else None)

    def _on_tdata_done(self, ok, text):
        if ok:
            self.login_status.setText(f"Готово: вошёл как {text}")
        else:
            self._toast(f"Не удалось перенести вход: {text}")
            self.login_status.setText(
                "Не получилось перенести вход из Telegram Desktop. Можно войти по QR.")

    def _build_main(self):
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        # --- левая колонка ---
        left = Glass(26, 30)
        left.setFixedWidth(340)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 14, 10, 12)
        ll.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск или @username / ID + Enter")
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._search_enter)
        self.chat_list = QListWidget()
        self.chat_list.setIconSize(QSize(46, 46))
        self.chat_list.itemClicked.connect(lambda it: self._select(it.data(Qt.ItemDataRole.UserRole)))
        ref = QPushButton("⟳  Обновить чаты")
        ref.clicked.connect(lambda: self.tg.submit(self.tg.load_dialogs()))
        ll.addWidget(self.search)
        ll.addWidget(self.chat_list, 1)
        ll.addWidget(ref)
        row.addWidget(left)
        # --- правая колонка ---
        col = QVBoxLayout()
        col.setSpacing(12)
        head = Glass(24, 30)
        head.setFixedHeight(76)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 10, 16, 10)
        self.h_av = QLabel()
        self.h_av.setFixedSize(48, 48)
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        self.h_title = QLabel("Выбери чат слева")
        self.h_title.setStyleSheet("font-size:17px;font-weight:700;")
        self.h_sub = QLabel("")
        self.h_sub.setStyleSheet("color:#9fb4cc;")
        tcol.addWidget(self.h_title)
        tcol.addWidget(self.h_sub)
        b_id = QPushButton("Копировать ID")
        b_id.clicked.connect(self._copy_id)
        b_rf = QPushButton("⟳")
        b_rf.setToolTip("Обновить сообщения")
        b_rf.clicked.connect(self._reload_msgs)
        hl.addWidget(self.h_av)
        hl.addLayout(tcol, 1)
        hl.addWidget(b_id)
        hl.addWidget(b_rf)
        col.addWidget(head)
        msgs = Glass(26, 22)
        ml = QVBoxLayout(msgs)
        ml.setContentsMargins(8, 8, 8, 8)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.viewport().setAutoFillBackground(False)
        host = QWidget()
        host.setAutoFillBackground(False)
        self.msg_lay = QVBoxLayout(host)
        self.msg_lay.setContentsMargins(14, 10, 14, 10)
        self.msg_lay.setSpacing(8)
        self.msg_lay.addStretch(1)
        self.scroll.setWidget(host)
        ml.addWidget(self.scroll)
        col.addWidget(msgs, 1)
        # панель режима «отправить и удалить»
        self.burst_panel = Glass(24, 30)
        bl = QVBoxLayout(self.burst_panel)
        bl.setContentsMargins(16, 12, 16, 12)
        bl.setSpacing(8)
        r1 = QHBoxLayout()
        r1.setSpacing(10)
        self.sp_count = QSpinBox()
        self.sp_count.setRange(1, 5000)
        self.sp_count.setValue(5)
        self.sp_pause = QDoubleSpinBox()
        self.sp_pause.setRange(0, 60)
        self.sp_pause.setDecimals(2)
        self.sp_pause.setSingleStep(0.1)
        self.sp_pause.setValue(0.5)
        self.sp_pause.setSuffix(" с")
        self.sp_del = QDoubleSpinBox()
        self.sp_del.setRange(0, 3600)
        self.sp_del.setDecimals(2)
        self.sp_del.setSingleStep(0.5)
        self.sp_del.setSpecialValueText("сразу")
        self.sp_del.setSuffix(" с")
        self.chk_rev = QCheckBox("Удалять у обоих")
        self.chk_rev.setChecked(True)
        for text, w_ in (("Количество", self.sp_count), ("Пауза между отправками", self.sp_pause),
                         ("Удалить через", self.sp_del)):
            c = QVBoxLayout()
            c.setSpacing(2)
            lb = QLabel(text)
            lb.setStyleSheet("color:#9fb4cc;font-size:12px;")
            c.addWidget(lb)
            c.addWidget(w_)
            r1.addLayout(c)
        r1.addWidget(self.chk_rev, 0, Qt.AlignmentFlag.AlignBottom)
        r1.addStretch(1)
        bl.addLayout(r1)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 100)
        bl.addWidget(self.bar)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(78)
        self.log_box.setMaximumBlockCount(300)
        bl.addWidget(self.log_box)
        col.addWidget(self.burst_panel)
        # вложение
        self.attach_bar = Glass(18, 28)
        self.attach_bar.setFixedHeight(54)
        al = QHBoxLayout(self.attach_bar)
        al.setContentsMargins(12, 6, 12, 6)
        self.att_thumb = QLabel()
        self.att_thumb.setFixedSize(40, 40)
        self.att_text = QLabel("")
        b_x = QPushButton("✕")
        b_x.setFixedWidth(40)
        b_x.clicked.connect(self._clear_attach)
        al.addWidget(self.att_thumb)
        al.addWidget(self.att_text, 1)
        al.addWidget(b_x)
        self.attach_bar.hide()
        col.addWidget(self.attach_bar)
        # композер
        comp = Glass(30, 34)
        comp.setFixedHeight(68)
        cl = QHBoxLayout(comp)
        cl.setContentsMargins(12, 10, 12, 10)
        cl.setSpacing(8)
        b_file = QPushButton("📎")
        b_file.setObjectName("round")
        b_file.setToolTip("Файл")
        b_file.clicked.connect(self._pick_file)
        b_gif = QPushButton("GIF")
        b_gif.setObjectName("round")
        b_gif.setStyleSheet("font-size:12px;font-weight:700;")
        b_gif.clicked.connect(self._open_gif)
        b_pdf = QPushButton("📄")
        b_pdf.setObjectName("round")
        b_pdf.setToolTip("Сохранить фотографии чата за выбранный промежуток в PDF")
        b_pdf.clicked.connect(self._open_pdf)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Сообщение или подпись")
        self.input.returnPressed.connect(self._send)
        self.b_burst = QPushButton("⚡ Отправить и удалить")
        self.b_burst.setObjectName("accent")
        self.b_burst.setMinimumHeight(44)
        self.b_burst.clicked.connect(self._burst)
        b_send = QPushButton("➤")
        b_send.setObjectName("round")
        b_send.setToolTip("Отправить обычное сообщение")
        b_send.clicked.connect(self._send)
        for w_ in (b_file, b_gif, b_pdf):
            cl.addWidget(w_)
        cl.addWidget(self.input, 1)
        cl.addWidget(self.b_burst)
        cl.addWidget(b_send)
        col.addWidget(comp)
        row.addLayout(col, 1)
        return page

    # ---------- общие слоты ----------
    def _log(self, s):
        self.log_box.appendPlainText(s)

    def _toast(self, s):
        self.log_box.appendPlainText(s)
        self.statusBar().showMessage(s, 6000)

    def _toggle_bypass(self):
        self.tg.submit(self.tg.set_bypass(not self.pill.enabled_))

    def _on_qr(self, url):
        self.stack.setCurrentIndex(0)
        try:
            self.qr.set_url(url)
        except ImportError:
            self.login_status.setText("Установи qrcode: pip install qrcode")

    def _ask_password(self):
        pw, ok = QInputDialog.getText(self, "Двухфакторная защита", "Пароль Telegram:",
                                      QLineEdit.EchoMode.Password)
        self.tg.give_password(pw if ok else "")

    def _on_logged(self, name):
        self.me_lbl.setText(f"· {name}")
        self.stack.setCurrentIndex(1)

    # ---------- чаты ----------
    def _on_dialogs(self, items):
        self.chat_list.clear()
        self.items.clear()
        for d in items:
            unread = f"   ● {d['unread']}" if d["unread"] else ""
            it = QListWidgetItem(QIcon(make_avatar(d["name"], d["id"], 46)),
                                 f"{d['name']}{unread}\nID: {d['id']}")
            it.setData(Qt.ItemDataRole.UserRole, d)
            it.setSizeHint(QSize(0, 62))
            self.chat_list.addItem(it)
            self.items[d["id"]] = it

    def _on_avatar(self, cid, data):
        it = self.items.get(cid)
        if it:
            d = it.data(Qt.ItemDataRole.UserRole)
            it.setIcon(QIcon(make_avatar(d["name"], cid, 46, data)))

    def _filter(self, text):
        t = text.strip().lower()
        for i in range(self.chat_list.count()):
            it = self.chat_list.item(i)
            d = it.data(Qt.ItemDataRole.UserRole)
            it.setHidden(bool(t) and t not in d["name"].lower() and t not in str(d["id"]))

    def _search_enter(self):
        t = self.search.text().strip()
        if t and all(self.chat_list.item(i).isHidden() for i in range(self.chat_list.count())):
            self.tg.submit(self.tg.resolve_open(t))

    def _select(self, d):
        self.cur = d
        self.h_title.setText(d["name"])
        un = f"   |   @{d['username']}" if d.get("username") else ""
        self.h_sub.setText(f"ID: {d['id']}{un}")
        self.h_av.setPixmap(make_avatar(d["name"], d["id"], 48))
        self._reload_msgs()

    def _copy_id(self):
        if self.cur:
            QApplication.clipboard().setText(str(self.cur["id"]))
            self._toast(f"ID {self.cur['id']} скопирован")

    def _reload_msgs(self):
        if self.cur:
            self.tg.submit(self.tg.load_messages(self.cur["id"], self.cur["entity"]))

    def _on_messages(self, cid, rows):
        if not self.cur or cid != self.cur["id"]:
            return
        while self.msg_lay.count() > 1:
            w = self.msg_lay.takeAt(1).widget()
            if w:
                w.deleteLater()
        for m in rows:
            self.msg_lay.addWidget(self._bubble(m))
        QTimer.singleShot(80, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()))

    def _bubble(self, m):
        out = m["out"]
        fr = QFrame()
        fr.setObjectName("bubOut" if out else "bubIn")
        v = QVBoxLayout(fr)
        v.setContentsMargins(14, 8, 14, 8)
        v.setSpacing(2)
        col = "#d9ecff" if out else "#9be7a8"
        head = QLabel(f"<span style='color:{col};font-weight:600'>{esc(m['who'])}</span> "
                      f"<span style='color:#b4c3d8;font-size:11px'>ID {m['sid']}</span>")
        body = QLabel(esc(m["body"]))
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setMaximumWidth(520)
        meta = QLabel(f"{m['when']} · #{m['mid']}")
        meta.setStyleSheet("color:#c3d0e2;font-size:11px;")
        meta.setAlignment(Qt.AlignmentFlag.AlignRight)
        v.addWidget(head)
        v.addWidget(body)
        v.addWidget(meta)
        fr.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        if out:
            h.addStretch(1)
            h.addWidget(fr)
        else:
            h.addWidget(fr)
            h.addStretch(1)
        return wrap

    # ---------- вложения ----------
    def _pick_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "Файл", "",
                                           "GIF / видео / картинки (*.gif *.mp4 *.webm *.jpg *.png);;Все файлы (*)")
        if p:
            self.file_path, self.use_gif = p, False
            self.tg.cur_gif = None
            self.att_thumb.clear()
            self.att_text.setText(f"📎 {Path(p).name}")
            self.attach_bar.show()

    def _open_gif(self):
        GifPicker(self, self.tg, self.b, self._gif_picked).show()

    def _open_pdf(self):
        if not self._need_chat():
            return
        PdfPicker(self, self.tg, self.b, self.cur["name"], self.cur["entity"]).show()

    def _open_settings(self):
        dlg = SettingsDialog(self, self.tg)
        dlg.exec()

    def _gif_picked(self, pm):
        self.use_gif, self.file_path = True, None
        if pm:
            self.att_thumb.setPixmap(pm.scaled(40, 40, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
        self.att_text.setText("GIF выбран")
        self.attach_bar.show()

    def _clear_attach(self):
        self.file_path, self.use_gif = None, False
        self.tg.cur_gif = None
        self.attach_bar.hide()

    # ---------- отправка ----------
    def _need_chat(self):
        if not self.cur:
            self._toast("Сначала выбери чат слева")
            return False
        return True

    def _send(self):
        if not self._need_chat():
            return
        text = self.input.text()
        if not (text or self.file_path or self.use_gif):
            return
        self.tg.submit(self.tg.send_once(self.cur["entity"], text, self.file_path, self.use_gif))
        self.input.clear()
        QTimer.singleShot(1500, self._reload_msgs)

    def _burst(self):
        if self.running or not self._need_chat():
            return
        self.running = True
        self.b_burst.setEnabled(False)
        self.b_burst.setText("Работаю…")
        self.bar.setValue(0)
        self.tg.submit(self.tg.burst(
            self.cur["entity"], self.sp_count.value(), self.input.text(), self.file_path,
            self.use_gif, self.sp_pause.value(), self.sp_del.value(), self.chk_rev.isChecked()))

    def _burst_done(self):
        self.running = False
        self.b_burst.setEnabled(True)
        self.b_burst.setText("⚡ Отправить и удалить")
        self._reload_msgs()

    def closeEvent(self, e):
        self.tg.shutdown()
        super().closeEvent(e)

def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    w = Main()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()