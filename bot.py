#!/usr/bin/env python3
"""Admin-only Telegram client for local LM Studio.

Telegram -> this bot -> LM Studio OpenAI-compatible API and Michael's
lmstudio-control.sh script. No Hermes/OpenAI provider is used.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from PIL import Image
from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_IDS = {int(part) for part in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if part.strip().isdigit()}
BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/")
CHATGPT_WEB_BASE_URL = os.getenv("CHATGPT_WEB_BASE_URL", "https://codex.guber.dev/v1").rstrip("/")
CHATGPT_WEB_MODEL = os.getenv("CHATGPT_WEB_MODEL", "chatgpt-5.5-high-web").strip() or "chatgpt-5.5-high-web"
HERMES_ENV_PATH = Path(os.getenv("HERMES_ENV_PATH", str(Path.home() / ".hermes" / ".env"))).expanduser()
CONTROL_SCRIPT = Path(os.getenv("LMSTUDIO_CONTROL_SCRIPT", "/home/mg/Desktop/LMStudioControl/lmstudio-control.sh")).expanduser()
DEFAULT_PROFILE = os.getenv("DEFAULT_PROFILE", "mythosnano").strip() or "mythosnano"
DEFAULT_SYSTEM_PROMPT = os.getenv(
    "DEFAULT_SYSTEM_PROMPT",
    "Ты личный локальный LM Studio ассистент Michael. Отвечай прямо, полезно и кратко.",
)
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
LMSTUDIO_TIMEOUT_SECONDS = float(os.getenv("LMSTUDIO_TIMEOUT_SECONDS", "600"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
STATE_PATH = Path(os.getenv("STATE_PATH", str(ROOT / "data" / "state.json"))).expanduser()
VISION_MAX_DIMENSION = int(os.getenv("VISION_MAX_DIMENSION", "768"))
VISION_JPEG_QUALITY = int(os.getenv("VISION_JPEG_QUALITY", "82"))
VISION_MAX_BYTES = int(os.getenv("VISION_MAX_BYTES", str(220 * 1024)))

PROFILES: dict[str, str] = {
    "gemma4unc": "gemma4unc",
    "uncensored": "oymuncq4",
    "openyourmind": "oymuncq4",
    "oym": "oymuncq4",
    "coder": "gemma4coderq4",
    "coderq4": "gemma4coderq4",
    "coderq3": "gemma4coderq3",
    "qwythos": "qwythos9bq5",
    "qwythos9b": "qwythos9bq5",
    "mythosnano": "mythosnanoq6",
    "mythos": "mythosnanoq6",
    "nano": "mythosnanoq6",
    "qwenvisionunc": "qwenvl3bunc",
    "qwenvision": "qwenvl3bunc",
    "qwenvision3b": "qwenvl3bunc",
    "qwenvl": "qwenvl3bunc",
    "qwenvl3b": "qwenvl3bunc",
    "qwenvl3bunc": "qwenvl3bunc",
    "cyberneurova": "qwenvl3bunc",
    "chatgptweb": CHATGPT_WEB_MODEL,
    "chatgpt_web": CHATGPT_WEB_MODEL,
    "chatgpt": CHATGPT_WEB_MODEL,
    "chatgpt-5.5-high-web": CHATGPT_WEB_MODEL,
    "codexguber": CHATGPT_WEB_MODEL,
}

PROFILE_MENU: list[tuple[str, str]] = [
    ("gemma4unc", "Gemma4Unc"),
    ("uncensored", "OpenYourMind"),
    ("coder", "Coder Q4"),
    ("coderq3", "Coder Q3"),
    ("qwythos", "Qwythos Q5"),
    ("mythosnano", "Mythos Nano"),
    ("qwenvisionunc", "Qwen VL 3B"),
    ("chatgptweb", "ChatGPT Web"),
]

SCRIPT_ACTIONS_WITH_PROFILE = {
    "summary": "menu-summary",
    "menu-summary": "menu-summary",
    "status": "status",
    "load": "load-model",
    "load-model": "load-model",
    "unload": "unload-model",
    "unload-model": "unload-model",
    "start-public": "start-public",
    "stop-public": "stop-public",
}
SCRIPT_ACTIONS_NO_PROFILE = {
    "profiles": "models",
    "models": "models",
    "ngrok-status": "ngrok-status",
    "ngrok": "ngrok-status",
    "ngrok-address": "ngrok-address",
    "url": "ngrok-address",
    "start-ngrok": "start-ngrok",
    "stop-ngrok": "stop-ngrok",
    "help": "help",
}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("lmstudio-telegram-bot")


@dataclass
class ChatState:
    profile: str = DEFAULT_PROFILE
    model: str = field(default_factory=lambda: profile_to_model(DEFAULT_PROFILE))
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    history: list[dict[str, str]] = field(default_factory=list)


def profile_to_model(value: str) -> str:
    key = (value or DEFAULT_PROFILE).strip().lower()
    return PROFILES.get(key, value.strip() or PROFILES.get(DEFAULT_PROFILE, DEFAULT_PROFILE))


def is_external_profile(profile: str | None) -> bool:
    return normalize_profile(profile) in {"chatgptweb", "chatgpt_web", "chatgpt", "chatgpt-5.5-high-web", "codexguber"}


def profile_base_url(profile: str | None) -> str:
    return CHATGPT_WEB_BASE_URL if is_external_profile(profile) else BASE_URL


def read_env_value(path: Path, key: str) -> str:
    try:
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            left, right = line.split("=", 1)
            if left.strip() == key:
                return right.strip().strip('"').strip("'")
    except FileNotFoundError:
        return ""
    except Exception:
        log.exception("Failed reading env value %s from %s", key, path)
    return ""


def auth_headers_for_profile(profile: str | None) -> dict[str, str]:
    if not is_external_profile(profile):
        return {}
    key = os.getenv("CHATGPT_WEB_PROVIDER_API_KEY", "").strip() or read_env_value(HERMES_ENV_PATH, "CHATGPT_WEB_PROVIDER_API_KEY")
    if not key:
        raise RuntimeError(f"CHATGPT_WEB_PROVIDER_API_KEY is missing in environment or {HERMES_ENV_PATH}")
    return {"Authorization": "Bearer " + key}


def prepare_image_for_lmstudio(image_bytes: bytes, image_mime: str) -> tuple[bytes, str]:
    mime = (image_mime or "image/jpeg").lower()
    if not mime.startswith("image/"):
        mime = "image/jpeg"

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            elif img.mode == "L":
                img = img.convert("RGB")

            width, height = img.size
            max_dim = max(width, height)
            if max_dim > VISION_MAX_DIMENSION:
                scale = VISION_MAX_DIMENSION / float(max_dim)
                img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)

            quality = max(45, min(95, VISION_JPEG_QUALITY))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=quality, optimize=True)
            data = out.getvalue()

            while len(data) > VISION_MAX_BYTES and quality > 45:
                quality -= 7
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=quality, optimize=True)
                data = out.getvalue()

            if len(data) > VISION_MAX_BYTES:
                current = img
                for shrink in (0.85, 0.75, 0.65):
                    resized = current.resize((max(1, int(current.width * shrink)), max(1, int(current.height * shrink))), Image.Resampling.LANCZOS)
                    out = io.BytesIO()
                    resized.save(out, format="JPEG", quality=max(45, quality), optimize=True)
                    data = out.getvalue()
                    current = resized
                    if len(data) <= VISION_MAX_BYTES:
                        break
            return data, "image/jpeg"
    except Exception:
        log.exception("Failed to preprocess image for LM Studio; using original bytes")
        return image_bytes, mime


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"chats": {}}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        log.exception("Failed to load state")
        return {"chats": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_PATH)


def get_chat_state(chat_id: int) -> ChatState:
    state = load_state()
    raw = state.setdefault("chats", {}).setdefault(str(chat_id), {})
    return ChatState(
        profile=raw.get("profile") or DEFAULT_PROFILE,
        model=raw.get("model") or profile_to_model(raw.get("profile") or DEFAULT_PROFILE),
        system_prompt=raw.get("system_prompt") or DEFAULT_SYSTEM_PROMPT,
        history=raw.get("history") or [],
    )


def put_chat_state(chat_id: int, chat_state: ChatState) -> None:
    state = load_state()
    state.setdefault("chats", {})[str(chat_id)] = {
        "profile": chat_state.profile,
        "model": chat_state.model,
        "system_prompt": chat_state.system_prompt,
        "history": chat_state.history[-MAX_HISTORY_MESSAGES:],
        "updated_at": int(time.time()),
    }
    save_state(state)


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


def require_admin(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update):
            if update.effective_message:
                await update.effective_message.reply_text("Access denied.")
            log.warning("Denied user_id=%s chat_id=%s", getattr(update.effective_user, "id", None), getattr(update.effective_chat, "id", None))
            return
        return await func(update, context)

    return wrapper


def chunks(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    while text:
        cut = min(limit, len(text))
        if cut < len(text):
            nl = text.rfind("\n", 0, cut)
            if nl > 1000:
                cut = nl
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return out


def compact_output(text: str, limit: int = 3600) -> str:
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text or "")
    text = re.sub(r"[⠁-⣿]+", "", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    if len(text) <= limit:
        return text or "OK"
    head = text[:900].rstrip()
    tail = text[-(limit - len(head) - 80) :].lstrip()
    return f"{head}\n\n...[trimmed {len(text) - len(head) - len(tail)} chars]...\n\n{tail}"


def format_exception(exc: BaseException) -> str:
    detail = str(exc).strip()
    name = type(exc).__name__
    if isinstance(exc, httpx.TimeoutException):
        return f"{name}: LM Studio did not answer within {LMSTUDIO_TIMEOUT_SECONDS:.0f}s"
    if not detail:
        return name
    return f"{name}: {detail}"


def normalize_profile(value: str | None) -> str:
    return (value or DEFAULT_PROFILE).strip().lower() or DEFAULT_PROFILE


def profile_list_text(chat_state: ChatState) -> str:
    return (
        "Profiles - tap a model to open actions.\n"
        f"Current chat profile: {chat_state.profile}\n"
        f"Current chat model: {chat_state.model}"
    )


def profile_action_text(chat_state: ChatState, profile: str) -> str:
    model = profile_to_model(profile)
    marker = "yes" if normalize_profile(chat_state.profile) == profile else "no"
    return (
        f"Profile: {profile}\n"
        f"Model: {model}\n"
        f"Selected for chat: {marker}\n\n"
        "Choose action:"
    )


def profile_list_keyboard(current_profile: str) -> InlineKeyboardMarkup:
    rows = []
    current = normalize_profile(current_profile)
    for key, label in PROFILE_MENU:
        prefix = "✅ " if key == current else ""
        rows.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"prof:show:{key}")])
    rows.append([
        InlineKeyboardButton("New session", callback_data="prof:newsession"),
        InlineKeyboardButton("Refresh", callback_data="prof:refresh"),
    ])
    return InlineKeyboardMarkup(rows)


def profile_action_keyboard(profile: str, current_profile: str) -> InlineKeyboardMarkup:
    selected_label = "✅ Use for chat" if normalize_profile(current_profile) == profile else "Use for chat"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Load", callback_data=f"prof:load:{profile}"),
            InlineKeyboardButton("Unload", callback_data=f"prof:unload:{profile}"),
        ],
        [
            InlineKeyboardButton("Start public", callback_data=f"prof:start:{profile}"),
            InlineKeyboardButton("Stop public", callback_data=f"prof:stop:{profile}"),
        ],
        [
            InlineKeyboardButton("Status", callback_data=f"prof:status:{profile}"),
            InlineKeyboardButton(selected_label, callback_data=f"prof:set:{profile}"),
        ],
        [InlineKeyboardButton("New session", callback_data=f"prof:newsession:{profile}")],
        [InlineKeyboardButton("← Back", callback_data="prof:back")],
    ])


async def run_control(action: str, profile: str | None = None, timeout: int | None = None) -> tuple[int, str]:
    if not CONTROL_SCRIPT.exists():
        return 127, f"Control script not found: {CONTROL_SCRIPT}"
    cmd = [str(CONTROL_SCRIPT), action]
    if profile:
        cmd.append(profile)
    env = os.environ.copy()
    env.update({"NO_PAUSE": "1", "TERM": "dumb", "NO_COLOR": "1", "LMS_NO_COLOR": "1"})
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout or 900)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"Timed out running: {' '.join(cmd)}"
    text = (stdout or b"").decode(errors="replace")
    err = (stderr or b"").decode(errors="replace")
    return proc.returncode or 0, compact_output((text + ("\n" + err if err else "")).strip())


async def loaded_lmstudio_models() -> list[str]:
    """Return identifiers from `lms ps`, not just /v1/models availability."""
    lms = Path.home() / ".lmstudio" / "bin" / "lms"
    if not lms.exists():
        return []
    proc = await asyncio.create_subprocess_exec(
        str(lms), "ps",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return []
    text = stdout.decode(errors="replace")
    models: list[str] = []
    in_rows = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("IDENTIFIER") and "MODEL" in stripped and "STATUS" in stripped:
            in_rows = True
            continue
        if not in_rows or not stripped:
            continue
        if stripped.startswith("No models are currently loaded"):
            return []
        parts = stripped.split()
        if parts:
            models.append(parts[0])
    return models


async def choose_chat_model(chat_state: ChatState) -> str:
    wanted = profile_to_model(chat_state.profile)
    if is_external_profile(chat_state.profile):
        return wanted
    loaded = await loaded_lmstudio_models()
    if wanted in loaded:
        return wanted
    if chat_state.model in loaded:
        return chat_state.model
    if len(loaded) == 1:
        return loaded[0]

    # Selected profile is not loaded (or several unrelated models are loaded).
    # Load the selected profile so plain chat targets the expected model instead
    # of an arbitrary /v1/models entry.
    code, output = await run_control("load-model", chat_state.profile, timeout=900)
    if code != 0:
        raise RuntimeError(f"Could not load profile {chat_state.profile}: {output}")
    loaded = await loaded_lmstudio_models()
    return wanted if wanted in loaded else (loaded[0] if loaded else wanted)


async def reply_long(update: Update, text: str) -> None:
    assert update.effective_message
    for part in chunks(text):
        await update.effective_message.reply_text(part, disable_web_page_preview=True)


async def send_long(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, reply_to_message_id: int | None = None) -> None:
    for part in chunks(text):
        await context.bot.send_message(
            chat_id=chat_id,
            text=part,
            reply_to_message_id=reply_to_message_id,
            disable_web_page_preview=True,
        )


async def lmstudio_models() -> list[str]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{BASE_URL}/models")
        r.raise_for_status()
        data = r.json()
        return [item.get("id", "") for item in data.get("data", []) if item.get("id")]


async def chat_completion(chat_state: ChatState, user_text: str) -> str:
    chat_state.model = await choose_chat_model(chat_state)
    messages = [{"role": "system", "content": chat_state.system_prompt}]
    messages.extend(chat_state.history[-MAX_HISTORY_MESSAGES:])
    messages.append({"role": "user", "content": user_text})
    payload = {
        "model": chat_state.model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 768,
        "stream": False,
    }
    base_url = profile_base_url(chat_state.profile)
    headers = auth_headers_for_profile(chat_state.profile)
    async with httpx.AsyncClient(timeout=LMSTUDIO_TIMEOUT_SECONDS) as client:
        r = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"Provider HTTP {r.status_code}: {r.text[:1000]}")
        data = r.json()
    return data["choices"][0]["message"]["content"].strip()


async def image_chat_completion(chat_state: ChatState, prompt: str, image_bytes: bytes, image_mime: str = "image/jpeg") -> str:
    chat_state.model = await choose_chat_model(chat_state)
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    image_data_url = f"data:{image_mime};base64,{image_b64}"
    messages: list[dict[str, Any]] = [{"role": "system", "content": chat_state.system_prompt}]
    messages.extend(chat_state.history[-MAX_HISTORY_MESSAGES:])
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        }
    )
    payload = {
        "model": chat_state.model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 768,
        "stream": False,
    }
    base_url = profile_base_url(chat_state.profile)
    headers = auth_headers_for_profile(chat_state.profile)
    async with httpx.AsyncClient(timeout=LMSTUDIO_TIMEOUT_SECONDS) as client:
        r = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"Provider HTTP {r.status_code}: {r.text[:1000]}")
        data = r.json()
    return data["choices"][0]["message"]["content"].strip()


@require_admin
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_state = get_chat_state(update.effective_chat.id)
    await update.effective_message.reply_text(
        "LM Studio bot ready.\n"
        f"Profile: {chat_state.profile}\n"
        f"Chat model: {chat_state.model}\n\n"
        "Send text to chat with LM Studio, /profiles for buttons, or /help for controls."
    )


@require_admin
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Commands:\n"
        "/health - check LM Studio API\n"
        "/profiles - buttons for model actions\n"
        "/profile <key> - set default script/chat profile\n"
        "/current - current bot state\n"
        "/summary - script menu summary\n"
        "/status [profile] - LM Studio/script status\n"
        "/load [profile] - load model\n"
        "/unload [profile] - unload model\n"
        "/start_public [profile] - load + ngrok\n"
        "/stop_public [profile] - stop public/ngrok + unload\n"
        "/ngrok - ngrok status\n"
        "/url - current ngrok URL\n"
        "/start_ngrok /stop_ngrok - tunnel only\n"
        "/new_session or /new - clear chat context, keep selected profile/model\n"
        "/reset - alias for /new_session\n"
        "/system <prompt> - set system prompt\n"
        "/chatmodel <model-or-profile> - set model id for chat\n"
        "/run <script-action> [profile] - raw allowed script action\n\n"
        "Plain text = chat with LM Studio. Access is admin-only."
    )


@require_admin
async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        models = await lmstudio_models()
        await update.effective_message.reply_text(f"LM Studio API OK\nBase: {BASE_URL}\nModels: {', '.join(models[:20]) or 'none'}")
    except Exception as exc:
        await update.effective_message.reply_text(f"LM Studio API failed: {exc}")


@require_admin
async def current(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_state = get_chat_state(update.effective_chat.id)
    await update.effective_message.reply_text(
        f"Profile: {chat_state.profile}\n"
        f"Chat model: {chat_state.model}\n"
        f"History messages: {len(chat_state.history)}\n"
        f"Base URL: {profile_base_url(chat_state.profile)}\n"
        f"Control script: {CONTROL_SCRIPT}"
    )


@require_admin
async def set_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /profile qwenvisionunc")
        return
    profile = context.args[0].strip().lower()
    chat_state = get_chat_state(update.effective_chat.id)
    chat_state.profile = profile
    chat_state.model = profile_to_model(profile)
    put_chat_state(update.effective_chat.id, chat_state)
    await update.effective_message.reply_text(f"Profile set: {profile}\nChat model: {chat_state.model}")


@require_admin
async def set_chat_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /chatmodel mythosnanoq6")
        return
    value = context.args[0].strip()
    chat_state = get_chat_state(update.effective_chat.id)
    chat_state.model = profile_to_model(value)
    put_chat_state(update.effective_chat.id, chat_state)
    await update.effective_message.reply_text(f"Chat model set: {chat_state.model}")


@require_admin
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    chat_state = get_chat_state(update.effective_chat.id)
    old_count = len(chat_state.history)
    chat_state.history = []
    put_chat_state(update.effective_chat.id, chat_state)
    await update.effective_message.reply_text(
        f"New session started. Cleared {old_count} history message(s).\n"
        f"Profile: {chat_state.profile}\n"
        f"Chat model: {chat_state.model}"
    )


@require_admin
async def system_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text or ""
    prompt = text.partition(" ")[2].strip()
    if not prompt:
        await update.effective_message.reply_text("Usage: /system <prompt>")
        return
    chat_state = get_chat_state(update.effective_chat.id)
    chat_state.system_prompt = prompt
    put_chat_state(update.effective_chat.id, chat_state)
    await update.effective_message.reply_text("System prompt updated.")


async def script_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    use_profile: bool = False,
    background: bool = False,
    remember_profile: bool = False,
    profile_override: str | None = None,
) -> None:
    chat_id = update.effective_chat.id
    message_id = update.effective_message.message_id if update.effective_message else None
    chat_state = get_chat_state(chat_id)
    profile = profile_override or (context.args[0].strip() if context.args else chat_state.profile)
    if remember_profile and use_profile and profile:
        chat_state.profile = profile.strip().lower()
        chat_state.model = profile_to_model(chat_state.profile)
        put_chat_state(chat_id, chat_state)

    async def finish() -> None:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            code, output = await run_control(action, profile if use_profile else None)
            prefix = "OK" if code == 0 else f"FAILED exit={code}"
            await send_long(
                context,
                chat_id,
                f"{prefix}: {action}{' ' + profile if use_profile else ''}\n\n{output}",
                reply_to_message_id=message_id,
            )
        except Exception as exc:
            log.exception("script action failed action=%s profile=%s", action, profile if use_profile else None)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"FAILED: {action}{' ' + profile if use_profile else ''}\n\n{exc}",
                reply_to_message_id=message_id,
            )

    if background:
        await update.effective_message.reply_text(
            f"Started: {action}{' ' + profile if use_profile else ''}\nI'll send the result here when it finishes."
        )
        asyncio.create_task(finish())
        return

    await update.effective_chat.send_action(ChatAction.TYPING)
    await finish()


async def reply_profile_list(update: Update) -> None:
    chat_state = get_chat_state(update.effective_chat.id)
    await update.effective_message.reply_text(
        profile_list_text(chat_state),
        reply_markup=profile_list_keyboard(chat_state.profile),
    )


async def run_profile_callback_action(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    profile: str,
    *,
    remember_profile: bool = False,
    background: bool = False,
) -> None:
    chat_id = query.message.chat.id
    reply_to_message_id = query.message.message_id
    profile = normalize_profile(profile)
    if remember_profile:
        chat_state = get_chat_state(chat_id)
        chat_state.profile = profile
        chat_state.model = profile_to_model(profile)
        put_chat_state(chat_id, chat_state)

    async def finish() -> None:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            code, output = await run_control(action, profile, timeout=900)
            prefix = "OK" if code == 0 else f"FAILED exit={code}"
            await send_long(
                context,
                chat_id,
                f"{prefix}: {action} {profile}\n\n{output}",
                reply_to_message_id=reply_to_message_id,
            )
        except Exception as exc:
            log.exception("profile callback action failed action=%s profile=%s", action, profile)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"FAILED: {action} {profile}\n\n{format_exception(exc)}",
                reply_to_message_id=reply_to_message_id,
            )

    if background:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Started: {action} {profile}\nI'll send the result here when it finishes.",
            reply_to_message_id=reply_to_message_id,
        )
        asyncio.create_task(finish())
        return

    await finish()


@require_admin
async def profiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_profile_list(update)


@require_admin
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await script_reply(update, context, "menu-summary", True)


@require_admin
async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_chat:
        return
    await query.answer()
    data = query.data or ""
    parts = data.split(":", 2)
    if len(parts) < 2 or parts[0] != "prof":
        return

    chat_id = update.effective_chat.id
    chat_state = get_chat_state(chat_id)
    verb = parts[1]
    profile = normalize_profile(parts[2]) if len(parts) > 2 else chat_state.profile

    if verb in {"refresh", "back"}:
        await query.edit_message_text(
            profile_list_text(chat_state),
            reply_markup=profile_list_keyboard(chat_state.profile),
        )
        return
    if verb == "show":
        await query.edit_message_text(
            profile_action_text(chat_state, profile),
            reply_markup=profile_action_keyboard(profile, chat_state.profile),
        )
        return
    if verb == "set":
        chat_state.profile = profile
        chat_state.model = profile_to_model(profile)
        put_chat_state(chat_id, chat_state)
        chat_state = get_chat_state(chat_id)
        await query.edit_message_text(
            profile_action_text(chat_state, profile),
            reply_markup=profile_action_keyboard(profile, chat_state.profile),
        )
        return
    if verb == "newsession":
        old_count = len(chat_state.history)
        chat_state.history = []
        put_chat_state(chat_id, chat_state)
        text = (
            f"New session started. Cleared {old_count} history message(s).\n"
            f"Profile: {chat_state.profile}\n"
            f"Chat model: {chat_state.model}"
        )
        if len(parts) > 2:
            await query.edit_message_text(
                text + "\n\n" + profile_action_text(chat_state, profile),
                reply_markup=profile_action_keyboard(profile, chat_state.profile),
            )
        else:
            await query.edit_message_text(
                text + "\n\n" + profile_list_text(chat_state),
                reply_markup=profile_list_keyboard(chat_state.profile),
            )
        return

    action_map = {
        "load": ("load-model", True, True),
        "unload": ("unload-model", False, True),
        "start": ("start-public", True, True),
        "stop": ("stop-public", False, True),
        "status": ("status", False, False),
    }
    if verb not in action_map:
        return
    action, remember_profile, background = action_map[verb]
    await run_profile_callback_action(
        query,
        context,
        action,
        profile,
        remember_profile=remember_profile,
        background=background,
    )
    chat_state = get_chat_state(chat_id)
    await query.edit_message_text(
        profile_action_text(chat_state, profile),
        reply_markup=profile_action_keyboard(profile, chat_state.profile),
    )


@require_admin
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await script_reply(update, context, "status", True)


@require_admin
async def load_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await script_reply(update, context, "load-model", True, background=True, remember_profile=True)


@require_admin
async def unload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await script_reply(update, context, "unload-model", True, background=True)


@require_admin
async def start_public(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await script_reply(update, context, "start-public", True, background=True, remember_profile=True)


@require_admin
async def stop_public(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await script_reply(update, context, "stop-public", True, background=True)


@require_admin
async def ngrok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await script_reply(update, context, "ngrok-status", False)


@require_admin
async def url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await script_reply(update, context, "ngrok-address", False)


@require_admin
async def start_ngrok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await script_reply(update, context, "start-ngrok", False)


@require_admin
async def stop_ngrok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await script_reply(update, context, "stop-ngrok", False)


@require_admin
async def run_raw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /run <action> [profile]")
        return
    requested = context.args[0].strip().lower().replace("_", "-")
    profile = context.args[1].strip() if len(context.args) > 1 else get_chat_state(update.effective_chat.id).profile
    if requested in SCRIPT_ACTIONS_WITH_PROFILE:
        action = SCRIPT_ACTIONS_WITH_PROFILE[requested]
        is_long = action in {"load-model", "unload-model", "start-public", "stop-public"}
        remember = action in {"load-model", "start-public"}
        await script_reply(
            update,
            context,
            action,
            True,
            background=is_long,
            remember_profile=remember,
            profile_override=profile,
        )
    elif requested in SCRIPT_ACTIONS_NO_PROFILE:
        action = SCRIPT_ACTIONS_NO_PROFILE[requested]
        await script_reply(update, context, action, False)
    else:
        allowed = sorted(set(SCRIPT_ACTIONS_WITH_PROFILE) | set(SCRIPT_ACTIONS_NO_PROFILE))
        await update.effective_message.reply_text("Allowed actions:\n" + ", ".join(allowed))


@require_admin
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    chat_id = update.effective_chat.id
    message_id = update.effective_message.message_id
    initial_state = get_chat_state(chat_id)
    await update.effective_message.reply_text(
        f"Thinking with {initial_state.model}...\nI'll send the answer here when it finishes."
    )

    async def finish() -> None:
        chat_state = get_chat_state(chat_id)
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            answer = await chat_completion(chat_state, text)
        except Exception as exc:
            log.exception("LM Studio chat failed chat_id=%s model=%s", chat_id, getattr(chat_state, "model", None))
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"LM Studio chat failed: {format_exception(exc)}",
                reply_to_message_id=message_id,
            )
            return
        chat_state.history.extend([
            {"role": "user", "content": text},
            {"role": "assistant", "content": answer},
        ])
        chat_state.history = chat_state.history[-MAX_HISTORY_MESSAGES:]
        put_chat_state(chat_id, chat_state)
        await send_long(context, chat_id, answer, reply_to_message_id=message_id)

    asyncio.create_task(finish())


@require_admin
async def chat_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    photo = message.photo[-1] if message.photo else None
    document = message.document if message.document and (message.document.mime_type or "").startswith("image/") else None
    if not photo and not document:
        return

    prompt = (message.caption or "").strip() or "Опиши, что на изображении."
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    message_id = message.message_id
    initial_state = get_chat_state(chat_id)
    await message.reply_text(
        f"Analyzing image with {initial_state.model}...\nI'll send the answer here when it finishes."
    )

    async def finish() -> None:
        chat_state = get_chat_state(chat_id)
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            source = photo if photo is not None else document
            if source is None:
                raise RuntimeError("No image source found in Telegram message")
            image_mime = "image/jpeg" if photo is not None else ((document.mime_type or "image/jpeg") if document else "image/jpeg")
            telegram_file = await source.get_file()
            image_bytes = bytes(await telegram_file.download_as_bytearray())
            image_bytes, image_mime = prepare_image_for_lmstudio(image_bytes, image_mime)
            answer = await image_chat_completion(chat_state, prompt, image_bytes, image_mime=image_mime)
        except Exception as exc:
            log.exception("LM Studio image chat failed chat_id=%s model=%s", chat_id, getattr(chat_state, "model", None))
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"LM Studio image chat failed: {format_exception(exc)}",
                reply_to_message_id=message_id,
            )
            return
        chat_state.history.extend([
            {"role": "user", "content": f"[Image attached] {prompt}"},
            {"role": "assistant", "content": answer},
        ])
        chat_state.history = chat_state.history[-MAX_HISTORY_MESSAGES:]
        put_chat_state(chat_id, chat_state)
        await send_long(context, chat_id, answer, reply_to_message_id=message_id)

    asyncio.create_task(finish())


async def post_init(app: Application) -> None:
    commands = [
        BotCommand("start", "start"),
        BotCommand("help", "commands"),
        BotCommand("health", "check LM Studio"),
        BotCommand("profiles", "list model profiles"),
        BotCommand("profile", "set profile"),
        BotCommand("current", "current state"),
        BotCommand("summary", "script summary"),
        BotCommand("status", "status"),
        BotCommand("load", "load model"),
        BotCommand("unload", "unload model"),
        BotCommand("start_public", "load + ngrok"),
        BotCommand("stop_public", "stop public + unload"),
        BotCommand("ngrok", "ngrok status"),
        BotCommand("url", "current ngrok URL"),
        BotCommand("new_session", "new chat session"),
        BotCommand("reset", "reset chat"),
        BotCommand("system", "set system prompt"),
        BotCommand("chatmodel", "set chat model id"),
        BotCommand("run", "raw allowed script action"),
    ]
    # Hide commands from everyone except Michael/admin chat menus.
    await app.bot.delete_my_commands()
    for admin_id in ADMIN_IDS:
        await app.bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=admin_id))
    me = await app.bot.get_me()
    log.info("Bot ready username=%s admin_ids=%s base_url=%s", me.username, sorted(ADMIN_IDS), BASE_URL)


def build_app() -> Application:
    if not TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")
    if not ADMIN_IDS:
        raise SystemExit("Missing ADMIN_IDS in .env")
    app = Application.builder().token(TOKEN).post_init(post_init).concurrent_updates(4).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("profiles", profiles))
    app.add_handler(CommandHandler("models", profiles))
    app.add_handler(CommandHandler("profile", set_profile))
    app.add_handler(CommandHandler("current", current))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("load", load_cmd))
    app.add_handler(CommandHandler("unload", unload_cmd))
    app.add_handler(CommandHandler("start_public", start_public))
    app.add_handler(CommandHandler("stop_public", stop_public))
    app.add_handler(CommandHandler("ngrok", ngrok))
    app.add_handler(CommandHandler("url", url))
    app.add_handler(CommandHandler("start_ngrok", start_ngrok))
    app.add_handler(CommandHandler("stop_ngrok", stop_ngrok))
    app.add_handler(CommandHandler("new_session", reset))
    app.add_handler(CommandHandler("new", reset))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("system", system_prompt))
    app.add_handler(CommandHandler("chatmodel", set_chat_model))
    app.add_handler(CommandHandler("run", run_raw))
    app.add_handler(CallbackQueryHandler(profile_callback, pattern=r"^prof:"))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND, chat_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    return app


if __name__ == "__main__":
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)
