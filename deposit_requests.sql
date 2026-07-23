-- Chỉ dùng nếu ông KHÔNG để SQLAlchemy tự tạo bảng.
-- Cần chỉnh kiểu user_id cho khớp bảng users hiện tại.

CREATE TABLE IF NOT EXISTS deposit_requests (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code VARCHAR(30) NOT NULL UNIQUE,
    amount NUMERIC(15,0) NOT NULL CHECK (amount > 0),
    bank_name VARCHAR(100) NOT NULL,
    account_number VARCHAR(100) NOT NULL,
    account_name VARCHAR(150) NOT NULL,
    transfer_content VARCHAR(100) NOT NULL UNIQUE,
    status VARCHAR(20) NOT NULL DEFAULT 'processing',
    failure_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    processed_by INTEGER,
    wallet_credited BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS ix_deposit_requests_user_id
ON deposit_requests(user_id);

CREATE INDEX IF NOT EXISTS ix_deposit_requests_status
ON deposit_requests(status);
