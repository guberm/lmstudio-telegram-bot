#!/usr/bin/env python3
"""Admin-only Telegram client for local LM Studio.

Telegram -> this bot -> LM Studio OpenAI-compatible API and an optional
local control script. No hosted OpenAI/Hermes provider is used.
"""
from __future__ import annotations

import asyncio
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
from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_IDS = {int(part) for part in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if part.strip().isdigit()}
BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/")
CONTROL_SCRIPT = Path(os.getenv("LMSTUDIO_CONTROL_SCRIPT", str(ROOT / "lmstudio-control.sh"))).expanduser()
DEFAULT_PROFILE = os.getenv("DEFAULT_PROFILE", "mythosnano").strip() or "mythosnano"
DEFAULT_SYSTEM_PROMPT = os.getenv(
    "DEFAULT_SYSTEM_PROMPT",
    "You are a concise local LM Studio assistant. Answer in the user's language when clear.",
)
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
LMSTUDIO_TIMEOUT_SECONDS = float(os.getenv("LMSTUDIO_TIMEOUT_SECONDS", "600"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
STATE_PATH = Path(os.getenv("STATE_PATH", str(ROOT / "data" / "state.json"))).expanduser()

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
}

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
    async with httpx.AsyncClient(timeout=LMSTUDIO_TIMEOUT_SECONDS) as client:
        r = await client.post(f"{BASE_URL}/chat/completions", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"LM Studio HTTP {r.status_code}: {r.text[:1000]}")
        data = r.json()
    return data["choices"][0]["message"]["content"].strip()


@require_admin
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_state = get_chat_state(update.effective_chat.id)
    await update.effective_message.reply_text(
        "LM Studio bot ready.\n"
        f"Profile: {chat_state.profile}\n"
        f"Chat model: {chat_state.model}\n\n"
        "Send text to chat with LM Studio, or /help for controls."
    )


@require_admin
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Commands:\n"
        "/health - check LM Studio API\n"
        "/profiles - model profiles from script\n"
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
        "/reset - clear chat context\n"
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
        f"Base URL: {BASE_URL}\n"
        f"Control script: {CONTROL_SCRIPT}"
    )


@require_admin
async def set_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /profile mythosnano")
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
    chat_state = get_chat_state(update.effective_chat.id)
    chat_state.history = []
    put_chat_state(update.effective_chat.id, chat_state)
    await update.effective_message.reply_text("Chat context reset.")


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


@require_admin
async def profiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await script_reply(update, context, "models", False)


@require_admin
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await script_reply(update, context, "menu-summary", True)


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
        BotCommand("reset", "reset chat"),
        BotCommand("system", "set system prompt"),
        BotCommand("chatmodel", "set chat model id"),
        BotCommand("run", "raw allowed script action"),
    ]
    # Hide commands from everyone except configured admin chat menus.
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
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("system", system_prompt))
    app.add_handler(CommandHandler("chatmodel", set_chat_model))
    app.add_handler(CommandHandler("run", run_raw))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    return app


if __name__ == "__main__":
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)
