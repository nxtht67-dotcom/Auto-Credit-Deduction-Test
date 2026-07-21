<div align="center">

# 🚀 Credit Deduction QA Automation

### Checks whether each tool on our sites correctly deducts credits when used — so you don't have to test it by hand.

![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-Automation-2EAD33?logo=playwright&logoColor=white)
![Status](https://img.shields.io/badge/Status-Active-brightgreen)

</div>

---

# 📚 Contents

- [First-time setup](#-first-time-setup-do-this-once)
- [Running a test](#-running-a-test-do-this-every-time)
- [Reading the results](#-reading-the-results)
- [Adding a new tool](#-adding-a-new-tool)
- [Troubleshooting](#-troubleshooting)
- [Rules of thumb](#-rules-of-thumb)

---

# 🧰 First-time setup (do this once)

## 1. Install Python 3.12+

Skip this if you already have it. Check with:
```bash
python --version
```

## 2. Install Playwright

```bash
pip install playwright
playwright install
```

## 3. Clone/download this repo

Everything you need — test files, site configs, credit rate tables — is already inside the
project folder. Nothing to move to your Desktop, nothing to point at a custom path.

That's it. No browser setup, no config file editing — the script handles all of that itself
when you run it.

---

# ▶️ Running a test (do this every time)

## Step 1 — Run the script

```bash
python credits_deduction.py
```

## Step 2 — Answer the prompts

```
Select environment to test:
  1. Staging
  2. Live
```
Pick **Staging** unless you're specifically told to test Live.

```
Select a website to test:
  1. editpad.org
  2. grammarcheck.ai
  ...
  11. Run All Sites
  12. Enter a custom site URL
```
Pick the site you're testing, or `11` to run everything.

```
Detected default browser: Chrome
Use Chrome? (Press Enter, or type Chrome/Brave/Edge/Firefox):
```
Press **Enter** to accept the detected browser, or type a different one if you'd rather use
Brave/Edge/Firefox.

## Step 3 — If your browser is already open

You'll see:
```
RESTART REQUIRED: CHROME IS RUNNING
```
**Save any unsaved work in your browser first.** Then just press Enter — the script closes
your browser and reopens it itself with the right settings. Your tabs come back
automatically if you have session-restore turned on. You don't need to do anything else.

## Step 4 — First time only: log in

A browser window opens. If you're not already logged into the Premium test account on the
site being tested, **log in now**, then leave the window open. The script picks up your
session from there. On future runs you won't need to log in again (same browser profile is
reused).

## Step 5 — Let it run

```
Tools to test:
    1. Plagiarism Checker: https://staging.editpad.org/tool/plagiarism-checker
    2. Paraphrasing Tool: https://staging.editpad.org/tool/paraphrasing-tool
    ...

Add a new tool for editpad.org? (y/n) [n]:
```
Press **Enter**/type `n` unless you're specifically adding a new tool (see [below](#-adding-a-new-tool)).

```
Press Enter to start running...
```
Press Enter, then **leave the browser alone** — don't click anything, switch tabs, or close
the window while it's running. It'll go tool by tool on its own.

---

# 📊 Reading the results

Each tool prints a block like this:

```
┌─ [1/9] Plagiarism Checker
  │ Credits before: 25000 Credits Used 42
  │ Navigating → https://staging.editpad.org/tool/plagiarism-checker
  │ Uploading: credits test.txt
  │ Conversion done ✓ (5.5s)
  │ Credits after:  25000 Credits Used 45
  └─ ✓ PASS Credit changed (42 used → 45 used)
```

- **✓ PASS** — credits changed after using the tool. Working as expected.
- **✗ FAIL** — credits did *not* change. This is a real finding — flag it, don't ignore it.
- **⚠ WARN** — something looked off (slow page load, retry needed) but the script kept going.
  Not necessarily a bug, just worth a glance if the final result also looks wrong.

At the end of a full run, you'll get a summary per site — how many tools passed/failed, and
a list of exactly which ones failed so you don't have to scroll back through the whole log.

If something fails unexpectedly (not a real credit bug, but the script itself couldn't find
a button/input), check the `debug_dumps/` folder — it auto-saves a screenshot + the page's
HTML at the moment it got stuck, which is the fastest way to see what actually went wrong.

---

# ➕ Adding a new tool

You don't need to touch any code for this — the script asks you interactively.

When it prints the tool list, right before "Press Enter to start running," it'll ask:
```
Add a new tool for editpad.org? (y/n) [n]:
```
Type `y` and it walks you through:

1. **Tool name**
2. **Tool URL**
3. **Input method** — does it take a file upload, or do you paste/type text into a box?
4. **Submit button** — open the tool in your browser, right-click the convert/submit
   button → **Inspect**, and paste the element it shows you
5. **Result indicator** — same idea, but for something that only appears *after* a result is
   generated (a download icon, a "Start Over" button, etc.) — this is how the script knows
   the conversion actually finished

It's saved immediately — runs in that same session, and it's already there next time anyone
runs the script. You can add more than one tool in a row; it'll keep asking.

---

# 🛠 Troubleshooting

| Problem | What to do |
|---|---|
| Browser won't connect / times out waiting for debugging port | The script auto-retries once (closes and relaunches). If it still fails, see **"Debugging port keeps timing out"** below — it's usually not random |
| Edge specifically keeps failing even after retrying | Turn off Edge's **"Continue running background extensions and apps when Microsoft Edge is closed"** in `edge://settings/system`, then re-run. Edge can silently respawn itself in the background right after being closed, which steals the next launch before debugging gets enabled on it |
| "Couldn't create the data directory" | Shouldn't happen — let the person who maintains this script know if it does |
| A tool shows FAIL that you think should be PASS | Double-check you're logged into the **Premium** test account, not a free/limited one |
| Script can't find a button/input on a tool's page | Check `debug_dumps/` for the screenshot+HTML it saved, then flag it — the selector likely needs updating |
| Port 9222 already used by something else | Run `set QA_DEBUG_PORT=9333` before running the script |
| You closed the browser by accident mid-run | Just re-run the script from scratch |

## Debugging port keeps timing out (not just a one-off)

If the port timeout happens **repeatedly** on a specific machine, even after the script's
automatic retry, the real cause is usually **not** a firewall, antivirus, or a stray
lingering process — it's the browser's own **single-instance lock**.

**What's actually happening:** the script launches Chrome/Edge/Brave pointed at your *real*
profile (on purpose — so it can reuse your already-logged-in session). If that browser's
background process re-locks the profile between the kill and the relaunch, the new
debug-enabled process gets silently handed off to the existing (non-debug) instance instead
of starting fresh. A window may open, but debugging was never enabled on it — the port never
binds, and the script just sees a timeout with no clearer error.

**If closing all windows and retrying doesn't fix it on a specific machine:** the more
bulletproof (but different-tradeoff) fix is to have that person's copy of the script use a
**separate, isolated browser profile** just for automation, instead of their real one. This
sidesteps the single-instance lock entirely — but means a one-time separate login in that
isolated profile, since it won't have their everyday session already in it. If this is
needed on a colleague's machine, flag it to whoever maintains the script rather than
changing this yourself — it's a deliberate before/after tradeoff, not a simple setting.

---

# ✅ Rules of thumb

- Always pick **Staging** unless told otherwise.
- Don't touch the browser once a run has started.
- A **FAIL** is a real result to report, not an error to dismiss.
- Prefer the **in-script wizard** over hand-editing JSON files when adding a tool.
- If in doubt, check `debug_dumps/` before asking — it usually shows exactly what the script saw.
