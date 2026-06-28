# LM Studio Telegram Bot

Admin-only Telegram client for a local LM Studio instance.

```text
Telegram -> bot.py -> LM Studio OpenAI-compatible API
                 └-> lmstudio-control.sh for model/ngrok controls
```

This bot is intentionally **not** a Hermes/OpenAI client. It talks only to:

- LM Studio API: `http://127.0.0.1:1234/v1`
- Control script: `/home/mg/Desktop/LMStudioControl/lmstudio-control.sh`

The live Michael deployment currently uses bot username `@mg_lmstudio_client_bot` and is restricted to Michael's Telegram user id via `ADMIN_IDS`.

## Features

- Admin-only Telegram bot using polling - no webhook/domain/TLS needed.
- Plain text chat via LM Studio `/v1/chat/completions`.
- Photo and image-document analysis via LM Studio vision-capable models.
- Per-chat conversation history in local JSON state.
- Configurable system prompt.
- Uses `lms ps` to prefer the selected **loaded** model; if no model is loaded, it loads the selected profile.
- Exposes the useful `lmstudio-control.sh` actions through safe Telegram commands.
- Long model operations (`/load`, `/unload`, `/start_public`, `/stop_public`) acknowledge immediately, run in the background, and post the final result when finished.
- Plain text chat also acknowledges immediately and posts the model answer later, so slow LM Studio generations do not look like a dead bot.
- Supports the uncensored vision profile `qwenvisionunc` -> `qwenvl3bunc`.
- Supports external profile `chatgptweb` -> `chatgpt-5.5-high-web` via `https://codex.guber.dev/v1` using `CHATGPT_WEB_PROVIDER_API_KEY` from the Linux host environment/Hermes `.env`.
- Downscales/compresses Telegram images before LM Studio vision requests to fit low-VRAM hosts more reliably.
- Chat timeouts report the exception class/details instead of a blank `LM Studio chat failed:` message.
- Processes multiple Telegram updates concurrently so `/start`/`/status` are not stuck behind a model load.
- `/profiles` opens inline buttons so Michael can tap a model, then tap Load / Unload / Start public / Stop public / Status / Use for chat.
- User systemd service + `flock` wrapper for restartable deployment.
- Telegram commands are scoped only to configured admin chat IDs.

## Repository safety

Do **not** commit secrets or runtime state.

Ignored by `.gitignore`:

- `.env` / `*.env`
- `.venv/`
- `data/`
- `logs/`
- Python caches

Commit only `.env.example` with placeholder values.

## Requirements

- Linux host running LM Studio with local API enabled on port `1234`.
- `lmstudio-control.sh` available, by default:

  ```bash
  /home/mg/Desktop/LMStudioControl/lmstudio-control.sh
  ```

- Python 3.11+.
- `uv` or Python venv tooling.
- Telegram BotFather token.
- Telegram numeric user id for `ADMIN_IDS`.

## Installation

### 1. Clone

```bash
git clone https://github.com/guberm/lmstudio-telegram-bot.git ~/lmstudio-telegram-bot
cd ~/lmstudio-telegram-bot
```

### 2. Create venv and install deps

Using `uv`:

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

Or with stdlib venv:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 3. Configure `.env`

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Required fields:

```env
TELEGRAM_BOT_TOKEN=REPLACE_WITH_BOTFATHER_TOKEN
ADMIN_IDS=29990301
LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1
LMSTUDIO_CONTROL_SCRIPT=/home/mg/Desktop/LMStudioControl/lmstudio-control.sh
DEFAULT_PROFILE=mythosnano
DEFAULT_SYSTEM_PROMPT=Ты личный локальный LM Studio ассистент Michael. Отвечай прямо, полезно и кратко. Если пользователь пишет по-русски, отвечай по-русски.
MAX_HISTORY_MESSAGES=20
LMSTUDIO_TIMEOUT_SECONDS=600
LOG_LEVEL=INFO
```

Notes:

- `TELEGRAM_BOT_TOKEN` comes from `@BotFather`.
- `ADMIN_IDS` is a comma/space-separated allowlist of Telegram numeric user IDs.
- Users not in `ADMIN_IDS` receive `Access denied.`
- The bot uses polling, so Telegram only needs outbound internet from the host.

### 4. Verify LM Studio

Make sure LM Studio is running and local API responds:

```bash
curl -fsS http://127.0.0.1:1234/v1/models | python3 -m json.tool | head
```

Verify the control script:

```bash
NO_PAUSE=1 /home/mg/Desktop/LMStudioControl/lmstudio-control.sh status mythosnano
```

### 5. Run manually

```bash
./start.sh
```

Or foreground for debugging:

```bash
source .venv/bin/activate
python bot.py
```

### 6. Install as user systemd service

