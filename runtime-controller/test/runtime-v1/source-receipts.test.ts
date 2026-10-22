import { randomUUID } from "node:crypto";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Pool } from "pg";
import { testDatabaseConfig } from "./test-database.js";
import { applyMigrations } from "../../src/db/migrate.js";
import { DEFAULT_RUNTIME_POLICY, resolveWorkloadPolicy } from "../../src/policy/default-policy.js";
import { admitAndSubmitRuntimeJob } from "../../src/runtime/submit.js";
import {
  admitSourceReceipt,
  cancelSourceReceipt,
  listSourceReceipts,
  markSourceReceiptSubmitted,
} from "../../src/runtime/source-receipts.js";

function testDatabaseName(): string {
  return `runtime_controller_source_receipts_test_${Date.now()}_${randomUUID().replace(/-/g, "")}`;
}

describe.sequential("runtime source receipts", () => {
  let adminPool: Pool;
  let pool: Pool;
  let dbName: string;

  beforeAll(async () => {
    dbName = testDatabaseName();
    const config = testDatabaseConfig();
    adminPool = new Pool({ ...config, database: "postgres" });
    await adminPool.query(`CREATE DATABASE ${dbName}`);
    pool = new Pool({ ...config, database: dbName });
    await applyMigrations(pool);
  }, 20_000);

  afterAll(async () => {
    await pool?.end();
    await adminPool?.query(`DROP DATABASE IF EXISTS ${dbName}`);
    await adminPool?.end();
  }, 20_000);

  it("admits source receipts idempotently by source system and source key", async () => {
    const first = await admitSourceReceipt(pool, {
      sourceSystem: "telegram",
      sourceKey: "tg:rss-audio:-1001:101",
      producerSystem: "rss-audio",
      workloadType: "rss_publish",
      routeId: "rss.publish.telegram.v1",
      routeKind: "rss_publish_route",
      channelId: "rss-audio:-1001:10",
      matchedRuleVersion: "v1",
      sourceJson: {
        chatId: -1001,
        threadId: 10,
        messageId: 101,
      },
    });
    expect(first.action).toBe("created");
    expect(first.receipt.status).toBe("accepted");

    const duplicate = await admitSourceReceipt(pool, {
      sourceSystem: "telegram",
      sourceKey: "tg:rss-audio:-1001:101",
      producerSystem: "rss-audio",
      workloadType: "rss_publish",
      routeId: "rss.publish.telegram.v1",
      routeKind: "rss_publish_route",
      channelId: "rss-audio:-1001:10",
      matchedRuleVersion: "v1",
      sourceJson: {
        messageId: 101,
        ignored: "duplicate should not rewrite",
      },
    });
    expect(duplicate.action).toBe("existing");
    expect(duplicate.receipt.receiptId).toBe(first.receipt.receiptId);
    expect(duplicate.receipt.sourceJson).toMatchObject({ chatId: -1001, messageId: 101 });
  }, 20_000);

  it("links accepted receipts to exactly one runtime job", async () => {
    const receipt = await admitSourceReceipt(pool, {
      sourceSystem: "telegram",
      sourceKey: "tg:meeting-summary:-1002:201",
      producerSystem: "meeting-summary",
      workloadType: "meeting_note",
      routeId: "meeting.note.telegram.v1",
      routeKind: "meeting_note_route",
      channelId: "meeting-summary:-1002:30",
      matchedRuleVersion: "v1",
      sourceJson: {
        chatId: -1002,
        threadId: 30,
        messageId: 201,
      },
    });

    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "transcription")!;
    const submitted = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "meeting-summary",
      producerRef: "tg:-1002:201",
      resultOwner: "meeting-summary",
      resultRoute: "meeting-summary.telegram.reply.v1",
      workloadType: "transcription",
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      idempotencyKey: "meeting-summary:tg:-1002:201",
    }, {
      ...policy,
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
    });
    if (submitted.action !== "created") throw new Error("expected job");

    const linked = await markSourceReceiptSubmitted(pool, {
      receiptId: receipt.receipt.receiptId,
      jobId: submitted.job.jobId,
    });
    expect(linked.status).toBe("submitted");
    expect(linked.jobId).toBe(submitted.job.jobId);

    const idempotent = await markSourceReceiptSubmitted(pool, {
      receiptId: receipt.receipt.receiptId,
      jobId: submitted.job.jobId,
    });
    expect(idempotent.jobId).toBe(submitted.job.jobId);
  }, 20_000);

  it("lists receipts by route provenance and supports accepted cancellation", async () => {
    const receipt = await admitSourceReceipt(pool, {
      sourceSystem: "telegram",
      sourceKey: "tg:transcribe:-1003:301",
      producerSystem: "transcribe-audio",
      workloadType: "transcribe_audio",
      routeId: "transcribe.audio.telegram.v1",
      routeKind: "transcribe_audio_route",
      channelId: "rss-audio:-1003:22",
      matchedRuleVersion: "v1",
      sourceJson: {
        chatId: -1003,
        threadId: 22,
        messageId: 301,
      },
    });

    const listed = await listSourceReceipts(pool, {
      routeKind: "transcribe_audio_route",
      status: ["accepted"],
    });
    expect(listed.items.some((item) => item.receiptId === receipt.receipt.receiptId)).toBe(true);

    const cancelled = await cancelSourceReceipt(pool, {
      receiptId: receipt.receipt.receiptId,
      reason: "operator rejected duplicate",
    });
    expect(cancelled.status).toBe("cancelled");
  }, 20_000);
});
