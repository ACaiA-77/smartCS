-- Phase 6 audit sink (plan v2 §6.9, phase6-design.md §3)
--
-- The harness pushes pi tool events onto a bounded in-memory queue and ships
-- them in batches to POST /internal/audit. This table is where they land.
--
-- Semantics: BEST-EFFORT. Losing the tail of the queue (process crash, bounded
-- overflow) is acceptable and is counted on the harness side, but a distorted
-- or duplicated row is NOT acceptable. `event_id` is the idempotency key the
-- ingest endpoint relies on, so re-sending a batch is always safe.
--
-- Idempotent by construction: CREATE TABLE IF NOT EXISTS, same pattern as
-- migrations 001/002/003.
--
-- NOTE (no semicolons inside comments — naive statement splitters treat them as
-- statement boundaries).

CREATE TABLE IF NOT EXISTS audit_event (
  id                BIGINT       NOT NULL AUTO_INCREMENT,
  event_id          CHAR(36)     COLLATE utf8mb4_bin NOT NULL,
  session_id        VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  client_request_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  kind              ENUM('tool_call','tool_result') NOT NULL,
  tool_name         VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  tool_call_id      VARCHAR(128) COLLATE utf8mb4_bin NULL,
  operation_id      VARCHAR(128) COLLATE utf8mb4_bin NULL,
  payload           JSON         NULL,
  trace_id          CHAR(32)     COLLATE utf8mb4_bin NULL,
  created_at        TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_audit_event_id (event_id),
  KEY idx_audit_session_created (session_id, created_at),
  KEY idx_audit_trace (trace_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
