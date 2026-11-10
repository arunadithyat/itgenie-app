import hashlib
import html
import json
import re
import tempfile
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

import frappe
import requests
from frappe.utils import add_to_date, cint, get_datetime, now_datetime
from sarvamai import SarvamAI


# Prefer: bench --site yoursite set-config sarvam_api_key "sk_..."
# Never commit a real key to Git.
SARVAM_API_KEY = "sk_zmzlu9nm_zAut1Mfc1PCFRVTteDMvGrvk"

# Automatic transcription only for Call Logs created on or after this moment.
TRANSCRIPTION_START_DATE = "2026-10-08 00:00:00"

MAX_ATTEMPTS = 6              # automatic attempts per Call Log (rate limits don't count)
RETRY_AFTER_MINUTES = 5       # wait between automatic retries
STALE_AFTER_MINUTES = 35      # Pending/Processing older than this is treated as stuck

# Rate-limit protection
MAX_CONCURRENT_JOBS = 2       # Sarvam jobs allowed to run at the same time
BATCH_SIZE = 2                # max new jobs the scheduler queues per run
SARVAM_POLL_INTERVAL = 15     # seconds between Sarvam status checks
RATE_LIMIT_RETRIES = 4        # in-worker retries for a single Sarvam call
RATE_LIMIT_BACKOFF = (20, 40, 80, 160)  # seconds to sleep before each retry
COOLDOWN_SECONDS = 300        # pause all new jobs after Sarvam returns 429

AUTO_STATUSES = ("", "Pending", "Waiting for Recording", "Rate Limited", "Failed")
RETRYABLE_STATUSES = ("", "Waiting for Recording", "Rate Limited", "Failed")


class RecordingNotReady(Exception):
    """Smartflo has the URL but the audio is not downloadable yet."""


class SarvamRateLimited(Exception):
    """Sarvam kept returning HTTP 429 after in-worker retries."""


# ---------------------------------------------------------------- helpers

def get_sarvam_key():
    key = str(frappe.conf.get("sarvam_api_key") or SARVAM_API_KEY).strip()

    if not key or key == "PASTE_YOUR_SARVAM_API_KEY_HERE":
        frappe.throw("Configure sarvam_api_key in site configuration.")

    return key


def is_after_start_date(doc):
    return get_datetime(doc.creation) >= get_datetime(TRANSCRIPTION_START_DATE)


def validate_recording_url(url):
    try:
        parsed = urlparse(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "cloudphone.tatateleservices.com"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        )
    except ValueError:
        valid = False

    if not valid:
        frappe.throw("Use a valid HTTPS Smartflo recording URL.")


def lock_key(name):
    digest = hashlib.sha256(name.encode()).hexdigest()
    return f"sarvam-call:{frappe.local.site}:{digest}"


def cooldown_key():
    return f"sarvam-cooldown:{frappe.local.site}"


def in_cooldown():
    return bool(frappe.cache().get(cooldown_key()))


def start_cooldown():
    frappe.cache().set(cooldown_key(), "1", ex=COOLDOWN_SECONDS)


def is_rate_limit_error(error):
    return (
        getattr(error, "status_code", None) == 429
        or type(error).__name__ == "TooManyRequestsError"
    )


