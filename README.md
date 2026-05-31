# Think Tank Monitor 🏛️

Daily automated monitoring of 10 top US think tanks — new research & reports pushed to QQ email via GitHub Actions.

## Think Tanks Tracked

| Focus Area | Think Tanks |
|---|---|
| International Security & Strategy | CSIS, RAND, CNAS, Hudson Institute, Atlantic Council, Stimson Center |
| Great Power Strategy & Geopolitics | CFR, Carnegie Endowment |
| International Political Economy | Brookings, AEI Foreign & Defense Policy |

## How It Works

- GitHub Actions runs daily at 9:00 AM Beijing time (UTC+8)
- Fetches RSS/Atom feeds from all 10 think tanks
- Detects new items via SHA256 dedup
- Sends HTML email to QQ mailbox
- State file persists between runs via GitHub Actions cache

## Setup

1. Fork/clone this repo
2. Go to Settings → Secrets and variables → Actions
3. Add these secrets:
   - `QQ_SMTP_SENDER`: your QQ email (e.g., 1821339784@qq.com)
   - `QQ_SMTP_AUTH_CODE`: QQ Mail SMTP authorization code
4. Enable Actions (if disabled by default)
5. Trigger manually once to test, or wait for the daily schedule

## Manual Trigger

Go to Actions → "Think Tank Daily Monitor" → Run workflow

## Requirements

- Python 3.12 (GitHub Actions default)
- Zero external dependencies (stdlib only)
- QQ Mail SMTP enabled with auth code
