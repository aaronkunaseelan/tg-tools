import type pg from "pg";

export type DatabaseLike = pg.Pool | pg.PoolClient;

function isPoolClient(db: DatabaseLike): db is pg.PoolClient {
  return typeof (db as pg.PoolClient).release === "function";
}

function isPool(db: DatabaseLike): db is pg.Pool {
  return typeof (db as pg.Pool).connect === "function" && !isPoolClient(db);
}

export async function withTransaction<T>(db: DatabaseLike, fn: (client: pg.PoolClient) => Promise<T>): Promise<T> {
  const pool = isPool(db) ? db : null;
  const client: pg.PoolClient = pool ? await pool.connect() : db as pg.PoolClient;
  const ownsTransaction = Boolean(pool);

  try {
    if (ownsTransaction) {
      await client.query("BEGIN");
    }
    const result = await fn(client);
    if (ownsTransaction) {
      await client.query("COMMIT");
    }
    return result;
  } catch (error) {
    if (ownsTransaction) {
      await client.query("ROLLBACK");
    }
    throw error;
  } finally {
    if (ownsTransaction) {
      client.release();
    }
  }
}