def sarvam_call(func, *args, **kwargs):
    """Run one Sarvam SDK call, backing off and retrying on HTTP 429."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except Exception as error:
            if not is_rate_limit_error(error):
                raise
            start_cooldown()
            if attempt >= RATE_LIMIT_RETRIES:
                raise SarvamRateLimited("Sarvam rate limit exceeded after retries.") from error
            time.sleep(RATE_LIMIT_BACKOFF[min(attempt, len(RATE_LIMIT_BACKOFF) - 1)])


def active_job_count():
    return frappe.db.count(
        "Call Log",
        {
            "custom_transcription_status": ["in", ["Pending", "Processing"]],
            "modified": [">", add_to_date(now_datetime(), minutes=-STALE_AFTER_MINUTES)],
        },
    )


def transcription_error_details(error):
    details = "".join(
        traceback.TracebackException.from_exception(
            error, capture_locals=False
        ).format()
    )

    for value in (SARVAM_API_KEY, frappe.conf.get("sarvam_api_key")):
        value = str(value or "").strip()
        if value:
            details = details.replace(value, "[API key hidden]")

    details = re.sub(r"https?://[^\s<>\"']+", "[URL hidden]", details)
    return details


# ---------------------------------------------------------------- entry points

@frappe.whitelist()
def start_transcription(call_log_name):
    """Manual button: works for any Call Log (old ones too) with write permission.
    Resets the automatic retry counter. Ignores the concurrency cap."""
    doc = frappe.get_doc("Call Log", call_log_name)
    doc.check_permission("write")

    if in_cooldown():
        frappe.throw("Sarvam rate limit hit recently. Please try again in a few minutes.")

    if doc.meta.has_field("custom_transcription_attempts"):
        doc.db_set("custom_transcription_attempts", 0, update_modified=False)

    return _queue_transcription(call_log_name)


@frappe.whitelist()
def start_automatic_transcription(call_log_name):
    """Allowed only from inside a Server Script (Frappe sets in_safe_exec)."""
    if not frappe.flags.get("in_safe_exec"):
        frappe.throw(
            "Automatic transcription must run from a Server Script.",
            frappe.PermissionError,
        )

    try:
        return _auto_queue_if_eligible(call_log_name)
    except Exception as error:
        frappe.log_error(
            title="Automatic Transcription Queue Failed",
            message=(
                f"Call Log: {call_log_name}\n"
                f"User: {frappe.session.user}\n"
                "Stage: Validating and queueing transcription\n\n"
                + transcription_error_details(error)
            ),
        )
        raise


@frappe.whitelist()
def run_transcription_retry_scheduler():
    """Called from the Scheduler Event Server Script every 5 minutes."""
    if not frappe.flags.get("in_safe_exec"):
        frappe.throw(
            "This method can only run from a Server Script.",
            frappe.PermissionError,
        )

    retry_pending_transcriptions()
    return {"message": "Retry scan completed."}


def _auto_queue_if_eligible(call_log_name, check_capacity=True):
    doc = frappe.get_doc("Call Log", call_log_name)

    if not is_after_start_date(doc):
        return {"message": "Call Log is older than the automatic transcription start date."}

    if (doc.get("custom_transcript") or "").strip():
        return {"message": "Transcript already exists."}

    if not (doc.get("custom_url") or "").strip():
        return {"message": "Recording is not available yet."}

    status = (doc.get("custom_transcription_status") or "").strip()
    if status not in AUTO_STATUSES:
        return {"message": "Call is not awaiting automatic transcription."}

    if cint(doc.get("custom_transcription_attempts")) >= MAX_ATTEMPTS:
        return {"message": "Automatic retry limit reached. Use the manual button."}

    # Busy or rate-limited: leave the status alone; the scheduler picks it up later.
    if in_cooldown():
        return {"message": "Sarvam cooldown active. The scheduler will retry."}

    if check_capacity and active_job_count() >= MAX_CONCURRENT_JOBS:
        return {"message": "Transcription queue is busy. The scheduler will retry."}

    return _queue_transcription(call_log_name)


def retry_pending_transcriptions():
    """Queue a few eligible logs per run, respecting concurrency and cooldown.
    Only logs created on/after the start date."""
    if in_cooldown():
        return

    now = now_datetime()
    stale_before = add_to_date(now, minutes=-STALE_AFTER_MINUTES)
    base_filters = {
        "start_date": get_datetime(TRANSCRIPTION_START_DATE),
        "max_attempts": MAX_ATTEMPTS,
        "retry_before": add_to_date(now, minutes=-RETRY_AFTER_MINUTES),
        "stale_before": stale_before,
    }

    # 1) Recover stuck jobs (worker died). Lock prevents double-queueing.
    stuck = frappe.db.sql(
        """
        select name from `tabCall Log`
        where creation >= %(start_date)s
          and ifnull(custom_url, '') != ''
          and ifnull(custom_transcript, '') = ''
          and custom_transcription_status in ('Pending', 'Processing')
          and modified <= %(stale_before)s
          and ifnull(custom_transcription_attempts, 0) < %(max_attempts)s
        order by creation asc
        limit 10
        """,
        base_filters,
        pluck=True,
    )
    for name in stuck:
        _safe_queue(name, stuck_job=True)

    # 2) New / retryable logs, limited by free slots.
    slots = min(BATCH_SIZE, MAX_CONCURRENT_JOBS - active_job_count())
    if slots <= 0:
        return

    names = frappe.db.sql(
        """
        select name from `tabCall Log`
        where creation >= %(start_date)s
          and ifnull(custom_url, '') != ''
          and ifnull(custom_transcript, '') = ''
          and ifnull(custom_transcription_attempts, 0) < %(max_attempts)s
          and ifnull(custom_transcription_status, '') in ('', 'Waiting for Recording', 'Rate Limited', 'Failed')
          and modified <= %(retry_before)s
        order by creation asc
        limit %(limit)s
        """,
        dict(base_filters, limit=slots),
        pluck=True,
    )
    for name in names:
        if in_cooldown():
            break
        _safe_queue(name)


def _safe_queue(name, stuck_job=False):
    try:
        if stuck_job:
            _queue_transcription(name)
        else:
            # Slots were already calculated by the scheduler.
            _auto_queue_if_eligible(name, check_capacity=False)
        frappe.db.commit()
    except Exception as error:
        frappe.db.rollback()
        frappe.log_error(
            title="Transcription Retry Scheduler Failed",
            message=f"Call Log: {name}\n\n" + transcription_error_details(error),
        )


def _queue_transcription(call_log_name):
    """Internal queue implementation; deliberately not whitelisted."""
    doc = frappe.get_doc("Call Log", call_log_name)
    recording_url = (doc.get("custom_url") or "").strip()

    if not recording_url:
        frappe.throw("The recording URL in custom_url is empty.")

    for fieldname in ("custom_transcript", "custom_transcription_status"):
        if not doc.meta.has_field(fieldname):
            frappe.throw(f"Add the {fieldname} field to Call Log.")

    transcript_type = doc.meta.get_field("custom_transcript").fieldtype
    if transcript_type not in ("Long Text", "Text", "Text Editor"):
        frappe.throw("Set custom_transcript to Long Text, Text or Text Editor.")

    validate_recording_url(recording_url)
    get_sarvam_key()

    key = lock_key(doc.name)
    acquired = frappe.cache().set(key, "queued", nx=True, ex=1800)
    if not acquired:
        return {"message": "Transcription is already queued or processing."}

    try:
        values = {"custom_transcription_status": "Pending"}
        if doc.meta.has_field("custom_transcription_attempts"):
            values["custom_transcription_attempts"] = (
                cint(doc.get("custom_transcription_attempts")) + 1
            )
        doc.db_set(values)

        frappe.enqueue(
            "itgenie.call_transcription.process_transcription",
            queue="long",
            timeout=1800,
            enqueue_after_commit=True,
            call_log_name=doc.name,
            recording_url=recording_url,
        )
    except Exception:
        frappe.cache().delete(key)
        raise

    return {
        "message": "Transcription queued. Reload the Call Log later to see the result."
    }


# ---------------------------------------------------------------- download

def detect_audio_extension(path):
    """Identify the real audio format from the file's first bytes."""
    with open(path, "rb") as f:
        head = f.read(16)

    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    return None


