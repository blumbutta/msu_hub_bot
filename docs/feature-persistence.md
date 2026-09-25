# Feature persistence

Use `storage.features` for small, typed feature documents and work that must
survive a bot restart. PostgreSQL owns the data; the authenticated Supabase RPC
boundary owns access. Features register models and job handlers in Python.
Ordinary payload fields require no SQL migration.

Keep relational identities, message history and reaction analytics in their
dedicated tables. This API supports exact keys and bounded collection listings,
not arbitrary JSON queries, joins or an alternative SQL language.

## A document and an atomic change

```python
from msu_hub_bot.storage.features import Payload, Scope

class Preferences(Payload):
    timezone: str = "Europe/Moscow"

preferences = store.collection(
    "example", "preferences", Preferences, retention=None,
)
scope = Scope(f"user:{user_id}")
record = await preferences.get(scope, "default")
tx = store.transaction("example", scope, operation_id=action_id)
if record is None:
    tx.expect_absent("preferences", "default")
    value = Preferences(timezone="Europe/London")
else:
    tx.expect(record)
    value = record.value.model_copy(update={"timezone": "Europe/London"})
tx.put(preferences, "default", value)
await tx.commit()
```

`retention=None` explicitly means permanent. A positive `timedelta` sets expiry
on each write; `expires_at=` overrides it for a specific mutation. Reads never
extend expiry. Explicit deletion remains available with `tx.delete(record)`.

Feature and collection names are stable lowercase identifiers. Scope/key values
are opaque, bounded identifiers: use user/chat/message IDs or activity tokens,
never message text or secrets. Bot ownership comes from the caller's principal.
`Scope(key, owner="application")` deliberately shares records among this app's
enabled principals; it does not grant another app access. Namespaces prevent
accidental collisions, not hostile code sharing credentials.

## Concurrency and uncertain writes

- Every changed record requires an exact `etag` or absence guard. Read-only
  guards can protect a related record: accepting a vote checks the open round
  and inserts the unique voter record in one transaction.
- A `Conflict` means reload, reconsider the change and use a new operation ID.
  Reapply only intended changes, preserving fields another writer updated.
- A timeout has an unknown outcome. Retry the **same frozen transaction** with
  its original operation ID; do not rerun the handler or reconstruct its request
  from newer reads. Different data under the same ID raises `OperationMismatch`.
- Successful requests and conflicts have seven-day receipts. After that window,
  reconcile durable entity state before retrying; receipts no longer guarantee
  deduplication. Jobs have their own persistent identity and generation.
- No network calls belong inside a retried state transformation. A database
  commit cannot atomically include a Telegram send or a provider request.

Transactions allow at most 64 guards and 64 mutations, 256 KiB per request and
64 KiB per document. Listings have a maximum page of 200 records, ordered by key;
pass the last key as `after` until a short page is returned. Keep unbounded
participants in child records (`parent=activity_key`), not a growing JSON array.
Feature-specific capacity limits still belong to the feature service.

## Evolving models

`Payload` preserves unknown fields and validates known fields. Nested stored
models must also inherit it. Validate command input separately; preserving
stored extras is not permission to accept arbitrary user input.

`payload_version` belongs to each collection's record envelope. It is independent
of concurrency `etag`, job generation, SQL migration ledger and RPC version.

- A compatible optional field needs an explicit stable default. No version bump
  is necessary if old code can safely ignore and preserve the field.
- Renames, required fields, changed units/types/meaning and changed defaults
  require explicit upgrades. Preserve the meaning of omitted old fields; never
  invent missing data or silently reset malformed documents.
- Register pure adjacent transforms with `upgrades={1: v1_to_v2, 2: v2_to_v3}`
  and `version=3`. Preserve unrelated fields; reject conflicting rename data.
- Reads upgrade a copy in memory. `Record.payload_version` reports its stored
  version; `Record.value` uses the current model. The next mutation writes the
  current version with the original `etag` guard.
- Missing/failed upgrades raise `InvalidPayload`; future versions raise
  `FutureVersion`. Neither writes anything. Errors exclude payloads.
- Forward transforms do not make an old image compatible with new data. Deploy
  compatible readers before breaking writers. Permanent records and old backups
  need retained upgrade paths or a verified backfill/restore plan.

Expensive conversions or ones requiring external data belong in bounded,
resumable administrative backfills. New indexes and database constraints still
need reviewed SQL migrations.

## Scheduled work

Create/change the record and schedule its work in the same transaction:

