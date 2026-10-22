-- meeting-summary schema: the tables the pipeline reads and writes.
--
-- Apply as the database owner (role cot_meeting_summary) or as postgres:
--   psql -d qmd_gsd_runtime_v1 -f schema.sql

CREATE SCHEMA IF NOT EXISTS tg_meeting_summary;

CREATE TABLE IF NOT EXISTS tg_meeting_summary.meeting_inputs (
    input_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_channel      text NOT NULL,
    external_thread_id  text,
    external_message_id text NOT NULL,
    external_sender_ref text,
    input_kind          text,
    input_content_hash  text,
    received_at         timestamptz NOT NULL DEFAULT NOW(),
    raw_payload_json    jsonb,
    metadata_json       jsonb,
    status              text,
    updated_at          timestamptz NOT NULL DEFAULT NOW(),
    UNIQUE (source_channel, external_message_id)
);

CREATE TABLE IF NOT EXISTS tg_meeting_summary.meeting_notes (
    meeting_note_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    input_id              uuid UNIQUE NOT NULL REFERENCES tg_meeting_summary.meeting_inputs (input_id),
    recording_id          uuid,
    transcript_ref        text,
    title                 text,
    overview              text,
    notes_json            jsonb,
    chat_summary_text     text,
    notes_content_hash    text,
    source_content_hash   text,
    synthesis_model       text,
    prompt_version        text,
    notes_schema_version  integer,
    review_status         text,
    review_version        integer,
    meeting_date          date,
    materialisation_state text,
    materialisation_id    uuid,
    materialisation_hash  text,
    document_url          text,
    status                text,
    ready_at              timestamptz,
    metadata_json         jsonb,
    updated_at            timestamptz NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tg_meeting_summary.meeting_note_reviews (
    review_id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    meeting_note_id                    uuid NOT NULL REFERENCES tg_meeting_summary.meeting_notes (meeting_note_id),
    review_version                     integer NOT NULL,
    status                             text NOT NULL,
    metadata_json                      jsonb,
    chat_id                            bigint,
    thread_id                          bigint,
    review_message_id                  bigint,
    latest_preview_document_message_id bigint,
    latest_preview_document_file_id    text,
    preview_document_status            text NOT NULL,
    preview_content_hash               text,
    accept_token                       text,
    created_by_user_id                 bigint,
    accepted_by_user_id                bigint,
    accepted_at                        timestamptz,
    updated_at                         timestamptz NOT NULL DEFAULT NOW(),
    UNIQUE (meeting_note_id, review_version)
);

CREATE TABLE IF NOT EXISTS tg_meeting_summary.meeting_note_appends (
    append_id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    meeting_note_id             uuid NOT NULL REFERENCES tg_meeting_summary.meeting_notes (meeting_note_id),
    append_version              integer NOT NULL,
    note_text                   text,
    chat_id                     bigint,
    thread_id                   bigint,
    source_message_id           bigint,
    created_by_user_id          bigint,
    markdown_content_hash       text,
    document_url                text,
    preview_document_message_id bigint,
    preview_document_file_id    text,
    created_at                  timestamptz NOT NULL DEFAULT NOW(),
    UNIQUE (meeting_note_id, append_version)
);