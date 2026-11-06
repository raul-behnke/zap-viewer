CREATE TABLE IF NOT EXISTS chats (
  id            text PRIMARY KEY,
  name          text,
  is_group      bool NOT NULL DEFAULT false,
  last_msg_ts   timestamptz,
  archived      bool NOT NULL DEFAULT false,
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
  id            text PRIMARY KEY,
  chat_id       text NOT NULL REFERENCES chats(id),
  ts            timestamptz NOT NULL,
  from_me       bool NOT NULL,
  sender_jid    text,
  body          text,
  type          text,
  has_media     bool NOT NULL DEFAULT false,
  media_url     text,
  media_mime    text,
  media_name    text,
  media_expires_at timestamptz,
  ack           smallint,
  reply_to_id   text,
  reactions     jsonb NOT NULL DEFAULT '[]'::jsonb,
  edited_at     timestamptz,
  revoked_at    timestamptz,
  raw           jsonb NOT NULL,
  search_vec    tsvector GENERATED ALWAYS AS
                (to_tsvector('portuguese', coalesce(body, ''))) STORED
);

CREATE INDEX IF NOT EXISTS messages_search_idx ON messages USING gin(search_vec);
CREATE INDEX IF NOT EXISTS messages_chat_ts_idx ON messages (chat_id, ts DESC);

CREATE TABLE IF NOT EXISTS sync_state (
  chat_id       text PRIMARY KEY REFERENCES chats(id),
  cursor_ts     timestamptz,
  done          bool NOT NULL DEFAULT false,
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_summaries (
  chat_id                text PRIMARY KEY REFERENCES chats(id),
  summary                text NOT NULL,
  covered_through_ts     timestamptz NOT NULL,
  covered_message_count  integer NOT NULL DEFAULT 0,
  model                  text,
  updated_at             timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS webhook_log (
  id            bigserial PRIMARY KEY,
  event         text,
  payload       jsonb,
  error         text,
  received_at   timestamptz NOT NULL DEFAULT now()
);

-- Auditoria de envios feitos pelo agente. Sem FK para chats de propósito:
-- uma tentativa recusada por chat_id inexistente é exatamente o evento que
-- mais importa registrar, e uma FK a impediria de ser gravada.
CREATE TABLE IF NOT EXISTS sent_log (
  id       bigserial PRIMARY KEY,
  chat_id  text NOT NULL,
  text     text NOT NULL,
  ts       timestamptz NOT NULL DEFAULT now(),
  ok       bool NOT NULL,
  error    text,
  uncertain bool NOT NULL DEFAULT false
);

ALTER TABLE sent_log
  ADD COLUMN IF NOT EXISTS uncertain bool NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS sent_log_ts_idx ON sent_log (ts DESC);

CREATE TABLE IF NOT EXISTS message_action_log (
  id            bigserial PRIMARY KEY,
  action        text NOT NULL CHECK (action IN ('edit', 'delete')),
  chat_id       text NOT NULL,
  message_id    text NOT NULL,
  previous_text text,
  new_text      text,
  ts            timestamptz NOT NULL DEFAULT now(),
  ok            bool NOT NULL,
  error         text,
  uncertain     bool NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS message_action_log_ts_idx
  ON message_action_log (ts DESC);
CREATE INDEX IF NOT EXISTS message_action_log_message_idx
  ON message_action_log (message_id, ts DESC);