```python
from msu_hub_bot.storage.features import RecordKey

tx.schedule(
    key=f"deliver:{reminder_id}", kind="deliver",
    record=RecordKey("reminders", reminder_id), run_at=due_at,
)
```

The referenced record must be guarded and exist after commit. Register a stable
`worker.register(feature, kind, handler)` callback at composition time. A handler
receives `JobContext`, reads its record, checks `await context.current()` before
effects, and returns on success. Leases renew during execution. Tasks, locks,
Telegram objects and executable Python are never serialized.

`JobRetry` explicitly asserts replay is safe; it uses bounded backoff and a
registered attempt limit. `JobHold` and unexpected failures stop automatic replay
for reconciliation. `JobExpired` closes work whose semantic window ended.
`serial_key` preserves job order within feature/scope, including retries and
holds. Rescheduling advances generation; stale completion cannot clear newer
work. Cancellation cannot recall an external request already sent.

Pending-dependent records need `expires_at=None`. Physical protection by a job
does not override read expiry. Set terminal retention or schedule cleanup only
after the activity finishes. Unfinished jobs do not expire automatically;
terminal jobs and operation receipts are cleaned after seven days. A separate
bounded administrative `retain_features` function performs physical cleanup.

For reminders, save the destination independently of the original message,
store a finite UTC deadline plus the IANA timezone, and retain the record until
delivery/cancellation is resolved. A reminder years ahead must not inherit a
seven-day creation TTL. Ambiguous sends require reconciliation: leases cannot
promise exactly-once external delivery. Preserve handler kind names while older
scheduled jobs can reference them.

## Operations and checks

### Shared application records

`storage/application.py` owns permanent documents in `Scope("global", owner="application")`:

| Feature / collection | Key | Payload |
| --- | --- | --- |
| `settings / chats` | Chat ID | `ChatPreferences`: explicit boolean defaults and preserved extra fields. |
| `ecosystem / chats` | Chat ID | Directory metadata, including its original UUID and creation time. |
| `vk / subscriptions` | `owner_id:chat_id` | Subscription settings and delivery cursor, including original UUID and creation time. |

The repository's existing methods remain the application interface. Partial
preference changes preserve unrelated fields; delivery advances VK cursors
monotonically, while an explicit administration reset may move them backwards.
Listings paginate completely before applying numeric ordering. Loading saved
preferences observes a new chat without overwriting newer metadata with an old
callback's snapshot.

### Reminders

`reminders/service.py` owns `reminders / items` under the bot's `user:<author_id>`
scope. The stable creation key derives from the author, chat/topic and source
message; browser requests use a frozen UUID mapped into a separate negative
message-ID space. Replaying creation returns the original record, including
after its deadline, rather than moving a relative schedule forward.

The document stores text, author display snapshot, destination, UTC deadline,
IANA timezone and delivery state. Pending and actively sending reminders have
no expiry. Delivery, cancellation, failure or uncertain delivery schedules
cleanup after 30 days; cleanup removes the text. Until then an uncertain send
stays available for its owner to reconcile or explicitly retry. Changing a reminder never changes its
owner or destination.

Reminder payload version 2 adds optional recurrence and delivered/skipped
occurrence counters. Daily and weekly rules preserve local wall time; custom
intervals are elapsed UTC minutes (15 minutes to one year). Calendar repeats
skip nonexistent DST times and choose the first occurrence of an ambiguous
time. Missed occurrences coalesce into one overdue delivery; the next deadline
is strictly after the current clock. A successful delivery atomically advances
the same record and job generation. The series stays permanent while pending.
Uncertain delivery holds the whole series for explicit owner retry; it never
silently schedules another occurrence. Cancellation stops future occurrences,
but cannot recall an attempt whose sending marker is committed. Creating,
rescheduling, retrying and delivering a recurring group reminder check current
author membership and bot administrator rights. Cancellation and removing
recurrence do not require bot administrator rights.
Permanent owner-scoped `preferences/users` records supply the default timezone
for new command and web reminders; explicit timezones override that default.

Creation and rescheduling atomically update the document and its delivery job.
The worker marks sending before contacting Telegram and schedules reconciliation
for an interrupted attempt. Overdue reminders are delivered after restart; a
delay greater than one minute is labelled. A definitive rate-limit rejection
can retry automatically. A lost response cannot prove whether Telegram sent
the message: mark delivery uncertain and require explicit retry, which may
duplicate it. Do not infer failure from a timeout and send again blindly.

