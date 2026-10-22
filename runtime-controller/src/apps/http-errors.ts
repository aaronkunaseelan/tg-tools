import { ControllerError, isControllerError } from "../errors/controller-error.js";

interface PgLikeError extends Error {
  code?: string;
  constraint?: string;
  detail?: string;
}

function isPgLikeError(error: unknown): error is PgLikeError {
  return error instanceof Error && typeof (error as PgLikeError).code === "string";
}

export interface HttpErrorBody {
  error: string;
  code: string;
  details?: unknown;
}

export function toHttpError(error: unknown): ControllerError {
  if (isControllerError(error)) {
    return error;
  }
  if (isPgLikeError(error)) {
    switch (error.code) {
      case "22P02":
        return new ControllerError(400, "invalid_identifier", error.message || "invalid identifier");
      case "23503":
        return new ControllerError(409, "foreign_key_violation", error.message || "foreign key violation", {
          constraint: error.constraint ?? null,
          detail: error.detail ?? null,
        });
      case "23505":
        return new ControllerError(409, "unique_violation", error.message || "unique constraint violation", {
          constraint: error.constraint ?? null,
          detail: error.detail ?? null,
        });
      case "23514":
        return new ControllerError(400, "check_violation", error.message || "check constraint violation", {
          constraint: error.constraint ?? null,
          detail: error.detail ?? null,
        });
    }
  }
  const message = error instanceof Error ? error.message : String(error);
  return new ControllerError(500, "internal_error", message || "internal error");
}

export function toHttpErrorBody(error: unknown): HttpErrorBody {
  const normalized = toHttpError(error);
  return normalized.details == null
    ? { error: normalized.message, code: normalized.code }
    : { error: normalized.message, code: normalized.code, details: normalized.details };
}
