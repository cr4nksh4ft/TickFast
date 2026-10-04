CREATE TABLE users (
    id INTEGER NOT NULL AUTO_INCREMENT,
    role VARCHAR(20) NOT NULL DEFAULT 'user',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT chk_users_role CHECK (role IN ('user', 'admin'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;