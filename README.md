# LM Studio Telegram Bot

Admin-only Telegram client for LM Studio and the Linux `lmstudio-control.sh` workflow.

```text
Telegram -> bot.py -> LM Studio OpenAI-compatible API
                 └-> lmstudio-control.sh for model/ngrok controls
```

## Available profiles

- `gemma4unc` — the only local LM Studio LLM profile and default/fallback
- `chatgptweb` — external `chatgpt-5.5-high-web` provider at `https://codex.guber.dev/v1`

External aliases (`chatgpt_web`, `chatgpt`, `chatgpt-5.5-high-web`, `codexguber`) normalize to `chatgptweb`. Unknown or retired profile values normalize to `gemma4unc` and are never passed to the Linux controller. At startup, persisted chat records are migrated atomically to an available canonical profile/model while preserving system prompts and bounded history.

## Features

- Admin-only polling bot.
- Plain text and image chat via OpenAI-compatible APIs.
- Per-chat JSON state and configurable system prompt.
- `/profiles` inline profile/action buttons.
- `/new_session`, `/new`, `/reset`, and inline New session clear only history.
- Selected-profile load/unload/start-public/stop-public/status actions.
- Generic models, ngrok, URL, and allowlisted raw script actions.
- External-provider bearer token stays on Linux; it is not committed.
- User systemd service and `flock` wrapper.

## Configuration

Copy `.env.example` to `.env` and provide the bot token/admin IDs. The supported default is:

```env
DEFAULT_PROFILE=gemma4unc
LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1
LMSTUDIO_CONTROL_SCRIPT=/home/mg/Desktop/LMStudioControl/lmstudio-control.sh
```

Secrets and runtime state (`.env`, `.venv/`, `data/`, `logs/`, caches) are ignored by Git.

## Commands

- `/start`, `/help`, `/health`, `/current`
- `/profiles` or `/models`
- `/profile gemma4unc|chatgptweb`
- `/chatmodel gemma4unc|chatgptweb`
- `/summary`, `/status`, `/load`, `/unload`
- `/start_public`, `/stop_public`
- `/ngrok`, `/url`, `/start_ngrok`, `/stop_ngrok`
- `/new_session`, `/new`, `/reset`
- `/system <prompt>`
- `/run <allowlisted-action> [profile]`

## Install/run

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt
./start.sh
```

## Verify and deploy

```bash
source .venv/bin/activate
python -m py_compile bot.py test_profiles.py
python -m unittest -v test_profiles.py
systemctl --user restart lmstudio-telegram-bot.service
systemctl --user is-active lmstudio-telegram-bot.service
```

The live deployment is `/home/mg/lmstudio-telegram-bot`; service name: `lmstudio-telegram-bot.service`.
