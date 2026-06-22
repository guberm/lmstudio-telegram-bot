# LM Studio Telegram Bot

Admin-only Telegram client for a local LM Studio instance.

```text
Telegram -> bot.py -> LM Studio OpenAI-compatible API
                 └-> optional local control script for model/ngrok controls
```

This bot is intentionally **not** a Hermes/OpenAI client. It talks only to:

- LM Studio API: `http://127.0.0.1:1234/v1`
- Optional local control script configured by `LMSTUDIO_CONTROL_SCRIPT`

Access is restricted to the numeric Telegram user IDs configured in `ADMIN_IDS`.

## Features

- Admin-only Telegram bot using polling - no webhook/domain/TLS needed.
- Plain text chat via LM Studio `/v1/chat/completions`.
- Per-chat conversation history in local JSON state.
- Configurable system prompt.
- Uses `lms ps` to prefer the selected **loaded** model; if no model is loaded, it loads the selected profile.
- Exposes the useful `lmstudio-control.sh` actions through safe Telegram commands.
- Long model operations (`/load`, `/unload`, `/start_public`, `/stop_public`) acknowledge immediately, run in the background, and post the final result when finished.
- Plain text chat also acknowledges immediately and posts the model answer later, so slow LM Studio generations do not look like a dead bot.
- Chat timeouts report the exception class/details instead of a blank `LM Studio chat failed:` message.
- Processes multiple Telegram updates concurrently so `/start`/`/status` are not stuck behind a model load.
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
- Optional `lmstudio-control.sh`-compatible helper script if you want Telegram commands for loading/unloading models or managing ngrok. Configure its absolute path with `LMSTUDIO_CONTROL_SCRIPT`.

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
ADMIN_IDS=123456789
LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1
LMSTUDIO_CONTROL_SCRIPT=/absolute/path/to/lmstudio-control.sh
DEFAULT_PROFILE=mythosnano
DEFAULT_SYSTEM_PROMPT=You are a concise local LM Studio assistant. Answer in the user's language when clear.
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
NO_PAUSE=1 "$LMSTUDIO_CONTROL_SCRIPT" status mythosnano
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
| `/profiles` or `/models` | List control-script model profiles |
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
2. `/health`
3. `/profile mythosnano`
4. `/current`
5. `/status`
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
NO_PAUSE=1 "$LMSTUDIO_CONTROL_SCRIPT" unload-model qwythos
```

Then load the desired profile:

```bash
NO_PAUSE=1 "$LMSTUDIO_CONTROL_SCRIPT" load-model mythosnano
```

### LM Studio API unavailable

Open LM Studio and enable/start the local server on port `1234`, then test:

```bash
curl -fsS http://127.0.0.1:1234/v1/models
```

## License

MIT. See [LICENSE](LICENSE).
