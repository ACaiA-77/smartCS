-- Phase 5 write enable (plan v2 §5.4/§6.3, phase5-design.md §2)
--
-- pending_action becomes Python/MySQL authority for the two-phase refund flow
-- (moved out of Redis session_state). receipt.open_write_operations already
-- exists from migration 001 and is activated by this phase.
--
-- Idempotent: guarded through information_schema + prepared statements, same
-- pattern as migrations 001/002.
--
-- NOTE (no semicolons inside comments — naive statement splitters treat them as
-- statement boundaries).

CREATE TABLE IF NOT EXISTS pending_action (
  id              CHAR(36)     COLLATE utf8mb4_bin NOT NULL,
  session_id      VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  user_id         VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  type            ENUM('refund_create') NOT NULL,
  payload         JSON         NOT NULL,
  status          ENUM('pending','consumed','cancelled','expired') NOT NULL DEFAULT 'pending',
  operation_id    CHAR(64)     COLLATE utf8mb4_bin NULL,
  created_at      TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  expires_at      TIMESTAMP(3) NOT NULL,
  PRIMARY KEY (id),
  KEY idx_pending_session (session_id),
  KEY idx_pending_status (status, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
