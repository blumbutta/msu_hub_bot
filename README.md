# MSU Hub Bot

A friends' Swiss-army Telegram bot, born in Moscow State University chats:
media tools, OCR, speech and song recognition, translation, code execution
through JDoodle, games, polls, chat administration, and a few inside jokes.

The runtime uses aiogram 3, Pydantic 2, Supabase, and Redis. Dependencies are locked for
reproducible builds. External tools depend on their providers' availability
and configuration; see the bot's `/help` for commands.

## Chat quizzes

`/geoguess` asks you to locate a photo; `/chess` asks for the best move in a
[Lichess puzzle](https://lichess.org/training). Each game keeps its question,
solution and results in one photo message. Ordinary chess results fit on one
page; extra pages are used only when the caption would exceed Telegram's limit.
Votes stay hidden until
anyone finishes the round or its ten-minute timer expires.

`/geoguess_top` and `/chess_top` show separate daily chat rankings (Moscow time):
a correct answer earns one point; an error loses one, with a floor of zero.
Scores expire after the following day. Round snapshots and their original
deadlines are saved in Redis until 24 hours after the deadline. On restart,
unfinished rounds resume; overdue rounds finish immediately after recovery.
Failed score/reveal delivery is retried every 30 seconds during that retention
period, with the same score date and idempotency key. Completed navigation can
reload an evicted snapshot. Redis/Telegram outages may delay visible delivery,
but do not extend voting past the stored deadline. A saved Redis snapshot is
required for restart recovery; games from before this update cannot be recovered.
Photo credits and source links in GeoGuess appear only after the round closes.
Lichess access is anonymous and subject to its shared request limits.

`/reactions` shows the chat's reaction receivers, givers, popular posts and emoji
over 24 hours, seven days or thirty days. The bot needs administrator rights to
collect new reactions; reaction state expires after thirty days. See
[reaction scores and coverage](docs/reactions.md).

## Development

Use Python 3.14 and uv 0.12.15 or newer. Native development works on macOS; the
ACRCloud native SDK is installed only on Linux x86-64. FFmpeg and Tesseract are
needed for media and OCR commands. yt-dlp, its JavaScript solver and Deno are
installed by uv; video extraction does not download runtime components.

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check src tests tools
uv run python tools/check_types.py
uv run mypy
uv run pytest -q
cp .env.example .env
```

Fill in `HUB_BOT_TOKEN` and `HUB_REDIS_HOST`, then configure the Supabase API URL,
publishable key and dedicated Auth account
described in [database configuration](docs/deployment.md#database-configuration), then run:

```sh
uv run --env-file .env msu-hub-bot
```

Only one poller may use a Telegram token at a time. Local tests block network
access and use synthetic data; they require no live bot or database.

Configuration is documented by `.env.example` and the typed settings in
`src/msu_hub_bot/settings.py`. Lists, tuples, and mappings use JSON. Optional services
stay registered when unconfigured and return an unavailable response when used.
Administrator IDs default to no access. The Redis namespace defaults to `hub`;
changing it disconnects the bot from its existing state.

Real environment files, credentials, logs, dumps, private keys, and core dumps
are excluded from Git and Docker builds. Never put tokens in build arguments or
paste an expanded production Compose configuration into logs or issues.

## CI and production

Pull requests run locked Python checks on Linux and macOS, Ruff, strict progressive mypy, tests, secret
scanning, workflow validation, and a Linux amd64 container smoke test. Actions
references and tool versions are pinned. Production credentials are confined
to the deployment job on the `production` environment.

Main-branch changes run the same checks, build and test the release image,
and transfer it directly to the VPS over SSH. The host verifies the archive
and runs its immutable image ID. Images are kept on the runner and VPS.
The bot connects to separately managed Supabase APIs and Redis. Deployment does
not run migrations or recreate shared infrastructure. Supabase is the sole
application database backend; immutable schema history and recovery artifact
verification remain available.

The repository variable `DEPLOY_ENABLED=true` enables automatic deployment
from main. Pull requests run checks without deploying.

See [deployment operations](docs/deployment.md) for host setup, secrets,
rollback, and moving to another VPS.

## Maintenance and licensing

Application imports are side-effect free: `src/msu_hub_bot/app.py` owns service lifetimes,
`src/msu_hub_bot/routing.py` registers ordered feature routers, and handlers receive their
services through dependency injection. Conversation state is scoped to one user
within a chat and forum topic. `/cancel` clears the draft; work already started
continues. The imageboard SDK is isolated in a licensed compatibility package
using `pydantic.v1`; application models use Pydantic 2.

Within `src/msu_hub_bot/`, commands own user-facing behavior, Telegram helpers
own routing support and delivery, and providers, storage, media and execution
have separate service boundaries. Release tooling lives in `tools/deployment/`;
database history and operational contracts live in `dbschema/` and `docs/`.

Ruff exceptions are limited to specific files. Strict mypy coverage expands
monotonically as modules are typed; new application modules must be included.

Demotivators use Liberation Serif; animated text uses Ubuntu Mono. The debate
dataset and font assets are bundled.

Application license: **GPL-3.0-only**. Fonts retain their own licenses; see
[third-party notices](THIRD_PARTY_NOTICES.md). ACRCloud binaries remain an
external dependency; deployment images are transferred privately and are not
published for redistribution.
