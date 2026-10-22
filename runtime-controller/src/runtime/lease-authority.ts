import type pg from "pg";
import { conflict } from "../errors/controller-error.js";
import type { RuntimeJob, RuntimeJobAttempt } from "../types/runtime.js";

// Read the server clock AFTER acquiring row locks. Transaction NOW() and an
// application timestamp captured before a lock wait can authorize an expired lease.
// suppliedNow is an internal test clock; HTTP handlers never accept it from clients.
export async function lockedAuthorityTime(client: pg.PoolClient, suppliedNow?: Date): Promise<Date> {
  if (suppliedNow !== undefined) {
    if (!Number.isFinite(suppliedNow.getTime())) throw new Error("invalid authority clock");
    return suppliedNow;
  }
  const result = await client.query<{ now: Date }>("SELECT clock_timestamp() AS now");
  return new Date(result.rows[0]!.now);
}

export function requireCurrentExpiry(expiresAt: string | null, now: Date, code: string): void {
  const expiry = expiresAt === null ? NaN : new Date(expiresAt).getTime();
  if (!Number.isFinite(expiry) || expiry <= now.getTime()) {
    throw conflict(code, "lease authority expired; reconciliation must reclaim it");
  }
}

export function requireCurrentAttempt(job: RuntimeJob, attempt: RuntimeJobAttempt, now: Date): void {
  requireCurrentExpiry(job.leaseExpiresAt, now, "expired_lease");
  requireCurrentExpiry(attempt.leaseExpiresAt, now, "expired_lease");
}
