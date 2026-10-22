export class ControllerError extends Error {
  readonly statusCode: number;
  readonly code: string;
  readonly details: unknown;

  constructor(statusCode: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ControllerError";
    this.statusCode = statusCode;
    this.code = code;
    this.details = details ?? null;
  }
}

export function isControllerError(error: unknown): error is ControllerError {
  return error instanceof ControllerError;
}

export function badRequest(code: string, message: string, details?: unknown): ControllerError {
  return new ControllerError(400, code, message, details);
}

export function unauthorized(code: string, message: string, details?: unknown): ControllerError {
  return new ControllerError(401, code, message, details);
}

export function notFound(code: string, message: string, details?: unknown): ControllerError {
  return new ControllerError(404, code, message, details);
}

export function conflict(code: string, message: string, details?: unknown): ControllerError {
  return new ControllerError(409, code, message, details);
}

export function internalError(code: string, message: string, details?: unknown): ControllerError {
  return new ControllerError(500, code, message, details);
}
