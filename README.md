# MSU Hub Bot

A friends' Swiss-army Telegram bot, born in Moscow State University chats:
media tools, OCR, speech and song recognition, translation, code execution
through JDoodle, games, polls, chat administration, and a few inside jokes.

The runtime uses aiogram 3, Pydantic 2 and Supabase. Dependencies are locked for
reproducible builds. External tools depend on their providers' availability
and configuration; see the bot's `/help` for commands.

## Ask with a reply

Reply to an attachment and mention the bot: `@msu_hub_bot сделай PDF`,
`@msu_hub_bot вытащи текст` or `@msu_hub_bot убери фон`. Russian and English
requests can select one of `/pdf`, `/text`, `/bg`, `/song` and `/anime`.
Clear requests run the existing command; uncertain requests get a hint.
PDF conversion accepts documents, OCR/background/anime accept images, and
song identification accepts audio or video. Files remain subject to command limits.

This entry requires both a mention and a reply; it does not inspect chat history.
Active conversations retain their `/cancel` behavior. Ordinary commands work as before.
Keep Telegram's inline mode disabled in BotFather so typing the bot's username
does not open inline search.
Only the instruction and coarse attachment types go to Jev through OpenRouter;
the replied-to text, files, filenames and Telegram identities stay out of classification.

Operators enable it with `HUB_JEV_ENABLED=true` and `HUB_OPENROUTER_API_KEY`.
`HUB_JEV_CONFIDENCE` controls the execution threshold (default `0.8`);
confidence is a routing signal, not a guarantee of correctness.
The provider is optional and does not run when disabled. Each user can have one
request in flight, with a five-second cooldown; four requests can run concurrently.

## Chat quizzes

`/art` asks you to identify a painting's artist from six options;
`/geoguess` asks you to locate a photo; `/chess` asks for the best move in a
[Lichess puzzle](https://lichess.org/training). Each game keeps its question,
solution and paginated results in one photo message. Votes stay hidden until
anyone finishes the round or its ten-minute timer expires.

`/art_top`, `/geoguess_top` and `/chess_top` show separate daily chat rankings (Moscow time):
a correct answer earns one point; an error loses one, with a floor of zero.
Rounds, votes and scores survive restarts in Supabase. Result navigation stays
available for 24 hours after closure; daily scores are retained permanently.
Lichess access is anonymous and subject to its shared request limits.

Art questions come on demand from the
[Cleveland Museum of Art API](https://openaccess-api.clevelandart.org/), without an API key
or a local painting catalog. The bot samples the eligible public-domain painting
collection, verifies a single individual artist and excludes the chat's last
15 paintings. Museum titles, dates, attribution and links appear only after
closure; artist names use the museum's spelling. Art has a 20-second total
publication budget, including storage, downloading the image and Telegram upload.
The catalog provider uses at most 16 seconds. The image is downloaded to bounded
memory with an identified bot client, then uploaded to Telegram; no media files
are kept on disk.
Unavailable or ambiguous records fail cleanly without publishing a question.

`/reactions` shows the chat's reaction receivers, givers, popular posts and emoji
over 24 hours, seven days or thirty days. The bot needs administrator rights to
collect new reactions; reaction state expires after thirty days. See
[reaction scores and coverage](docs/reactions.md).

## Chess with friends

`/chess_play` opens a public invitation in a group: the author plays white,
the first other person to join plays black. One match runs per chat, across
forum topics. Choose a piece and a legal destination on the board's buttons;
everyone can watch, but only the players control the match.

The clock is 10+5: ten minutes per player, with five seconds added after each
move. The caption refreshes about every five seconds while playing; the saved
deadline decides timeouts even when the display lags. Invitations expire after
ten minutes. Players can resign, offer a draw, or claim one under chess rules.

`/chess_rating` shows permanent, bot-wide Elo (start 800, K=32). Matches,
clocks and ratings survive restarts through the [feature store](docs/feature-persistence.md).
Completed matches remain available for 24 hours; ratings do not expire.
Game buttons work during another conversation without changing its draft.

## Feedback

Use `/feedback description` to report a bug or suggest a feature. Choose which
chat details, messages and command diagnostics to include, then preview before
sending. Reports go to the configured review chat and survive restarts;
see [feedback and context choices](docs/feedback.md).

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

Fill in `HUB_BOT_TOKEN`, then configure the Supabase API URL,
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
Administrator IDs default to no access. Conversation state and scheduled work
survive restarts through the shared feature store.

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
The bot connects to separately managed Supabase APIs. Deployment does
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

For contributor-owned persistence, use the [typed feature store](docs/feature-persistence.md).
It provides versioned documents, atomic changes and durable jobs; chess and geoguess
share a quiz service that keeps rounds, votes and daily scores across process restarts.
The [membership reader](docs/memberships.md) exposes known chat participants with
status evidence and coverage limits, without periodic membership checks.

Ruff exceptions are limited to specific files. Strict mypy coverage expands
monotonically as modules are typed; new application modules must be included.

Demotivators use Liberation Serif; animated text uses Ubuntu Mono. The debate
dataset and font assets are bundled.

Application license: **GPL-3.0-only**. Fonts retain their own licenses; see
[third-party notices](THIRD_PARTY_NOTICES.md). ACRCloud binaries remain an
external dependency; deployment images are transferred privately and are not
published for redistribution.
