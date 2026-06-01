-- ============================================================
-- Migration: Payment & Subscription System
-- Run this in your Supabase SQL Editor
-- ============================================================

-- ─────────────────────────────────────────────────────────────
-- 1. Add new columns to the `users` table
-- ─────────────────────────────────────────────────────────────

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS subscription_plan  VARCHAR(20),
    ADD COLUMN IF NOT EXISTS subscription_start TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS payment_method     VARCHAR(20),
    ADD COLUMN IF NOT EXISTS payment_amount     DECIMAL(10, 2),
    ADD COLUMN IF NOT EXISTS payment_status     VARCHAR(20);

-- ─────────────────────────────────────────────────────────────
-- 2. Create the subscriptions table (payment audit trail)
-- ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS subscriptions (
    id             SERIAL PRIMARY KEY,
    telegram_id    BIGINT       NOT NULL,
    plan_name      VARCHAR(20)  NOT NULL,
    amount         DECIMAL(10, 2) NOT NULL,
    payment_method VARCHAR(20)  NOT NULL,
    payment_status VARCHAR(20)  NOT NULL DEFAULT 'pending',
    razorpay_order_id TEXT,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    activated_at   TIMESTAMPTZ,
    expires_at     TIMESTAMPTZ
);

-- ─────────────────────────────────────────────────────────────
-- 3. Indexes for the subscriptions table
-- ─────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_subscriptions_telegram_id
    ON subscriptions(telegram_id);

CREATE INDEX IF NOT EXISTS idx_subscriptions_status
    ON subscriptions(payment_status);

CREATE INDEX IF NOT EXISTS idx_subscriptions_expires_at
    ON subscriptions(expires_at);

-- ─────────────────────────────────────────────────────────────
-- 4. Index for subscription_plan on users (for reporting)
-- ─────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_users_subscription_plan
    ON users(subscription_plan);