`/remind` accepts relative English/Russian times and explicit dates, defaults to
Europe/Moscow, and lists or changes the author's reminders in the current
chat/topic. Buttons carry exact revisions. The [Mini App](mini-app.md) provides
an owner-wide list and edits using the same service and revision checks.

### Chess, geoguess and art

`games/quiz.py` supplies the shared activity lifecycle; `games/definitions.py`
adapts providers and the existing bounded caption renderers. Each game's
namespace has four collections, all scoped to the bot and chat:

| Collection | Contents | Lifecycle |
| --- | --- | --- |
| `chats` | Active round pointer and bounded recent-question history. | Expires after 30 days without writes. |
| `rounds` | Frozen question, answer order, message/topic binding, deadlines and settlement progress. | No expiry while active or awaiting settlement; closed rounds are cleaned after settlement and the 24-hour result window, abandoned publications after recovery resolves. |
| `votes` | One immutable answer and display-label snapshot per user/round, parented by round token. | Deleted with the completed round. |
| `scores` | Daily totals and latest scoring display-label snapshot per user, parented by Moscow calendar day. | Permanent; round/vote cleanup does not delete scores. |

One active round belongs to a chat; its saved topic controls delivery. Every
accepted vote is a separate record, with an explicit 10,000-participant limit.
Game buttons can vote, finish and page through results during an unrelated FSM
conversation without changing its state or draft. Typed commands still follow
the conversation's input rules until `/cancel`.

Rounds preserve the exact question, answer order, attribution, message binding,
deadline and votes. A restart resumes the ten-minute deadline and keeps result
navigation available for 24 hours after closure. Uncertain initial photo sends
are not repeated automatically: a matching bot-authored photo/button callback
can recover the binding during the publication window. Otherwise the incomplete
round is abandoned and cleaned; already accepted votes are never acknowledged
from process memory alone.

Settlement freezes the score day and vote set. Jobs are ordered per
game/chat/Moscow day because penalties floored at zero depend on round order.
Automatic closure uses the original deadline's day, including after downtime.
Each transaction guards the round, up to 30 votes and their score records,
writes new totals and advances the round's saved cursor together. A crash or lost response
cannot leave a committed score without the progress that prevents replay.
There is no separate scoring expiry: held work requires repair, and its round
and votes remain until settlement resolves.

`/chess_top`, `/geoguess_top` and `/art_top` scan today's score records in bounded pages and
keep only the ten best entries in memory. A failed or timed-out scan produces an
unavailable response, never a ranking made from only the pages retrieved.
Rankings can reflect committed batches while a round is settling; the result
message marks scoring complete only after every accepted vote is accounted for.
Older daily totals stay available in storage, including player labels, although
the commands show only the current Moscow day.

Presentation caches are disposable; rebuilding one must never change the
selected question, votes or scores. All three quizzes use only the feature store for
persistence.

The art namespace stores the museum artwork ID, title, date, artist and trusted
image/source URLs in the frozen question; these details appear only after closure.
Its chat history excludes the last 15 paintings. Art has a 20-second publication
deadline starting before chat reservation; the provider has a nested 16-second
budget. Other quiz publication budgets and the shared ten-minute round duration
remain unchanged. Timeout reporting and publication recovery use the same bounded
Telegram delivery path as other quizzes.

### Raffles

`games/raffle.py` owns `raffle` documents scoped to `chat:<id>:topic:<id-or-0>`.
`rounds` stores the creator, original Telegram message, entrant count and winner.
`members` maps each round/user pair to one numbered `participants` record; the
three changes commit atomically. Numbered entries allow bounded page reads and
uniform winner selection with one lookup, without growing the round payload.

A draw commits its winner before any Telegram edit. Repeating a draw or pressing
refresh renders that same result; it never picks again. Only the stored creator
can finish. Callback origin must match the bound bot message and chat/topic.
An uncertain initial send is never resent automatically; a callback can bind
its original card after verifying sender, reply, markup and publication time.
No animation jobs or separate winner messages are needed.

Rounds, membership indexes and entrant names expire together 90 days after
creation. Writes and reads do not extend that window. Expired or preexisting
cards receive the ordinary unavailable-button response.

### Public chess matches

`games/chess_play/service.py` owns `chess_play` documents in the bot's
`Scope("global")`. Matches and ratings share this scope so settlement can update
one match and both players atomically, including games played in different chats.

