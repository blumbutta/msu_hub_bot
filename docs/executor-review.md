# Shared worker and sticker conversion review

Review date: 2026-09-16. All validation used synthetic data and offline bot
fixtures. No Telegram sticker packs or running services were changed.

## Corrected in this change

| Finding | Correction |
| --- | --- |
| Timeout or caller cancellation released a worker slot while its thread was still running, allowing further submissions into the pool's unbounded queue. | Release the slot when the underlying concurrent future finishes, independently of the caller. |
| Broken-pool recovery recursively acquired another slot and passed the timeout as a positional job argument. This deadlocked with one worker and could exhaust larger pools. | Retry once, outside the previous slot, with the original arguments and one shared deadline. Concurrent failures replace the same broken pool only once. |
| Queue waiting was excluded from the timeout. A worker's own `TimeoutError` was incorrectly reported as an executor deadline. | Include queue waiting in the deadline and preserve exceptions raised by jobs. |
| Zero or negative deadlines could still start work. Zero or noninteger worker counts could produce invalid admission limits. | Reject expired work before submission and validate a positive integer worker count. |
| Shutting down an unused executor created a pool. | Keep shutdown lazy and explicitly reject subsequent submissions. |
| Sticker conversion used a separate default thread pool. | Use the shared `AppCPUExecutor` pool and handle its timeout result before upload. |
| Anamorphic video was distorted, unsupported documents fell back to the avatar, and image decompression errors escaped the friendly error path. | Preserve display aspect ratio, reject unsupported documents, and handle decompression errors. |

`AppCPUExecutor` explicitly constructs a `TPExecutor`. The existing `PPExecutor`
compatibility name still refers to threads. Changing all jobs to a process pool
would require reviewing callable serialization and worker termination first.

## Effective limits

| Scope | Limit and meaning |
| --- | --- |
| Shared executor | Three submitted, unfinished jobs across all commands using it. Timed-out running jobs retain their slots. |
| Caller deadline | 180 seconds by default, including admission waiting and recovery. `timeout=None` disables this deadline. |
| Broken pool | At most one retry per call, within that deadline. |
| Waiting callers | No separate count limit in this executor; callers waiting for a slot retain their arguments in memory. |
| Sticker input | 20 MiB, checked against Telegram metadata before download and actual bytes before decoding. Downloads occur before worker admission. |
| Video input duration | Positive and at most seven seconds. Inputs over three seconds are accelerated to approximately 2.95 seconds; longer inputs are rejected. |
| Video output | Silent VP9 WEBM, at most three seconds, at most 30 FPS, longest side 512 pixels, at most 256 KiB. One encoding attempt. |
| Static output | Longest side 512 pixels, lossless WEBP, at most 512 KiB. One encoding attempt. |
| Existing TGS | At most 64 KiB compressed, at most 2 MiB expanded JSON, positive duration at most three seconds. Arbitrary TGS documents are rejected. |
| Sticker subprocesses | Each FFmpeg/FFprobe invocation has a 60-second timeout. Video preparation can perform one input probe, one encode, and one output probe. |
| FFmpeg threads | The new sticker encoder requests two codec threads; this is not a total CPU or thread limit for decoding, filtering, or other commands. |
| Shutdown | Reject new submissions and cancel futures that have not started. `wait=False` does not terminate running threads. Waiting callers must finish, reach their deadline, or be cancelled by the application. |

## Remaining reliability work

1. **Isolate untrusted regular expressions.** `/sed` executes Python `re.sub` in
   a worker thread. A pathological expression can hold the GIL and prevent the
   event loop from delivering its own timeout. A bounded, disposable-process
   probe confirmed this: a 28-character input failed to return despite a 50 ms
   executor deadline and the probe was terminated externally after two seconds.
   Use a terminable process or a regex engine with a suitable execution bound.
   Merely switching to `ProcessPoolExecutor` would not make cancellation kill a
   running job.
2. **Add deadlines and guaranteed cleanup to older subprocess paths.**
   `hub_bot/utils/ffmpeg.py` uses `Popen.communicate()` without a timeout, and
   unsuccessful conversions return before deleting temporary files. OCR also
   invokes Tesseract without an explicit timeout. These paths can keep shared
   workers occupied after callers give up. They are separate from the new
   sticker conversion helper.
3. **Set resource and admission budgets.** The generated deployment configuration
   does not set CPU, memory, or PID ceilings. Worker counts and compressed file
   sizes do not bound decoded media memory. Consider bounded pending admission,
   image/video dimension limits, and container budgets chosen for host capacity.

## Validation

- Executor regression tests cover timeout and cancellation with one and three
  workers, queue deadlines, cancellation while queued, pool failure during
  submission and future completion, concurrent recovery, retry limits, exception
  propagation, invalid limits, and the actual applet's initialization/shutdown.
- Sticker tests cover media limits, rejected inputs, timeout handling, admin
  checks, upload/create/add/delete requests, and state retention after failure.
- Native FFmpeg probes exercised short, three-second, accelerated six/seven-second,
  over-limit video, GIF playback, existing WEBM, and non-square pixel aspect ratios.
- Live Telegram API acceptance and host-level resource exhaustion were not tested.
