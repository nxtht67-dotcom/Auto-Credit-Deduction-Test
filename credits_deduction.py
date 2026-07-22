import os
import sys
import json
import time
import socket
import subprocess
import shutil
import re
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# Force UTF-8 encoding on stdout for Windows consoles
if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── ANSI Color Helpers ──────────────────────────────────────────────────────
class C:
    """ANSI color codes for colorful CLI output."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    # Foreground
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"
    # Background
    BG_GREEN  = "\033[42m"
    BG_RED    = "\033[41m"
    BG_YELLOW = "\033[43m"

DEBUG_DIR = "debug_dumps"

def _build_locator(page, sel):
    """Build a Locator from a selector string. Supports a 'regexverb:TAG:WORD' pseudo-selector
    for word-boundary-safe text matching — plain CSS :has-text('Convert') does SUBSTRING
    matching, so it also matches "Converter" (e.g. a page's own "PDF to Excel Converter" H1,
    which is exactly what was getting clicked instead of the real button). \\bWORD\\b avoids
    that. Any other selector string is passed straight to page.locator() unchanged."""
    if sel.startswith("regexverb:"):
        _, tag, word = sel.split(":", 2)
        pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
        return page.locator(tag).filter(has_text=pattern)
    return page.locator(sel)

def find_file_input(page, cached_selector, timeout_ms=4000, existence_ms=800):
    """Find the file input, trying the cached/known selector first, then generic
    input[type='file'] as a fallback — not every tool on a site shares the same
    upload widget/id (e.g. image tools use input#file, PDF tools may not).
    Existence is checked with a short bounded wait (not an instant snapshot) so a
    selector that hasn't rendered yet still gets a fair chance, while selectors that
    will never exist are still abandoned quickly instead of costing the full timeout."""
    candidates = [cached_selector] if cached_selector else []
    candidates += ["input#file", "input[type='file']"]
    seen = set()
    for sel in candidates:
        if not sel or sel in seen:
            continue
        seen.add(sel)
        try:
            el = page.locator(sel).first
            el.wait_for(state="attached", timeout=existence_ms)
            el.wait_for(state="attached", timeout=timeout_ms)
            return el, sel
        except Exception:
            continue
    return None, None

# Selectors broad enough that a match isn't guaranteed to be the element we actually
# want — e.g. a persistent site-wide "Convert" link in a nav/footer widget elsewhere
# on the page. If more than one element matches, we can't tell which is right, so
# these are skipped rather than blindly clicking .first (which was landing on the
# wrong link and triggering a real page navigation/reload instead of a conversion).
AMBIGUITY_RISK_SELECTORS_PREFIXES = ("regexverb:a:", "regexverb:div:", "regexverb:span:", "regexverb:[role='button']:", "text=")

def find_clickable(page, candidates, timeout_each_ms=1500, existence_ms=500):
    """Try each candidate selector in turn, returning the first Locator that's visible
    plus the selector string that matched — or (None, None). Does not click; caller
    decides when. Shared by mode-selection and submit-button lookup. Each candidate
    gets a short bounded existence check (not an instant snapshot) so late-rendering
    elements (e.g. a button that appears ~1s after upload) still get found, while
    selectors that will never match are abandoned quickly rather than costing the
    full visibility timeout. Broad/ambiguous selectors (see AMBIGUITY_RISK_SELECTORS_PREFIXES)
    are skipped outright if they match more than one element."""
    for sel in candidates:
        try:
            loc = _build_locator(page, sel)
            loc.first.wait_for(state="attached", timeout=existence_ms)
            count = loc.count()
            if count == 0:
                continue
            if count > 1 and sel.startswith(AMBIGUITY_RISK_SELECTORS_PREFIXES):
                continue  # can't tell which match is the real target — too risky to guess
            el = loc.first
            if el.is_visible():
                return el, sel
            el.wait_for(state="visible", timeout=timeout_each_ms)
            return el, sel
        except Exception:
            continue
    return None, None

def fill_text_input(page, candidates, text, timeout_each_ms=1500, existence_ms=500):
    """Find the first visible element matching any candidate selector and type text
    into it. Tries .fill() first (works for textarea/input and most modern
    contenteditable elements); if that throws (some contenteditable widgets reject
    .fill() outright), falls back to click + keyboard.type so rich-text editors still
    get the text. Returns the selector that worked, or None."""
    el, matched_sel = find_clickable(page, candidates, timeout_each_ms=timeout_each_ms, existence_ms=existence_ms)
    if not el:
        return None
    try:
        el.click(timeout=4000)
    except Exception:
        pass
    try:
        el.fill(text)
        return matched_sel
    except Exception:
        pass
    try:
        el.click(timeout=4000)
        page.keyboard.type(text)
        return matched_sel
    except Exception:
        return None

def select_mode_option(page, candidates, timeout_each_ms=1200, existence_ms=500):
    """Try each candidate selector in turn and click the first one that's visible.
    Returns the selector string that worked, or None if none matched — the caller
    decides whether/how to log and debug-dump on failure. Same short-bounded-existence
    behavior as find_clickable — tolerant of late rendering, fast to abandon genuine
    non-matches."""
    if not candidates:
        return None
    for sel in candidates:
        try:
            loc = _build_locator(page, sel)
            loc.first.wait_for(state="attached", timeout=existence_ms)
            if loc.count() == 0:
                continue
            el = loc.first
            if not el.is_visible():
                el.wait_for(state="visible", timeout=timeout_each_ms)
            el.click()
            return sel
        except Exception:
            continue
    return None

def dump_debug_info(page, label, tag=""):
    """Save a screenshot + relevant HTML snippet so a failed selector can be fixed quickly.
    Looks for any element whose text contains `label` and prints/saves its outerHTML,
    which is usually enough to write a correct selector on the next attempt."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe_tag = re.sub(r'[^a-zA-Z0-9_-]', '_', tag) or "debug"
        ts = int(time.time())
        shot_path = os.path.join(DEBUG_DIR, f"{safe_tag}_{ts}.png")
        html_path = os.path.join(DEBUG_DIR, f"{safe_tag}_{ts}.html")
        try:
            page.screenshot(path=shot_path)
        except Exception:
            pass
        try:
            snippet = page.evaluate(
                """(label) => {
                    const els = Array.from(document.querySelectorAll('body *'))
                        .filter(e => e.children.length === 0 && e.textContent && e.textContent.trim().includes(label));
                    return els.slice(0, 5).map(e => e.outerHTML).join('\\n---\\n');
                }""",
                label
            )
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(snippet or "(no matching elements found)")
            cprint(f"  │ {warn_label()} Debug info saved: {html_path} / {shot_path}", C.YELLOW)
        except Exception:
            pass
    except Exception:
        pass

def safe_goto(page, url, timeout=20000, label=""):
    """goto() with a bounded timeout that reports instead of hanging silently.
    Falls back to 'domcontentloaded' wait if the full 'load' event never fires
    (common on pages with persistent polling/chat widgets/ads)."""
    try:
        page.goto(url, timeout=timeout)
        return True
    except PlaywrightTimeoutError:
        cprint(f"  │ {warn_label()} Navigation to {label or url} exceeded {timeout/1000:.0f}s (page 'load' event never fired) — continuing anyway", C.YELLOW)
        return False
    except Exception as e:
        cprint(f"  │ {warn_label()} Navigation to {label or url} failed: {e}", C.YELLOW)
        return False

def safe_wait_dom(page, timeout=5000, label=""):
    """Fast domcontentloaded wait — use this instead of safe_wait_idle() wherever the
    call is immediately followed by wait_for_credit_text() (i.e. every account-page
    balance check). networkidle routinely never fires on pages with a chat widget or
    analytics beacons running in the background, so it was burning its full timeout
    and printing a warning on nearly every single credit check; domcontentloaded
    fires as soon as the DOM is parseable and wait_for_credit_text() does the actual
    'is the number ready yet' polling right after this anyway."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except PlaywrightTimeoutError:
        pass
    except Exception:
        pass

def safe_wait_idle(page, timeout=2500, label=""):
    """wait_for_load_state('networkidle') with a short bounded timeout.
    networkidle frequently never fires on real sites (analytics/chat widgets/ads
    keep the network 'busy' forever) — treat a timeout here as normal, not fatal,
    and just proceed since goto() already confirmed the page loaded. Kept short
    (2.5s) because it's only an opportunistic best-effort check, not the real
    synchronization signal — actual readiness is confirmed by element-specific
    waits downstream (wait_for_credit_text, file_input.wait_for, etc.)."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
        return True
    except PlaywrightTimeoutError:
        cprint(f"  │ {warn_label()} {label or 'Page'} never reached full network-idle within {timeout/1000:.0f}s (likely background chat/analytics traffic) — proceeding", C.DIM)
        return False
    except Exception:
        return False

def wait_for_credit_text(page, timeout=8000):
    """Poll for the credit-balance text to actually be present in the DOM, using the
    same detection logic as get_credit_balance(). This is the real signal we care
    about on the account page — much faster than a blind networkidle wait, since it
    returns the instant the number is renderable instead of waiting out a fixed timer."""
    js_check = r"""
    () => {
        const elements = Array.from(document.querySelectorAll('div, p, span, li, td, h3, h4, b, strong, label'));
        for (const el of elements) {
            const text = el.textContent.trim();
            if (text.length > 0 && text.length < 300 && /credit/i.test(text) && /\d/.test(text)) return true;
            if (text.length > 0 && text.length < 300 && /usage/i.test(text) && /\d/.test(text)) return true;
        }
        return false;
    }
    """
    try:
        page.wait_for_function(js_check, timeout=timeout)
        return True
    except PlaywrightTimeoutError:
        return False
    except Exception:
        return False

def cache_busted_url(url):
    """Append a changing query param (before any #fragment) so each navigation to the account
    page is a genuinely NEW url rather than an exact repeat of the last one.

    Playwright's goto() to a URL identical to the page's current URL (path + hash unchanged)
    doesn't reliably force the site's SPA to refetch the account balance from the server —
    the browser/app can just leave the already-rendered (stale) numbers in place, which is
    why the automated 'before'/'after' checks kept reading the SAME balance even though a
    manual F5 refresh in the real browser showed the updated one. A unique query string
    guarantees a real navigation + fresh fetch every time, while the #fragment (e.g. '#plan')
    is preserved so the correct tab still loads."""
    base, sep_hash, frag = url.partition("#")
    join = "&" if "?" in base else "?"
    busted = f"{base}{join}_cb={int(time.time() * 1000)}"
    return f"{busted}#{frag}" if sep_hash else busted

def wait_for_pool_text(page, pool_name, timeout=8000):
    """Poll until a specific named credit-pool label (e.g. 'Plagiarism Checker', 'AI Writing
    Tools') is actually present in the page's rendered text. Used ahead of the targeted
    get_credit_balance(pool_name=...) read so it isn't racing page hydration — without this,
    an early read could land between navigation and render, find no match, and silently fall
    back to dumping the whole page (the huge wall of text seen in earlier runs)."""
    js_check = r"""
    (name) => {
        const text = document.body.innerText || "";
        return new RegExp(name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i').test(text);
    }
    """
    try:
        page.wait_for_function(js_check, arg=pool_name, timeout=timeout)
        return True
    except PlaywrightTimeoutError:
        return False
    except Exception:
        return False

def cprint(msg, color=C.WHITE, bold=False):
    prefix = C.BOLD if bold else ""
    print(f"{prefix}{color}{msg}{C.RESET}")

def pass_label():
    return f"{C.BOLD}{C.GREEN}✓ PASS{C.RESET}"

def fail_label():
    return f"{C.BOLD}{C.RED}✗ FAIL{C.RESET}"

def skip_label():
    return f"{C.BOLD}{C.YELLOW}⊘ SKIP{C.RESET}"

def warn_label():
    return f"{C.YELLOW}⚠ WARN{C.RESET}"

def info_label():
    return f"{C.CYAN}ℹ INFO{C.RESET}"

# Enable ANSI colors on Windows
if sys.platform.startswith("win"):
    os.system("")

# Path Configurations
MAPPINGS_FILE = "site_mappings.json"
# Resolved relative to THIS SCRIPT's own location (not the current working directory, and
# definitely not a hardcoded "C:\Users\Enzipe\Desktop\..." path tied to one machine) — the
# "Test Data" folder now lives inside the project directory alongside the script itself, so
# this works for anyone who clones/downloads the whole project folder, on any machine, run
# from anywhere, with zero setup.
TEST_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Test Data")

# Per-site account page paths (override the default /account)
SITE_ACCOUNT_PATHS = {
    "imagetotext.cc": "/profile",
    "imagetotext.info": "/profile",
    "imagetotext.io": "/user-account",
    "jpgtotext.com": "/manage-plans",              # confirmed direct URL to the credits/membership view — no tab click needed
    "editpad.org": "/tool/account#plan",
    "prepostseo.com": "/account",                   # then click "Plan Details" tab
    "grammarcheck.ai": "/profile",
    "summarizer.org": "/my-account#plan_details",
    "paraphrasing.io": "/user/profile",             # then click "Plan Details" tab
    # Add more overrides here as needed. Sites not listed default to /account.
}

# Tab labels to try clicking on the account page before reading credit balance --
# different sites surface the credit/usage numbers under different tab names, and
# some (e.g. imagetotext.io's /user-account, jpgtotext.com's /dashboard,
# prepostseo.com's /account, paraphrasing.io's /user/profile) land on a different
# tab by default and need an explicit click on "Plans"/"Membership"/"Plan Details"
# before the credit numbers even render.
# Order matters: more specific/likely labels first.
ACCOUNT_CREDIT_TAB_CANDIDATES = ["Plans", "Plan Details", "Membership", "Usage", "Billing", "Subscription"]

def has_credit_ratio_text(page):
    """Instant (non-waiting) check for whether the real credit balance is already visible on
    the page, so click_credit_tab_if_present() can skip clicking any tab. Two layouts count:
      1. A 'used / total' ratio (e.g. '0 / 18000') in a single element.
      2. A separate 'Credits Used <number>' label (e.g. editpad.org's Plan Details table,
         which shows 'Credits Allowed' and 'Credits Used' as distinct numbers rather than a
         single ratio).
    Without recognizing (2), editpad.org's Plan Details page — which is already the correct,
    already-active view when landed on directly via account_url's #plan fragment — looked
    like it had no balance yet, so the script went on to click the 'Plan Details' sidebar
    link again, which turned out to trigger a real navigation to /api/plans instead of a
    client-side tab switch. (The (1) case predates this: clicking 'Plans' on jpgtotext.com
    was landing on a plan-comparison/upgrade view instead of the current plan's ratio.)"""
    js_check = r"""
    () => {
        const ratio = /\d[\d,]*\s*\/\s*[\d,]+/;
        const elements = Array.from(document.querySelectorAll('div, p, span, li, td, h3, h4, b, strong, label'));
        for (const el of elements) {
            const text = el.textContent.trim();
            if (text.length > 0 && text.length < 300 && /credit|usage/i.test(text) && ratio.test(text)) return true;
        }
        const usedElsewhere = /credits\s*used\D{0,40}\d/i;
        return usedElsewhere.test(document.body.innerText);
    }
    """
    try:
        return bool(page.evaluate(js_check))
    except Exception:
        return False

def click_credit_tab_if_present(page):
    """Best-effort click on whichever account-page tab exposes credit/usage info.
    Tries each candidate label in turn and clicks the first visible match; silently
    no-ops if none are present (plenty of sites show credits directly with no tabs
    at all, so this must never be treated as an error).

    Skips entirely if the real credit ratio is already visible — see
    has_credit_ratio_text() for why that check exists."""
    if has_credit_ratio_text(page):
        return None
    for label in ACCOUNT_CREDIT_TAB_CANDIDATES:
        try:
            tab = page.locator(f"text={label}").first
            if tab.is_visible():
                tab.click()
                return label
        except Exception:
            continue
    return None

# editpad.org's "Plan Details" account page doesn't show one site-wide balance — it lists
# several INDEPENDENT credit pools as separate rows (Tools | Credits Allowed | Credits Used):
# "AI Writing Tools", "Plagiarism Checker", "Extract Text From Image", "Humanize Ai Text".
# Reading the whole page generically (as get_credit_balance does for single-balance sites)
# was picking up an unrelated pricing/upgrade widget's text instead of any of these real rows.
# Map each tool to the specific row it must be checked against; anything not listed shares the
# general "AI Writing Tools" pool. Note the page's own label is "Humanize Ai Text" (lowercase
# "i"), which differs from the tool name "Humanize AI Text" used elsewhere in this script.
EDITPAD_TOOL_POOL_MAP = {
    "Plagiarism Checker": "Plagiarism Checker",
    "Extract Text From Image": "Extract Text From Image",
    "Humanize AI Text": "Humanize Ai Text",
}
EDITPAD_DEFAULT_POOL = "AI Writing Tools"
# Order these labels actually appear in on the Plan Details page — used to bound where one
# pool's row ends and the next begins when scanning the page's flattened text.
EDITPAD_POOL_ORDER = ["AI Writing Tools", "Plagiarism Checker", "Extract Text From Image", "Humanize Ai Text"]

# grammarcheck.ai's /profile page shows one single "Usage" section with a "Credits allowed:
# Unlimited" line (not a real number — always ignore it) alongside the actual count that DOES
# change, rendered as "<number> used" (number BEFORE the word, lowercase, unlike editpad.org's
# "Credits Used <number>" — see get_credit_balance_for_pool's used-number regex, which checks
# both orderings). All 7 tools share this one pool, so every tool maps to the same label.
GRAMMARCHECK_POOL_LABEL = "Usage"
# "Need More Credits" isn't a real alternate pool — it's the next line on the page after the
# Usage section (an upgrade prompt) — but reusing it here as a boundary keeps the extracted
# block scoped to just the Usage section instead of running to the end of the page/footer.
GRAMMARCHECK_POOL_ORDER = ["Usage", "Need More Credits"]

def get_pool_name_for_tool(domain, tool_name):
    """Return the specific credit-pool label to isolate on this (domain, tool)'s account page,
    or None for sites that show one single overall balance (use the generic whole-page
    extraction in that case)."""
    if domain == "editpad.org":
        return EDITPAD_TOOL_POOL_MAP.get(tool_name, EDITPAD_DEFAULT_POOL)
    if domain == "grammarcheck.ai":
        return GRAMMARCHECK_POOL_LABEL
    return None

def get_pool_order_for_domain(domain):
    """Return the ordered list of pool labels for this domain's account page (used to bound
    where one pool's block of text ends and the next begins), or None for single-pool/generic
    sites where no boundary list is needed."""
    if domain == "editpad.org":
        return EDITPAD_POOL_ORDER
    if domain == "grammarcheck.ai":
        return GRAMMARCHECK_POOL_ORDER
    return None

# Tools that auto-convert the instant a file is uploaded — no Convert/Submit button exists
# to click at all. Keyed by (domain, env, tool_name). Confirmed case: imagetotext.info's home
# Image-To-Text tool on STAGING only (the same tool on LIVE does have a Convert button).
AUTO_CONVERT_NO_BUTTON = {
    ("imagetotext.info", "staging", "Image To Text - Simple OCR"),
    ("imagetotext.info", "staging", "Image To Text - Formatted OCR"),
}

# Tools that take TYPED text input instead of a file upload — confirmed via live DOM
# inspection. editpad.org's AI Essay Writer and AI Email Writer have no upload control
# at all; you type a topic/prompt into a textarea instead. "text" is a representative
# sample matching each tool's own suggested-topic placeholder text.
#
# Two formats supported: a single "selector" string (exact, confirmed element), or a
# "selectors" list tried in priority order via find_clickable-style fallback (used when
# the exact input markup isn't confirmed yet, or a site reuses one shared box across
# several tools whose class/id may vary slightly).
#
# grammarcheck.ai's own screenshot shows a "Type or paste your content here..." box —
# clicking the tool's submit button (e.g. "Check AI") with this box empty produces a
# "No text found" popup rather than a real conversion/deduction, which is why every
# grammarcheck.ai tool needs its text actually typed in first instead of (as before)
# going straight to file-upload logic that doesn't touch this box at all. The exact
# element wasn't confirmed via DOM inspection (unlike the button/result overrides
# above), so this tries several plausible candidates — if none land, dump_debug_info's
# screenshot/HTML dump (saved under DEBUG_DIR) will show the real markup to lock in
# an exact selector.
GRAMMARCHECK_TEXT_INPUT_SAMPLE = (
    "This is a sample paragraph used by our QA automation script to test weather the "
    "credit deduction system is working correctly, it help us verify that each tool "
    "on this website is deducting the right amount of credits per word when a request "
    "is submitted for checking or generating text."
)
GRAMMARCHECK_TEXT_INPUT_SELECTORS = [
    "form#checkform textarea",
    "#checkform textarea",
    "textarea#inputText",
    "textarea[placeholder*='paste' i]",
    "textarea[placeholder*='type' i]",
    "[contenteditable='true']",
    "#textpad-content[contenteditable]",
    ".editable-content",
    "textarea",
]
SUMMARIZER_GRAMMAR_INPUT_SAMPLE = (
    "This are a sample paragraph used by our QA automation script for testing weather the "
    "credit deduction system is working correctly on this tool, it help us verify that the "
    "right amount of credits is deducted per word when a grammar check is performed."
)
# Confirmed via live DOM inspection (Grammar Checker's #input_text, placeholder "Upload or
# Paste your content here...") that summarizer.org's content box is a shared, reused widget —
# same idea as "#submit-btn" being reused site-wide. Story/Sentence/Essay/Conclusion Generator
# (and likely the other generator-style tools) have no file input at all — they take this same
# typed content — which is why they were failing with "No file input found on page" when the
# script defaulted to file-upload logic for them. This "default" entry makes typed input the
# site-wide fallback for any summarizer.org tool without its own more specific override, tried
# via several candidates since not every tool page necessarily uses the exact same id/name.
SUMMARIZER_DEFAULT_INPUT_SAMPLE = (
    "Remote work has changed the way modern teams communicate and collaborate. This is a "
    "sample paragraph used by our QA automation script to test weather the credit deduction "
    "system on this website is working correctly for each tool."
)
SUMMARIZER_DEFAULT_INPUT_SELECTORS = [
    "#input_text",
    "textarea[name='input-content']",
    "#topic",
    "input[name='topic']",
    "textarea[placeholder*='topic' i]",
    "input[placeholder*='topic' i]",
    "textarea[placeholder*='paste' i]",
    "textarea",
]
TEXT_INPUT_OVERRIDES = {
    ("editpad.org", "AI Essay Writer"): {"selector": "#topic", "text": "The Impact of Social Media on Youth Development"},
    ("editpad.org", "AI Email Writer"): {"selector": "#content", "text": "To invite people to a team meeting next week"},
    ("grammarcheck.ai", "default"): {"selectors": GRAMMARCHECK_TEXT_INPUT_SELECTORS, "text": GRAMMARCHECK_TEXT_INPUT_SAMPLE},
    ("summarizer.org", "default"): {"selectors": SUMMARIZER_DEFAULT_INPUT_SELECTORS, "text": SUMMARIZER_DEFAULT_INPUT_SAMPLE},
    # Confirmed via live DOM inspection: unlike every other summarizer.org tool (which has a
    # "#submit-btn" that must be clicked), Grammar Checker has NO submit button at all — you
    # just paste/type into #input_text and it auto-analyzes (debounced), with the "Fix All"
    # widget (#fix_all_grammar, see RESULT_INDICATOR_OVERRIDES below) appearing once results
    # are ready. "no_submit_needed" tells the main loop to skip the submit-button-click step
    # entirely for this one tool instead of raising "No submit/convert button found".
    ("summarizer.org", "Grammar Checker"): {"selectors": ["#input_text", "textarea[name='input-content']", ".scroll-div.textarea", "textarea"], "text": SUMMARIZER_GRAMMAR_INPUT_SAMPLE, "no_submit_needed": True},
}

# Per-(domain, tool) submit-button overrides, confirmed via live DOM inspection — checked
# with top priority ahead of the generic verb-guessing candidates below. 8 of editpad.org's
# 9 tools share id="main_tool_btn" (same ID reused per-page across separate tool pages,
# which is fine); AI Essay Writer's "Write My Essay" button is the one exception.
SUBMIT_BTN_OVERRIDES = {
    ("editpad.org", "default"): ["#main_tool_btn"],
    ("editpad.org", "AI Essay Writer"): [".write__essay"],
    # Confirmed via live DOM inspection: all 7 grammarcheck.ai tools share the same submit
    # button class combo (.toolSubmitBtn.getRespBtn) inside form="checkform" — the id varies
    # (#btnShadowRoot on some tools, none at all on others), so the class combo is the one
    # constant to rely on across the whole site.
    ("grammarcheck.ai", "default"): ["button.toolSubmitBtn.getRespBtn", "#btnShadowRoot", "button[form='checkform']"],
    # Confirmed via live DOM inspection: 11 of summarizer.org's 12 tools share id="submit-btn"
    # (reused per-page across separate tool pages, same pattern as editpad.org/jpgtotext.com).
    # Grammar Checker is the one exception — it has no submit button at all (see
    # TEXT_INPUT_OVERRIDES's "no_submit_needed" entry above), so it's deliberately not listed
    # here; the main loop skips this lookup entirely for that tool.
    ("summarizer.org", "default"): ["#submit-btn"],
    # Confirmed via live DOM inspection that this tool's REAL button (unlike the rest of the
    # site) has no id="submit-btn" at all — it's matched by its data-init-text attribute
    # instead. Without this override, the generic "#submit-btn" default either found nothing
    # or matched an unrelated element on the page, which is why clicking was just scrolling
    # the page instead of triggering generation.
    ("summarizer.org", "Sentence Expander"): ["button[data-init-text^='Expand Text']", "button.btn.btn-dark:has-text('Expand Text')"],
    # Confirmed via live DOM inspection: all 6 paraphrasing.io tools share id="submit_btn"
    # (reused per-page, same pattern as summarizer.org's "#submit-btn" — note the underscore
    # vs hyphen difference between the two sites, easy to mix up).
    ("paraphrasing.io", "default"): ["#submit_btn"],
}

# Per-(domain, tool) result-indicator overrides, confirmed via live DOM inspection —
# checked with top priority ahead of the generic HIGH_CONFIDENCE_SELECTORS list below.
# editpad.org has no "Start Over" button on most tools at all; the only signal a result
# actually exists is each tool's own Download/Copy control appearing.
# ("Extract Text From Image" isn't listed — it does have a working Start Over button,
# already covered by the generic HIGH_CONFIDENCE_SELECTORS list.)
RESULT_INDICATOR_OVERRIDES = {
    ("editpad.org", "Plagiarism Checker"): ["#genrateReport", ".downloadReport.report-download-btn", "text=Download Report"],
    ("editpad.org", "Paraphrasing Tool"): ["button#capitalized[onclick=\"downloadReport()\"]", "#capitalized"],
    ("editpad.org", "Article Rewriter"): ["button#capitalized[onclick=\"downloadReport()\"]", "#capitalized"],
    ("editpad.org", "Text Summarizer"): [".icon-btn.download-btn[data-tooltip='Download']", ".download-btn"],
    ("editpad.org", "AI Detector"): [".downloadReport", "button.downloadReport"],
    ("editpad.org", "AI Essay Writer"): [".download__content", "[onclick*='downloadResultDocx']"],
    ("editpad.org", "AI Email Writer"): [".copy-email-btn.copy-result", "text=Copy Email"],
    ("editpad.org", "Humanize AI Text"): ["button#capitalized[onclick=\"downloadReport()\"]", "#capitalized"],
    # Confirmed via live DOM inspection. Grammar/Punctuation Checker both render the same
    # "Fix All" summary widget once results exist (with a live mistake count in parens).
    # Paraphrase/AI Humanizer/AI Summarizer all show the same copy/download icon once their
    # output populates. AI Checker gets its own "Result" panel with a Download-report button.
    # (Plagiarism Checker isn't listed — no confirmed selector yet; falls back to the generic
    # HIGH_CONFIDENCE_SELECTORS/result_selectors lists below.)
    ("grammarcheck.ai", "Grammar Checker"): [".fix_all_button.all_correct", ".fix_all_text.mistakes"],
    ("grammarcheck.ai", "Punctuation Checker"): [".fix_all_button.all_correct", ".fix_all_text.mistakes"],
    ("grammarcheck.ai", "Paraphrase Tool"): ["#copy-textpad-content"],
    ("grammarcheck.ai", "AI Checker"): ["#downloadReport", ".result"],
    ("grammarcheck.ai", "AI Humanizer"): ["#copy-textpad-content"],
    ("grammarcheck.ai", "AI Summarizer"): ["#copy-textpad-content"],
    # Confirmed via live DOM inspection. 11 of summarizer.org's 12 tools show the same shared
    # download icon (img[alt='download']) once a result exists — Plagiarism Checker's variant
    # additionally has ".download_report_btn"/"Download Report" text, both covered here.
    # "default" applies to any tool on this site not given its own explicit entry.
    ("summarizer.org", "default"): ["img[alt='download' i]", ".download_report_btn", "#downloadButton", ".result_option.on_result_visible"],
    # Grammar Checker has no download icon at all — its own "Fix All" widget appearing IS the
    # result signal (analysis runs automatically on typed/pasted input; see TEXT_INPUT_OVERRIDES).
    ("summarizer.org", "Grammar Checker"): ["#fix_all_grammar", ".fix_all"],
    # Confirmed via live DOM inspection. All 6 paraphrasing.io tools show a download icon once
    # a result exists — most as img[alt='download icon'], Plagiarism Checker's variant uses
    # ".download-icon" (alt="image") instead, and AI Content Detector has its own #download_report
    # id (alt="Download Report"). "default" covers all of them without needing 6 near-duplicate entries.
    ("paraphrasing.io", "default"): ["#download_report", ".download-icon", "img[alt='download icon' i]", "img[alt='Download Report' i]"],
}

# ==============================================================================
# Portable Browser Detection & Debug Port Management
# ==============================================================================
#
# ROOT CAUSE (confirmed independently on two colleagues' machines):
# Launching the browser with --remote-debugging-port pointed at the REAL user
# profile (e.g. %LOCALAPPDATA%\Google\Chrome\User Data) can trigger Chromium's
# single-instance mechanism: if any process still owns that profile (even one
# mid-teardown from a moment ago), the new debug-flagged launch gets silently
# handed off to the EXISTING instance — which was never started with the debug
# flag. A window may still appear, but the debug port never binds, and the
# script just sees a timeout with no clearer signal of why.
#
# THE FIX: launch into a completely separate, script-owned profile directory
# (%LOCALAPPDATA%\CreditQADebugProfile\<Browser>) instead of the real one. A
# different --user-data-dir means the browser treats it as a brand-new,
# independent instance — the single-instance check never fires, so the debug
# port reliably binds every time. The user's real browsing profile/session is
# never touched or closed.
#
# Trade-off worth knowing: since this profile starts empty, each person needs
# to log into the Premium test account ONCE inside it — same as they would in
# any fresh browser profile. After that first login, the profile persists
# exactly like a normal one across every future run.
# ==============================================================================

def _local_appdata():
    """Resolve the CURRENT machine's actual %LOCALAPPDATA%, never a hardcoded username."""
    return os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")

def _roaming_appdata():
    """Resolve the CURRENT machine's actual %APPDATA%, never a hardcoded username."""
    return os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")

def _programfiles():
    return os.environ.get("PROGRAMFILES", r"C:\Program Files")

def _programfiles_x86():
    return os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")

def _find_browser_exe_from_registry(reg_key_path, value_name=""):
    """Read a browser exe path from the Windows registry's App Paths key — the most
    reliable source across custom install locations and drive letters, since Windows
    itself relies on this to launch the browser via 'start chrome.exe' etc. Checks both
    HKLM (system-wide install) and HKCU (per-user install, no admin rights needed).
    Returns None on any failure rather than raising — this is a best-effort lookup with
    several fallback candidate paths tried afterward."""
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, reg_key_path) as key:
                val, _ = winreg.QueryValueEx(key, value_name)
                val = val.strip().strip('"').split('"')[0]
                if os.path.isfile(val):
                    return val
        except Exception:
            continue
    return None

