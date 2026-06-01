-- ============================================================
-- Migration: Razorpay Webhook Support
-- Run this in your Supabase SQL Editor AFTER migration_payment_system.sql
-- ============================================================

-- ─────────────────────────────────────────────────────────────
-- 1. Add razorpay_payment_id column to subscriptions table
--    (stores the pay_XXXX ID from Razorpay, distinct from link ID)
-- ─────────────────────────────────────────────────────────────

ALTER TABLE subscriptions
    ADD COLUMN IF NOT EXISTS razorpay_payment_id TEXT;

-- ─────────────────────────────────────────────────────────────
-- 2. Create processed_webhooks table (deduplication guard)
--    payment_id is UNIQUE — DB enforces idempotency at the row level.
--    Even if two webhook deliveries arrive simultaneously, only one
--    INSERT will succeed due to the unique constraint.
-- ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS processed_webhooks (
    id           SERIAL      PRIMARY KEY,
    payment_id   TEXT        NOT NULL UNIQUE,   -- Razorpay pay_XXXX ID
    telegram_id  BIGINT      NOT NULL,
    plan_key     VARCHAR(20) NOT NULL,
    amount_inr   DECIMAL(10, 2) NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- 3. Indexes for processed_webhooks
-- ─────────────────────────────────────────────────────────────

-- Primary lookup: by payment_id (deduplication check)
CREATE UNIQUE INDEX IF NOT EXISTS idx_processed_webhooks_payment_id
    ON processed_webhooks(payment_id);

-- Secondary lookup: by telegram_id (for admin queries)
CREATE INDEX IF NOT EXISTS idx_processed_webhooks_telegram_id
    ON processed_webhooks(telegram_id);

-- ─────────────────────────────────────────────────────────────
-- 4. Index razorpay_payment_id on subscriptions
-- ─────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_subscriptions_razorpay_payment_id
    ON subscriptions(razorpay_payment_id);
