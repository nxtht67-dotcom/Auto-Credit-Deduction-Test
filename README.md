# Credit Deduction QA Automation

Automated Playwright framework for verifying whether premium tools correctly deduct user credits after performing an action (OCR, PDF conversion, AI tools, etc.).

---

# Features

- Automatically checks credit balance before and after each test.
- Supports multiple websites from a single configuration.
- Works with both Live and Staging environments.
- Supports:
  - Image uploads
  - PDF uploads
  - Text input tools
- Detects successful conversions before checking credits.
- Validates exact deduction amount where supported.
- Produces clear PASS / FAIL output.

---

# Project Structure

```
.
├── credits_deduction.py          # Main automation script
├── credits_deduction_save.py     # Backup/alternate version
├── site_mappings.json            # Tool URLs and site information
├── resolved_tool_urls.json       # Complete tool configuration
├── credits_overview_data.txt     # Expected credit costs
├── README.md
```

---

# Requirements

- Python 3.11+
- Google Chrome
- Playwright

Install dependencies:

```bash
pip install playwright
playwright install
```

---

# Before Running

## 1. Start Chrome with Remote Debugging

Close every Chrome window first.

Windows:

```cmd
chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\ChromeAutomation"
```

or

```cmd
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\ChromeAutomation"
```

---

## 2. Login

Using the opened Chrome window:

- Login to the website(s)
- Make sure the account has premium access
- Leave Chrome open

The script connects to this browser instead of opening a new one.

---

## 3. Test Data

Update the test data folder inside the script if necessary.

Example:

```python
TEST_DATA_DIR = r"C:\Users\YourName\Desktop\Test Data"
```

The folder should contain files required by different tools such as:

- Images
- PDFs
- Word documents
- PPT files

---

# Running

Run:

```bash
python credits_deduction.py
```

The script will:

1. Connect to Chrome
2. Open each configured tool
3. Read current credits
4. Perform the conversion
5. Wait until processing finishes
6. Read credits again
7. Report PASS or FAIL

---

# Configuration Files

## resolved_tool_urls.json

Contains:

- Tool URL
- Expected credit cost
- File type
- Selectors
- Premium status
- Pre-actions

Add new tools here.

---

## site_mappings.json

Contains:

- Website URLs
- Pricing page
- Tool paths
- Site-specific notes

---

## credits_overview_data.txt

Reference file containing expected credit usage collected from pricing pages.

Update this whenever pricing changes.

---

# Adding a New Tool

1. Add the tool to `resolved_tool_urls.json`.
2. Add its URL to `site_mappings.json`.
3. Update `credits_overview_data.txt` with expected credits.
4. Run the script.

No code changes are required unless the tool has unique behavior.

---

# Console Output

Example:

```
Checking Image to Text...

Credits Before: 150
Credits After : 149

✓ PASS
```

or

```
Checking PDF to Excel...

Credits Before: 100
Credits After : 100

✗ FAIL
```

---

# Troubleshooting

## Browser won't connect

- Close all Chrome windows.
- Restart Chrome using the remote debugging command.
- Login again.

---

## Tool not detected

Usually caused by:

- Changed CSS selectors
- UI redesign
- New button names

Update the selectors inside:

```
resolved_tool_urls.json
```

---

## Credits don't update

Possible reasons:

- Site caching
- Account page changed
- Premium subscription expired
- Tool no longer deducts credits

Verify manually before updating the configuration.

---

# Notes

- Keep the Chrome window open while the script is running.
- Do not interact with the browser during execution.
- If pricing changes, update the configuration files before running new tests.
- The framework is configuration-driven, so most new tools can be added without modifying the Python script.

---

# Maintainers

QA Automation Team
