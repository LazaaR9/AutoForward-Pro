-- Add is_working column to users table to support /work and /stop commands
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_working BOOLEAN DEFAULT TRUE;
