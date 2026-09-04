-- Accounts for the command centre.
--
-- Separate from `merchants` on purpose. A merchant is a shop this system
-- sells on behalf of; a user is a person who signs in. One person may later
-- hold several shops, and a shop may outlive the person who registered it.

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT        NOT NULL,
    -- Lowercased email, so nobody can register the same address twice with
    -- different capitalisation and end up with two accounts.
    email_key     TEXT        NOT NULL UNIQUE,
    name          TEXT        NOT NULL,
    website_name  TEXT,
    website_url   TEXT,

    -- scrypt output and its per-user salt, both hex. Never the password.
    password_hash TEXT        NOT NULL,
    password_salt TEXT        NOT NULL,

    -- The shop this account manages, once one exists. Null until they
    -- complete setup, because signing up and connecting a store are separate
    -- steps and a half-finished signup should not leave a stray merchant row.
    merchant_id   TEXT        REFERENCES merchants(id) ON DELETE SET NULL,

    active        BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,

    CONSTRAINT email_shape CHECK (email_key ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$')
);

-- Sessions are rows rather than signed tokens so that signing out actually
-- ends the session. A self-contained token stays valid until it expires,
-- whatever the server decides afterwards.
CREATE TABLE IF NOT EXISTS sessions (
    -- SHA-256 of the token that was handed to the browser. Holding the hash
    -- means a leaked database does not hand over live sessions.
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    user_agent  TEXT,
    ip          TEXT
);

CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions (user_id);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions (expires_at);

ALTER TABLE users    ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
