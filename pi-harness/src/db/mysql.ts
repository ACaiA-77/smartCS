/**
 * MySQL access for the pi-harness.
 *
 * Scope discipline (plan v2 §0.3): the harness reads/writes ONLY the two Phase 1
 * tables it owns — `agent_run_receipt` and `memory_source_event` — plus the
 * read-only `harness_version` column it needs for routing. Business state stays
 * behind the Python Business Runtime.
 */

import { createPool, type Pool, type PoolConnection, type RowDataPacket } from "mysql2/promise";
import { resolveMysqlConfig, type MysqlConfig } from "../config/env.js";

export interface Database {
  query<T extends RowDataPacket>(sql: string, params?: unknown[]): Promise<T[]>;
  execute(sql: string, params?: unknown[]): Promise<{ affectedRows: number; insertId: number }>;
  withConnection<T>(fn: (connection: PoolConnection) => Promise<T>): Promise<T>;
  close(): Promise<void>;
}

export function createDatabase(config: Partial<MysqlConfig> = {}): Database {
  const resolved = resolveMysqlConfig(config);
  const pool: Pool = createPool({
    ...resolved,
    charset: "utf8mb4",
    waitForConnections: true,
    connectionLimit: 8,
    connectTimeout: 5_000,
    supportBigNumbers: true,
    bigNumberStrings: false,
    // Keep DECIMAL/BIGINT sane; the columns we touch are ids and timestamps.
    dateStrings: false,
  });

  return {
    async query<T extends RowDataPacket>(sql: string, params: unknown[] = []): Promise<T[]> {
      const [rows] = await pool.query<T[]>(sql, params);
      return rows;
    },
    async execute(sql: string, params: unknown[] = []) {
      const [result] = await pool.execute(sql, params);
      const header = result as { affectedRows?: number; insertId?: number };
      return { affectedRows: header.affectedRows ?? 0, insertId: header.insertId ?? 0 };
    },
    async withConnection<T>(fn: (connection: PoolConnection) => Promise<T>): Promise<T> {
      const connection = await pool.getConnection();
      try {
        return await fn(connection);
      } finally {
        connection.release();
      }
    },
    async close() {
      await pool.end();
    },
  };
}
