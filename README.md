<div align="center">

# 🚀 Credit Deduction QA Automation

### Automated Playwright framework for validating premium credit deduction across multiple SaaS tools.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-Automation-2EAD33?logo=playwright&logoColor=white)
![Status](https://img.shields.io/badge/Status-Active-brightgreen)
![QA](https://img.shields.io/badge/Purpose-QA%20Automation-blue)

</div>

---

# 📚 Table of Contents

- [Overview](#-overview)
- [Features](#-features)
- [Project Structure](#-project-structure)
- [Requirements](#-requirements)
- [Quick Start](#-quick-start)
- [How It Works](#-how-it-works)
- [Configuration Files](#-configuration-files)
- [Adding a New Tool](#-adding-a-new-tool)
- [Example Output](#-example-output)
- [Troubleshooting](#-troubleshooting)
- [Best Practices](#-best-practices)

---

# 🎯 Overview

This project automates **credit deduction verification** for premium SaaS tools.

Instead of manually checking credits after every conversion, the script automatically:

- Reads the user's current credits
- Performs the conversion
- Waits until processing completes
- Reads credits again
- Reports whether the expected deduction occurred

Supported tools include:

- 🖼️ Image to Text
- 📄 PDF Tools
- 🌍 OCR Tools
- 🤖 AI Writing Tools
- ✨ Paraphrasing
- 📝 Grammar Tools
- and many more...

---

# ✨ Features

| Feature | Description |
|----------|-------------|
| ✅ Automatic Credit Verification | Checks credits before and after every test |
| 🌐 Multi-site Support | Test multiple websites from one framework |
| ⚙️ Configuration Driven | Add tools without changing Python code |
| 📂 Multiple File Types | Images, PDFs, DOCX, PPTX, Text |
| 📊 Quantity Validation | Verifies exact deduction where supported |
| 🎨 Colorized Output | Easy-to-read PASS / FAIL console logs |
| 🔍 Smart Waiting | Waits for real completion instead of fixed delays |

---

# 📁 Project Structure

```text
.
├── credits_deduction.py          ⭐ Main Script
├── credits_deduction_save.py     Backup Version
├── resolved_tool_urls.json       Tool Configuration
├── site_mappings.json            Website Mapping
├── credits_overview_data.txt     Expected Credit Costs
├── debug_dumps/                  Screenshots & HTML on failures
└── README.md
```

---

# ⚙️ Requirements

## Software

- Python **3.11+**
- Google Chrome
- Playwright

---

## Install Dependencies

```bash
pip install playwright
```

```bash
playwright install
```

---

# 🚀 Quick Start

## Step 1 — Launch Chrome

Close every Chrome window first.

```cmd
chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\ChromeAutomation"
```

---

## Step 2 — Login

Using the opened browser:

✅ Login to Premium Account

✅ Keep Chrome open

❌ Do NOT close it

---

## Step 3 — Configure Test Data

Inside **credits_deduction.py**

```python
TEST_DATA_DIR = r"C:\Users\<YourName>\Desktop\Test Data"
```

Example folder:

```text
Test Data
│
├── sample.jpg
├── sample.pdf
├── sample.docx
├── sample.pptx
└── sample.txt
```

---

## Step 4 — Run

```bash
python credits_deduction.py
```

---

# 🔄 How It Works

```text
┌────────────────────┐
│ Read Current Credit│
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐
│ Open Target Tool   │
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐
│ Upload/Test Input  │
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐
│ Run Conversion     │
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐
│ Wait for Result    │
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐
│ Read Credits Again │
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐
│ Compare Values     │
└─────────┬──────────┘
          │
      PASS / FAIL
```

---

# 🗂 Configuration Files

## 📄 resolved_tool_urls.json

Contains:

- Tool URLs
- Expected credit cost
- File type
- Selectors
- Premium flag
- Pre-actions
- Skip rules

---

## 🌐 site_mappings.json

Contains:

- Base URLs
- Pricing Pages
- Tool Paths
- Website Notes

---

## 💳 credits_overview_data.txt

Reference list of expected premium credit costs.

Update whenever pricing changes.

---

# ➕ Adding a New Tool

### 1️⃣ Add Tool Information

Update:

```
resolved_tool_urls.json
```

---

### 2️⃣ Add Site Mapping

Update:

```
site_mappings.json
```

---

### 3️⃣ Update Credit Cost

Update:

```
credits_overview_data.txt
```

---

### 4️⃣ Run the Script

```bash
python credits_deduction.py
```

> 💡 Most tools can be added without changing any Python code.

---

# 📈 Example Output

## ✅ PASS

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Image to Text

Credits Before : 150
Credits After  : 149

✓ PASS

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## ❌ FAIL

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PDF to Excel

Credits Before : 100
Credits After  : 100

✗ FAIL

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

# 🛠 Troubleshooting

| Problem | Solution |
|----------|----------|
| Browser won't connect | Restart Chrome with Remote Debugging |
| Login expired | Login again using the debug browser |
| Tool not detected | Update selectors in JSON |
| Credits don't change | Verify Premium account |
| Wrong deduction | Update expected credit values |
| Browser closes | Keep Chrome open while testing |

---

# 💡 Best Practices

✅ Keep Chrome open while testing

✅ Do not interact with the browser during execution

✅ Update pricing whenever plans change

✅ Prefer JSON configuration over modifying Python

✅ Keep test files inside the Test Data folder

✅ Commit configuration updates together with pricing changes

---

# 🎉 That's It!

Run one command:

```bash
python credits_deduction.py
```

The framework will automatically verify whether each tool deducts credits correctly and clearly report the result.

---

<div align="center">

### Happy Testing! 🚀

Made for the QA Team ❤️

</div>
