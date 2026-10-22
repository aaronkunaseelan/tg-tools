import { realpathSync } from "node:fs";
import { isAbsolute } from "node:path";

// Integration tests create/drop databases. They require an explicitly bound
// disposable cluster and must never default to the production Unix socket.
export function testDatabaseConfig() {
  const host = process.env.RUNTIME_TEST_PGHOST;
  if (!host || !isAbsolute(host) || realpathSync(host) === "/run/postgresql") {
    throw new Error("RUNTIME_TEST_PGHOST must name a disposable cluster's private Unix socket directory");
  }
  const port = Number(process.env.RUNTIME_TEST_PGPORT ?? "5432");
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("invalid test PostgreSQL port");
  return { host, port, user: process.env.RUNTIME_TEST_PGUSER ?? "ubuntu" };
}
