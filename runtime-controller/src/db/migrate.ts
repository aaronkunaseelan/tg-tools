import { existsSync } from "node:fs";
import { readdir, readFile } from "node:fs/promises";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Pool, PoolClient } from "pg";

export function defaultMigrationsDir(): string {
  let currentDir = dirname(fileURLToPath(import.meta.url));
  for (let depth = 0; depth < 8; depth += 1) {
    const candidate = resolve(currentDir, "sql/migrations");
    if (existsSync(candidate)) {
      return candidate;
    }
    currentDir = resolve(currentDir, "..");
  }
  throw new Error("could not locate sql/migrations directory");
}

export async function listMigrationFiles(migrationsDir = defaultMigrationsDir()): Promise<string[]> {
  const entries = await readdir(migrationsDir, { withFileTypes: true });
  return entries
    .filter((entry) => entry.isFile() && entry.name.endsWith(".sql"))
    .map((entry) => resolve(migrationsDir, entry.name))
    .sort((left, right) => left.localeCompare(right));
}

async function ensureMigrationTable(db: Pool | PoolClient): Promise<void> {
  await db.query(`CREATE SCHEMA IF NOT EXISTS runtime`);
  await db.query(`
    CREATE TABLE IF NOT EXISTS runtime.schema_migrations (
      filename text PRIMARY KEY,
      applied_at timestamptz NOT NULL DEFAULT NOW()
    )
  `);
}

async function loadAppliedMigrationNames(db: Pool | PoolClient): Promise<Set<string>> {
  const result = await db.query<{ filename: string }>(`SELECT filename FROM runtime.schema_migrations`);
  return new Set(result.rows.map((row) => row.filename));
}

async function bootstrapAppliedMigrationNames(db: Pool | PoolClient, files: string[], applied: Set<string>): Promise<Set<string>> {
  if (applied.size > 0) return applied;

  const markAppliedIf = async (condition: boolean, prefix: string): Promise<void> => {
    if (!condition) return;
    const name = files.map((file) => basename(file)).find((value) => value.startsWith(prefix));
    if (!name) return;
    await db.query(
      `INSERT INTO runtime.schema_migrations (filename)
       VALUES ($1)
       ON CONFLICT (filename) DO NOTHING`,
      [name],
    );
    applied.add(name);
  };

  const jobsTable = await db.query<{ name: string | null }>(`SELECT to_regclass('runtime.jobs')::text AS name`);
  await markAppliedIf(Boolean(jobsTable.rows[0]?.name), "001_");

  const claimIndex = await db.query<{ name: string | null }>(`SELECT to_regclass('runtime.jobs_claim_by_profile_workload_idx')::text AS name`);
  await markAppliedIf(Boolean(claimIndex.rows[0]?.name), "002_");

  const workersTable = await db.query<{ name: string | null }>(`SELECT to_regclass('runtime.workers')::text AS name`);
  await markAppliedIf(Boolean(workersTable.rows[0]?.name), "004_");

  return applied;
}

export async function applyMigrations(db: Pool | PoolClient, migrationsDir = defaultMigrationsDir()): Promise<string[]> {
  const files = await listMigrationFiles(migrationsDir);
  await ensureMigrationTable(db);
  let applied = await loadAppliedMigrationNames(db);
  applied = await bootstrapAppliedMigrationNames(db, files, applied);

  const newlyApplied: string[] = [];
  for (const file of files) {
    const filename = basename(file);
    if (applied.has(filename)) {
      continue;
    }
    const sql = await readFile(file, "utf8");
    await db.query(sql);
    await db.query(`INSERT INTO runtime.schema_migrations (filename) VALUES ($1)`, [filename]);
    newlyApplied.push(file);
  }
  return newlyApplied;
}
