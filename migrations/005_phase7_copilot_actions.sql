-- Milestone 3: pending_action becomes the shared two-phase authority for
-- Copilot external actions, not just refunds.
--
-- `type='tool'` identifies a frozen {"tool": ..., "arguments": {...}} snapshot
-- created by `copilot_action_prepare` and consumed by `copilot_action_confirm`.
--
-- Idempotent: the current COLUMN_TYPE is inspected first, so re-running this
-- migration is a no-op. Same guarded-PREPARE pattern as migrations 001/003.
--
-- NOTE (no semicolons inside comments — naive statement splitters treat them as
-- statement boundaries).

SET @pending_type := (
  SELECT COLUMN_TYPE FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'pending_action'
    AND COLUMN_NAME = 'type'
);
SET @ddl := IF(
  @pending_type LIKE '%''tool''%',
  'DO 0',
  'ALTER TABLE pending_action MODIFY COLUMN type ENUM(''refund_create'',''tool'') NOT NULL'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
