-- Phase 3 memory outbox (plan v2 §6.6, phase3-design.md §3)
--
-- Adds the durable enqueue state to agent_run_receipt so a crash between
-- "response completed" and "memory enqueued" is recoverable: the dispatcher
-- simply finds the row again on restart.
--
-- Idempotent: MySQL 8 has no `ADD COLUMN IF NOT EXISTS`, so both columns are
-- guarded through information_schema + prepared statements (same pattern as
-- migration 001).

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'agent_run_receipt'
    AND COLUMN_NAME = 'memory_enqueue_status'
);
SET @ddl := IF(
  @col_exists = 0,
  'ALTER TABLE agent_run_receipt ADD COLUMN memory_enqueue_status ENUM(''pending'',''done'',''failed'') NOT NULL DEFAULT ''pending'' AFTER pi_revision_note',
  'DO 0'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'agent_run_receipt'
    AND COLUMN_NAME = 'memory_attempts'
);
SET @ddl := IF(
  @col_exists = 0,
  'ALTER TABLE agent_run_receipt ADD COLUMN memory_attempts TINYINT NOT NULL DEFAULT 0 AFTER memory_enqueue_status',
  'DO 0'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- `memory_source_event` becomes the provenance source for the Pi path
-- (phase3-design.md §3). The existing memory repository requires a monotonic
-- per-event integer `seq` and a `created_at` on the provenance row (it stores
-- them on candidates as `source_seq` / `source_created_at`), so the ledger
-- needs one. AUTO_INCREMENT on a UNIQUE key gives a strictly increasing value
-- in insertion order — i.e. the order in which user messages actually arrived.
SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'memory_source_event'
    AND COLUMN_NAME = 'seq'
);
SET @ddl := IF(
  @col_exists = 0,
  'ALTER TABLE memory_source_event ADD COLUMN seq BIGINT NOT NULL AUTO_INCREMENT, ADD UNIQUE KEY uq_memory_source_seq (seq)',
  'DO 0'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- The dispatcher scans for completed-and-still-pending rows. Without this
-- index that scan degrades to a full table scan as receipts accumulate.
-- (Note: no semicolons inside comments here — naive statement splitters treat
-- them as statement boundaries.)
SET @idx_exists := (
  SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'agent_run_receipt'
    AND INDEX_NAME = 'idx_memory_outbox'
);
SET @ddl := IF(
  @idx_exists = 0,
  'ALTER TABLE agent_run_receipt ADD KEY idx_memory_outbox (memory_enqueue_status, updated_at)',
  'DO 0'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