def _find_exe_from_candidates(candidates):
    """Return the first path in the list that actually exists on this machine."""
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None

def _resolve_chrome_exe():
    reg = _find_browser_exe_from_registry(r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
    if reg:
        return reg
    local, pf, pf86 = _local_appdata(), _programfiles(), _programfiles_x86()
    return _find_exe_from_candidates([
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),  # per-user install, no admin rights
    ])

def _resolve_brave_exe():
    reg = _find_browser_exe_from_registry(r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\brave.exe")
    if reg:
        return reg
    local, pf, pf86 = _local_appdata(), _programfiles(), _programfiles_x86()
    return _find_exe_from_candidates([
        os.path.join(pf, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        os.path.join(pf86, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        os.path.join(local, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
    ])

def _resolve_edge_exe():
    reg = _find_browser_exe_from_registry(r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe")
    if reg:
        return reg
    pf, pf86 = _programfiles(), _programfiles_x86()
    return _find_exe_from_candidates([
        os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
    ])

def _resolve_firefox_exe():
    reg = _find_browser_exe_from_registry(r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe")
    if reg:
        return reg
    pf, pf86 = _programfiles(), _programfiles_x86()
    return _find_exe_from_candidates([
        os.path.join(pf, "Mozilla Firefox", "firefox.exe"),
        os.path.join(pf86, "Mozilla Firefox", "firefox.exe"),
    ])

# Resolved once at import time. None means not found on this machine — the launcher
# will prompt for a manual path in that case, same as before.
_CHROME_EXE = _resolve_chrome_exe()
_BRAVE_EXE = _resolve_brave_exe()
_EDGE_EXE = _resolve_edge_exe()
_FIREFOX_EXE = _resolve_firefox_exe()

# Isolated, script-owned profile directory — see the module docstring above for why this
# replaced pointing at the user's real browser profile.
_DEBUG_PROFILE_BASE = os.path.join(_local_appdata(), "CreditQADebugProfile")

BROWSER_CONFIGS = {
    "Brave": {
        "path": _BRAVE_EXE or os.path.join(_programfiles(), "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        "user_data": os.path.join(_DEBUG_PROFILE_BASE, "Brave"),
    },
    "Chrome": {
        "path": _CHROME_EXE or os.path.join(_programfiles(), "Google", "Chrome", "Application", "chrome.exe"),
        "user_data": os.path.join(_DEBUG_PROFILE_BASE, "Chrome"),
    },
    "Edge": {
        "path": _EDGE_EXE or os.path.join(_programfiles_x86(), "Microsoft", "Edge", "Application", "msedge.exe"),
        "user_data": os.path.join(_DEBUG_PROFILE_BASE, "Edge"),
    },
    "Firefox": {
        "path": _FIREFOX_EXE or os.path.join(_programfiles(), "Mozilla Firefox", "firefox.exe"),
        "user_data": os.path.join(_DEBUG_PROFILE_BASE, "Firefox"),
    },
}

def _find_free_port(preferred, search_range=30):
    """Scan [preferred, preferred + search_range] and return the first port not currently
    bound. Only used as a last-resort fallback (see ensure_browser_debugging) when the
    preferred port is occupied by something we couldn't close — e.g. an unrelated tool, not
    a leftover browser instance. Falls back to returning `preferred` unchanged if the whole
    range is somehow taken, letting the actual browser launch surface the real error."""
    for port in range(preferred, preferred + search_range):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred

# Debugging port — centralized here instead of scattered as a literal 9222 throughout the
# file. QA_DEBUG_PORT env var always wins outright if set (e.g. two people running this on
# the same machine at once: QA_DEBUG_PORT=9223 for one of them). Otherwise defaults to 9222;
# ensure_browser_debugging() will fall forward to the next free port automatically if 9222
# turns out to be occupied by something it can't close, updating this value for the rest of
# the run (including the later Playwright CDP connection).
DEBUG_PORT = int(os.environ.get("QA_DEBUG_PORT", "9222"))

def get_default_browser_name():
    """Detect default browser using Windows Registry UserChoice."""
    try:
        cmd = ["reg", "query", r"HKEY_CURRENT_USER\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice", "/v", "ProgId"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        for line in res.stdout.splitlines():
            if "ProgId" in line:
                prog_id = line.split()[-1]
                if "Brave" in prog_id:
                    return "Brave"
                elif "Chrome" in prog_id:
                    return "Chrome"
                elif "Edge" in prog_id or "MSEdge" in prog_id:
                    return "Edge"
                elif "Firefox" in prog_id:
                    return "Firefox"
    except Exception as e:
        print(f"Error detecting default browser: {e}")
    return "Chrome"  # Fallback to Chrome

def is_port_in_use(port):
    """Check if a local port is already open. 1s timeout prevents an indefinite hang if a
    firewall silently drops the connection attempt instead of actively refusing it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(('127.0.0.1', port)) == 0

def is_browser_process_running(browser_name):
    """Check if the browser executable is running."""
    exe_name = get_exe_name_for_browser(browser_name)
    try:
        res = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}"], capture_output=True, text=True, check=True)
        return exe_name in res.stdout
    except Exception:
        return False

def close_browser(browser_name, wait_timeout_s=10):
    """Force close ALL processes matching this browser's name and wait for them to actually
    be gone before returning. NOT currently called anywhere in the isolated-debug-profile
    flow above — killing by process name can't distinguish "our isolated instance" from the
    person's regular browser window, so ensure_browser_debugging() deliberately avoids it
    (its retry path kills by exact PID instead). Kept as a utility function in case a future
    need for a broader force-close arises (e.g. a manual `--reset` flag).

    Edge specifically has a "Startup boost" setting (edge://settings/system → "Continue
    running background extensions and apps when Microsoft Edge is closed") that can respawn
    a background msedge.exe almost immediately after it's killed — sweeping repeatedly for a
    few seconds, and also killing Edge's background helper processes, catches this instead of
    a single kill-and-hope, if this function is ever wired back in."""
    exe_name = get_exe_name_for_browser(browser_name)
    helper_processes = []
    if browser_name == "Edge":
        helper_processes = ["msedgewebview2.exe", "identity_helper.exe"]

    print(f"Closing {browser_name} browser to release profile locks...")

    def kill_sweep():
        for name in [exe_name] + helper_processes:
            try:
                subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True, check=False)
            except Exception:
                pass

    try:
        kill_sweep()
    except Exception as e:
        print(f"Could not automatically close browser: {e}")
        return

    sweep_deadline = time.time() + 3
    while time.time() < sweep_deadline:
        if is_browser_process_running(browser_name):
            kill_sweep()
        time.sleep(0.4)

    deadline = time.time() + wait_timeout_s
    while time.time() < deadline:
        if not is_browser_process_running(browser_name):
            time.sleep(0.5)
            return
        time.sleep(0.3)
    print(f"[WARN] {browser_name} processes may still be shutting down after {wait_timeout_s}s — proceeding anyway.")

def clear_stale_singleton_locks(user_data_dir):
    """Remove Chromium's SingletonLock/SingletonCookie/SingletonSocket files (and the
    Default profile's LOCK file) left behind by a force-killed process. Safe to delete:
    they're pure lock markers, not user data."""
    if not user_data_dir or not os.path.isdir(user_data_dir):
        return
    stale_paths = [
        os.path.join(user_data_dir, "SingletonLock"),
        os.path.join(user_data_dir, "SingletonCookie"),
        os.path.join(user_data_dir, "SingletonSocket"),
        os.path.join(user_data_dir, "Default", "LOCK"),
        os.path.join(user_data_dir, "Default", "lockfile"),
    ]
    for p in stale_paths:
        try:
            if os.path.exists(p) or os.path.islink(p):
                os.remove(p)
        except Exception:
            pass

def get_exe_name_for_browser(browser_name):
    """Map a browser choice ('Chrome'/'Brave'/'Edge'/'Firefox') to its actual process name."""
    return "msedge.exe" if browser_name == "Edge" else f"{browser_name.lower()}.exe"

def get_process_holding_port(port):
    """Return the exe name (e.g. 'brave.exe') of whichever process is actually bound to this
    port, by resolving the LISTENING PID from netstat and looking it up in tasklist. Returns
    None if it can't be determined.

    This exists because is_port_in_use() alone can't tell WHICH browser opened the port — if
    Brave was left running with debugging enabled from an earlier session, is_port_in_use()
    still returns True even when the person just chose Edge this run, and the script would
    silently keep using Brave instead of ever launching Edge at all."""
    try:
        res = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, check=True)
        pid = None
        for line in res.stdout.splitlines():
            if "LISTENING" in line and re.search(rf"[:.]{port}\s", line):
                pid = line.split()[-1]
                break
        if not pid:
            return None
        res2 = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, check=True)
        for line in res2.stdout.splitlines():
            if pid in line:
                return line.split()[0].lower()
    except Exception:
        pass
    return None

def ensure_browser_debugging(browser_name, _retry=True):
    """Make sure the browser is running with remote debugging enabled on DEBUG_PORT, using
    an isolated script-owned profile (see the module docstring above for why).

    Self-healing behavior:
    - Detects if a DIFFERENT browser is already holding the port and closes it.
    - Uses the isolated debug profile — never the user's real profile — so the person's
      everyday browser is never asked to close.
    - On timeout, wipes the ENTIRE isolated profile directory (not just lock files, since
      it's safe to nuke a profile that's ours alone) and retries once automatically.
    - Captures browser stderr so a real launch error is visible instead of a bare timeout.
    - If the port is still unavailable after closing a mismatched holder, falls forward to
      the next free port automatically rather than failing outright.
    """
    global DEBUG_PORT
    expected_exe = get_exe_name_for_browser(browser_name)

    if is_port_in_use(DEBUG_PORT):
        holder_exe = get_process_holding_port(DEBUG_PORT)
        if holder_exe is None or holder_exe == expected_exe:
            print(f"Debugging port {DEBUG_PORT} is active.")
            return True
        else:
            print(f"Port {DEBUG_PORT} is currently held by {holder_exe}, not {expected_exe} — closing it so {browser_name} can be used instead.")
            try:
                subprocess.run(["taskkill", "/F", "/IM", holder_exe], capture_output=True, check=False)
            except Exception as e:
                print(f"[WARN] Could not close {holder_exe}: {e}")
            deadline = time.time() + 10
            while time.time() < deadline and is_port_in_use(DEBUG_PORT):
                time.sleep(0.3)
            if is_port_in_use(DEBUG_PORT):
                # Something we couldn't close is still squatting on this port (e.g. an
                # unrelated dev tool, not a leftover browser) — fall forward to the next
                # free port instead of failing outright.
                new_port = _find_free_port(DEBUG_PORT + 1)
                print(f"Port {DEBUG_PORT} still occupied — using {new_port} instead for this run.")
                DEBUG_PORT = new_port

    # Get browser settings
    cfg = BROWSER_CONFIGS.get(browser_name)
    if not cfg or not os.path.isfile(cfg["path"]):
        print(f"[ERROR] Browser executable not found at: {cfg.get('path') if cfg else 'Unknown'}")
        print("Please enter the custom path to your browser executable: ")
        custom_path = input("> ").strip().strip('"')
        if os.path.isfile(custom_path):
            cfg = dict(cfg) if cfg else {"user_data": os.path.join(_DEBUG_PROFILE_BASE, browser_name)}
            cfg["path"] = custom_path
            BROWSER_CONFIGS[browser_name] = cfg
        else:
            print("Invalid browser path. Aborting.")
            sys.exit(1)

    # Nothing to close here: the isolated debug profile is a separate --user-data-dir, and
    # browsers happily run multiple simultaneous instances with different profiles side by
    # side — so the person's regular, everyday browser window is never touched and never
    # needs to close. (Deliberately not force-closing anything by process name in this
    # normal path — taskkill by name can't distinguish "our isolated instance" from "the
    # person's regular browser with real unsaved work"; it would kill both. The retry path
    # below kills only the exact PID of the specific launch attempt that failed, for the
    # same reason.)

    debug_profile_dir = cfg["user_data"]
    try:
        os.makedirs(debug_profile_dir, exist_ok=True)
    except Exception as e:
        print(f"[ERROR] Could not create/access debug profile directory: {debug_profile_dir}")
        print(f"        {e}")
        print("        Check that you have write permission to this folder, then try again.")
        return False

    clear_stale_singleton_locks(debug_profile_dir)

    print(f"Launching {browser_name} with remote debugging on port {DEBUG_PORT}...")
    print(f"  Debug profile: {debug_profile_dir}")
    try:
        cmd = [
            cfg["path"],
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={debug_profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--disable-extensions",
        ]
        # Capture stderr so any real browser startup error is visible on failure instead of
        # silently swallowed behind a bare "timeout" message.
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        print(f"Waiting for debugging port ({DEBUG_PORT}) to open...")
        for _ in range(30):
            if is_port_in_use(DEBUG_PORT):
                print("Debugging port is active.")
                time.sleep(1.5)
                return True
            time.sleep(0.5)

        try:
            stderr_output = proc.stderr.read(4096).decode("utf-8", errors="replace").strip()
        except Exception:
            stderr_output = ""

        print(f"Timeout waiting for debugging port ({DEBUG_PORT}) to open.")
        if stderr_output:
            print(f"[Browser stderr]\n{stderr_output}")

        if _retry:
            # One automatic recovery attempt: kill the SPECIFIC process we just launched (by
            # PID, not by process name — taskkill by name would also catch the person's
            # regular, everyday browser window if one happens to be open, which has nothing
            # to do with this failed isolated-profile launch), wipe the isolated debug
            # profile entirely, and relaunch from a completely clean slate.
            print("Retrying once: closing this attempt and relaunching...")
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
            except Exception:
                pass
            try:
                if os.path.isdir(debug_profile_dir):
                    shutil.rmtree(debug_profile_dir, ignore_errors=True)
                    print(f"Debug profile wiped: {debug_profile_dir}")
            except Exception as wipe_err:
                print(f"[WARN] Could not wipe debug profile: {wipe_err}")
            return ensure_browser_debugging(browser_name, _retry=False)

        print(f"The browser process may still be running but never opened port {DEBUG_PORT}.")
        print(f"Debug profile directory: {debug_profile_dir}")
        if browser_name == "Edge":
            print("Edge specifically has a 'Startup boost' setting that can silently relaunch")
            print("a background copy of itself right after being closed, which then steals the")
            print("next launch before debugging ever gets enabled on it. Try turning this off:")
            print("  edge://settings/system -> 'Continue running background extensions and")
            print("  apps when Microsoft Edge is closed' -> OFF, then re-run the script.")
        print("Diagnostics:")
        print(f"  Check what's on this port:  netstat -ano | findstr :{DEBUG_PORT}")
        print(f"  Try a different port:       set QA_DEBUG_PORT=9333")
        print("If this keeps happening, check Task Manager for any lingering browser")
        print(f"processes (including hidden background/updater processes) and end them.")
        return False
    except Exception as e:
        print(f"Failed to launch browser: {e}")
        return False

# ==============================================================================
# Mapping & Cache Functions
# ==============================================================================

def load_mappings():
    if os.path.exists(MAPPINGS_FILE):
        try:
            with open(MAPPINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_mappings(mappings):
    try:
        with open(MAPPINGS_FILE, "w") as f:
            json.dump(mappings, f, indent=2)
    except Exception as e:
        print(f"Error saving mappings: {e}")

# ==============================================================================
# Pricing & Account Heuristic Parsers
# ==============================================================================

def parse_pricing_page_heuristically(page):
    """JS-based DOM scanner to extract pricing and premium tools."""
    print("Running DOM heuristic parser on pricing page...")
    
    js_parser = r"""
    () => {
        const results = [];
        
        // ── Strategy 1: .credits-tool-row and .credits-mode-item (OCR.best style) ──
        document.querySelectorAll('.credits-tool-row, .credits-mode-item').forEach(el => {
            const text = el.textContent.trim();
            const nameEl = el.querySelector('.credits-tool-name, h3, h4, strong, b');
            let name = nameEl ? nameEl.textContent.trim() : '';
            
            // If no dedicated name element, grab the first text node that isn't a number/credit
            if (!name) {
                for (const child of el.childNodes) {
                    const t = (child.textContent || '').trim();
                    if (t.length > 2 && !/^\d+$/.test(t) && !/credit/i.test(t) && !/per /i.test(t)) {
                        name = t.split('\\n')[0].trim();
                        break;
                    }
                }
            }
            
            const unitBox = el.querySelector('.credits-unit-box');
            if (unitBox) {
                const numMatch = unitBox.textContent.match(/(\d+)/);
                if (numMatch && name) {
                    results.push({ name: name.replace(/\s+/g, ' ').trim(), cost: parseInt(numMatch[1], 10) });
                }
            }
        });
        
        // ── Strategy 2: Generic — scan for elements containing "N credit(s)" ──
        if (results.length === 0) {
            const elements = Array.from(document.querySelectorAll('td, li, p, span, div'));
            for (const el of elements) {
                const text = el.textContent.trim();
                if (text.length < 80 && /\\bcredits?\\b/i.test(text) && /\d/.test(text)) {
                    const creditMatch = text.match(/(\d+)\s*credits?/i);
                    if (creditMatch) {
                        const cost = parseInt(creditMatch[1], 10);
                        let name = '';
                        const parent = el.parentElement;
                        if (parent) {
                            for (const child of Array.from(parent.children)) {
                                const ct = child.textContent.trim();
                                if (child !== el && ct.length > 2 && !/credit/i.test(ct) && !/^\d+$/.test(ct)) {
                                    name = ct;
                                    break;
                                }
                            }
                        }
                        if (!name) {
                            let sib = el.previousElementSibling;
                            while (sib) {
                                const st = sib.textContent.trim();
                                if (st.length > 2 && !/credit/i.test(st)) { name = st; break; }
                                sib = sib.previousElementSibling;
                            }
                        }
                        if (name && cost > 0) {
                            results.push({ name: name.replace(/\s+/g, ' ').trim(), cost });
                        }
                    }
                }
            }
        }
        
        // De-duplicate by name
        const unique = {};
        for (const item of results) { unique[item.name] = item.cost; }
        return Object.entries(unique).map(([name, cost]) => ({ name, cost }));
    }
    """
    try:
        tools = page.evaluate(js_parser)
        # Filter out obvious false positives
        clean_tools = []
        for t in tools:
            name = t["name"]
            if len(name) > 2 and not any(word in name.lower() for word in ["pricing", "plan", "contact", "support", "sign", "upload credits"]):
                clean_tools.append(t)
        return clean_tools
    except Exception as e:
        print(f"Error parsing pricing page: {e}")
        return []

def get_credit_balance_for_pool(page, pool_name, pool_order=None):
    """Extract the 'Credits Allowed' / 'Credits Used' numbers for ONE specific named pool row
    on a multi-pool plan/account page (e.g. editpad.org's Plan Details table, which lists
    'AI Writing Tools', 'Plagiarism Checker', 'Extract Text From Image', and 'Humanize Ai Text'
    as separate rows, each with its own allowed/used counters, alongside an unrelated
    pricing/upgrade widget elsewhere on the page that also matches generic "credit" text).

    Isolates the block of page text between this pool's label and the next known pool label
    (per pool_order) so only that row's numbers are read. Returns None if the label can't be
    found at all, so the caller can fall back to the generic whole-page extraction."""
    js = r"""
    (args) => {
        const [poolName, order] = args;
        const norm = s => s.replace(/\s+/g, ' ').trim();
        const text = norm(document.body.innerText);
        const escapeRe = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const startMatch = text.match(new RegExp(escapeRe(poolName), 'i'));
        if (!startMatch) return null;
        const startIdx = startMatch.index;
        const afterLabelIdx = startIdx + poolName.length;
        let endIdx = text.length;
        for (const label of (order || [])) {
            if (label.toLowerCase() === poolName.toLowerCase()) continue;
            const m = text.slice(afterLabelIdx).match(new RegExp(escapeRe(label), 'i'));
            if (m) {
                const candidateEnd = afterLabelIdx + m.index;
                if (candidateEnd < endIdx) endIdx = candidateEnd;
            }
        }
        const block = text.slice(startIdx, endIdx);
        // Two known orderings seen across sites:
        //  - editpad.org: label THEN number, e.g. "Credits Used 3138"
        //  - grammarcheck.ai: number THEN word, e.g. "719 used" (note: NOT "Credits allowed:
        //    Unlimited" — that has no digits at all, so it can never falsely match either regex)
        const usedMatchLabelFirst = block.match(/Credits\s*Used\D*([\d,]+)/i);
        const usedMatchNumberFirst = block.match(/([\d,]+)\s*used\b/i);
        const usedMatch = usedMatchLabelFirst || usedMatchNumberFirst;
        const nums = (block.match(/[\d,]+/g) || []);
        return { block, nums, used: usedMatch ? usedMatch[1] : null };
    }
    """
    try:
        result = page.evaluate(js, [pool_name, pool_order or []])
        if not result or not result.get("block"):
            return None
        block = " ".join(result["block"].split())
        used_raw = result.get("used")
        numbers = [int(n.replace(",", "")) for n in result.get("nums", []) if n.replace(",", "").isdigit()]
        used_int = int(used_raw.replace(",", "")) if used_raw and used_raw.replace(",", "").isdigit() else None
        # Keep the display text short — just "<N> used" — instead of the full row/section
        # text (plan name, feature checklist, expiry date, etc.), which is only needed
        # internally for the numeric parsing above, not for what gets printed to the console.
        display_text = f"{used_int} used" if used_int is not None else f"{pool_name}: {block[:200]}"
        return {
            "text": display_text,
            "numbers": numbers,
            "pools": [display_text],
            "used": used_int,
        }
    except Exception as e:
        print(f"Error extracting pool '{pool_name}' credit balance: {e}")
        return None

def get_credit_balance(page, pool_name=None, pool_order=None):
    """Extract credit balance representation(s) from the account page.

    If pool_name is given (multi-pool account pages, e.g. editpad.org), isolate just that
    pool's row via get_credit_balance_for_pool() instead of scanning the whole page — falls
    back to the generic whole-page extraction below only if that row can't be found.

    Some accounts show MULTIPLE independent credit pools simultaneously — e.g.
    imagetotext.io's /user-account Plans tab lists a "Monthly" plan (73/10000)
    AND a "Premium" plan (441/100000) at the same time, each with its own counter.
    The previous version collected every matching element but then kept only the
    single SHORTEST one, which silently locked onto whichever pool happened to have
    the shorter rendered text (here, the static Monthly pool) while the real
    deduction landed on the other (Premium) pool — so a real deduction was reported
    as "credit did NOT change" every time.

    Fix: keep ALL distinct matching pools (deduped, and with pure substrings of a
    longer kept match dropped so nested wrapper elements aren't double-counted).
    Comparisons downstream then flag a PASS if ANY pool's numbers changed."""
    if pool_name:
        pool_result = get_credit_balance_for_pool(page, pool_name, pool_order)
        if pool_result is not None:
            return pool_result
        # Row genuinely not found (e.g. wrong page, or label text changed) — return a clearly
        # labeled placeholder instead of falling through to the generic whole-page scan below,
        # which has no way to know it should be looking for this one pool and, when nothing
        # credit-shaped matches, resorts to dumping the first chunk of the ENTIRE page as a
        # last resort (the huge unreadable wall of text seen in earlier runs).
        print(f"[WARN] Could not locate '{pool_name}' pool row on the account page.")
        return {"text": f"Unknown ('{pool_name}' pool not found on page)", "numbers": [], "pools": [], "used": None}
    js_extractor = r"""
    () => {
        const tags = 'div, p, span, li, td, h3, h4, b, strong, label';
        const elements = Array.from(document.querySelectorAll(tags));
        const collect = (pattern) => {
            const seen = new Set();
            const matches = [];
            for (const el of elements) {
                const text = el.textContent.trim();
                if (text.length > 0 && text.length < 300 && pattern.test(text) && /\d/.test(text)) {
                    if (!seen.has(text)) {
                        seen.add(text);
                        matches.push(text);
                    }
                }
            }
            return matches;
        };
        // Real usage displays render as "used / total" (e.g. "0 / 18000", "73/10000") —
        // prefer that ratio format first, since a plan-comparison/pricing table on the
        // same page can contain many other "credit ... <number>" strings (per-tier costs
        // like "1 credit / File", tier quotas like "Total Credits: 300,000") that match
        // the broad /credit/i + digit test but aren't the account's actual balance.
        const ratioPattern = /\d[\d,]*\s*\/\s*[\d,]+/;
        let candidates = collect(/credit/i).filter(t => ratioPattern.test(t));
        if (candidates.length === 0) candidates = collect(/credit/i);
        if (candidates.length === 0) candidates = collect(/usage/i).filter(t => ratioPattern.test(t));
        if (candidates.length === 0) candidates = collect(/usage/i);
        if (candidates.length > 0) {
            // Shortest first (most specific/tightest rows), then drop any candidate
            // that is a pure substring of an already-kept (longer) candidate — this
            // removes duplicate wrapper-element matches without discarding genuinely
            // separate pools like Monthly vs Premium.
            candidates.sort((a, b) => a.length - b.length);
            const kept = [];
            for (const c of candidates) {
                if (!kept.some(k => k.includes(c))) kept.push(c);
            }
            return kept;
        }
        return [document.body.innerText.substring(0, 1000)]; // Last resort: first chunk of page text
    }
    """
    try:
        raw_list = page.evaluate(js_extractor)
        if raw_list:
            normalized_list = [" ".join(t.split()) for t in raw_list]
            combined_text = " | ".join(normalized_list)
            all_numbers = []
            for t in normalized_list:
                nums = re.findall(r'[\d,]+', t)
                all_numbers.extend(int(n.replace(',', '')) for n in nums if n.replace(',', '').isdigit())
            return {"text": combined_text, "numbers": all_numbers, "pools": normalized_list}
    except Exception as e:
        print(f"Error extracting credit balance: {e}")
    return {"text": "Unknown", "numbers": [], "pools": []}

def describe_pool_changes(before, after):
    """Compare pool lists between two balance snapshots and return a short
    description of which specific pool(s) changed and how — pinpoints the exact
    plan that took the deduction on accounts with multiple simultaneous credit
    pools (e.g. Monthly vs Premium), instead of just a generic before/after blob."""
    before_pools = before.get("pools") or ([before["text"]] if before.get("text") else [])
    after_pools = after.get("pools") or ([after["text"]] if after.get("text") else [])
    changes = []
    if len(before_pools) == len(after_pools):
        for b, a in zip(before_pools, after_pools):
            if b != a:
                changes.append(f"{b} → {a}")
    else:
        before_set, after_set = set(before_pools), set(after_pools)
        for a in after_set - before_set:
            changes.append(f"(new) {a}")
        for b in before_set - after_set:
            changes.append(f"(gone) {b}")
    return "; ".join(changes)

# ==============================================================================
# File Association Loader
# ==============================================================================

def get_sample_file_for_type(file_type):
    """Scan the local Test Data directory and return a known-good sample file.
    First checks for a filename confirmed to work (see PREFERRED_TEST_FILES — some
    files pass our own size sanity check but still get rejected by a site's own
    upload validator, e.g. "Invalid File" on a .doc that's structurally malformed
    despite being a normal size). Falls back to the smallest valid-sized file if no
    preferred match exists."""
    subdir = file_type.lower()
    if subdir == "text":
        subdir = "txt"
    # word/excel/ppt/pdf/image already match their own subdir name

    target_dir = os.path.join(TEST_DATA_DIR, subdir)
    if not os.path.exists(target_dir):
        # Fallback to general file list in parent test directory
        target_dir = TEST_DATA_DIR

    ext_map = {
        "image": [".png", ".jpg", ".jpeg", ".webp"],
        "pdf": [".pdf"],
        "word": [".doc", ".docx"],
        "excel": [".xls", ".xlsx"],
        "ppt": [".ppt", ".pptx"],
        "text": [".txt"],
    }
    valid_exts = ext_map.get(file_type, [])
    MIN_SANE_SIZE = 2048  # bytes — below this, a doc/pdf/ppt is almost certainly a broken placeholder

    # Highest-priority override: a small file explicitly named "credits test" (any extension),
    # confirmed by hand to be fast and reliable to upload — replaces whatever the size-based /
    # PREFERRED_TEST_FILES logic below would otherwise pick (which was landing on large files,
    # e.g. "sample2 - Copy (2) - Copy.txt", slowing every conversion down to 15-35s). Checked in
    # both the type-specific subdir and the TEST_DATA_DIR root, and used for every file_type as
    # long as its extension matches what that type expects.
    FORCED_TEST_FILE_TOKEN = "credits test"
    for search_dir in (target_dir, TEST_DATA_DIR):
        try:
            if not os.path.exists(search_dir):
                continue
            for file in os.listdir(search_dir):
                path = os.path.join(search_dir, file)
                name_no_ext, ext = os.path.splitext(file)
                if (os.path.isfile(path)
                        and FORCED_TEST_FILE_TOKEN in name_no_ext.lower()
                        and (not valid_exts or ext.lower() in valid_exts)):
                    return path
        except Exception as e:
            print(f"Error scanning {search_dir} for forced test file: {e}")

    # Filenames (case-insensitive substring match) confirmed to actually work end-to-end,
    # checked before the generic size-based pick.
    PREFERRED_TEST_FILES = {
        "word": ["essayness"],
        "pdf": ["pdf-test"],
    }

    try:
        candidates = []
        for file in os.listdir(target_dir):
            path = os.path.join(target_dir, file)
            if os.path.isfile(path) and os.path.splitext(file)[1].lower() in valid_exts:
                candidates.append((os.path.getsize(path), path, file.lower()))

        preferred_names = PREFERRED_TEST_FILES.get(file_type, [])
        for size, path, fname_lower in candidates:
            if any(pref in fname_lower for pref in preferred_names):
                return path

        if candidates:
            candidates.sort(key=lambda c: c[0])  # smallest file first
            # Prefer the smallest file that clears the sanity floor; if every candidate is
            # below it (all tiny test files), fall back to the largest available rather than
            # risking an empty/corrupt one.
            for size, path, _ in candidates:
                if size >= MIN_SANE_SIZE:
                    return path
            return candidates[-1][1]
    except Exception as e:
        print(f"Error scanning files: {e}")

    # Return a generic fallback file if nothing matches
    return os.path.join(TEST_DATA_DIR, "image", "bullets & numbering.png")

# ==============================================================================
# Seed Files Loader & Cash Initialization
# ==============================================================================

OVERVIEW_FILE = "credits_overview_data.txt"
URLS_FILE = "resolved_tool_urls.json"

def initialize_cache_from_seeds():
    """Load and merge credits_overview_data.txt and resolved_tool_urls.json."""
    if not os.path.exists(OVERVIEW_FILE) or not os.path.exists(URLS_FILE):
        print("[WARNING] Seed files not found. Relying on existing cache or live parsing.")
        return load_mappings()

    print("\n[INFO] Loading and merging seed files...")
    
    # 1. Load URLs file
    try:
        with open(URLS_FILE, 'r', encoding='utf-8') as f:
            urls_data = json.load(f)
    except Exception as e:
        print(f"[ERROR] Failed to load {URLS_FILE}: {e}")
        return load_mappings()
        
    # 2. Parse overview file
    try:
        with open(OVERVIEW_FILE, 'r', encoding='utf-8') as f:
            overview_text = f.read()
    except Exception as e:
        print(f"[ERROR] Failed to load {OVERVIEW_FILE}: {e}")
        return load_mappings()

    sections = re.split(r'===\s*=+', overview_text)
    parsed_overview = {}
    
    for sec in sections:
        if not sec.strip():
            continue
        site_match = re.search(r'\d+\.\s*SITE:\s*([a-zA-Z0-9.\-]+)', sec)
        if not site_match:
            continue
        domain = site_match.group(1).strip()
        
        qty_match = re.search(r'QUANTITY CHECK:\s*(YES|NO)', sec)
        qty_supported = True
        if qty_match and qty_match.group(1) == 'NO':
            qty_supported = False
            
        pricing_match = re.search(r'PRICING PAGE:\s*([^\n]+)', sec)
        pricing_page = pricing_match.group(1).strip() if pricing_match else ""
        
        # Split section to isolate tools list
        parts = sec.split('-----------------------------------------------------------------')
        if len(parts) < 2:
            continue
        tools_section = parts[-1]
        lines = tools_section.splitlines()
        
        tools = []
        current_parent = ""
        
        for line in lines:
            line_str = line.strip()
            if not line_str or line_str.startswith('==') or line_str.startswith('NOTE:'):
                continue
                
            if not line_str.startswith('-') and '->' not in line_str and not line_str.startswith('Premium tools'):
                if ':' in line_str or 'credit' in line_str.lower():
                    pass
                else:
                    current_parent = line_str
                    continue
            
            name = ""
            cost = 1
            is_premium = True
            
            if '->' in line_str:
                parts = line_str.split('->')
                name_part = parts[0].strip().lstrip('-').strip()
                cost_part = parts[1].strip()
                
                if current_parent:
                    name = f"{current_parent} - {name_part}"
                else:
                    name = name_part
                    
                if 'free' in cost_part.lower():
                    is_premium = False
                    cost = 0
                else:
                    num_match = re.search(r'(\d+)', cost_part)
                    if num_match:
                        cost = int(num_match.group(1))
            else:
                name_part = line_str.lstrip('-').strip()
                name = name_part
                cost = 1
                
            if name:
                tools.append({
                    "name": name,
                    "cost": cost,
                    "is_premium": is_premium
                })
                
        parsed_overview[domain] = {
            "pricing_page": pricing_page,
            "quantity_check_supported": qty_supported,
            "tools": tools
        }

    # 3. Merge and Diff
    merged_cache = load_mappings()
    
    def infer_file_type(tool_name):
        """Infer file type from tool name, checking primary function first (not mode qualifiers)."""
        name_lower = tool_name.lower()
        # Primary function prefix takes priority
        if name_lower.startswith("pdf") or "pdf to " in name_lower:
            return "pdf"
        if name_lower.startswith("word") or "word to " in name_lower or name_lower.startswith("docx"):
            return "word"
        if name_lower.startswith("ppt") or "ppt to " in name_lower:
            return "ppt"
        if name_lower.startswith("excel") or "excel to " in name_lower or name_lower.startswith("xlsx"):
            return "excel"
        # Image-based tools
        if any(x in name_lower for x in ["image", "jpg", "png", "photo", "picture", "translator"]):
            return "image"
        # OCR tools (without PDF prefix) default to image
        if "ocr" in name_lower:
            return "image"
        # Text-based tools
        if any(x in name_lower for x in [
            "paraphras", "grammar", "plagiarism", "summariz", "humaniz",
            "essay", "paragraph", "sentence", "rewrite", "checker",
            "detector", "generator", "expander", "extender", "answer",
            "caption", "hashtag", "hook", "headline", "article",
            "reverse text", "text to ", "word counter", "case converter",
            "writer", "writing", "mail", "email"
        ]):
            return "text"
        return "image"  # Default fallback

    def infer_pre_action(tool_name, domain=None):
        """Return a LIST of candidate selectors to try (in order) before uploading, for tools
        that need mode selection. We no longer bet on a single guessed selector — each site's
        markup differs (button vs label vs div), so we try several strategies and fall back
        to a debug dump if none match. See select_mode_option() for how this list is consumed."""
        name_lower = tool_name.lower()

        # jpgtotext.com's "JPG to Word" tool exposes 3 radio-style mode options with
        # site-specific wording confirmed via DOM inspection — the generic
        # formatted/with-ocr/without-ocr wording below doesn't match this site's labels
        # ("Direct Image Insert" / "Extract Text (Plain)" / "Extract Text (Formatted)"),
        # so "JPG to Word - OCR" previously got no pre_action at all and silently ran
        # against whatever mode was already selected.
        if domain == "jpgtotext.com" and "jpg to text" in name_lower:
            if "formatted" in name_lower:
                return [
                    "button.ocr-options-btn[mode='2']",
                    "button[mode='2']",
                    "button.ocr-options-btn:has-text('Formatted OCR')",
                    "text=Formatted OCR",
                ]
            if "simple" in name_lower:
                return [
                    "button.ocr-options-btn[mode='1']",
                    "button[mode='1']",
                    "button.ocr-options-btn:has-text('Simple OCR')",
                    "text=Simple OCR",
                ]

        if domain == "jpgtotext.com" and "jpg to word" in name_lower:
            if "formatted" in name_lower:
                return [
                    "label:has-text('Extract Text (Formatted)')",
                    "text=Extract Text (Formatted)",
                ]
            if "ocr" in name_lower:
                return [
                    "label:has-text('Extract Text (Plain)')",
                    "text=Extract Text (Plain)",
                ]
            if "simple" in name_lower:
                return [
                    "label:has-text('Direct Image Insert')",
                    "text=Direct Image Insert",
                ]

        if "formatted" in name_lower:
            return [
                "label[data-mode='formatted']",
                "[data-mode='formatted']",
                "button:has-text('Formatted')",
                "role=button[name='Formatted']",
                "text=Formatted",
            ]
        if "with ocr" in name_lower and "without" not in name_lower:
            return [
                "label[data-mode='with-ocr']",
                "[data-mode='with-ocr']",
                "button:has-text('With OCR')",
                "role=button[name='With OCR']",
                "text=With OCR",
            ]
        if "without ocr" in name_lower:
            return [
                "label[data-mode='without-ocr']",
                "[data-mode='without-ocr']",
                "button:has-text('Without OCR')",
                "role=button[name='Without OCR']",
                "text=Without OCR",
            ]
        return []  # No pre-action needed
    
    def should_skip_tool(domain, tool_name, notes):
        # Precise skip map based on instructions
        skip_map = {
            # imagetotext.io's "Image Translator" was previously skipped pending
            # confirmation of its actual convert control. Confirmed via DOM inspection:
            # <button id="jsShadowRoot" class="btn translate-btn">Translate</button> —
            # this is already covered by the "#jsShadowRoot" high-priority submit
            # candidate + is_translate_tool verb-ordering logic below, so it's safe
            # to run now. No longer skipped.
            "grammarcheck.ai": ["AI Summarizer"],
            "imagetotext.cc": ["Image to Excel"],
        }
        if domain in skip_map:
            for sk in skip_map[domain]:
                if sk.lower() == tool_name.lower():
                    return True
        for note in notes:
            quoted_terms = re.findall(r"'(.*?)'", note)
            for term in quoted_terms:
                if term.lower() == tool_name.lower():
                    return True
        return False

    for domain, url_info in urls_data.items():
        domain_clean = domain.strip()
        overview_info = parsed_overview.get(domain_clean, {})
        
        if not overview_info:
            continue
            
        merged_tools = []
        url_tools = url_info.get("tools", {})
        ov_tools = overview_info.get("tools", [])
        
        def normalize(n):
            cleaned = n.lower().replace("features", "")
            return re.sub(r'[^a-z0-9]', '', cleaned)
            
        mapped_ov_names = set()
        
        for name_url, path in url_tools.items():
            norm_url = normalize(name_url)
            match_ov = None
            for ot in ov_tools:
                norm_ot = normalize(ot["name"])
                if norm_url == norm_ot or norm_url in norm_ot or norm_ot in norm_url:
                    match_ov = ot
                    mapped_ov_names.add(ot["name"])
                    break
                    
            cost = 1
            is_premium = True
            
            if match_ov:
                cost = match_ov["cost"]
                is_premium = match_ov["is_premium"]
            else:
                print(f"[WARN] Tool '{name_url}' on {domain_clean} is in resolved_tool_urls.json but missing from credits_overview_data.txt (Source files mismatch)")
                
            should_skip = should_skip_tool(domain_clean, name_url, url_info.get("needs_confirmation", []))
                    
            merged_tools.append({
                "name": name_url,
                "url": f"{url_info['base_url'].rstrip('/')}/{path.lstrip('/')}" if path else "",
                "is_premium": is_premium,
                "cost": cost,
                "file_type": infer_file_type(name_url),
                "pre_action": infer_pre_action(name_url, domain_clean),
                "selectors": {
                    "file_input": "input#file",
                    "submit_btn": "button#submitBtn",
                    "result_indicator": ""
                },
                "skip": should_skip
            })
            
        for ot in ov_tools:
            if ot["name"] not in mapped_ov_names:
                norm_ot = normalize(ot["name"])
                has_match = False
                for name_url in url_tools.keys():
                    if norm_ot in normalize(name_url) or normalize(name_url) in norm_ot:
                        has_match = True
                        break
                if not has_match and "Premium tools" not in ot["name"]:
                    print(f"[WARN] Tool '{ot['name']}' on {domain_clean} is in credits_overview_data.txt but missing from resolved_tool_urls.json (Source files mismatch)")
                    
        quantity_supported = overview_info.get("quantity_check_supported", True)
        if url_info.get("quantity_check") is False:
            quantity_supported = False
            
        merged_cache[domain_clean] = {
            "pricing_url": f"{url_info['base_url'].rstrip('/')}/{url_info['pricing_page'].lstrip('/')}",
            "account_url": f"{url_info['base_url'].rstrip('/')}/{SITE_ACCOUNT_PATHS.get(domain_clean, '/account').lstrip('/')}",
            "quantityCheckSupported": quantity_supported,
            "tools": merged_tools
        }
        
    save_mappings(merged_cache)
    print("[INFO] Seed merge completed and site_mappings.json updated.\n")
    return merged_cache

def check_staging_host(domain):
    """Check if staging subdomain DNS resolves."""
    staging_host = f"staging.{domain}"
    try:
        socket.gethostbyname(staging_host)
        return True
    except Exception:
        return False

def get_staging_url(url, domain):
    """Prefix the domain in the URL with staging."""
    parsed = urlparse(url)
    netloc = parsed.netloc
    if "www." in netloc:
        netloc = netloc.replace("www.", "")
    staging_netloc = f"staging.{domain}"
    rebuilt = parsed._replace(netloc=staging_netloc)
    return rebuilt.geturl()

def prompt_new_tool_wizard(base_url, domain):
    """Interactively collect a brand-new tool's config — name, URL, input method, submit
    button, result indicator — copy-pasted once by hand from live DOM inspection. Returns a
    dict in the exact same shape as a configured_tools entry, so it runs through the main
    loop identically to any pricing-page-discovered tool (no code changes needed) and gets
    cached in site_mappings.json, so it's already there on every future run of this site.
    Returns None if the person cancels (blank tool name)."""
    cprint(f"\n  --- Add a new tool for {domain} ---", C.CYAN, bold=True)
    name = input("  Tool name: ").strip()
    if not name:
        cprint("  (No name entered — cancelled, not adding a tool.)", C.YELLOW)
        return None

    default_url = f"{base_url.rstrip('/')}/{name.lower().replace(' ', '-')}"
    url = input(f"  Tool URL (relative path or full URL) [{default_url}]: ").strip() or default_url
    if not url.startswith("http"):
        # Allow a bare relative path like "/my-tool" or "my-tool" typed by hand
        url = f"{base_url.rstrip('/')}/{url.lstrip('/')}"

    print("  Input method:")
    print("    1. File upload")
    print("    2. Typed/pasted text")
    input_method = input("  Choice (1-2) [1]: ").strip() or "1"

    file_type = "text"
    file_input_sel = "input[type='file']"
    text_input_conf = None

    if input_method == "2":
        text_input_sel = input("  Text input selector (paste the exact element's id/class, e.g. #input_text): ").strip()
        sample_text = input("  Sample text to type in [Enter for a generic default]: ").strip()
        if not sample_text:
            sample_text = ("This is a sample paragraph used by our QA automation script to test "
                            "whether the credit deduction system is working correctly for this tool.")
        no_submit = input("  Does typing/pasting alone trigger the result — NO submit button to click? (y/n) [n]: ").strip().lower() == "y"
        text_input_conf = {"selector": text_input_sel, "text": sample_text}
        if no_submit:
            text_input_conf["no_submit_needed"] = True
    else:
        file_type = input("  File type (image/pdf/word/excel/ppt/text) [text]: ").strip() or "text"
        file_input_sel = input("  File input selector [input[type='file']]: ").strip() or "input[type='file']"

    submit_sel = ""
    if not (text_input_conf and text_input_conf.get("no_submit_needed")):
        submit_sel = input("  Submit/Convert button selector (paste the exact element's id/class): ").strip()

    result_sel = input("  Result-indicator selector (an element that ONLY appears once the result is generated): ").strip()

    new_tool = {
        "name": name,
        "url": url,
        "is_premium": True,
        "cost": 1,
        "file_type": file_type,
        "selectors": {
            "file_input": file_input_sel,
            "submit_btn": submit_sel,
            "result_indicator": result_sel
        },
        "skip": False
    }
    if text_input_conf:
        new_tool["text_input"] = text_input_conf

    cprint(f"  ✓ '{name}' added — runs in this session and is now saved for every future run.", C.GREEN)
    return new_tool

# ==============================================================================
# Core CLI Automation Execution Flow
# ==============================================================================

def run_cli_flow():
    # Load and seed cache on startup
    mappings = initialize_cache_from_seeds()

    # Parse CLI arguments for non-interactive execution
    import argparse
    parser = argparse.ArgumentParser(description="Credit/Query Deduction QA Automation")
    parser.add_argument("-e", "--env", choices=["staging", "live"], default=None, help="Testing environment")
    parser.add_argument("-s", "--site", help="Website domain (e.g., ocr.best) or menu index")
    parser.add_argument("-b", "--browser", choices=["Brave", "Chrome", "Edge", "Firefox"], default=None, help="Browser to use")
    parser.add_argument("-y", "--yes", action="store_true", help="Auto-confirm all confirmation prompts")
    parser.add_argument("--re-parse", action="store_true", help="Force re-parsing of pricing page")
    args, unknown = parser.parse_known_args()

    print("="*80)
    print("Credit/Query Deduction QA Automation (Phase 1 — Staging)")
    print("="*80)

    # 1. Prompt Environment
    if args.env:
        env = args.env
    else:
        print("\nSelect environment to test:")
        print("  1. Staging")
        print("  2. Live")
        env_choice = input("Enter choice (1-2) [1]: ").strip()
        if not env_choice or env_choice == "1":
            env = "staging"
        elif env_choice == "2":
            env = "live"
        else:
            print("Invalid selection. Exiting.")
            sys.exit(1)
            
    if env == "live":
        print("[INFO] Live validation requires exact quantity checks (Phase 2). Stubbing/not supported yet.")
        sys.exit(0)

    # 2. Numbered Menu Selection
    sorted_domains = sorted(list(mappings.keys()))
    
    choice = None
    if args.site:
        choice = args.site.strip()
    else:
        print("\nSelect a website to test:")
        for idx, domain in enumerate(sorted_domains):
            print(f"  {idx + 1}. {domain}")
        print(f"  {len(sorted_domains) + 1}. Run All Sites")
        print(f"  {len(sorted_domains) + 2}. Enter a custom site URL")
        choice = input(f"Enter choice (1-{len(sorted_domains) + 2}): ").strip()

    if not choice:
        print("No selection. Exiting.")
        sys.exit(0)

    target_domains = []
    custom_url = ""

    # Check if choice is a domain name directly
    if choice in sorted_domains:
        target_domains = [choice]
    elif choice.lower() == "all":
        target_domains = sorted_domains
    else:
        try:
            choice_idx = int(choice)
            if 1 <= choice_idx <= len(sorted_domains):
                target_domains = [sorted_domains[choice_idx - 1]]
            elif choice_idx == len(sorted_domains) + 1:
                target_domains = sorted_domains
            elif choice_idx == len(sorted_domains) + 2:
                if args.site: # non-interactive
                    print("[ERROR] Custom URL choice requires interactive input or passing site name.")
                    sys.exit(1)
                custom_url = input("Enter custom pricing page URL: ").strip()
                if not custom_url:
                    print("Custom URL is required.")
                    sys.exit(1)
            else:
                print("Invalid selection. Exiting.")
                sys.exit(1)
        except ValueError:
            print("Invalid selection. Exiting.")
            sys.exit(1)

    # Build target runs list
    run_sites = []
    
    for domain in target_domains:
        site_map = mappings.get(domain, {})
        if env == "staging":
            if not check_staging_host(domain):
                print(f"[WARN] Staging host staging.{domain} is not resolving. Skipping.")
                run_sites.append({
                    "domain": domain,
                    "status": "staging-unavailable",
                    "site_map": site_map,
                    "env": env
                })
                continue
                
            staging_pricing = get_staging_url(site_map["pricing_url"], domain)
            staging_account = get_staging_url(site_map["account_url"], domain)
            run_sites.append({
                "domain": domain,
                "status": "ready",
                "pricing_url": staging_pricing,
                "account_url": staging_account,
                "site_map": site_map,
                "env": env
            })
        else:
            run_sites.append({
                "domain": domain,
                "status": "ready",
                "pricing_url": site_map["pricing_url"],
                "account_url": site_map["account_url"],
                "site_map": site_map,
                "env": env
            })

    if custom_url:
        parsed = urlparse(custom_url)
        domain = parsed.netloc
        if "www." in domain:
            domain = domain.replace("www.", "")
        run_sites.append({
            "domain": domain,
            "status": "ready",
            "pricing_url": custom_url,
            "account_url": f"{parsed.scheme}://{parsed.netloc}/account",
            "site_map": mappings.get(domain, {}),
            "env": env,
            "is_custom": True
        })

    ready_runs = [s for s in run_sites if s["status"] == "ready"]
    unavailable_runs = [s for s in run_sites if s["status"] == "staging-unavailable"]

    if not ready_runs:
        print("\nNo sites available to test.")
        # Output summary report immediately for unavailable sites
        print("\n" + "="*80)
        print("QA REPORT SUMMARY")
        print("="*80)
        for s in unavailable_runs:
            print(f"[Site]: {s['domain']} → staging-unavailable")
        print("="*80)
        sys.exit(0)

    # 3. Detect and setup Browser
    browser_name = get_default_browser_name()
    if args.browser:
        browser_name = args.browser
    else:
        print(f"\nDetected default browser: {browser_name}")
        choice = input(f"Use {browser_name}? (Press Enter, or type Chrome/Brave/Edge/Firefox): ").strip()
        if choice in BROWSER_CONFIGS:
            browser_name = choice

    if not ensure_browser_debugging(browser_name):
        print("[ERROR] Could not setup browser debugging. Aborting.")
        sys.exit(1)

    # Launch Playwright
    print("\nConnecting Playwright to the active browser...")
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        except Exception as e:
            print(f"Failed to connect via CDP: {e}")
            sys.exit(1)

        context = browser.contexts[0]
        final_reports = {}

        for run in ready_runs:
            domain = run["domain"]
            pricing_url = run["pricing_url"]
            account_url = run["account_url"]
            site_map = run["site_map"]

            print("\n" + "="*80)
            print(f"RUNNING QA VALIDATION ON SITE: {domain}")
            print("="*80)

            # Re-derive base URL
            parsed = urlparse(pricing_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}/"

            # Check force refresh options
            force_refresh = False
            if args.re_parse:
                force_refresh = True
            elif run.get("is_custom"):
                force_refresh = True
            elif site_map:
                if args.yes:
                    force_refresh = False
                else:
                    print(f"Loaded config for {domain}.")
                    choice = input(f"Re-parse pricing page for {domain}? (y/n) [n]: ").strip().lower()
                    if choice == "y":
                        force_refresh = True

            # Open tabs for this site (no pricing tab — not needed for execution)
            cprint("  Opening account & tool tabs...", C.DIM)

            account_tab = context.new_page()
            safe_goto(account_tab, account_url, label="account page")
            safe_wait_dom(account_tab)

            tool_tab = context.new_page()
            safe_goto(tool_tab, base_url, label="tool page")

            # 4. Handle Pricing Parsing
            tools = []
            if site_map and not force_refresh:
                print("Loading cached tools list...")
                tools = site_map.get("tools", [])
            else:
                print("Parsing pricing page dynamically...")
                cprint("  Opening pricing tab to parse page dynamically...", C.DIM)
                pricing_tab = context.new_page()
                safe_goto(pricing_tab, pricing_url, label="pricing page")
                safe_wait_idle(pricing_tab, label="Pricing page")
                tools = parse_pricing_page_heuristically(pricing_tab)
                pricing_tab.close()
                
                if not tools:
                    print("Heuristic parsing found no tools.")
                else:
                    print(f"Found {len(tools)} potential tools on pricing page:")
                    for idx, t in enumerate(tools):
                        print(f"  {idx + 1}. {t['name']} (Credit cost: {t['cost']})")
                    
                    if args.yes:
                        keep_choice = "y"
                    else:
                        print("\nWould you like to keep all these premium tools for testing? (y/n) [y]: ")
                        keep_choice = input("> ").strip().lower()
                        
                    if keep_choice == "n":
                        selected_tools = []
                        for t in tools:
                            c = input(f"Include {t['name']}? (y/n) [y]: ").strip().lower()
                            if c != "n":
                                selected_tools.append(t)
                        tools = selected_tools

            # Setup configuration / interactive wizard for new tools
            configured_tools = []
            cached_tools_dict = {t["name"]: t for t in site_map.get("tools", [])}

            for t in tools:
                name = t["name"]
                cached = cached_tools_dict.get(name, {})
                
                # Auto-deduce defaults
                def_url = cached.get("url", f"{base_url.rstrip('/')}/{name.lower().replace(' ', '-')}")
                def_type = cached.get("file_type", "image" if any(x in name.lower() for x in ["image", "translator", "ocr", "jpg", "png", "photo"]) else "pdf")
                def_input = cached.get("selectors", {}).get("file_input", "input#file")
                def_submit = cached.get("selectors", {}).get("submit_btn", "button#submitBtn")
                def_indicator = cached.get("selectors", {}).get("result_indicator", ".result-box")

                if not cached or force_refresh:
                    if args.yes:
                        url = def_url
                        file_type = def_type
                        input_sel = def_input
                        submit_sel = def_submit
                        result_sel = def_indicator
                    else:
                        print(f"\n--- Configure Automation for '{name}' ---")
                        url = input(f"Tool URL [{def_url}]: ").strip() or def_url
                        file_type = input(f"File Type (image/pdf/word/excel/text) [{def_type}]: ").strip() or def_type
                        input_sel = input(f"File input selector [{def_input}]: ").strip() or def_input
                        submit_sel = input(f"Submit/Convert selector [{def_submit}]: ").strip() or def_submit
                        result_sel = input(f"Result indicator selector [{def_indicator}]: ").strip() or def_indicator
                    
                    configured_tools.append({
                        "name": name,
                        "url": url,
                        "is_premium": t.get("is_premium", True),
                        "cost": t.get("cost", 1),
                        "file_type": file_type,
                        "selectors": {
                            "file_input": input_sel,
                            "submit_btn": submit_sel,
                            "result_indicator": result_sel
                        },
                        "skip": t.get("skip", False)
                    })
                else:
                    configured_tools.append(cached)

            # Save Cache
            if configured_tools:
                site_map = {
                    "pricing_url": pricing_url,
                    "account_url": account_url,
                    "quantityCheckSupported": site_map.get("quantityCheckSupported", True),
                    "tools": configured_tools
                }
                mappings[domain] = site_map
                save_mappings(mappings)

            # Confirm extracted tools and URLs before running
            def print_tools_list():
                cprint("\n  Tools to test:", C.CYAN, bold=True)
                for idx, t in enumerate(configured_tools):
                    skip_info = f" {C.YELLOW}[SKIP]{C.RESET}" if t.get("skip") else ""
                    t_url = get_staging_url(t['url'], domain) if env == "staging" else t['url']
                    cprint(f"    {idx + 1}. {t['name']}: {C.DIM}{t_url}{C.RESET}{skip_info}", C.WHITE)
            print_tools_list()

            # Offer to add a tool not already in this list — e.g. a new tool the site just
            # launched that isn't on the pricing page yet, or one that was deliberately
            # excluded above. Loops so more than one can be added in a row. Each addition is
            # appended to configured_tools (runs in THIS session too) and saved to
            # site_mappings.json immediately, so it's already there on every future run.
            if not args.yes:
                while True:
                    add_choice = input(f"\n  Add a new tool for {domain}? (y/n) [n]: ").strip().lower()
                    if add_choice != "y":
                        break
                    new_tool = prompt_new_tool_wizard(base_url, domain)
                    if new_tool:
                        configured_tools.append(new_tool)
                        site_map = {
                            "pricing_url": pricing_url,
                            "account_url": account_url,
                            "quantityCheckSupported": site_map.get("quantityCheckSupported", True) if isinstance(site_map, dict) else True,
                            "tools": configured_tools
                        }
                        mappings[domain] = site_map
                        save_mappings(mappings)
                        print_tools_list()

            if not args.yes:
                input(f"\n  {C.DIM}Press Enter to start running...{C.RESET}")

            # Run automation and verification
            report = []
            total_tools = len([t for t in configured_tools if not t.get("skip")])
            tested_idx = 0

            for tool in configured_tools:
                tool_name = tool["name"]
                if tool.get("skip"):
                    cprint(f"\n  {skip_label()} {tool_name} — skipped per config", C.YELLOW)
                    report.append({
                        "tool": tool_name,
                        "status": "SKIPPED",
                        "before": "N/A",
                        "after": "N/A",
                        "detail": "Flagged as skipped in configuration notes"
                    })
                    continue

                tested_idx += 1
                cprint(f"\n  ┌─ [{tested_idx}/{total_tools}] {tool_name}", C.CYAN, bold=True)
                
                # Check for closed pages and reopen if necessary
                try:
                    if account_tab.is_closed():
                        cprint("  │ Account tab was closed. Re-opening...", C.YELLOW)
                        account_tab = context.new_page()
                        safe_goto(account_tab, account_url, label="account page")
                        safe_wait_dom(account_tab)
                except Exception as tab_ex:
                    cprint(f"  │ Error checking/reopening account_tab: {tab_ex}. Recreating...", C.YELLOW)
                    try:
                        account_tab = context.new_page()
                        safe_goto(account_tab, account_url, label="account page")
                        safe_wait_dom(account_tab)
                    except:
                        pass

                try:
                    if tool_tab.is_closed():
                        cprint("  │ Tool tab was closed. Re-opening...", C.YELLOW)
                        tool_tab = context.new_page()
                except Exception as tab_ex:
                    cprint(f"  │ Error checking/reopening tool_tab: {tab_ex}. Recreating...", C.YELLOW)
                    try:
                        tool_tab = context.new_page()
                    except:
                        pass

                balance_before = {"text": "N/A", "numbers": []}
                
                try:
                    # A. Get credits before action
                    account_tab.bring_to_front()
                    # Re-navigate to a cache-busted account_url instead of a blind reload().
                    # Two problems this fixes together:
                    #  1) reload() re-fetches whatever URL the tab is CURRENTLY on — if a prior
                    #     click_credit_tab_if_present() click on a "Plans"/"Membership"/etc. label
                    #     turned out to be a real link (e.g. editpad.org's "Plans" tab navigating
                    #     to /api/plans instead of switching a client-side tab), the tab's URL
                    #     silently drifts away from account_url and every later reload() just
                    #     keeps refreshing that wrong page.
                    #  2) goto() to the exact same URL+hash as last time doesn't reliably force
                    #     the SPA to refetch the balance from the server, so it can keep showing
                    #     stale numbers until a manual F5 — a unique query string guarantees a
                    #     real, fresh navigation every single check.
                    pool_name = get_pool_name_for_tool(domain, tool_name)
                    pool_order = get_pool_order_for_domain(domain)
                    safe_goto(account_tab, cache_busted_url(account_url), label="account page")
                    safe_wait_dom(account_tab)
                    if pool_name:
                        # Multi-pool sites (editpad.org) land directly on the right view via
                        # account_url's own #fragment — no tab needs clicking, and the generic
                        # wait_for_credit_text() checks below don't even match this page's
                        # "Credits Allowed"/"Credits Used" layout, so they'd just burn up to
                        # 16s waiting out their own timeouts for nothing. Wait only for the
                        # specific pool's own text instead — faster and more accurate.
                        wait_for_pool_text(account_tab, pool_name)
                    else:
                        # Let client-rendered content actually hydrate first — on an SPA,
                        # tab labels don't exist in the DOM yet right after domcontentloaded,
                        # so clicking a tab before this would silently no-op.
                        wait_for_credit_text(account_tab)
                        # Click whichever tab exposes credit info (Plans/Usage/Billing/Subscription) —
                        # some account pages (e.g. imagetotext.io) land on a different tab by default.
                        # Skips entirely if the balance is already visible (see has_credit_ratio_text).
                        click_credit_tab_if_present(account_tab)
                        wait_for_credit_text(account_tab)
                    balance_before = get_credit_balance(account_tab, pool_name=pool_name, pool_order=pool_order)
                    cprint(f"  │ Credits before: {balance_before['text']}", C.DIM)

                    # B. Execute conversion
                    tool_tab.bring_to_front()
                    tool_url = get_staging_url(tool['url'], domain) if env == "staging" else tool['url']
                    cprint(f"  │ Navigating → {tool_url}", C.DIM)
                    safe_goto(tool_tab, tool_url, label="tool page")
                    safe_wait_idle(tool_tab, label="Tool page")
                    
                    text_input_override = TEXT_INPUT_OVERRIDES.get((domain, tool_name)) or TEXT_INPUT_OVERRIDES.get((domain, "default")) or tool.get("text_input")
                    pre_action = tool.get("pre_action", [])
                    if isinstance(pre_action, str):  # backward-compat with old cached single-string format
                        pre_action = [s.strip() for s in pre_action.split(",") if s.strip()]

                    if text_input_override:
                        # This tool has no working file-upload path into its actual input box —
                        # type/paste into its textbox instead.
                        cprint(f"  │ Typing input text (type=typed_text)", C.DIM)
                        if "selectors" in text_input_override:
                            # Exact element not confirmed yet, or confirmed but on a
                            # client-rendered page that can take a moment to hydrate — try
                            # several plausible candidates with a generous per-candidate wait
                            # rather than a single quick guess.
                            matched_sel = fill_text_input(tool_tab, text_input_override["selectors"], text_input_override["text"], timeout_each_ms=4000, existence_ms=2000)
                            if not matched_sel:
                                dump_debug_info(tool_tab, "", tag=f"{domain}_{tool_name}_textinput")
                                raise Exception("No text input field found on page")
                        else:
                            textarea = tool_tab.locator(text_input_override["selector"]).first
                            try:
                                textarea.click(timeout=4000)
                                textarea.fill(text_input_override["text"])
                            except Exception:
                                dump_debug_info(tool_tab, "", tag=f"{domain}_{tool_name}_textinput")
                                raise Exception("No text input field found on page")
                        time.sleep(0.5)
                    else:
                        file_path = get_sample_file_for_type(tool["file_type"])
                        cprint(f"  │ Uploading: {os.path.basename(file_path)} (type={tool['file_type']})", C.DIM)

                        # B1. Try mode selector BEFORE upload — some tools (e.g. auto-convert-on-upload
                        # ones) need this, and the toggle is visible pre-upload on several sites.
                        # Silent/short attempt: it's genuinely expected to fail here on tools where the
                        # mode panel only renders after a file is present (e.g. PDF-to-Excel), so no
                        # warning yet — we retry properly after upload if this doesn't land.
                        mode_selected = False
                        if pre_action:
                            matched_sel = select_mode_option(tool_tab, pre_action, timeout_each_ms=1200)
                            if matched_sel:
                                cprint(f"  │ Mode selected: {matched_sel}", C.MAGENTA)
                                mode_selected = True
                                time.sleep(0.5)

                        # B2. Upload file — try the cached selector, falling back to a generic
                        # input[type='file'] since not every tool on a site shares the same widget/id.
                        file_input, matched_input_sel = find_file_input(tool_tab, tool["selectors"]["file_input"])
                        if not file_input:
                            dump_debug_info(tool_tab, "", tag=f"{domain}_{tool_name}_upload")
                            raise Exception("No file input found on page")
                        file_input.set_input_files(file_path)
                        time.sleep(2.5)

                        # B3. Retry mode selector AFTER upload if it didn't match before — on some
                        # tools (e.g. PDF-to-Excel's Without/With OCR panel) the mode toggle only
                        # renders once a file is present.
                        if pre_action and not mode_selected:
                            matched_sel = select_mode_option(tool_tab, pre_action)
                            if matched_sel:
                                cprint(f"  │ Mode selected: {matched_sel}", C.MAGENTA)
                                time.sleep(0.5)
                            else:
                                cprint(f"  │ {warn_label()} None of the mode selectors matched, using default", C.YELLOW)
                                label_guess = pre_action[-1].split("=")[-1].strip("'\"")
                                dump_debug_info(tool_tab, label_guess, tag=f"{domain}_{tool_name}_mode")

                    # B4. Click submit/convert/translate button — UNLESS this tool is known to
                    # auto-convert on upload with no button at all (currently only seen on
                    # imagetotext.info's home Image-To-Text tool, and only on staging; the same
                    # tool on live does have a Convert button), OR the text-input override says
                    # this tool has no submit button at all (e.g. summarizer.org's Grammar
                    # Checker — typing/pasting auto-triggers analysis, see TEXT_INPUT_OVERRIDES).
                    auto_converts = (domain, env, tool_name) in AUTO_CONVERT_NO_BUTTON or (text_input_override and text_input_override.get("no_submit_needed"))
                    if auto_converts:
                        cprint(f"  │ No convert button expected here (auto-converts on upload/input) — skipping click", C.DIM)
                    else:
                        submit_sel = tool["selectors"]["submit_btn"]
                        # Prioritize the verb that actually matches this tool's purpose — a
                        # generic "Convert first" ordering was landing on decoy 'Convert' links
                        # elsewhere on the page for translator tools instead of the real
                        # 'Translate' button.
                        is_translate_tool = "translat" in tool_name.lower()
                        verbs = ["Translate", "Convert", "Submit"] if is_translate_tool else ["Convert", "Translate", "Submit"]
                        # "Extract" covers jpgtotext.com's JPG-to-Text tools ("Extract Now"),
                        # which don't say "Convert" at all — appended last since #extract-btn
                        # below should already catch that site before this verb is needed.
                        verbs = verbs + ["Extract"]

                        submit_candidates = []
                        if submit_sel and submit_sel != "button#submitBtn":
                            # A real, confirmed selector for THIS tool (either hand-entered via
                            # the "add a new tool" wizard, or edited into site_mappings.json
                            # directly) — trust it ahead of every guess below. Without this, a
                            # brand-new tool on a domain with no SUBMIT_BTN_OVERRIDES entry would
                            # fall straight to the generic verb-guessing / jsShadowRoot fallback
                            # further down (which belongs to a different, unrelated site) before
                            # ever trying the selector the user actually confirmed by hand.
                            # "button#submitBtn" is excluded since it's just the wizard's
                            # untouched placeholder default, not a real confirmed value.
                            submit_candidates.append(submit_sel)
                        if domain == "jpgtotext.com":
                            # Confirmed via live DOM inspection: JPG to Text tools use
                            # id="extract-btn" ("Extract Now"), everything else on this site
                            # uses id="convert-btn" ("Convert"/"Convert Now"). Plain CSS ID
                            # selectors aren't ambiguity-guarded (see AMBIGUITY_RISK_SELECTORS_PREFIXES),
                            # so even PDF to Word's duplicate id="convert-btn" markup (one plain,
                            # one with the loader SVG) resolves fine — .first just takes the first
                            # of the two, both of which trigger the same conversion.
                            submit_candidates += ["#extract-btn", "#convert-btn"]
                        elif domain == "editpad.org":
                            # Confirmed via live DOM inspection: 8 of 9 tools share
                            # id="main_tool_btn" (reused per-page across separate tool pages,
                            # which is fine); AI Essay Writer's ".write__essay" is the exception.
                            submit_candidates += SUBMIT_BTN_OVERRIDES.get(
                                (domain, tool_name), SUBMIT_BTN_OVERRIDES[(domain, "default")]
                            )
                        elif (domain, tool_name) in SUBMIT_BTN_OVERRIDES or (domain, "default") in SUBMIT_BTN_OVERRIDES:
                            # Config-driven: any site with an entry in SUBMIT_BTN_OVERRIDES (see
                            # that dict's comments for what's confirmed on each site — currently
                            # editpad.org, grammarcheck.ai, summarizer.org, paraphrasing.io) uses
                            # its per-tool selector if one exists, else that site's "default".
                            submit_candidates += SUBMIT_BTN_OVERRIDES.get(
                                (domain, tool_name), SUBMIT_BTN_OVERRIDES.get((domain, "default"), [])
                            )
                        else:
                            # Confirmed via live DOM inspection across 5 tools on a different
                            # site (JPG To Excel, PDF To Excel, PDF To Word, Word To Excel,
                            # PPT To PDF): the real convert control there is a <div>/<span
                            # id="jsShadowRoot" class="convert-btn"> — not a <button>, <a>, or
                            # [role=button] at all, which is exactly why every tag-based guess
                            # below was finding nothing on that site.
                            submit_candidates += ["#jsShadowRoot", ".convert-btn"]
                        # button: is specific/safe — try it in verb-priority order next.
                        # Uses word-boundary matching (see _build_locator) so "Convert" can't
                        # match inside "Converter" — e.g. a page's own "PDF to Excel Converter"
                        # H1/title, which was getting matched and clicked as if it were the button.
                        submit_candidates += [f"regexverb:button:{v}" for v in verbs]
                        # div:/span: cover sites (like this one) whose "button" is actually a
                        # styled div/span with a click handler rather than a real <button>.
                        # a: and [role=button]: are broader still. All four are ambiguity-guarded
                        # in find_clickable — skipped outright if more than one element matches,
                        # since a non-unique match risks clicking an unrelated decoy element and
                        # triggering a real page navigation instead of a conversion.
                        submit_candidates += [f"regexverb:div:{v}" for v in verbs]
                        submit_candidates += [f"regexverb:span:{v}" for v in verbs]
                        submit_candidates += [f"regexverb:a:{v}" for v in verbs]
                        submit_candidates += [f"regexverb:[role='button']:{v}" for v in verbs]

                        # Cached/generic ID guesses last — these are unverified per-tool defaults
                        # (often a site-wide fallback like button#submitBtn) that can false-match
                        # an unrelated form elsewhere on the page and trigger a real page
                        # submit/reload instead of the actual convert button.
                        generic_fallbacks = [
                            "button#submitBtn", "button#convertBtn", "button#translateShadowBtn", "button.convertBtn",
                            "button[type='submit']", "input[type='submit']",
                        ]
                        submit_candidates += generic_fallbacks

                        submit_btn, matched_btn_sel = find_clickable(tool_tab, submit_candidates, timeout_each_ms=3000, existence_ms=1500)
                        if submit_btn:
                            try:
                                el_preview = submit_btn.inner_text(timeout=1000).strip().replace("\n", " ")[:50]
                            except Exception:
                                el_preview = "?"
                            cprint(f"  │ Clicking convert ({matched_btn_sel} → \"{el_preview}\")...", C.DIM)
                            submit_btn.click()
                        else:
                            dump_debug_info(tool_tab, "", tag=f"{domain}_{tool_name}_submit")
                            raise Exception("No submit/convert button found on page")


                    # Wait for conversion: smart polling for result elements
                    cprint(f"  │ Waiting for result...", C.DIM)
                    result_detected = False
                    # A real, confirmed result-indicator selector for THIS specific tool (either
                    # hand-entered via the "add a new tool" wizard, or edited into
                    # site_mappings.json) — trusted ahead of everything else below.
                    # ".result-box" is excluded since it's just the wizard's untouched
                    # placeholder default, not a real confirmed value.
                    tool_result_indicator = (tool.get("selectors", {}) or {}).get("result_indicator", "").strip()
                    if tool_result_indicator == ".result-box":
                        tool_result_indicator = ""
                    # High-confidence markers: real action buttons confirmed via live DOM
                    # inspection across all 9 tools on this site — these can only exist once a
                    # result actually exists (you can't "start over" from nothing), unlike a
                    # heading which can render as a loading placeholder the instant processing
                    # starts. These get a much shorter stability requirement below.
                    HIGH_CONFIDENCE_SELECTORS = ([tool_result_indicator] if tool_result_indicator else []) + (RESULT_INDICATOR_OVERRIDES.get((domain, tool_name)) or RESULT_INDICATOR_OVERRIDES.get((domain, "default")) or []) + [
                        ".start-over-btn", "button.start-over", "#js-start-over", "#startAgain",  # "Start Over"/"Start Again" button
                        "text=Start Again", "text=Start Over", "text=Upload Another Image",  # redundant text fallback (jpgtotext.com's button shows "Upload Another Image" at desktop widths, "Start Over" only below the sm breakpoint)
                        ".js-reset-icon", ".reset-icon",                     # Image Translator's "Reset" control
                    ]
                    # Weaker/guessed markers — still useful, but require the fuller stability
                    # window since some of these (e.g. a "Result (1)" heading) can render as a
                    # loading placeholder before the actual content populates.
                    result_selectors = [
                        "text=Result (",
                        "text=Finished",
                        "a[download]",           # Download link
                        "text=Download",
                        "a.downloadBtn",         # Download button class
                        "button.downloadBtn",    # Download button
                        "#downloadBtn",          # Download button by ID
                        ".download-section:visible",  # Download section
                        ".result-box:visible",   # Result text box
                        "#resultDiv:visible",    # Result div
                        ".result-content:visible", # Result content
                        "#output-text:visible",  # Output text
                        ".output-text:visible",  # Output text class
                        ".converted-result:visible", # Converted result
                    ]
                    def is_really_visible(locator, timeout_ms=120):
                        """Same actionability signal Playwright's own trial-click check gives —
                        display/visibility/opacity, zero-size, and being covered by another
                        element — but via a JS evaluate instead of an actual trial click.
                        Playwright's actionability sequence (even for trial=True, which never
                        clicks) includes an automatic 'scroll into view' step; since this runs
                        on a poll loop every ~0.4s for up to 45s across several selectors, that
                        was scrolling the page on nearly every tick while waiting for a result,
                        fighting any manual scrolling. This version never touches scroll
                        position: off-screen elements are treated as visible-if-styled-visible
                        rather than hidden (occlusion is only checked when the element already
                        happens to be within the current viewport, since elementFromPoint only
                        makes sense for on-screen coordinates)."""
                        try:
                            el = locator.element_handle(timeout=timeout_ms)
                            if el is None:
                                return False
                            return bool(el.evaluate("""
                                (node) => {
                                    const style = getComputedStyle(node);
                                    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) return false;
                                    const rect = node.getBoundingClientRect();
                                    if (rect.width === 0 || rect.height === 0) return false;
                                    const inViewport = rect.top < window.innerHeight && rect.bottom > 0 &&
                                                        rect.left < window.innerWidth && rect.right > 0;
                                    if (inViewport) {
                                        const cx = Math.min(Math.max(rect.left + rect.width / 2, 0), window.innerWidth - 1);
                                        const cy = Math.min(Math.max(rect.top + rect.height / 2, 0), window.innerHeight - 1);
                                        const topEl = document.elementFromPoint(cx, cy);
                                        if (!topEl || !(node === topEl || node.contains(topEl) || topEl.contains(node))) return false;
                                    }
                                    return true;
                                }
                            """))
                        except Exception:
                            return False

                    # If any of these "still working" indicators are visible, we know for certain
                    # it isn't done yet, regardless of what else matched.
                    STILL_PROCESSING_PATTERN = re.compile(r"processing|uploading|please wait|generating|loading\.\.\.|converting", re.IGNORECASE)

                    def is_still_processing():
                        try:
                            loc = tool_tab.locator("body").get_by_text(STILL_PROCESSING_PATTERN)
                            return loc.count() > 0 and is_really_visible(loc.first)
                        except Exception:
                            return False

                    poll_start = time.time()
                    poll_timeout = 45  # seconds hard cap
                    poll_interval = 0.4  # seconds between checks

                    def get_body_text_len():
                        try:
                            return len(tool_tab.locator("body").inner_text())
                        except Exception:
                            return -1

                    baseline_len = get_body_text_len()
                    last_len = baseline_len
                    stable_checks = 0
                    STABLE_CHECKS_REQUIRED = 3       # weaker markers/growth: ~1.2s of no change
                    HIGH_CONFIDENCE_STABLE_REQUIRED = 1  # real action button: ~0.4s is enough
                    GROWTH_THRESHOLD = 15             # ignore trivial DOM churn (spinners, counters)

                    while time.time() - poll_start < poll_timeout:
                        high_confidence_found = False
                        for sel in HIGH_CONFIDENCE_SELECTORS:
                            try:
                                el = tool_tab.locator(sel).first
                                if el.count() > 0 and is_really_visible(el):
                                    high_confidence_found = True
                                    break
                            except:
                                continue

                        marker_found = False
                        if not high_confidence_found:
                            for sel in result_selectors:
                                try:
                                    el = tool_tab.locator(sel).first
                                    if el.count() > 0 and is_really_visible(el):
                                        marker_found = True
                                        break
                                except:
                                    continue

                        cur_len = get_body_text_len()
                        text_grew = cur_len >= 0 and cur_len - baseline_len > GROWTH_THRESHOLD
                        if cur_len == last_len:
                            stable_checks += 1
                        else:
                            stable_checks = 0
                        last_len = cur_len

                        required_stable = HIGH_CONFIDENCE_STABLE_REQUIRED if high_confidence_found else STABLE_CHECKS_REQUIRED
                        still_processing = False if high_confidence_found else is_still_processing()
                        if (high_confidence_found or marker_found or text_grew) and stable_checks >= required_stable and not still_processing:
                            result_detected = True
                            break
                        time.sleep(poll_interval)

                    if not result_detected:
                        cprint(f"  │ {warn_label()} No result detected within {poll_timeout}s — dumping debug info", C.YELLOW)
                        dump_debug_info(tool_tab, "", tag=f"{domain}_{tool_name}_result")
                        # Last resort: give the page a brief settle window rather than blindly re-waiting the full cap
                        try:
                            tool_tab.wait_for_load_state("networkidle", timeout=3000)
                        except PlaywrightTimeoutError:
                            pass
                    cprint(f"  │ Conversion done ✓ ({time.time() - poll_start:.1f}s)", C.GREEN)
                    
                    # C. Check credits after action (wait longer for server-side deduction)
                    time.sleep(3)
                    account_tab.bring_to_front()
                    # Same fix as the pre-action check above: cache-bust the URL so this is a
                    # genuinely fresh navigation (not an identical repeat goto() the SPA can
                    # treat as a no-op) and won't get stuck on a drifted URL (e.g. /api/plans).
                    safe_goto(account_tab, cache_busted_url(account_url), label="account page")
                    safe_wait_dom(account_tab)
                    if pool_name:
                        wait_for_pool_text(account_tab, pool_name)
                    else:
                        wait_for_credit_text(account_tab)
                        click_credit_tab_if_present(account_tab)
                        wait_for_credit_text(account_tab)
                    balance_after = get_credit_balance(account_tab, pool_name=pool_name, pool_order=pool_order)
                    cprint(f"  │ Credits after:  {balance_after['text']}", C.DIM)
                    
                    # D. Compare
                    if balance_before["numbers"] != balance_after["numbers"] or balance_before["text"] != balance_after["text"]:
                        pool_detail = describe_pool_changes(balance_before, balance_after)
                        cprint(f"  └─ {pass_label()} {C.GREEN}Credit changed ({balance_before['text']} → {balance_after['text']}){C.RESET}", C.GREEN)
                        if pool_detail:
                            cprint(f"  │   ↳ Pool affected: {pool_detail}", C.DIM)
                        report.append({
                            "tool": tool_name,
                            "status": "PASS",
                            "before": balance_before["text"],
                            "after": balance_after["text"],
                            "detail": pool_detail
                        })
                    else:
                        cprint("  │ Credits did not change yet. Retrying balance check in 5s (lagging database check)...", C.YELLOW)
                        time.sleep(5)
                        account_tab.bring_to_front()
                        # Same fix again: fresh cache-busted navigation instead of an identical
                        # repeat goto() (which can leave stale numbers in place) or a reload()
                        # of a potentially drifted URL (e.g. /api/plans).
                        safe_goto(account_tab, cache_busted_url(account_url), label="account page")
                        safe_wait_dom(account_tab)
                        if pool_name:
                            wait_for_pool_text(account_tab, pool_name)
                        else:
                            wait_for_credit_text(account_tab)
                            click_credit_tab_if_present(account_tab)
                            wait_for_credit_text(account_tab)
                        balance_after = get_credit_balance(account_tab, pool_name=pool_name, pool_order=pool_order)
                        cprint(f"  │ Credits after retry:  {balance_after['text']}", C.DIM)
                        
                        if balance_before["numbers"] != balance_after["numbers"] or balance_before["text"] != balance_after["text"]:
                            pool_detail = describe_pool_changes(balance_before, balance_after)
                            detail_msg = "Resolved after delayed check" + (f" — pool affected: {pool_detail}" if pool_detail else "")
                            cprint(f"  └─ {pass_label()} {C.GREEN}Credit changed after retry ({balance_before['text']} → {balance_after['text']}){C.RESET}", C.GREEN)
                            if pool_detail:
                                cprint(f"  │   ↳ Pool affected: {pool_detail}", C.DIM)
                            report.append({
                                "tool": tool_name,
                                "status": "PASS",
                                "before": balance_before["text"],
                                "after": balance_after["text"],
                                "detail": detail_msg
                            })
                        else:
                            cprint(f"  └─ {fail_label()} {C.RED}Credit did NOT change ({balance_before['text']}){C.RESET}", C.RED)
                            report.append({
                                "tool": tool_name,
                                "status": "FAIL",
                                "before": balance_before["text"],
                                "after": balance_after["text"],
                                "detail": "No credit deduction detected"
                            })
                except Exception as ex:
                    cprint(f"  └─ {fail_label()} {C.RED}Automation error: {ex}{C.RESET}", C.RED)
                    report.append({
                        "tool": tool_name,
                        "status": "FAIL",
                        "before": balance_before["text"] if balance_before else "N/A",
                        "after": "N/A",
                        "detail": f"Automation error: {ex}"
                    })

            final_reports[domain] = report
            
            # Close tabs for clean site transition
            account_tab.close()
            tool_tab.close()

        # 6. Global Reporting Print
        print()
        cprint("═" * 80, C.CYAN, bold=True)
        cprint("  QA REPORT SUMMARY", C.CYAN, bold=True)
        cprint("═" * 80, C.CYAN, bold=True)
        
        # Staging-unavailable domains
        for s in unavailable_runs:
            cprint(f"  {C.YELLOW}⊘{C.RESET} {s['domain']} — staging-unavailable", C.YELLOW)
            
        # Executed sites reports
        for domain, report in final_reports.items():
            cprint(f"\n  {C.BOLD}{C.WHITE}Site: {domain}{C.RESET}", C.WHITE)
            passed = sum(1 for r in report if r["status"] == "PASS")
            failed = sum(1 for r in report if r["status"] == "FAIL")
            skipped = sum(1 for r in report if r["status"] == "SKIPPED")
            cprint(f"  {C.GREEN}{passed} passed{C.RESET}  {C.RED}{failed} failed{C.RESET}  {C.YELLOW}{skipped} skipped{C.RESET}", C.DIM)
            print()
            for r in report:
                name = r['tool'].ljust(30)
                if r["status"] == "SKIPPED":
                    print(f"    {skip_label()} {name}")
                elif r["status"] == "PASS":
                    print(f"    {pass_label()} {name} {C.DIM}({r['before']} → {r['after']}){C.RESET}")
                else:
                    print(f"    {fail_label()} {name} {C.DIM}({r['before']} → {r['after']}) — {r['detail']}{C.RESET}")
        print()
        cprint("═" * 80, C.CYAN, bold=True)

if __name__ == "__main__":
    run_cli_flow()



    # zekrex...