def _download_recording(recording_url, audio_path):
    with requests.get(
        recording_url,
        stream=True,
        timeout=(15, 60),
        allow_redirects=False,
    ) as response:
        if 300 <= response.status_code < 400:
            raise RuntimeError(
                "Recording URL redirected; its download configuration needs adjustment."
            )

        # Smartflo returns these while the recording is still being prepared.
        if response.status_code in (202, 204, 403, 404):
            raise RecordingNotReady(f"Smartflo returned HTTP {response.status_code}.")

        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type or "application/json" in content_type:
            raise RecordingNotReady("Recording URL returned a page instead of audio.")

        size = 0
        with audio_path.open("wb") as output:
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise RuntimeError("Recording exceeds the 100 MB download limit.")
                output.write(chunk)

        if not size:
            raise RecordingNotReady("Recording download was empty.")


# ---------------------------------------------------------------- worker

def _set_status(call_log_name, status, refund_attempt=False):
    if not frappe.db.exists("Call Log", call_log_name):
        return
    values = {"custom_transcription_status": status}
    if refund_attempt:
        attempts = cint(
            frappe.db.get_value("Call Log", call_log_name, "custom_transcription_attempts")
        )
        values["custom_transcription_attempts"] = max(0, attempts - 1)
    frappe.db.set_value("Call Log", call_log_name, values)