| Collection | Key and contents | Lifecycle |
| --- | --- | --- |
| `chats` | Chat ID; pointer to its active invitation or match. | No expiry while active; expires after 30 days once released. |
| `matches` | `chat_id:token`; players, move history, clocks, message/topic, result and rating settlement. | No expiry while unresolved; cleanup after settlement and the 24-hour result window. |
| `ratings` | User ID; global Elo and display-label snapshot. | Permanent. |

Creating an invitation reserves the chat and queues publication recovery in one
transaction. The initial photo is sent once. An uncertain response leaves a
one-minute window for a matching bot-authored board callback to recover the
message binding; otherwise the invitation is abandoned. A restart never creates
a second board. Later renders edit the saved message and can safely retry.

Joining freezes both starting ratings. Each action checks the board revision,
actor and absolute deadline before a conditional write. The ten-minute clocks
gain five seconds per legal move; no process timer owns game state. The shared
worker adjudicates deadlines and refreshes clock captions approximately every
five seconds. Delayed display updates never extend a player's time.

Finishing releases the chat before result delivery. Settlement applies the Elo
delta calculated from the starting pair to each player's current rating and
records the before/after values in the match in the same transaction. Concurrent
matches cannot overwrite each other's points; replay cannot settle a match
twice. Cleanup waits for settlement and removes only that match, leaving ratings
intact. Rendering failures do not stop clocks or prevent scoring.

`/chess_rating` paginates the global ranking. Since the document API is ordered
by key, ranking requires a complete bounded-time scan; failure returns an
unavailable response instead of a partial ranking. Navigation is bounded to the
first 10,000 places; the personal rank counts every player. Chess actions never
modify the user's active FSM conversation.

### Telegram conversations and deletions

`telegram/fsm_storage.py` implements aiogram `BaseStorage` through feature
`telegram_state`; `telegram/deletions.py` uses the same feature and shared worker.

| Collection | Identity | Lifecycle |
| --- | --- | --- |
| `conversations` | Bot scope; SHA-256 of canonical bot/chat/user/topic/business-connection/destiny fields, retained in the payload for validation. | No expiration by default. `/cancel` clears the state and data; a fully empty record is removed. Imported state/data deadlines remain independent. |
| `deletions` | Bot scope; `chat_id:message_id`, paired with one `delete_message` job. | Pending work has no expiry; completion keeps a seven-day record before cleanup. |

FSM reads return independent JSON data. `set_data` replaces only data;
`set_state` changes only state. `update_data` uses compare-and-swap with bounded
conflict retries, preserving unrelated concurrent fields. Setting either field
clears only that field's live imported expiration; mutations also remove
already expired fields. Uncertain commit responses retry the same frozen
operation ID, so a lost response cannot replay a new write. Payloads share the 64 KiB feature
limit; malformed, oversized or future-version data is never defaulted and
written back. Unknown payload fields survive updates and prevent automatic
empty-record deletion. `close()` does not close the borrowed database client.

`USER_IN_TOPIC`, `/cancel` behavior and releasable event isolation remain explicit
middleware policies. Isolation uses process-local locks; storage CAS protects
individual writes, not whole multi-step handlers across several pollers. Run
exactly one poller per token.

Scheduling a deletion atomically replaces its document and increments its job
generation. An older lease cannot complete the replacement job. The worker
checks the current generation before the Telegram call; rescheduling cannot
recall an HTTP deletion already in flight. Deletion is idempotent: uncertain
network failures retry, rate limits honor Telegram's delay, and missing or
inaccessible messages finish without retry loops. Unclassified bad requests
are held for review. Expired leases recover through the shared worker after a
restart; there is no separate scan loop or process timer.

For protected snapshot validation, repeatable restoration and compatible
rollback, follow [deployment operations](deployment.md#restoring-conversation-state-and-scheduled-deletions).

### Schema and runtime

Apply the feature schema administratively before deploying consumers. Startup
checks the feature RPC contract; it never applies migrations. Follow
[database operations](database-operations.md) to update installed retention
guards, schedule bounded feature cleanup and verify backup/restore coverage.
Shutdown stops claiming work and drains handlers before closing clients;
interrupted claims expire for another worker to recover.

Test upgrades, preserved extras, concurrent changes, replayed commits, expired
records, stale leases/generations and interrupted external effects. The real
PostgreSQL suite validates authentication and transaction guarantees. Inspect
held work and retry pressure through redacted diagnostics, never payload logs.
See [queue operations](operations.md) for registered feature/kind metrics,
snapshot freshness and the feature-owned reconciliation procedure.
