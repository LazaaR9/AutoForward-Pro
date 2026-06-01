-- ============================================================
-- Telegram Forwarding Bot — Supabase SQL Schema
-- Run this in your Supabase SQL Editor before starting the bot
-- ============================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ─────────────────────────────────────────────────────────────
-- Table: users
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    user_id         BIGINT PRIMARY KEY,
    username        TEXT,
    role            TEXT NOT NULL DEFAULT 'user'
                        CHECK (role IN ('superadmin', 'admin', 'user')),
    trial_start     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    subscription_end TIMESTAMPTZ
);

-- ─────────────────────────────────────────────────────────────
-- Table: source_channels
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS source_channels (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    channel_id      BIGINT NOT NULL,
    channel_username TEXT,
    added_by        BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    UNIQUE (added_by)  -- One source channel per admin
);

-- ─────────────────────────────────────────────────────────────
-- Table: target_channels
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS target_channels (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    channel_id      BIGINT NOT NULL,
    channel_username TEXT,
    admin_id        BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    UNIQUE (admin_id, channel_id)  -- No duplicate targets per admin
);

-- ─────────────────────────────────────────────────────────────
-- Table: filters
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS filters (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    admin_id        BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    find_text       TEXT NOT NULL,
    replace_text    TEXT NOT NULL DEFAULT ''
);

-- ─────────────────────────────────────────────────────────────
-- Table: scheduled_messages
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scheduled_messages (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    admin_id        BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    content         TEXT NOT NULL,
    post_time       TIME NOT NULL,
    frequency       TEXT NOT NULL DEFAULT 'once'
                        CHECK (frequency IN ('once', 'daily')),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- Table: transactions
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transactions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    admin_id        BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    amount          NUMERIC(10, 2) NOT NULL DEFAULT 0,
    duration_days   INT NOT NULL,
    promoted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL
);

-- ─────────────────────────────────────────────────────────────
-- Indexes for performance
-- ─────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_source_channels_added_by ON source_channels(added_by);
CREATE INDEX IF NOT EXISTS idx_target_channels_admin_id ON target_channels(admin_id);
CREATE INDEX IF NOT EXISTS idx_filters_admin_id         ON filters(admin_id);
CREATE INDEX IF NOT EXISTS idx_scheduled_admin_active   ON scheduled_messages(admin_id, is_active);
CREATE INDEX IF NOT EXISTS idx_users_role               ON users(role);

-- ─────────────────────────────────────────────────────────────
-- Table: bot_content (Dynamic texts)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bot_content (
    content_key TEXT PRIMARY KEY,
    content_value TEXT NOT NULL
);
