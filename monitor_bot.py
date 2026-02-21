#!/usr/bin/env python3
"""Telegram bot untuk memantau antrian di https://antrian.rsuii.co.id/."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE_URL = "https://antrian.rsuii.co.id/"
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
NOTIFY_FORCE_SECONDS = int(os.getenv("NOTIFY_FORCE_SECONDS", "600"))
DATA_FILE = os.getenv("DATA_FILE", "subscriptions.json")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("antrian-rsuii")


@dataclass
class QueueEntry:
    label: str
    number: int
    checked_in: bool


@dataclass
class QueueSnapshot:
    poli_label: str
    doctor_label: str
    total: int
    current: str
    upcoming: list[QueueEntry]
    skipped: list[str]
    finished: list[str]
    fetched_at: float

    def fingerprint(self) -> str:
        payload = {
            "total": self.total,
            "current": self.current,
            "upcoming": [(x.label, x.checked_in) for x in self.upcoming],
            "skipped": self.skipped,
            "finished": self.finished,
        }
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


class RSUIIClient:
    def __init__(self, timeout: int = 25):
        self.client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def _get_soup(self, data: dict[str, str] | None = None) -> BeautifulSoup:
        if data is None:
            resp = await self.client.get(BASE_URL)
        else:
            resp = await self.client.post(BASE_URL, data=data)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")

    @staticmethod
    def _form_payload(soup: BeautifulSoup) -> dict[str, str]:
        payload: dict[str, str] = {}
        form = soup.find("form", id="frm")
        if not form:
            raise RuntimeError("Form ASP.NET tidak ditemukan")
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
                payload[name] = inp.get("value", "")
        return payload

    @staticmethod
    def _options(soup: BeautifulSoup, select_id: str) -> list[tuple[str, str]]:
        select = soup.find("select", id=select_id)
        if not select:
            return []
        result = []
        for opt in select.find_all("option"):
            value = (opt.get("value") or "").strip()
            label = opt.get_text(" ", strip=True)
            if value:
                result.append((value, label))
        return result

    async def get_poli_options(self) -> list[tuple[str, str]]:
        soup = await self._get_soup()
        return self._options(soup, "ddUNIT")

    async def get_doctors_for_poli(self, poli_value: str) -> list[tuple[str, str]]:
        soup = await self._get_soup()
        payload = self._form_payload(soup)
        payload.update({"__EVENTTARGET": "ddUNIT", "ddUNIT": poli_value, "ddDaftarDokter": ""})
        soup2 = await self._get_soup(payload)
        return self._options(soup2, "ddDaftarDokter")

    async def fetch_snapshot(self, poli_value: str, doctor_value: str) -> QueueSnapshot:
        soup = await self._get_soup()
        p1 = self._form_payload(soup)
        p1.update({"__EVENTTARGET": "ddUNIT", "ddUNIT": poli_value, "ddDaftarDokter": ""})
        soup = await self._get_soup(p1)

        p2 = self._form_payload(soup)
        p2.update({"__EVENTTARGET": "ddDaftarDokter", "ddUNIT": poli_value, "ddDaftarDokter": doctor_value})
        soup = await self._get_soup(p2)

        poli_label = dict(self._options(soup, "ddUNIT")).get(poli_value, poli_value)
        doctor_label = dict(self._options(soup, "ddDaftarDokter")).get(doctor_value, doctor_value)

        total = int((soup.find(id="lblTotal") or Tag(name="x")).get_text(strip=True) or 0)
        current = (soup.find(id="lblCurrent") or Tag(name="x")).get_text(strip=True)

        upcoming = self._extract_entries(soup, "Antrian Selanjutnya")
        skipped = [x.label for x in self._extract_entries(soup, "Antrian Dilewati")]
        finished = [x.label for x in self._extract_entries(soup, "Antrian Selesai")]

        return QueueSnapshot(
            poli_label=poli_label,
            doctor_label=doctor_label,
            total=total,
            current=current,
            upcoming=upcoming,
            skipped=skipped,
            finished=finished,
            fetched_at=time.time(),
        )

    @staticmethod
    def _extract_entries(soup: BeautifulSoup, title: str) -> list[QueueEntry]:
        heading = soup.find(lambda t: t.name in {"h4", "h5"} and t.get_text(strip=True).lower() == title.lower())
        if not heading:
            return []
        container = heading.find_parent("div")
        values_div = container.find_next_sibling("div") if container else None
        if not values_div:
            return []
        entries: list[QueueEntry] = []
        for h1 in values_div.find_all("h1"):
            text = h1.get_text(" ", strip=True)
            if not text:
                continue
            raw = text.replace("*", "").strip()
            try:
                number = int(raw.split("-")[-1])
            except ValueError:
                continue
            entries.append(QueueEntry(label=raw, number=number, checked_in="*" not in text))
        return entries


@dataclass
class Subscription:
    chat_id: int
    poli_value: str
    poli_label: str
    doctor_value: str
    doctor_label: str
    my_number: int
    last_fingerprint: str = ""
    last_notified_at: float = 0.0


class BotState:
    def __init__(self, path: str):
        self.path = path
        self.subscriptions: dict[int, Subscription] = {}
        self.pending_poli: dict[int, str] = {}
        self.pending_doctor: dict[int, str] = {}

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for row in data:
            sub = Subscription(**row)
            self.subscriptions[sub.chat_id] = sub

    def save(self) -> None:
        data = [asdict(s) for s in self.subscriptions.values()]
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def compute_metrics(snapshot: QueueSnapshot, my_number: int) -> dict[str, Any]:
    checked_in = sum(1 for e in snapshot.upcoming if e.checked_in)
    not_checked = len(snapshot.upcoming) - checked_in

    before = [e for e in snapshot.upcoming if e.number < my_number]
    before_checkin = [e for e in before if e.checked_in]

    return {
        "checked_in": checked_in,
        "not_checked_in": not_checked,
        "remaining_fastest": len(before_checkin),
        "remaining_slowest": len(before),
        "is_upcoming": any(e.number == my_number for e in snapshot.upcoming),
    }


def render_status(sub: Subscription, snap: QueueSnapshot) -> str:
    m = compute_metrics(snap, sub.my_number)
    return (
        f"📍 {sub.poli_label}\n"
        f"👨‍⚕️ {sub.doctor_label}\n"
        f"🎟️ Antrian kamu: {sub.my_number}\n\n"
        f"Total antrian: {snap.total}\n"
        f"Antrian saat ini: {snap.current or '-'}\n"
        f"Menunggu (Antrian Selanjutnya): {len(snap.upcoming)}\n"
        f"✅ Sudah check-in (tanpa *): {m['checked_in']}\n"
        f"⏳ Belum check-in (dengan *): {m['not_checked_in']}\n"
        f"🚀 Sisa tercepat (asumsi semua yg check-in duluan): {m['remaining_fastest']}\n"
        f"🐢 Sisa terlama (asumsi semua sebelum nomor kamu dipanggil): {m['remaining_slowest']}"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Mengambil daftar poli...")
    client: RSUIIClient = context.application.bot_data["client"]
    state: BotState = context.application.bot_data["state"]
    polis = await client.get_poli_options()
    keyboard = [
        [InlineKeyboardButton(label[:60], callback_data=f"poli:{value}")]
        for value, label in polis[:40]
    ]
    if len(polis) > 40:
        keyboard.append([InlineKeyboardButton("(Daftar dipotong 40 opsi pertama)", callback_data="noop")])
    await update.message.reply_text("Pilih poliklinik:", reply_markup=InlineKeyboardMarkup(keyboard))
    state.pending_poli.pop(update.effective_chat.id, None)
    state.pending_doctor.pop(update.effective_chat.id, None)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "noop":
        return

    client: RSUIIClient = context.application.bot_data["client"]
    state: BotState = context.application.bot_data["state"]
    chat_id = query.message.chat_id

    if data.startswith("poli:"):
        poli_value = data.split(":", 1)[1]
        state.pending_poli[chat_id] = poli_value
        doctors = await client.get_doctors_for_poli(poli_value)
        keyboard = [
            [InlineKeyboardButton(label[:60], callback_data=f"dok:{value}")]
            for value, label in doctors[:50]
        ]
        await query.message.reply_text(
            "Pilih dokter/praktek:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("dok:"):
        doctor_value = data.split(":", 1)[1]
        state.pending_doctor[chat_id] = doctor_value
        await query.message.reply_text("Masukkan nomor antrian kamu (angka saja, misal 28):")


async def on_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    if not text.isdigit():
        return

    state: BotState = context.application.bot_data["state"]
    client: RSUIIClient = context.application.bot_data["client"]

    poli_value = state.pending_poli.get(chat_id)
    doctor_value = state.pending_doctor.get(chat_id)
    if not poli_value or not doctor_value:
        return

    my_number = int(text)
    snap = await client.fetch_snapshot(poli_value, doctor_value)

    sub = Subscription(
        chat_id=chat_id,
        poli_value=poli_value,
        poli_label=snap.poli_label,
        doctor_value=doctor_value,
        doctor_label=snap.doctor_label,
        my_number=my_number,
        last_fingerprint=snap.fingerprint(),
        last_notified_at=time.time(),
    )
    state.subscriptions[chat_id] = sub
    state.save()

    await update.message.reply_text("Monitoring aktif ✅")
    await update.message.reply_text(render_status(sub, snap))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    client: RSUIIClient = context.application.bot_data["client"]
    sub = state.subscriptions.get(update.effective_chat.id)
    if not sub:
        await update.message.reply_text("Belum ada monitoring. Kirim /start dulu.")
        return
    snap = await client.fetch_snapshot(sub.poli_value, sub.doctor_value)
    await update.message.reply_text(render_status(sub, snap))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    removed = state.subscriptions.pop(update.effective_chat.id, None)
    state.save()
    await update.message.reply_text("Monitoring dihentikan." if removed else "Tidak ada monitoring aktif.")


async def periodic_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["state"]
    client: RSUIIClient = context.application.bot_data["client"]

    for chat_id, sub in list(state.subscriptions.items()):
        try:
            snap = await client.fetch_snapshot(sub.poli_value, sub.doctor_value)
            fp = snap.fingerprint()
            now = time.time()
            changed = fp != sub.last_fingerprint
            force_notify = (now - sub.last_notified_at) >= NOTIFY_FORCE_SECONDS
            if changed or force_notify:
                await context.bot.send_message(chat_id=chat_id, text=render_status(sub, snap))
                sub.last_fingerprint = fp
                sub.last_notified_at = now
                state.save()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Gagal cek antrian chat_id=%s: %s", chat_id, exc)


async def on_shutdown(app: Application) -> None:
    client: RSUIIClient = app.bot_data.get("client")
    if client:
        await client.close()


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN terlebih dulu")

    state = BotState(DATA_FILE)
    state.load()

    app = Application.builder().token(token).post_shutdown(on_shutdown).build()
    app.bot_data["state"] = state
    app.bot_data["client"] = RSUIIClient()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_number))

    app.job_queue.run_repeating(periodic_check, interval=POLL_SECONDS, first=10)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
