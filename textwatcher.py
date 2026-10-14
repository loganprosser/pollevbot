#!/usr/bin/env python3

from playwright.sync_api import sync_playwright
from pathlib import Path
from datetime import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
import threading
import queue
import requests
import re


def log(*args, **kwargs):
    # Diagnostics only, never real output -- keeps stdout clean for anything
    # that pipes this process (e.g. an MCP server embedding these functions,
    # where stdout must carry nothing but protocol messages).
    print(*args, file=sys.stderr, **kwargs)


def _load_dotenv(path=".env"):
    """Loads KEY=VALUE (or KEY: VALUE) lines from a local .env into
    os.environ, without overwriting anything already set (a real env var /
    `source config.sh` still wins) -- so running the script directly, with
    no shell setup step first, still picks up secrets. Confirmed live:
    forgetting to `source config.sh` silently dropped IMESSAGE_RECIPIENT and
    every failure text with it.

    Only picks up already-SCREAMING_CASE keys (real env-var names, e.g.
    IMESSAGE_RECIPIENT) -- deliberately skips lowercase entries like this
    file's `user:`/`pass:` (the throwaway PollEv test account, read directly
    by other tooling). Force-uppercasing every key would silently clobber
    unrelated real env vars a lowercase key happens to collide with (e.g.
    `user:` -> $USER, your actual OS username)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sep = "=" if "=" in line else ":" if ":" in line else None
        if not sep:
            continue
        key, value = line.split(sep, 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            continue
        os.environ.setdefault(key, value.strip())


_load_dotenv()

# =========================
# CONFIG
# =========================

POLL_URL = "https://pollev.com/YOUR_PRESENTER"  # <- your class's pollev.com/<name> link
USERNAME = "YOUR_SCREEN_NAME"  # screen name to register as when PollEv asks
# Optional: email to auto-fill on PollEv's own "log in with email" step when a
# poll requires a registered participant. Leave "" to type it yourself.
SSO_EMAIL = os.environ.get("SSO_EMAIL", "")
HEADLESS = False

NTPY_TOPIC = "CHANGE-ME-pollev-alerts"   # <- pick something unique, anyone who knows it can read it
ENABLE_NTFY = True
ENABLE_MAC_NOTIFY = True
ENABLE_MAC_SOUND = True

ENABLE_IMESSAGE = True
IMESSAGE_RECIPIENT = os.environ.get("IMESSAGE_RECIPIENT", "SET ME")  # set in config.sh, not committed

if ENABLE_IMESSAGE and IMESSAGE_RECIPIENT == "SET ME":
    log("[config] ENABLE_IMESSAGE is True but IMESSAGE_RECIPIENT isn't set -- "
        "failure/crash texts won't go anywhere. Did you forget `source config.sh`?")

# Set OPENAI_API_KEY (see config.sh) to have the LLM clean up question/option
# text pulled off the page. Without a key, the deterministic DOM scrape below
# is used as-is -- still gets you type + raw text, just less polished.
ENABLE_LLM_REFINE = True
OPENAI_MODEL = "gpt-5.4"

# PollEv can request browser geolocation independent of anything the poll
# itself needs -- this feeds it a fixed position instead of your real one,
# via Playwright's own built-in override (the same mechanism Chrome DevTools'
# "Sensors" panel uses), not a JS-injection trick. Set OVERRIDE_GEOLOCATION
# to False to just deny the permission outright instead (PollEv gets nothing
# at all rather than a fake position -- stronger privacy, but some sites
# behave differently on outright denial vs. a granted position).
OVERRIDE_GEOLOCATION = True
GEO_LATITUDE = 0.0        # <- any position you like
GEO_LONGITUDE = 0.0
GEO_ACCURACY = 50.0       # meters

# Snapshot throttling
MIN_SECONDS_BETWEEN_SNAPSHOTS = 2.0

SESSION_STATE_PATH = Path("pollev_session_state.json")  # gitignored -- persists a completed login across runs

# Local storage
BASE_DIR = Path("pollev_logs")
HTML_DIR = BASE_DIR / "html"
TEXT_DIR = BASE_DIR / "text"
IMG_DIR = BASE_DIR / "screenshots"
META_DIR = BASE_DIR / "meta"
WS_DIR = BASE_DIR / "websocket_frames"
QUESTIONS_DIR = BASE_DIR / "questions"
# Project-relative, deliberately -- Claude Code's sandbox denies file/Read
# access outside the working directory (confirmed live: pointed at a /tmp
# image, every Read attempt was denied and it silently guessed a location
# instead of failing), so clickable-image downloads/renders must live here,
# not in a system temp dir.
IMAGES_DIR = BASE_DIR / "images"

for d in [BASE_DIR, HTML_DIR, TEXT_DIR, IMG_DIR, META_DIR, WS_DIR, QUESTIONS_DIR, IMAGES_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# =========================
# HELPERS
# =========================

def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def short_preview(text: str, n: int = 180) -> str:
    one_line = " ".join(text.split())
    return one_line[:n] + ("..." if len(one_line) > n else "")


def sanitize_filename(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return s[:120]


def mac_notify(title: str, message: str):
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'display notification "{safe_message}" with title "{safe_title}"'],
        check=False,
    )


def mac_sound(name: str = "Glass"):
    sound_path = f"/System/Library/Sounds/{name}.aiff"
    if Path(sound_path).exists():
        subprocess.run(["afplay", sound_path], check=False)


# When IMESSAGE_RECIPIENT is a number tied to the same Apple ID as this Mac
# (self-texting), Messages echoes our own outgoing text back into the thread
# as if received -- is_from_me=0, identical to a real reply -- confirmed live
# via chat.db (both an is_from_me=1/text=NULL delivery row and an
# is_from_me=0/real-text echo row appear for a single outgoing send). Anything
# that waits for "a new incoming message" needs to be able to tell that apart
# from a genuine reply. Every text we actually send gets recorded here so
# ai_answer._wait_for_imessage_reply can skip exact matches instead of
# mistaking our own notification for a human confirmation.
SENT_MESSAGE_TEXTS = set()


def imessage_notify(message: str):
    if not ENABLE_IMESSAGE or not IMESSAGE_RECIPIENT or IMESSAGE_RECIPIENT == "SET ME":
        return
    SENT_MESSAGE_TEXTS.add(message)
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Messages"\n'
        '  set targetService to 1st service whose service type = iMessage\n'
        f'  set targetBuddy to buddy "{IMESSAGE_RECIPIENT}" of targetService\n'
        f'  send "{safe_message}" to targetBuddy\n'
        'end tell'
    )
    subprocess.run(["osascript", "-e", script], check=False)


def imessage_send_image(image_path, caption: str = ""):
    """Send an image file as an iMessage attachment (AppleScript's `send
    POSIX file ... to buddy` -- a real file attachment, not text). Best
    effort: any failure is caught and logged, never raised, since this is
    always a nice-to-have alongside a text notification, not the thing that
    actually carries the information."""
    if not ENABLE_IMESSAGE or not IMESSAGE_RECIPIENT or IMESSAGE_RECIPIENT == "SET ME":
        return
    path = Path(image_path).resolve()
    if not path.exists():
        log(f"[imessage] image not found, skipping picture: {path}")
        return
    safe_caption = caption.replace("\\", "\\\\").replace('"', '\\"')
    script_lines = [
        'tell application "Messages"',
        '  set targetService to 1st service whose service type = iMessage',
        f'  set targetBuddy to buddy "{IMESSAGE_RECIPIENT}" of targetService',
    ]
    if safe_caption:
        SENT_MESSAGE_TEXTS.add(caption)
        script_lines.append(f'  send "{safe_caption}" to targetBuddy')
    script_lines += [
        f'  send POSIX file "{path}" to targetBuddy',
        'end tell',
    ]
    try:
        subprocess.run(["osascript", "-e", "\n".join(script_lines)], check=False, timeout=15)
    except Exception as e:
        log(f"[imessage] failed to send image: {e}")


def ntfy_notify(title: str, message: str, url: str = ""):
    if not ENABLE_NTFY:
        return

    body = message if not url else f"{message}\n{url}"
    headers = {"Title": title}

    try:
        requests.post(
            f"https://ntfy.sh/{NTPY_TOPIC}",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
    except Exception as e:
        log(f"[ntfy] failed: {e}")


def notify_all(title: str, message: str, url: str = ""):
    if ENABLE_MAC_NOTIFY:
        mac_notify(title, message)
    if ENABLE_MAC_SOUND:
        mac_sound()
    imessage_notify(f"{title}: {message}" + (f"\n{url}" if url else ""))
    ntfy_notify(title, message, url)


# =========================
# POLL EXTRACTION
# =========================
# PollEv's participant page hydrates its "legacy" activity renderer from an
# embedded JS object: window.legacyActivityResponseView.setup({activity: {...}}).
# That JSON is the app's own data model -- far more reliable than scraping
# labels/roles off the rendered DOM -- and it's absent entirely when no
# activity is active (idle "waiting for presentation" screen), so its mere
# presence doubles as the trigger. Confirmed live against real test polls:
#   multiple_choice_poll + responses_per_option == 1  -> single-select (radio)
#   multiple_choice_poll + responses_per_option != 1  -> multi-select (checkbox)
#   free_text_poll                                    -> short answer
# Other activity types (word_cloud, survey, clickable_image, competition, ...)
# aren't mapped -- they fall through to "unknown" with the raw JSON preserved
# so nothing is lost (can still be fed to a model downstream).

ACTIVITY_JSON_MARKER = "activity: {"


def extract_first_json_object(text: str, start_idx: int = 0):
    """Find and parse the first balanced {...} object starting at or after
    start_idx, respecting JSON string literals (so braces inside quoted
    strings, e.g. LaTeX '\\frac{1}{2}', don't throw off the brace count).
    Returns None if there's no valid JSON object to find."""
    start = text.find("{", start_idx)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def extract_activity_json(html: str):
    idx = html.find(ACTIVITY_JSON_MARKER)
    if idx == -1:
        return None
    return extract_first_json_object(html, idx)


def extract_poll_dom(page):
    """Returns None if no live activity is active."""
    try:
        activity = extract_activity_json(page.content())
    except Exception as e:
        log(f"[extract] failed to read page content: {e}")
        return None
    if not activity:
        return None

    a_type = activity.get("type")
    title = (activity.get("title") or "").strip()
    # Keep each option's PollEv option id alongside its text -- this is what
    # ties a specific answer back to a specific option once responses are
    # captured, instead of a bare list of strings that's ambiguous once
    # you're looking at many questions' worth of files side by side.
    options = [
        {"id": o.get("id"), "text": o["value"]}
        for o in activity.get("options", []) if o.get("value")
    ]

    if a_type == "multiple_choice_poll":
        # max_votes is PollEv's "each person may respond" cap -- 1 means a
        # participant can only click one option ever (single-select), null/other
        # means they can click more than one (checkbox-style select-multiple).
        # Verified live: clicking a 2nd option is disabled in the UI when
        # max_votes==1, and allowed (and both register) when it isn't.
        # NB: responses_per_option ("each option may be selected") is a
        # different, audience-wide cap on a single option's total vote count
        # across all participants -- it does not affect one participant's
        # ability to select multiple options, despite what the setting's
        # label might suggest.
        poll_type = "multiple_choice" if activity.get("max_votes") == 1 else "select_multiple"
        return {"type": poll_type, "question": title, "options": options, "raw": activity}
    if a_type == "free_text_poll":
        return {"type": "short_answer", "question": title, "options": [], "raw": activity}
    if a_type == "clickable_image":
        # Same max_votes semantics as multiple_choice_poll -- verified live:
        # ==1 means one click ever (undo required to change it), anything
        # else means multiple distinct clicks all register as separate
        # responses. There's exactly one catch-all "region:other" option
        # regardless of where you click -- no predefined per-region options
        # to resolve against, unlike multiple_choice.
        poll_type = "clickable_image" if activity.get("max_votes") == 1 else "clickable_image_multi"
        image_url = (activity.get("type_options") or {}).get("imageUrl")
        return {"type": poll_type, "question": title, "options": [], "image_url": image_url, "raw": activity}
    return {"type": "unknown", "question": title, "options": options, "raw": activity}


def llm_refine_unknown(raw_text: str, activity: dict):
    """Only invoked for activity types we don't have a deterministic mapping
    for (word_cloud, survey, competition, ...) -- for
    multiple_choice/select_multiple/short_answer/clickable_image the PollEv
    JSON already gives us what we need, no LLM needed."""
    if not ENABLE_LLM_REFINE:
        return None
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        prompt = (
            "This is a live PollEverywhere activity of an unrecognized type "
            f"('{activity.get('type')}'). Classify it as one of "
            '"multiple_choice", "select_multiple", or "short_answer" if it '
            "reasonably fits, else \"unknown\". Extract the question text and "
            "answer options if any (don't invent any). "
            'Return strict JSON only: {"type": str, "question": str, "options": [str, ...]}\n\n'
            f"Raw activity JSON: {json.dumps(activity)}\n\n"
            f"Page text:\n{raw_text}"
        )
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=[{"role": "user", "content": prompt}],
        )
        return json.loads(resp.output_text)
    except Exception as e:
        log(f"[llm] refine failed: {e}")
        return None


def build_question_record(page, text: str, extracted: dict, ts: str) -> dict:
    source = "dom"
    poll_type = extracted["type"]
    question = extracted["question"]
    options = extracted["options"]  # [{"id": <poll option id>, "text": str}, ...]

    if poll_type == "unknown":
        refined = llm_refine_unknown(text, extracted["raw"])
        if refined:
            poll_type = refined.get("type", poll_type)
            question = refined.get("question") or question
            # LLM has no access to PollEv's real option ids -- id stays None
            # so it's obvious these came from the fallback, not the source of truth.
            options = [{"id": None, "text": o} for o in refined.get("options") or []] or options
            source = "llm"

    raw = extracted.get("raw") or {}
    return {
        # PollEv's own activity id + url slug -- the stable identifiers that
        # tie this record to *this* question, even across many files/questions
        # detected in the same watch session (or the same question re-asked later).
        "question_id": raw.get("id"),
        "poll_key": raw.get("shared_key"),
        "timestamp": ts,
        "url": page.url,
        "type": poll_type,
        "question": question or short_preview(text, 300),
        "options": options,
        "image_url": extracted.get("image_url"),  # only set for clickable_image[_multi]
        "answer": None,  # set once/if response capture is added; each option's "id" is what an answer would reference
        "source": source,
    }


QUESTIONS_INDEX = QUESTIONS_DIR / "index.jsonl"


def save_question_json(record: dict, ts: str) -> Path:
    path = QUESTIONS_DIR / f"poll_{ts}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    # One line per question ever detected, newest last -- a quick "what
    # questions have we seen and in what order" view without opening every file.
    with open(QUESTIONS_INDEX, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": record["timestamp"],
            "question_id": record["question_id"],
            "type": record["type"],
            "question": record["question"],
            "file": path.name,
        }) + "\n")

    return path


# =========================
# ANSWER SUBMISSION API
# =========================
# Submission is done by driving the real page (clicking the real option
# button / filling the real textarea and clicking Submit), not by replicating
# PollEv's internal POST requests -- confirmed live that a click IS the vote
# (no separate submit step for MC/select_multiple; free_text has one visible
# Submit button). This reuses the page's own auth/session, so there's no
# CSRF/cookie plumbing to maintain.

class StaleQuestionError(RuntimeError):
    """The question you're trying to answer is no longer the live one."""


class AnswerError(ValueError):
    """`answer` doesn't match the question's type/current options."""


def get_current_question(page):
    """The live question right now, in the same shape as a saved question
    record, or None if nothing is live. Safe to poll repeatedly."""
    extracted = extract_poll_dom(page)
    if not extracted:
        return None
    text = page.locator("body").inner_text().strip()
    return build_question_record(page, text, extracted, now_ts())


def _resolve_option(options, value):
    for o in options:
        if o["id"] is not None and value == o["id"]:
            return o
    for o in options:
        if isinstance(value, str) and o["text"] == value:
            return o
    raise AnswerError(f"{value!r} doesn't match any current option: {[o['text'] for o in options]}")


def _click_option(page, option: dict):
    # PollEv briefly (~300-500ms) disables every option button in the group
    # while a just-submitted vote is in flight -- confirmed live. So we don't
    # pre-check disabled state ourselves; Playwright's own click() already
    # waits/retries for an element to become actionable. Only a click that's
    # STILL failing after the full timeout (5s, well past that debounce
    # window) means either it's genuinely stuck, or (more likely, if we've
    # already answered this question before) a DIFFERENT option is currently
    # selected and blocking this one -- try the presenter's "change answer"
    # path once (undo whichever option is selected, then retry) before
    # giving up for real.
    btn = page.locator("button.component-response-multiple-choice__option__vote", has_text=option["text"]).first
    try:
        btn.click(timeout=5000)
        return
    except Exception as e:
        first_error = e  # `except ... as e` unbinds e after this block, so save it

    undo_btn = page.locator("button.component-response-multiple-choice__option__undo:visible").first
    if undo_btn.count() == 0:
        raise AnswerError(
            f"couldn't click option '{option['text']}' -- you've likely already "
            f"answered this question and changing answers isn't allowed for it: {first_error}"
        ) from first_error

    try:
        undo_btn.click(timeout=3000)
        page.wait_for_timeout(400)
        btn.click(timeout=5000)
    except Exception as e:
        raise AnswerError(
            f"couldn't click option '{option['text']}' even after undoing the "
            f"previous answer -- it may not have been the one blocking it: {e}"
        ) from e


def download_image(url: str, dest_path) -> Path:
    """Download a clickable-image poll's image to a project-relative path
    (must be under the project directory, not a system temp dir -- see
    IMAGES_DIR). Raises on failure rather than silently returning nothing;
    a missing image means there's nothing to reason about at all."""
    dest_path = Path(dest_path)
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)
    return dest_path


def render_marked_image(browser, image_path, clicks, dest_path, marker_radius: int = 14) -> Path:
    """Overlay a marker at each fractional {"x","y"} point in `clicks` on a
    copy of the image, for the "text me a picture of the selection" step.
    Renders via Playwright (load the image + positioned marker divs in a
    throwaway page on the SAME browser, screenshot) rather than adding an
    image-manipulation dependency (e.g. Pillow) for one feature.

    Takes an existing `browser` (e.g. page.context.browser) and opens a new
    page on it -- NOT its own sync_playwright() context. Confirmed live that
    starting a second sync_playwright() from inside code already running
    under one raises "using Playwright Sync API inside the asyncio loop",
    which this always would if called from within answer_with_ai's own
    already-open browser session.

    Embeds the image as a base64 data: URI rather than a file:// URL --
    confirmed live that a page opened via set_content() (about:blank origin)
    silently fails to load a file:// <img src>, leaving naturalWidth == 0 and
    a near-empty screenshot with no error raised anywhere. A data: URI has
    no cross-origin loading restriction to hit."""
    import base64
    image_path = Path(image_path).resolve()
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    data_uri = f"data:{mime};base64,{base64.b64encode(image_path.read_bytes()).decode()}"

    # Lime fill + black/white double ring -- a red-only marker was confirmed
    # live to be invisible against a red target (blended right in). This
    # combination has no single color that can fully disappear into an
    # arbitrary background.
    markers_html = "".join(
        f'<div style="position:absolute;left:calc({c["x"]*100}% - {marker_radius}px);'
        f'top:calc({c["y"]*100}% - {marker_radius}px);width:{marker_radius*2}px;height:{marker_radius*2}px;'
        f'border-radius:50%;background:#39ff14;border:3px solid black;'
        f'box-shadow:0 0 0 2px white;box-sizing:border-box;"></div>'
        for c in clicks
    )
    html = (
        f'<div id="marked-wrapper" style="position:relative;display:inline-block;line-height:0">'
        f'<img id="target-image" src="{data_uri}" style="display:block">'
        f'{markers_html}</div>'
    )
    pg = browser.new_page()
    try:
        pg.set_content(html)
        pg.wait_for_selector("#target-image")
        pg.locator("#target-image").evaluate(
            "img => img.complete || new Promise(r => { img.onload = r; img.onerror = r; })"
        )
        natural_w = pg.evaluate("document.getElementById('target-image').naturalWidth")
        if not natural_w:
            raise RuntimeError("image failed to load while rendering marked image")
        pg.locator("#marked-wrapper").screenshot(path=str(dest_path))
    finally:
        pg.close()
    return Path(dest_path)


def _click_image_point(page, x_frac: float, y_frac: float):
    """Click a clickable-image poll at a fractional (0.0-1.0) position of
    the image's rendered size. Confirmed live that PollEv computes the same
    percentage from click position regardless of natural vs. rendered pixel
    size (uniform scaling, no cropping), so a plain fraction works without
    needing the image's natural dimensions at all. Same undo-then-retry
    fallback as _click_option, using the single "Clear last response" button
    (there's one undo button total here, not one per option -- there are no
    predefined options for a click)."""
    img = page.locator("img.component-response-clickable-image__image").first
    box = img.bounding_box()
    if not box:
        raise AnswerError("couldn't find the clickable image on the page")
    target = {"x": x_frac * box["width"], "y": y_frac * box["height"]}

    try:
        img.click(position=target, timeout=5000)
        return
    except Exception as e:
        first_error = e

    undo_btn = page.locator("button.component-response-clickable-image__undo:visible").first
    if undo_btn.count() == 0:
        raise AnswerError(
            f"couldn't click at ({x_frac:.3f}, {y_frac:.3f}) -- you've likely already "
            f"answered this question and changing answers isn't allowed for it: {first_error}"
        ) from first_error

    try:
        undo_btn.click(timeout=3000)
        page.wait_for_timeout(400)
        img.click(position=target, timeout=5000)
    except Exception as e:
        raise AnswerError(
            f"couldn't click at ({x_frac:.3f}, {y_frac:.3f}) even after undoing the "
            f"previous answer: {e}"
        ) from e


def submit_answer(page, question: dict, answer):
    """
    Submit `answer` for `question` (as returned by get_current_question()).

    Spec for `answer`, by question["type"]:
      multiple_choice        -> a single option id (int) or option text (str)
      select_multiple        -> a list of option ids/texts -- each one gets clicked
      short_answer           -> a plain non-empty string
      clickable_image[_multi] -> a list of {"x": float, "y": float} fractions (0.0-1.0
                                 of the image's width/height) -- single-click type still
                                 wants a length-1 list, for a uniform shape across both

    Calling this again for the same still-live question changes the answer:
    for multiple_choice/select_multiple it undoes whatever was previously
    selected and clicks the new option(s) (verified live -- this is exactly
    how PollEv's own "change answer" works, gated by the presenter's Response
    Settings > Change answer toggle, default allowed, and only possible while
    the question is still active). short_answer has no true replace on
    PollEv's side -- resubmitting adds a second response rather than
    overwriting the first (PollEv keeps a running list of open-ended
    responses per person), so "changing" a short answer isn't meaningful here.

    Raises StaleQuestionError if the live question has moved on since
    `question` was fetched, or AnswerError if `answer` doesn't match the
    type/current options, an option can't be clicked (e.g. changing isn't
    allowed for this question), or the short-answer form can't be submitted.
    Never lets a raw Playwright error escape -- always one of these two, or
    success. Returns `question` with "answer" filled in.
    """
    current = get_current_question(page)
    if not current or current["question_id"] != question["question_id"]:
        raise StaleQuestionError(
            f"question {question.get('question_id')} is no longer live "
            f"(current: {current.get('question_id') if current else None})"
        )

    qtype = question["type"]
    if qtype == "multiple_choice":
        option = _resolve_option(current["options"], answer)
        _click_option(page, option)
        submitted = option
    elif qtype == "select_multiple":
        if not isinstance(answer, (list, tuple)) or not answer:
            raise AnswerError("select_multiple answer must be a non-empty list of option ids/texts")
        submitted = []
        for value in answer:
            try:
                option = _resolve_option(current["options"], value)
                _click_option(page, option)
            except AnswerError as e:
                already = [o["text"] for o in submitted]
                raise AnswerError(
                    f"{e} ({len(already)} option(s) already submitted before this failure: {already})"
                ) from e
            page.wait_for_timeout(300)  # let each vote register before the next click
            submitted.append(option)
    elif qtype == "short_answer":
        if not isinstance(answer, str) or not answer.strip():
            raise AnswerError("short_answer answer must be a non-empty string")
        try:
            textarea = page.locator("textarea[name='response']")
            textarea.fill(answer, timeout=5000)
            page.locator("input[type='submit']").click(timeout=5000)
        except Exception as e:
            raise AnswerError(f"failed to submit short answer: {e}") from e
        submitted = answer
    elif qtype in ("clickable_image", "clickable_image_multi"):
        if not isinstance(answer, (list, tuple)) or not answer:
            raise AnswerError(f"{qtype} answer must be a non-empty list of {{'x':.., 'y':..}} fractional points")
        submitted = []
        for point in answer:
            try:
                x, y = float(point["x"]), float(point["y"])
            except (TypeError, KeyError, ValueError):
                raise AnswerError(f"each click must be {{'x':.., 'y':..}} fractional coordinates, got {point!r}")
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise AnswerError(f"click coordinates must be fractions 0.0-1.0, got {point!r}")
            try:
                _click_image_point(page, x, y)
            except AnswerError as e:
                already = list(submitted)
                raise AnswerError(
                    f"{e} ({len(already)} click(s) already submitted before this failure: {already})"
                ) from e
            page.wait_for_timeout(300)
            submitted.append({"x": x, "y": y})
    else:
        raise AnswerError(f"don't know how to submit an answer for type {qtype!r}")

    page.wait_for_timeout(500)
    result = dict(question)
    result["answer"] = submitted
    return result


# =========================
# SNAPSHOT MANAGER
# =========================

class SnapshotManager:
    def __init__(self):
        self.last_html_hash = None
        self.last_text_hash = None
        self.last_snapshot_monotonic = 0.0

    def should_snapshot(self, html: str, text: str) -> bool:
        html_hash = sha256_text(html)
        text_hash = sha256_text(text)

        changed = (html_hash != self.last_html_hash) or (text_hash != self.last_text_hash)
        if not changed:
            return False

        now = time.monotonic()
        if now - self.last_snapshot_monotonic < MIN_SECONDS_BETWEEN_SNAPSHOTS:
            return False

        self.last_html_hash = html_hash
        self.last_text_hash = text_hash
        self.last_snapshot_monotonic = now
        return True

    def save_snapshot(self, page, reason: str, ws_info: dict | None = None):
        ts = now_ts()

        html = page.content()
        text = page.locator("body").inner_text().strip()

        html_path = HTML_DIR / f"poll_{ts}.html"
        txt_path = TEXT_DIR / f"poll_{ts}.txt"
        img_path = IMG_DIR / f"poll_{ts}.png"
        meta_path = META_DIR / f"poll_{ts}.json"

        html_path.write_text(html, encoding="utf-8")
        txt_path.write_text(text, encoding="utf-8")
        page.screenshot(path=str(img_path), full_page=True)

        meta = {
            "timestamp": ts,
            "url": page.url,
            "reason": reason,
            "preview": short_preview(text),
            "html_file": str(html_path),
            "text_file": str(txt_path),
            "screenshot_file": str(img_path),
            "html_hash": sha256_text(html),
            "text_hash": sha256_text(text),
            "ws_info": ws_info or {},
        }

        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return ts, text, meta_path


# =========================
# WEBSOCKET LOGGING
# =========================

def save_ws_frame(direction: str, ws_url: str, payload: str):
    ts = now_ts()
    safe = sanitize_filename(ws_url)
    out = {
        "timestamp": ts,
        "direction": direction,
        "ws_url": ws_url,
        "payload": payload,
    }
    path = WS_DIR / f"{ts}_{direction}_{safe}.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return path

def new_persistent_context(browser):
    """A browser context that reuses a saved login session if one exists, so
    a completed SSO login doesn't have to be redone every run -- only when
    the underlying session/cookies actually expire server-side (at which
    point wait_for_manual_sso_login will correctly detect the wall again and
    ask for a fresh login, same as the first time). Also applies the
    geolocation override/denial from CONFIG, if enabled."""
    kwargs = {}
    if SESSION_STATE_PATH.exists():
        kwargs["storage_state"] = str(SESSION_STATE_PATH)
    if OVERRIDE_GEOLOCATION:
        kwargs["permissions"] = ["geolocation"]
        kwargs["geolocation"] = {"latitude": GEO_LATITUDE, "longitude": GEO_LONGITUDE, "accuracy": GEO_ACCURACY}
    return browser.new_context(**kwargs)


def save_session_state(context):
    try:
        context.storage_state(path=str(SESSION_STATE_PATH))
    except Exception as e:
        log(f"[session] failed to save session state: {e}")


SSO_LOGIN_WALL_PATTERN = re.compile(
    r"pre-registration required|"  # confirmed live, exact PollEv wall heading
    r"log in to (?:the )?poll everywhere account|"  # confirmed live: "...account registered with <name> to respond"
    r"log ?in to .{0,80}?to (respond|participate)|"
    r"sign ?in to (respond|participate)|"
    r"registered participants only|must be logged ?in|please log ?in to continue|"
    r"you must sign in",
    re.I | re.DOTALL,
)


def _sso_wall_showing(page) -> bool:
    try:
        return bool(SSO_LOGIN_WALL_PATTERN.search(page.locator("body").inner_text()))
    except Exception:
        return False


def advance_sso_identifier_steps(page, max_steps: int = 6):
    """Auto-fills PollEv's own email page (input#email / input[type=email])
    with SSO_EMAIL and continues, if SSO_EMAIL is set. Your school's own SSO
    pages (username, password, 2FA) are left to you. Only ever fills an
    email, never a password. Stops the instant a password field is visible,
    or after max_steps with nothing recognized."""
    for _ in range(max_steps):
        page.wait_for_timeout(800)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        try:
            if page.locator("input[type='password']").first.is_visible(timeout=500):
                return
        except Exception:
            return

        if not SSO_EMAIL:
            return
        try:
            email_input = page.locator("input#email, input[type='email']").first
            if email_input.is_visible(timeout=500):
                email_input.fill(SSO_EMAIL)
                page.locator("input[type='submit'], button[type='submit']").first.click(timeout=3000)
                log("[sso] filled email, continuing")
                continue
        except Exception:
            pass

        return  # nothing recognized -- hand off to you


def wait_for_manual_sso_login(page, timeout_seconds: int = 300, initial_grace_seconds: float = 5.0):
    """If this poll requires a registered (logged-in) participant, PollEv
    shows some login-wall phrasing in the response area instead of the
    anonymous screen-name flow. SSO credentials (password +
    2FA) should never be typed into a script -- this pauses and lets you
    complete the login yourself in this same visible browser window
    (HEADLESS=False shows it), then resumes once the wall phrasing is gone.
    No-op if no login is required.

    The initial check is polled over initial_grace_seconds rather than done
    once immediately after goto() -- PollEv is a React SPA, so right after
    "networkidle" the wall text may not have rendered yet, and a one-shot
    check on a blank page was confirmed live to wrongly conclude "no login
    needed" and save an empty (unauthenticated) session state.

    Auto-clicks the wall's own "Log in" button (role=button, exact text) to
    jump straight to the SSO page -- but never types credentials or
    touches 2FA; those need you. This button is scoped separately from
    PollEv's site nav, which has its own ever-present "Log in" *link* (for
    presenter accounts) that has nothing to do with participant access --
    matching on that naively and clicking it was confirmed live to navigate
    away to the presenter login page and break the session entirely.

    Also auto-fills PollEv's own email-entry page if SSO_EMAIL is set (see
    advance_sso_identifier_steps). Stops the instant a password field is visible.
    """
    start = time.time()
    while time.time() - start < initial_grace_seconds:
        if _sso_wall_showing(page):
            break
        page.wait_for_timeout(500)
    else:
        return

    log("[sso] this poll looks like it requires login. Complete it yourself "
        "in the browser window (SSO + 2FA) -- waiting for you...")

    try:
        # The wall's own button is an <a class="...mdc-button...">, not a
        # role=button element -- confirmed live. Scoped to that class (not
        # just link text "Log in") because the site nav has its own,
        # unrelated "Log in" link elsewhere on the same page (see above).
        btn = page.locator("a.mdc-button", has_text=re.compile(r"^\s*log ?in\s*$", re.I)).first
        if btn.is_visible(timeout=2000):
            btn.click(timeout=2000)
            log("[sso] clicked the poll's Log in button")
    except Exception:
        pass

    advance_sso_identifier_steps(page)

    start = time.time()
    while time.time() - start < timeout_seconds:
        page.wait_for_timeout(2000)
        # Must check both: advance_sso_identifier_steps navigates off
        # pollev.com onto your school's login pages, where the wall text is
        # trivially absent -- checking only that was confirmed live to
        # falsely declare "logged in" the instant the password page loaded,
        # before you'd typed anything.
        try:
            back_on_pollev = "pollev.com" in page.url
        except Exception:
            back_on_pollev = False
        if back_on_pollev and not _sso_wall_showing(page):
            log("[sso] login wall no longer showing, continuing")
            return

    raise TimeoutError(f"SSO login wasn't completed within {timeout_seconds}s")


def dismiss_cookie_banner(page, grace_seconds: float = 4.0):
    """Dismiss the cookie-consent banner if present -- called before the SSO
    wall check so the banner never covers the wall's Log in button. Polled
    over grace_seconds, not a one-shot check -- same SPA-hydration race as
    the SSO wall (confirmed live: right after goto()'s "networkidle", the
    page can still have zero buttons in the DOM at all)."""
    cookie_selectors = [
        lambda: page.get_by_role("button", name=re.compile(r"accept|agree|allow", re.I)),
        lambda: page.get_by_text(re.compile(r"accept|agree|allow", re.I)),
    ]

    start = time.time()
    while time.time() - start < grace_seconds:
        for selector_fn in cookie_selectors:
            try:
                locator = selector_fn()
                if locator.first.is_visible(timeout=500):
                    locator.first.click(timeout=2000)
                    log("[setup] accepted cookies")
                    page.wait_for_timeout(1000)
                    return
            except Exception:
                pass
        page.wait_for_timeout(500)

    log("[setup] no cookie banner found")


def handle_initial_pollev_steps(page):
    log("[setup] checking for username step")

    # ---- fill username if PollEv asks for one ----
    input_candidates = [
        page.locator("input[type='text']"),
        page.locator("input[type='name']"),
        page.locator("input"),
    ]

    name_filled = False
    for locator in input_candidates:
        try:
            if locator.first.is_visible(timeout=3000):
                locator.first.fill(USERNAME, timeout=3000)
                log(f"[setup] filled username: {USERNAME}")
                name_filled = True
                page.wait_for_timeout(500)
                break
        except Exception:
            pass

    if not name_filled:
        log("[setup] no username input found")
        return

    # ---- click continue / submit if present ----
    button_candidates = [
        lambda: page.get_by_role("button", name=re.compile(r"continue|submit|enter|join|start", re.I)),
        lambda: page.get_by_text(re.compile(r"continue|submit|enter|join|start", re.I)),
    ]

    clicked_continue = False
    for selector_fn in button_candidates:
        try:
            locator = selector_fn()
            if locator.first.is_visible(timeout=3000):
                locator.first.click(timeout=3000)
                log("[setup] clicked continue/submit")
                clicked_continue = True
                page.wait_for_timeout(2000)
                break
        except Exception:
            pass

    if not clicked_continue:
        # try Enter key as fallback
        try:
            page.keyboard.press("Enter")
            log("[setup] pressed Enter as fallback")
            page.wait_for_timeout(2000)
        except Exception:
            log("[setup] could not submit username step")


# =========================
# MAIN
# =========================

def main():
    events = queue.Queue()
    snaps = SnapshotManager()
    seen_questions = set()  # ponytail: in-memory only, resets on restart

    log(f"[watching] {POLL_URL}")
    log(f"[logs] {BASE_DIR.resolve()}")
    log(f"[ntfy topic] {NTPY_TOPIC}")

    def maybe_report_poll(page, text: str):
        extracted = extract_poll_dom(page)
        if not extracted:
            return

        option_texts = [o["text"] for o in extracted["options"]]
        key = sha256_text(extracted["type"] + "|" + extracted["question"] + "|" + "|".join(option_texts))
        if key in seen_questions:
            return
        seen_questions.add(key)

        ts = now_ts()
        record = build_question_record(page, text, extracted, ts)
        q_path = save_question_json(record, ts)

        log(f"[poll detected] {record['type']}: {record['question']}")
        log(f"[question json] {q_path}")

        summary = record["question"]
        if record["options"]:
            summary += " | " + " / ".join(o["text"] for o in record["options"])
        notify_all(f"PollEv: {record['type']}", summary, page.url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = new_persistent_context(browser)
        page = context.new_page()

        # Mark DOM mutations from inside the page.
        page.add_init_script(
            """
            () => {
              window.__POLLEV_MUTATION_TICK__ = 0;
              const obs = new MutationObserver(() => {
                window.__POLLEV_MUTATION_TICK__ += 1;
              });
              window.addEventListener('DOMContentLoaded', () => {
                obs.observe(document.documentElement, {
                  subtree: true,
                  childList: true,
                  attributes: true,
                  characterData: true
                });
              });
            }
            """
        )

        def enqueue(kind: str, data: dict):
            events.put((kind, data))

        def on_websocket(ws):
            ws_url = ws.url
            log(f"[ws] opened: {ws_url}")

            def frame_received(payload):
                try:
                    payload_str = payload if isinstance(payload, str) else str(payload)
                except Exception:
                    payload_str = "<unprintable-payload>"

                save_ws_frame("recv", ws_url, payload_str)
                enqueue("ws_recv", {"ws_url": ws_url, "payload": payload_str})

            def frame_sent(payload):
                try:
                    payload_str = payload if isinstance(payload, str) else str(payload)
                except Exception:
                    payload_str = "<unprintable-payload>"

                save_ws_frame("sent", ws_url, payload_str)
                enqueue("ws_sent", {"ws_url": ws_url, "payload": payload_str})

            def ws_closed():
                log(f"[ws] closed: {ws_url}")
                enqueue("ws_closed", {"ws_url": ws_url})

            ws.on("framereceived", frame_received)
            ws.on("framesent", frame_sent)
            ws.on("close", ws_closed)

        page.on("websocket", on_websocket)

        try:
            page.goto(POLL_URL, wait_until="networkidle")
            dismiss_cookie_banner(page)
            wait_for_manual_sso_login(page)
            save_session_state(context)  # persist login (or cookie consent/guest state) for next run
            handle_initial_pollev_steps(page)

            # Initial snapshot
            initial_html = page.content()
            initial_text = page.locator("body").inner_text().strip()
            if snaps.should_snapshot(initial_html, initial_text):
                ts, text, meta = snaps.save_snapshot(page, reason="initial_load")
                log(f"[saved] initial snapshot at {ts}")
                maybe_report_poll(page, text)

            last_mutation_tick = 0
            last_wall_check = 0.0

            while True:
                # The SSO wall check only runs once at startup -- if it
                # reappears mid-session (session expiry, or a later question
                # re-requiring registration), it settles into a static page
                # with no further mutations to react to, and this
                # mutation-driven loop would otherwise just go quiet forever.
                # Throttled to every 5s, not every 0.25s tick.
                if time.time() - last_wall_check > 5.0:
                    last_wall_check = time.time()
                    if _sso_wall_showing(page):
                        log("[sso] wall reappeared mid-session -- re-authenticating")
                        notify_all(
                            "PollEv: login needed again",
                            "The registration wall came back mid-session -- attempting to log back in automatically.",
                            page.url,
                        )
                        wait_for_manual_sso_login(page)
                        save_session_state(context)
                        log("[sso] re-authentication finished, resuming detection")

                # Process queued websocket events
                while not events.empty():
                    kind, data = events.get_nowait()

                    html = page.content()
                    text = page.locator("body").inner_text().strip()

                    if snaps.should_snapshot(html, text):
                        ts, text, meta_path = snaps.save_snapshot(
                            page,
                            reason=kind,
                            ws_info=data,
                        )
                        log(f"[saved] {ts} because {kind}")
                        log(f"[meta] {meta_path}")
                        maybe_report_poll(page, text)

                # Fallback: detect DOM mutation even if no visible websocket frame event was useful
                try:
                    tick = page.evaluate("window.__POLLEV_MUTATION_TICK__ || 0")
                except Exception:
                    tick = last_mutation_tick

                if tick != last_mutation_tick:
                    last_mutation_tick = tick
                    html = page.content()
                    text = page.locator("body").inner_text().strip()

                    if snaps.should_snapshot(html, text):
                        ts, text, meta_path = snaps.save_snapshot(page, reason="dom_mutation")
                        log(f"[saved] {ts} because dom_mutation")
                        log(f"[meta] {meta_path}")
                        maybe_report_poll(page, text)

                time.sleep(0.25)

        except KeyboardInterrupt:
            log("\n[stopped]")
        except Exception as e:
            # Anything that escapes here (SSO timeout, browser/page crash,
            # an unhandled bug) would otherwise die silently with just a
            # terminal traceback -- text it too, since that's the whole
            # point of running this unattended during class.
            log(f"[crashed] {type(e).__name__}: {e}")
            try:
                url = page.url
            except Exception:
                url = POLL_URL
            notify_all(
                "PollEv bot crashed",
                f"{type(e).__name__}: {e}\nIt has stopped watching -- go answer manually, then restart it.",
                url,
            )
            raise
        finally:
            save_session_state(context)  # re-save in case cookies rotated during the run

        browser.close()


if __name__ == "__main__":
    main()
