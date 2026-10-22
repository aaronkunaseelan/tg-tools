import { Pool } from "pg";

export interface DatabaseConfig {
  connectionString?: string;
  applicationName?: string;
  max?: number;
  host?: string;
  port?: number;
  database?: string;
  user?: string;
  password?: string;
}

export function databaseUrlFromEnv(envVar = "DATABASE_URL"): string {
  const value = process.env[envVar]?.trim();
  if (!value) {
    throw new Error(`${envVar} is required`);
  }
  return value;
}

export function localPeerDatabaseConfig(database = process.env.PGDATABASE ?? "postgres"): DatabaseConfig {
  return {
    host: process.env.PGHOST ?? "/var/run/postgresql",
    port: process.env.PGPORT ? Number(process.env.PGPORT) : undefined,
    database,
    user: process.env.PGUSER ?? process.env.USER ?? "ubuntu",
    password: process.env.PGPASSWORD,
  };
}

export function createPool(config: DatabaseConfig): Pool {
  return new Pool({
    connectionString: config.connectionString,
    host: config.host,
    port: config.port,
    database: config.database,
    user: config.user,
    password: config.password,
    application_name: config.applicationName ?? "runtime-controller",
    max: config.max ?? 10,
  });
}
