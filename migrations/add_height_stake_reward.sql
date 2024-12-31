CREATE TABLE IF NOT EXISTS height_stake_reward (
    id BIGSERIAL PRIMARY KEY,
    height BIGINT NOT NULL,
    address TEXT NOT NULL,
    amount BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(height, address)
);

CREATE INDEX idx_height_stake_reward_height ON height_stake_reward(height);
CREATE INDEX idx_height_stake_reward_address ON height_stake_reward(address); 