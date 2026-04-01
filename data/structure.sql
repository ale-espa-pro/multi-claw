CREATE EXTENSION IF NOT EXISTS "uuid-ossp";


/*DROP TABLE calendar;
DROP TABLE users;


CREATE TABLE users (
    user_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    payload JSONB
);
CREATE INDEX idx_users_username ON users (username);



CREATE TABLE IF NOT EXISTS calendar (
    date            DATE PRIMARY KEY,
    year            SMALLINT NOT NULL CHECK (year BETWEEN 1 AND 9999),
    month           SMALLINT NOT NULL CHECK (month BETWEEN 1 AND 12),
    day             SMALLINT NOT NULL CHECK (day BETWEEN 1 AND 31),
    day_of_week     SMALLINT NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),
    is_weekend      BOOLEAN NOT NULL,
    iso_week        SMALLINT NOT NULL CHECK (iso_week BETWEEN 1 AND 53),
    day_type        TEXT NOT NULL
);

*/







