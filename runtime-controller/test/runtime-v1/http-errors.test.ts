import { describe, expect, it } from "vitest";
import { toHttpError, toHttpErrorBody } from "../../src/apps/http-errors.js";
import { badRequest, conflict } from "../../src/errors/controller-error.js";

describe("http error mapping", () => {
  it("preserves typed controller errors", () => {
    const error = conflict("stale_lease_token", "stale lease token", { attemptId: "attempt-1" });

    expect(toHttpError(error).statusCode).toBe(409);
    expect(toHttpErrorBody(error)).toEqual({
      error: "stale lease token",
      code: "stale_lease_token",
      details: { attemptId: "attempt-1" },
    });
  });

  it("maps generic errors to internal_error", () => {
    const error = new Error("boom");

    expect(toHttpError(error).statusCode).toBe(500);
    expect(toHttpErrorBody(error)).toEqual({
      error: "boom",
      code: "internal_error",
    });
  });

  it("supports bad-request payloads without details", () => {
    const error = badRequest("invalid_json_body", "request body must be valid JSON");

    expect(toHttpErrorBody(error)).toEqual({
      error: "request body must be valid JSON",
      code: "invalid_json_body",
    });
  });

  it("maps common Postgres constraint and identifier errors", () => {
    const invalidId = Object.assign(new Error('invalid input syntax for type uuid: "bad"'), { code: "22P02" });
    expect(toHttpErrorBody(invalidId)).toEqual({
      error: 'invalid input syntax for type uuid: "bad"',
      code: "invalid_identifier",
    });

    const unique = Object.assign(new Error("duplicate key value violates unique constraint"), {
      code: "23505",
      constraint: "jobs_effect_key_uidx",
      detail: "Key (producer_system, effect_key)=(meeting-summary, tg:1:2) already exists.",
    });
    expect(toHttpErrorBody(unique)).toEqual({
      error: "duplicate key value violates unique constraint",
      code: "unique_violation",
      details: {
        constraint: "jobs_effect_key_uidx",
        detail: "Key (producer_system, effect_key)=(meeting-summary, tg:1:2) already exists.",
      },
    });
  });
});