Copy the template:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/lmstudio-telegram-bot.service ~/.config/systemd/user/lmstudio-telegram-bot.service
systemctl --user daemon-reload
systemctl --user enable --now lmstudio-telegram-bot.service
```

Check status/logs:

```bash
systemctl --user status lmstudio-telegram-bot.service --no-pager
journalctl --user -u lmstudio-telegram-bot.service -n 100 --no-pager
tail -100 ~/lmstudio-telegram-bot/logs/bot.log
```

If you need auto-start without an active desktop login:

```bash
loginctl enable-linger "$USER"
```

## Telegram usage

Open your bot in Telegram and press `/start`.

Plain text messages are sent to LM Studio chat completions.

### Main commands

| Command | Purpose |
|---|---|
| `/start` | Start/check the bot |
| `/help` | Show command list |
| `/health` | Check LM Studio API and available models |
| `/profiles` or `/models` | Show model buttons and open per-profile actions |
| `/profile <key>` | Set selected profile and chat model mapping |
| `/current` | Show current profile/model/state |
| `/reset` | Clear chat history |
| `/system <prompt>` | Set system prompt for this chat |
| `/chatmodel <model-or-profile>` | Override chat model id/profile |

### LM Studio script controls

| Command | Maps to script action |
|---|---|
| `/summary` | `menu-summary <profile>` |
| `/status [profile]` | `status <profile>` |
| `/load [profile]` | `load-model <profile>` |
| `/unload [profile]` | `unload-model <profile>` |
| `/start_public [profile]` | `start-public <profile>` |
| `/stop_public [profile]` | `stop-public <profile>` |
| `/ngrok` | `ngrok-status` |
| `/url` | `ngrok-address` |
| `/start_ngrok` | `start-ngrok` |
| `/stop_ngrok` | `stop-ngrok` |
| `/run <action> [profile]` | Run an allowlisted raw script action |

Allowed `/run` actions are intentionally allowlisted in `bot.py`.

## Model/profile mapping

The bot maps friendly profile keys to LM Studio model IDs:

| Profile key | Model id |
|---|---|
| `mythosnano`, `mythos`, `nano` | `mythosnanoq6` |
| `qwenvisionunc`, `qwenvision`, `qwenvision3b`, `qwenvl`, `qwenvl3b`, `qwenvl3bunc`, `cyberneurova` | `qwenvl3bunc` |
| `qwythos`, `qwythos9b` | `qwythos9bq5` |
| `coder`, `coderq4` | `gemma4coderq4` |
| `coderq3` | `gemma4coderq3` |
| `uncensored`, `openyourmind`, `oym` | `oymuncq4` |
| `gemma4unc` | `gemma4unc` |

For chat, the bot checks `lms ps` and uses the selected loaded model. If nothing suitable is loaded, it runs `load-model <profile>` first.

## Operational notes

- This bot is safest when run on the same host as LM Studio and pointed at `127.0.0.1`.
- Do not point the bot at a public ngrok URL unless you intentionally want remote LM Studio access.
- `start_public`/`stop_public` can change ngrok URLs.
- `/profiles` is the удобный flow: tap a profile, then tap the action button instead of typing `/load <profile>`.
- Logs are in `logs/bot.log`; runtime chat state is in `data/state.json`.
- Both are local runtime files and intentionally ignored by git.

## Verification checklist

After install/update:

```bash
source .venv/bin/activate
python -m py_compile bot.py
python - <<'PY'
import bot
print(bot.BASE_URL, sorted(bot.ADMIN_IDS), bot.profile_to_model('mythosnano'))
PY
curl -fsS http://127.0.0.1:1234/v1/models >/dev/null
systemctl --user restart lmstudio-telegram-bot.service
systemctl --user is-active lmstudio-telegram-bot.service
```

Then verify through Telegram from an allowed admin account:

1. `/start`
2. `/profiles` and tap the `Qwen VL 3B` buttons for `Load` / `Use for chat`
3. `/health`
4. `/current`
5. `/status qwenvisionunc`
6. Send a plain text prompt such as `Ответь одним словом: ping`

## Troubleshooting

### `Access denied.`

The Telegram sender is not in `ADMIN_IDS`. Add the numeric user id to `.env`, restart service.

### Bot does not respond

Check service and logs:

```bash
systemctl --user status lmstudio-telegram-bot.service --no-pager
tail -100 logs/bot.log
```

Also make sure no webhook is set if using polling:

```bash
source .venv/bin/activate
python - <<'PY'
import asyncio, bot, httpx
async def main():
    async with httpx.AsyncClient() as c:
        r = await c.get(f'https://api.telegram.org/bot{bot.TOKEN}/getWebhookInfo')
        print(r.json())
asyncio.run(main())
PY
```

### Chat hangs or times out

Check loaded models:

```bash
~/.lmstudio/bin/lms ps
```

If multiple models are loaded or one is stuck generating, unload the unwanted one:

```bash
NO_PAUSE=1 ~/Desktop/LMStudioControl/lmstudio-control.sh unload-model qwythos
```

Then load the desired profile:

```bash
NO_PAUSE=1 ~/Desktop/LMStudioControl/lmstudio-control.sh load-model mythosnano
```

### LM Studio API unavailable

Open LM Studio and enable/start the local server on port `1234`, then test:

```bash
curl -fsS http://127.0.0.1:1234/v1/models
```

## Current Michael deployment snapshot

- Local path: `/home/mg/lmstudio-telegram-bot`
- Bot username: `@mg_lmstudio_client_bot`
- Service: `lmstudio-telegram-bot.service`
- Default profile: `mythosnano`
- Additional uncensored vision profile: `qwenvisionunc` -> `qwenvl3bunc`
- Admin-only access: configured via `ADMIN_IDS`
- Secrets file: `.env` mode `600`, not committed

Optional external provider fields:

```env
CHATGPT_WEB_BASE_URL=https://codex.guber.dev/v1
CHATGPT_WEB_MODEL=chatgpt-5.5-high-web
# token can stay in ~/.hermes/.env as CHATGPT_WEB_PROVIDER_API_KEY
```
