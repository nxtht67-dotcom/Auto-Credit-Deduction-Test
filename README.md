<div align="center">

# credit-deduction-qa

**A Playwright-based QA automation tool that verifies whether SaaS tools correctly deduct credits/quota from a user's account when a conversion or generation action is run.**

![Python](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)
![Playwright](https://img.shields.io/badge/playwright-automation-45ba4b?logo=playwright&logoColor=white)
![Status](https://img.shields.io/badge/status-active-brightgreen)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

</div>

---

Built to test a portfolio of **10 content/OCR/writing-tool websites** that share a common
underlying billing backend, across staging and live environments — driven by a real
logged-in browser session (via Chrome DevTools Protocol), not a mocked one.

## 📋 Table of contents

- [What it does](#-what-it-does)
- [Why this isn't a simple script](#-why-this-isnt-a-simple-click-a-button-and-check-a-number-script)
- [Architecture](#-architecture)
- [Supported sites](#-supported-sites)
- [Requirements](#-requirements)
- [Usage](#-usage)
- [Project structure](#-project-structure)
- [Known limitations](#-known-limitations)

---

## 🎯 What it does

For every tool on a site, the script:

| Step | Action |
|---|---|
| **1. Read** | Capture the account's current credit balance |
| **2. Run** | Upload a test file (or type sample text, where the tool has no file input) and click the tool's convert/submit button |
| **3. Wait** | Poll for a real completion signal — not a fixed timer |
| **4. Verify** | Re-read the balance and compare |

Result: **✅ PASS** if the balance changed, **❌ FAIL** if it didn't — and on sites that expose
an exact per-tool credit rate, it can additionally verify the *exact* amount deducted.

---

## 🧩 Why this isn't a simple "click a button and check a number" script

Every one of the 10 target sites renders its UI differently, and several behaviors that
looked reliable on one site turned out to be wrong on another. The design reflects real
failure modes hit while building it — not theoretical edge cases.

<details>
<summary><b>No single "credit balance" format</b></summary>
<br>

Some sites show a `used / total` ratio in one element; others (e.g. a "Plan Details" table)
split the same info across `Credits Allowed` / `Credits Used` columns; others show a plain
`"<N> used"` string next to an `Unlimited` allowance that must be ignored. The balance reader
tries several extraction strategies per site rather than assuming one layout.
</details>

<details>
<summary><b>Multiple independent credit pools</b></summary>
<br>

Some accounts show more than one pool at once — e.g. a "Monthly" plan and a "Premium" plan
simultaneously, or per-tool-category pools like "Plagiarism Checker" vs "AI Writing Tools".
The script isolates and checks the *specific* pool a given tool actually draws from, instead
of treating the whole page as one balance and getting confused when only one of several
numbers moves.
</details>

<details>
<summary><b>Convert buttons aren't consistently &lt;button&gt; elements</b></summary>
<br>

Some sites use a styled `<div>`/`<span>` with a click handler instead. Selector matching
falls back through button → div/span → link → `[role=button]`, guarded so it never
blind-clicks an ambiguous match (e.g. a site-wide nav link that happens to also say "Convert").
</details>

<details>
<summary><b>"Reload the balance" isn't always a real reload</b></summary>
<br>

Navigating to the exact same account-page URL twice in a row can leave a single-page app
showing stale, cached numbers until a real (cache-busted) navigation is forced.
</details>

<details>
<summary><b>Some tools take typed/pasted text, not a file upload</b></summary>
<br>

A few tools have no upload control at all — sample text is typed into the relevant
textarea/contenteditable element instead, sometimes with no submit button at all
(auto-analyzes on input).
</details>

Each of these is handled through a small number of **general-purpose, config-driven
mechanisms** rather than one-off hacks per site, so adding a new site or tool is mostly a
matter of adding data, not new control flow.

---

## 🏗️ Architecture

| Concern | Mechanism |
|---|---|
| Finding a submit/convert control | `find_clickable()` — tries a prioritized candidate-selector list per (site, tool), skipping any match that's ambiguous (matches >1 element) rather than guessing |
| Reading the credit balance | `get_credit_balance()` — generic whole-page extraction by default; `get_credit_balance_for_pool()` for sites with multiple/separately-labeled pools |
| Per-tool selector/behavior overrides | Small lookup dicts keyed by `(domain, tool_name)` — `SUBMIT_BTN_OVERRIDES`, `RESULT_INDICATOR_OVERRIDES`, `TEXT_INPUT_OVERRIDES` — checked ahead of generic guessing, with a `"default"` fallback per site |
| Detecting a finished conversion | Two-tier selector list: high-confidence markers (a real "Start Over"/download control, which can only exist once a result exists) checked first, weaker guessed markers as a fallback |
| Stale/cached account pages | `cache_busted_url()` appends a changing query param so every balance check is a genuinely fresh navigation |
| Adding a new tool | Interactive wizard (`prompt_new_tool_wizard()`) — paste the tool's selectors once, it's saved to `site_mappings.json` and already there next run |
| Debuggability | `dump_debug_info()` saves a screenshot + relevant HTML snippet whenever a selector fails to match, so a fix can be written from real markup instead of guessing blind |

---

## 🌐 Supported sites

<table>
<tr>
<td>editpad.org</td><td>grammarcheck.ai</td><td>imagetotext.cc</td><td>imagetotext.info</td><td>imagetotext.io</td>
</tr>
<tr>
<td>jpgtotext.com</td><td>ocr.best</td><td>paraphrasing.io</td><td>prepostseo.com</td><td>summarizer.org</td>
</tr>
</table>

Per-site tool lists, resolved tool URLs, and per-tool credit rates are seeded from
`credits_overview_data.txt` / `resolved_tool_urls.json` and cached to `site_mappings.json`
on first run.

---

## ⚙️ Requirements

- Python 3.12
- Playwright for Python
  ```bash
  pip install playwright
  playwright install
  ```
- A Chromium-based browser (Brave/Chrome/Edge) — the script launches it with
  `--remote-debugging-port=9222` if it isn't already running that way, so it can attach to
  your real logged-in session rather than a blank automated profile

---

## ▶️ Usage

```bash
python credits_deduction.py
```

You'll be prompted to pick an environment (Staging/Live), then a site (or "Run All Sites").
The script opens an account tab and a tool tab, and walks through each of that site's tools —
with the option to add a new tool interactively before the run starts.

---

## 📁 Project structure

```
credits_deduction.py        # main script
site_mappings.json           # cached per-site tool list + resolved URLs (auto-generated/merged)
credits_overview_data.txt    # seed: per-site tool → credit rate
resolved_tool_urls.json      # seed: per-site tool → confirmed URL path
Test Data/                   # sample upload files, organized by type (image/, pdf/, word/, txt/, ...)
debug_dumps/                 # auto-saved screenshots + HTML snippets on selector failures
```

---

## ⚠️ Known limitations

- Selector overrides for some tools (e.g. grammarcheck.ai's text-input box) are
  best-effort candidate lists rather than confirmed-exact selectors, and may need
  tightening from a `debug_dumps/` dump on first run against a new site.
- Exact-amount credit verification (vs. deduction-only) is currently only implemented
  for sites that expose a documented per-tool credit rate.

---

<div align="center">

Personal QA tooling built and iterated against real staging/live sites as part of a
broader automation portfolio. Not affiliated with or endorsed by the tested sites.

</div>
