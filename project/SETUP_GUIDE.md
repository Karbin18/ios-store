# ZSign Telegram Bot — Complete Setup Guide

## What You'll Have When Done

```
Telegram → Bot → zsign signs IPA → Netlify serves files → Safari installs app
```

---

## 1. Server Requirements

A Linux VPS (Ubuntu 20.04+) with:
- Python 3.10+
- zsign installed
- Outbound internet access

---

## 2. Install zsign

```bash
# Ubuntu / Debian
sudo apt update
sudo apt install -y git cmake make libssl-dev libminizip-dev

git clone https://github.com/zhlynn/zsign.git
cd zsign
cmake .
make -j$(nproc)
sudo cp zsign /usr/local/bin/
zsign --version   # confirm it works
```

---

## 3. Python Dependencies

```bash
pip install python-telegram-bot aiosqlite
```

---

## 4. Project Structure

```
project/
├── bot.py
├── config.json          ← created automatically on first run
├── certs/
│   ├── cert.p12
│   └── profile.mobileprovision
├── data/                ← temp files + SQLite DB (auto-created)
│   └── temp/
└── public/              ← Netlify serves this folder
    ├── apps/            ← signed IPAs
    ├── plist/           ← OTA manifests
    └── install/         ← HTML install pages
```

---

## 5. Create Your Telegram Bot

1. Open Telegram → search **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy the token (format: `1234567890:ABCdef...`)

---

## 6. Get Your Certificates

You need an Apple Developer or Enterprise account:

| File | Where to get |
|------|-------------|
| `cert.p12` | Xcode → Keychain Access → export your iOS Distribution certificate |
| `profile.mobileprovision` | developer.apple.com → Profiles → download your distribution profile |

Place both files in the `certs/` folder.

---

## 7. Configure Netlify

### Option A: Netlify CLI (recommended)

```bash
npm install -g netlify-cli
cd project/
netlify init          # link to your Netlify account
netlify deploy --dir=public --prod
```

Note the **Site URL** (e.g., `https://my-app-123.netlify.app`).

### Option B: Netlify Drop

1. Go to [app.netlify.com/drop](https://app.netlify.com/drop)
2. Drag the `public/` folder onto the page
3. You'll get a URL like `https://random-name.netlify.app`

### Re-deploy after signing

Every time a new IPA is signed, the `public/` folder gets new files.
Run `netlify deploy --dir=public --prod` to push them live.

**Or:** Use Netlify's continuous deployment from a Git repo —
push `public/` to GitHub and Netlify auto-deploys on every push.

---

## 8. Edit config.json

On first run, `bot.py` auto-creates `config.json`. Fill it in:

```json
{
    "token": "1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ",
    "p12": "certs/cert.p12",
    "password": "your_p12_export_password",
    "mobileprovision": "certs/profile.mobileprovision",
    "domain": "https://my-app-123.netlify.app",
    "max_size_mb": 500,
    "rate_limit_per_hour": 10,
    "cleanup_hours": 48,
    "admin_ids": [123456789],
    "log_level": "INFO"
}
```

- `domain` — your Netlify URL, no trailing slash
- `admin_ids` — your Telegram user ID (get it from @userinfobot)

---

## 9. Run the Bot

```bash
python bot.py
```

To keep it running permanently:

```bash
# systemd service
sudo nano /etc/systemd/system/zsign-bot.service
```

```ini
[Unit]
Description=ZSign Telegram Bot
After=network.target

[Service]
WorkingDirectory=/path/to/project
ExecStart=/usr/bin/python3 /path/to/project/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now zsign-bot
sudo journalctl -fu zsign-bot   # view logs
```

---

## 10. Full Signing Flow (How It Works Internally)

```
1. User sends .ipa to Telegram bot
2. Bot checks rate limit (SQLite)
3. Bot downloads IPA to data/temp/<job_id>.ipa
4. Bot reads Info.plist → gets app name, bundle ID, version
5. zsign signs the IPA → public/apps/<job_id>.ipa
6. Bot writes OTA manifest → public/plist/<job_id>.plist
7. Bot writes install HTML page → public/install/<job_id>.html
8. Bot pushes public/ to Netlify (or Git auto-deploy)
9. Bot sends Telegram message with Install button
10. User opens link in Safari → taps Install → iOS installs the app
```

---

## 11. Netlify Automatic Deploy (Best Setup)

```bash
# One-time setup
git init
echo "data/" >> .gitignore
git remote add origin https://github.com/youruser/zsign-public.git

# After every signing job, run:
git add public/
git commit -m "new signed app"
git push
```

Netlify detects the push and deploys within seconds.
The install link is live before the user even taps it.

---

## 12. Troubleshooting

| Problem | Fix |
|---------|-----|
| zsign not found | Run `which zsign` — make sure it's in PATH |
| Signing fails: no identity | Verify your .p12 password in config.json |
| Install link doesn't work | Domain must be **HTTPS**. Netlify provides this automatically |
| "Untrusted Developer" error | User must go to Settings → VPN & Device Management → trust the cert |
| Safari shows blank page | Make sure Netlify has deployed the latest `public/` folder |

---

## Summary Checklist

- [ ] zsign installed and working (`zsign --version`)
- [ ] Python deps installed (`pip install python-telegram-bot aiosqlite`)
- [ ] `certs/cert.p12` and `certs/profile.mobileprovision` present
- [ ] `config.json` filled in with real token + domain
- [ ] Netlify site created and `public/` folder linked
- [ ] Bot running (`python bot.py` or systemd)
