WITH currently_blocked_users AS (
    -- 1. Find the latest block status per player (Guaranteed 1 row per player due to GROUP BY)
    SELECT
        player_id AS user_id,
        argMax(action, created_at) AS current_action,
        argMax(requester, created_at) AS requester,
        max(created_at) AS block_created_at,
        argMax(term_date, created_at) AS term_date,
        argMax(comment, created_at) AS comment
    FROM enpl_player_block_actions_history
    GROUP BY player_id
    HAVING current_action = 'blocked'
),

users_deduped AS (
    -- 2. Strictly deduplicate the users table to prevent fan-out multiplication
    SELECT
        id AS user_id,
        external_id user_external_id,
        domain,
        min(reg_dt) AS reg_dt -- Grabs the earliest registration date if duplicates exist
    FROM users_npl
    GROUP BY id, user_external_id,domain
)
select u.*, c.current_action, c.requester, c.block_created_at, c.term_date, c.comment
from users_deduped u
left join currently_blocked_users c
on u.user_id = c.user_id
