import { randomUUID } from "node:crypto";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Pool } from "pg";
import { testDatabaseConfig } from "./test-database.js";
import { applyMigrations } from "../../src/db/migrate.js";
import { DEFAULT_RUNTIME_POLICY, resolveWorkloadPolicy } from "../../src/policy/default-policy.js";
import { admitAndSubmitRuntimeJob } from "../../src/runtime/submit.js";
import { claimRuntimeJob } from "../../src/runtime/claim.js";
import { finalizeAttempt } from "../../src/runtime/finalize.js";
import { appendProgress, getProgress } from "../../src/runtime/progress.js";

describe.sequential("shared progress PostgreSQL authority", () => {
  let admin: Pool, pool: Pool, name: string;
  beforeAll(async () => {
    const config=testDatabaseConfig(); name=`progress_${randomUUID().replaceAll("-","")}`;
    admin=new Pool({...config,database:"postgres"});
    await admin.query(`CREATE DATABASE ${name}`);
    pool=new Pool({...config,database:name,max:8}); await applyMigrations(pool);
  },20_000);
  afterAll(async () => { await pool?.end(); if(name) await admin?.query(`DROP DATABASE ${name}`); await admin?.end(); });
  async function fixture() {
    const id=randomUUID();
    const submitted=await admitAndSubmitRuntimeJob(pool,{producerSystem:id,producerRef:id,resultOwner:id,resultRoute:"fixture",workloadType:"scorer",queueClass:"foreground",executionProfile:"cpu_small",executionShape:"local_bounded",idempotencyKey:id},resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY,"scorer")!);
    if(submitted.action!=="created") throw Error("fixture admission");
    const claim=await claimRuntimeJob(pool,{workerId:id,supportedExecutionProfiles:["cpu_small"],producerSystems:[id],leaseDurationSec:120});
    if(!claim) throw Error("fixture claim");
    return {schemaVersion:1,jobId:claim.job.jobId,attemptId:claim.attempt.attemptId,leaseToken:claim.attempt.leaseToken,eventId:randomUUID(),sequence:1,stage:"transcribe_chunks",occurrence:randomUUID(),kind:"started",facts:{completed:0,total:6,units:"chunks"}};
  }
  it("records lifecycle, deduplicates facts and never stores lease credentials",async()=>{
    const e=await fixture(); const before=await getProgress(pool,e.jobId);
    expect(before.events.length).toBeGreaterThan(0);
    const first=await appendProgress(pool,e); expect(first.action).toBe("created");
    expect(await appendProgress(pool,e)).toEqual({...first,action:"existing"});
    expect(JSON.stringify((await getProgress(pool,e.jobId)).events)).not.toContain(e.leaseToken);
    await expect(appendProgress(pool,{...e,facts:{completed:1}})).rejects.toMatchObject({code:"progress_event_conflict"});
  });
  it("closed attempts allow exact receipt replay but reject new progress",async()=>{
    const e=await fixture(); await appendProgress(pool,e);
    await finalizeAttempt(pool,{...e,outcome:"succeeded",resultJson:{ok:true}});
    expect((await appendProgress(pool,e)).action).toBe("existing");
    await expect(appendProgress(pool,{...e,eventId:randomUUID(),sequence:2})).rejects.toMatchObject({code:"stale_lease_token"});
    expect((await getProgress(pool,e.jobId)).job.status).toBe("succeeded");
  });
  it("serializes concurrent sequences and rejects regression",async()=>{
    const e=await fixture(); await appendProgress(pool,e);
    await appendProgress(pool,{...e,eventId:randomUUID(),sequence:3,kind:"advanced",facts:{completed:4,total:6}});
    await expect(appendProgress(pool,{...e,eventId:randomUUID(),sequence:2})).rejects.toMatchObject({code:"progress_sequence_conflict"});
    await expect(appendProgress(pool,{...e,eventId:randomUUID(),sequence:4,kind:"advanced",facts:{completed:3,total:6}})).rejects.toMatchObject({code:"progress_count_regression"});
  });
  it("rechecks database time after waiting for locks",async()=>{
    const e=await fixture();
    const blocker=await pool.connect(); await blocker.query("BEGIN");
    await blocker.query("SELECT 1 FROM runtime.jobs WHERE job_id=$1 FOR UPDATE",[e.jobId]);
    const write=appendProgress(pool,e).then(()=>null,error=>error);
    await blocker.query("UPDATE runtime.jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=$1",[e.jobId]);
    await blocker.query("COMMIT"); blocker.release();
    expect(await write).toMatchObject({code:"expired_lease"});
    expect((await getProgress(pool,e.jobId)).stages).toEqual([]);
  });
  it("rejects private content and invalid measured units",async()=>{
    const e=await fixture();
    await expect(appendProgress(pool,{...e,text:"private"})).rejects.toMatchObject({code:"invalid_progress"});
    await expect(appendProgress(pool,{...e,facts:{completed:7,total:6}})).rejects.toMatchObject({code:"invalid_progress"});
  });
});
