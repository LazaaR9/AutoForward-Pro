# Telegram Channel Forwarding Bot

A production-ready Telegram bot with role-based access control, channel message forwarding, text filters, APScheduler-based scheduling, and a subscription/trial system — built with **python-telegram-bot v20+** and **Supabase**.

---

## Features

| Feature | Details |
|---|---|
| 🔁 Real-time forwarding | All message types: text, photo, video, audio, document, sticker, animation, voice, poll, etc. |
| 🔍 Text filters | Admin-defined find/replace rules applied before forwarding (text + captions) |
| ⏰ Scheduling | One-time or daily recurring messages sent to target channels |
| 👥 Role system | Super Admin → Admin → User with trial and subscription management |
| 💰 Income tracking | Manually log payments; view stats via `/stats` |
| ⚡ Auto-demotion | Hourly check demotes expired admins automatically |

---

## Project Structure

```
auto-forword-bot/
├── bot/
│   ├── __init__.py
│   ├── main.py              # Entry point
│   ├── config.py            # Env var loader
│   ├── db/
│   │   ├── supabase_client.py
│   │   ├── users.py
│   │   ├── channels.py
│   │   ├── filters.py
│   │   ├── schedules.py
│   │   └── transactions.py
│   ├── utils/
│   │   ├── roles.py         # Role guard decorators
│   │   ├── filters.py       # Filter application logic
│   │   └── scheduler.py     # APScheduler wrapper
│   └── handlers/
│       ├── user.py          # /start, /plan
│       ├── admin.py         # /addsource, /addtarget, /filter, /schedule, etc.
│       ├── superadmin.py    # /stats, /addadmin, /removeadmin, etc.
│       └── forwarding.py    # Core forwarding engine
├── supabase_schema.sql      # Run this in Supabase SQL editor first
├── requirements.txt
├── .env.example
└── README.md
```

---

## Setup

### 1. Clone and install dependencies

```bash
cd auto-forword-bot
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
BOT_TOKEN=your_bot_token_here         # From @BotFather
SUPABASE_URL=https://xxx.supabase.co  # Your Supabase project URL
SUPABASE_KEY=your_supabase_key_here   # anon or service_role key
SUPER_ADMIN_ID=123456789              # Your Telegram user ID (from @userinfobot)
```

### 3. Set up Supabase database

1. Open your [Supabase project](https://supabase.com) → SQL Editor
2. Copy and run the entire contents of `supabase_schema.sql`

### 4. Add the bot to your channels

For **source channels**: Add the bot as an **Admin** (needs to read all messages).
For **target channels**: Add the bot as an **Admin** (needs to post messages).

### 5. Run the bot

```bash
python3 -m bot.main
```

---

## Commands Reference

### Super Admin (`SUPER_ADMIN_ID`)

| Command | Description |
|---|---|
| `/stats` | Total admins, channels, income |
| `/alladmins` | List all admins with expiry |
| `/allchannels` | List all source/target channels |
| `/addadmin` | Promote a user (asks: user ID → duration) |
| `/removeadmin` | Demote an admin immediately |
| `/addincome` | Log a manual payment |

### Admin

| Command | Description |
|---|---|
| `/addsource` | Set source channel (link, username, or forward) |
| `/addtarget` | Add a target channel |
| `/filter` | Add a text replacement filter |
| `/myfilters` | View and remove filters |
| `/schedule` | Schedule a message (content → time → once/daily) |
| `/removeschedule` | List and remove scheduled messages |
| `/mystatus` | View subscription expiry and channel info |

### User

| Command | Description |
|---|---|
| `/start` | Register and show trial/subscription status |
| `/plan` | Show current plan details |

---

## Notes

- **Private source channels**: The bot must be added as an admin to receive posts. Public channels work via username.
- **Time zone**: All scheduled message times are in **UTC**.
- **Filter order**: Filters are applied in the order they were added.
- **One source per admin**: Each admin has exactly one source channel (replaceable). Multiple target channels are supported.
- **Auto-demotion**: The bot checks for expired subscriptions every hour and automatically demotes admins.
