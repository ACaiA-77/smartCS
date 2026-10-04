-- Phase 1 session foundation (plan v2 §5.1/§5.3/§6.6, design phase1-design.md §2)
--
-- Idempotent: safe to re-run. MySQL 8 has no `ADD COLUMN IF NOT EXISTS`, so the
-- single ALTER is guarded through information_schema + a prepared statement.
--
-- Column-type notes (deltas vs the design draft are recorded in
-- pi-harness/PHASE1_REPORT.md §偏差, not silently changed):
--   * session_id mirrors platform_db.conversation_session.session_id
--     (VARCHAR(128) COLLATE utf8mb4_bin) so the tables stay joinable.
--   * business_user_id mirrors platform_user.business_user_id (VARCHAR(128)),
--     not the BIGINT the draft wrote — the platform stores it as a string.
--   * client_request_id mirrors conversation_session.client_request_id (VARCHAR(128)).
--
-- Deliberately NOT created: pi_session_registry (D4 裁决 → "先查后建" 纪律).

-- ① harness 版本固定（会话创建时写死，终身不变）
SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'conversation_session'
    AND COLUMN_NAME = 'harness_version'
);
SET @ddl := IF(
  @col_exists = 0,
  'ALTER TABLE conversation_session ADD COLUMN harness_version ENUM(''legacy'',''pi'') NOT NULL DEFAULT ''legacy'' AFTER title',
  'DO 0'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- ② 请求级幂等回执
CREATE TABLE IF NOT EXISTS agent_run_receipt (
  id                    BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  session_id            VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  client_request_id     VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  request_hash          CHAR(64)     COLLATE utf8mb4_bin NOT NULL,
  status                ENUM('processing','completed','failed_recoverable') NOT NULL DEFAULT 'processing',
  response              JSON         NULL,
  open_write_operations JSON         NULL,
  pi_revision_note      VARCHAR(255) NULL,
  created_at            TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at            TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_request (session_id, client_request_id),
  KEY idx_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ③ 用户消息 provenance 最小账本（记忆抽取的 durable source）
CREATE TABLE IF NOT EXISTS memory_source_event (
  event_id          CHAR(36)     COLLATE utf8mb4_bin NOT NULL,
  session_id        VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  business_user_id  VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  client_request_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  content           MEDIUMTEXT   NOT NULL,
  created_at        TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  cleared_at        TIMESTAMP(3) NULL,
  PRIMARY KEY (event_id),
  UNIQUE KEY uq_source_event_request (session_id, client_request_id),
  KEY idx_session (session_id, created_at),
  KEY idx_cleared (cleared_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