def process_transcription(call_log_name, recording_url):
    """Private background worker; store English speaker-labelled output."""
    stage = "Starting"

    try:
        frappe.db.set_value(
            "Call Log", call_log_name, "custom_transcription_status", "Processing"
        )
        frappe.db.commit()

        validate_recording_url(recording_url)
        client = SarvamAI(api_subscription_key=get_sarvam_key())

        with tempfile.TemporaryDirectory() as directory:
            download_path = Path(directory) / "call.download"

            stage = "Downloading Smartflo recording"
            _download_recording(recording_url, download_path)

            stage = "Checking audio format"
            extension = detect_audio_extension(download_path)
            if not extension:
                raise RecordingNotReady(
                    "Downloaded file is not a recognised audio format "
                    f"({download_path.stat().st_size} bytes)."
                )
            audio_path = download_path.rename(Path(directory) / f"call.{extension}")
            audio_size = audio_path.stat().st_size

            stage = "Creating Sarvam transcription job"
            job = sarvam_call(
                client.speech_to_text_job.create_job,
                model="saaras:v4",
                mode="translate",
                with_diarization=True,
            )

            stage = "Uploading audio to Sarvam"
            sarvam_call(job.upload_files, file_paths=[str(audio_path)])

            stage = "Starting Sarvam processing"
            sarvam_call(job.start)

            stage = "Waiting for Sarvam transcription"
            sarvam_call(
                job.wait_until_complete,
                poll_interval=SARVAM_POLL_INTERVAL,
                timeout=900,
            )

            stage = "Checking Sarvam result"
            results = sarvam_call(job.get_file_results)
            if not results.get("successful"):
                job_status = None
                try:
                    job_status = job.get_status()
                except Exception:
                    pass

                diagnostics = {
                    "job_id": getattr(job, "job_id", None),
                    "audio_format": extension,
                    "audio_bytes": audio_size,
                    "failed_files": results.get("failed"),
                    "job_status": job_status,
                }
                raise RuntimeError(
                    "Sarvam did not produce a successful result. Details: "
                    + json.dumps(diagnostics, default=str, ensure_ascii=False)[:4000]
                )

            stage = "Downloading transcript"
            output_dir = Path(directory) / "results"
            output_dir.mkdir()
            sarvam_call(job.download_outputs, output_dir=str(output_dir))

            files = list(output_dir.rglob("*.json"))
            if len(files) != 1:
                raise RuntimeError("Expected one transcript output.")

            stage = "Formatting speaker transcript"
            data = json.loads(files[0].read_text(encoding="utf-8"))
            diarized = data.get("diarized_transcript")
            entries = (diarized.get("entries") or []) if isinstance(diarized, dict) else []

            if not entries:
                plain_text = data.get("transcript")
                diagnostics = {
                    "request_id": data.get("request_id"),
                    "top_level_keys": list(data.keys()),
                    "diarized_type": type(diarized).__name__,
                    "diarized_keys": list(diarized.keys()) if isinstance(diarized, dict) else [],
                    "plain_transcript_characters": (
                        len(plain_text.strip()) if isinstance(plain_text, str) else 0
                    ),
                }
                raise RuntimeError(
                    "Sarvam returned no speaker entries. Diagnostics: "
                    + json.dumps(diagnostics, ensure_ascii=False)
                )

            entries = sorted(
                entries,
                key=lambda entry: float(entry.get("start_time_seconds") or 0),
            )
            labels = {}
            lines = []

            for entry in entries:
                text = (entry.get("transcript") or "").strip()
                if not text:
                    continue

                speaker_id = entry.get("speaker_id")
                if speaker_id is None:
                    label = "Unknown Speaker"
                else:
                    speaker_id = str(speaker_id)
                    if speaker_id not in labels:
                        labels[speaker_id] = f"Speaker {len(labels) + 1}"
                    label = labels[speaker_id]

                seconds = max(0, int(float(entry.get("start_time_seconds") or 0)))
                timestamp = f"{seconds // 60:02d}:{seconds % 60:02d}"
                lines.append(f"[{timestamp}] {label}: {text}")

            if not lines:
                raise RuntimeError("Transcript is empty.")
            transcript = "\n\n".join(lines)

            stage = "Saving transcript to Call Log"
            doc = frappe.get_doc("Call Log", call_log_name)
            current_url = (doc.get("custom_url") or "").strip()
            if current_url != recording_url:
                raise RecordingNotReady("Recording URL changed during processing.")

            if doc.meta.get_field("custom_transcript").fieldtype == "Text Editor":
                transcript = (
                    '<pre style="white-space:pre-wrap">'
                    + html.escape(transcript)
                    + "</pre>"
                )

            frappe.db.set_value(
                "Call Log",
                call_log_name,
                {
                    "custom_transcript": transcript,
                    "custom_transcription_status": "Completed",
                },
            )
            frappe.db.commit()

            from itgenie.call_summary import queue_summary_after_transcription

            queue_summary_after_transcription(call_log_name)

    except SarvamRateLimited:
        # Not the call's fault: don't burn an attempt, retry after cooldown.
        frappe.db.rollback()
        _set_status(call_log_name, "Rate Limited", refund_attempt=True)
        frappe.db.commit()

    except RecordingNotReady as error:
        frappe.db.rollback()
        attempts = cint(
            frappe.db.get_value("Call Log", call_log_name, "custom_transcription_attempts")
        )
        final = attempts >= MAX_ATTEMPTS

        if final:
            frappe.log_error(
                title="Call Recording Never Became Available",
                message=f"Call Log: {call_log_name}\nAttempts: {attempts}\nReason: {error}",
            )

        _set_status(call_log_name, "Failed" if final else "Waiting for Recording")
        frappe.db.commit()

    except Exception as error:
        details = transcription_error_details(error)
        frappe.db.rollback()
        frappe.log_error(
            title="Call Transcription Worker Failed",
            message=(
                f"Call Log: {call_log_name}\n"
                f"User: {frappe.session.user}\n"
                f"Stage: {stage}\n"
                f"Error type: {type(error).__name__}\n\n"
                f"Traceback:\n{details}"
            ),
        )
        _set_status(call_log_name, "Failed")
        frappe.db.commit()

    finally:
        frappe.cache().delete(lock_key(call_log_name))
