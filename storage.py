import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def connect():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(database_url, row_factory=dict_row)


def upsert_conversation(conversation_id, messages, title):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO ai_chat_conversations (conversation_id, title, messages)
            VALUES (%s, %s, %s)
            ON CONFLICT (conversation_id) DO UPDATE
            SET title = EXCLUDED.title,
                messages = EXCLUDED.messages,
                updated_at = NOW()
            """,
            (conversation_id, title, Jsonb(messages)),
        )


def create_job(job_id, conversation_id, agent_messages):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO ai_chat_jobs (job_id, conversation_id, status, agent_messages)
            VALUES (%s, %s, 'running', %s)
            """,
            (job_id, conversation_id, Jsonb(agent_messages)),
        )
        connection.execute(
            """
            UPDATE ai_chat_conversations
            SET active_job_id = %s, updated_at = NOW()
            WHERE conversation_id = %s
            """,
            (job_id, conversation_id),
        )


def get_job(job_id):
    with connect() as connection:
        return connection.execute(
            "SELECT * FROM ai_chat_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()


def get_job_and_conversation(job_id):
    with connect() as connection:
        return connection.execute(
            """
            SELECT j.*, c.vm_id, c.vm_session_id, c.messages AS conversation_messages
            FROM ai_chat_jobs j
            JOIN ai_chat_conversations c USING (conversation_id)
            WHERE j.job_id = %s
            """,
            (job_id,),
        ).fetchone()


def save_agent_messages(job_id, agent_messages):
    with connect() as connection:
        connection.execute(
            """
            UPDATE ai_chat_jobs
            SET agent_messages = %s, updated_at = NOW()
            WHERE job_id = %s
            """,
            (Jsonb(agent_messages), job_id),
        )


def append_event(job_id, payload):
    content = payload.get("content") if payload.get("type") == "delta" else None
    reasoning = payload.get("reasoning_content") if payload.get("type") == "delta" else None
    artifact = payload.get("event", {}).get("artifact") if payload.get("type") == "agent_event" else None
    artifact_link = None
    if artifact and artifact.get("artifact_id"):
        name = str(artifact.get("name") or "Letöltés").replace("[", "").replace("]", "")
        artifact_link = f"\n\n[{name}](/api/artifacts/{artifact['artifact_id']})"
    with connect() as connection:
        row = connection.execute(
            """
            INSERT INTO ai_chat_events (job_id, payload)
            VALUES (%s, %s)
            RETURNING event_id
            """,
            (job_id, Jsonb(payload)),
        ).fetchone()
        connection.execute(
            """
            UPDATE ai_chat_jobs
            SET response_content = response_content || %s || %s,
                response_reasoning = response_reasoning || %s,
                progress = COALESCE(%s, progress),
                updated_at = NOW()
            WHERE job_id = %s
            """,
            (
                content or "",
                artifact_link or "",
                reasoning or "",
                payload.get("progress"),
                job_id,
            ),
        )
        return row["event_id"]


def list_events(job_id, after=0):
    with connect() as connection:
        return connection.execute(
            """
            SELECT event_id, payload
            FROM ai_chat_events
            WHERE job_id = %s AND event_id > %s
            ORDER BY event_id
            """,
            (job_id, after),
        ).fetchall()


def latest_event_id(job_id):
    with connect() as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(event_id), 0) AS event_id FROM ai_chat_events WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        return row["event_id"]


def request_cancel(job_id):
    with connect() as connection:
        row = connection.execute(
            """
            UPDATE ai_chat_jobs
            SET cancel_requested = TRUE, updated_at = NOW()
            WHERE job_id = %s AND status IN ('queued', 'running')
            RETURNING job_id
            """,
            (job_id,),
        ).fetchone()
        return row is not None


def is_cancel_requested(job_id):
    with connect() as connection:
        row = connection.execute(
            "SELECT cancel_requested FROM ai_chat_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        return bool(row and row["cancel_requested"])


def add_redirect(job_id, message):
    message_content = message if isinstance(message, list) else message.strip()
    with connect() as connection:
        row = connection.execute(
            """
            UPDATE ai_chat_jobs
            SET redirects = redirects || %s::jsonb, updated_at = NOW()
            WHERE job_id = %s AND status = 'running'
            RETURNING conversation_id
            """,
            (Jsonb([message_content]), job_id),
        ).fetchone()
        if not row:
            return None
        connection.execute(
            """
            UPDATE ai_chat_conversations
            SET messages = messages || %s::jsonb, updated_at = NOW()
            WHERE conversation_id = %s
            """,
            (Jsonb([{"role": "user", "content": message_content}]), row["conversation_id"]),
        )
        return row["conversation_id"]


def take_redirects(job_id):
    with connect() as connection:
        row = connection.execute(
            """
            SELECT redirects
            FROM ai_chat_jobs
            WHERE job_id = %s
            FOR UPDATE
            """,
            (job_id,),
        ).fetchone()
        if not row:
            return []
        redirects = row["redirects"] or []
        if redirects:
            connection.execute(
                """
                UPDATE ai_chat_jobs
                SET redirects = '[]'::jsonb, updated_at = NOW()
                WHERE job_id = %s
                """,
                (job_id,),
            )
        return redirects


def has_redirects(job_id):
    with connect() as connection:
        row = connection.execute(
            "SELECT redirects FROM ai_chat_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        return bool(row and row["redirects"])


def finish_job(job_id, status, error=None):
    with connect() as connection:
        row = connection.execute(
            """
            UPDATE ai_chat_jobs
            SET status = %s, error = %s, updated_at = NOW()
            WHERE job_id = %s
            RETURNING conversation_id, response_content
            """,
            (status, error, job_id),
        ).fetchone()
        if not row:
            return
        conversation_id = row["conversation_id"]
        messages = connection.execute(
            "SELECT messages FROM ai_chat_conversations WHERE conversation_id = %s FOR UPDATE",
            (conversation_id,),
        ).fetchone()["messages"]
        assistant = {"role": "assistant", "content": row["response_content"]}
        if messages and messages[-1].get("role") == "assistant":
            messages[-1] = assistant
        else:
            messages.append(assistant)
        connection.execute(
            """
            UPDATE ai_chat_conversations
            SET messages = %s,
                active_job_id = CASE WHEN active_job_id = %s THEN NULL ELSE active_job_id END,
                updated_at = NOW()
            WHERE conversation_id = %s
            """,
            (Jsonb(messages), job_id, conversation_id),
        )


def list_conversations(conversation_ids):
    if not conversation_ids:
        return []
    with connect() as connection:
        conversations = connection.execute(
            """
            SELECT conversation_id, title, messages, vm_id, active_job_id, updated_at
            FROM ai_chat_conversations
            WHERE conversation_id = ANY(%s)
            ORDER BY updated_at DESC
            """,
            (conversation_ids,),
        ).fetchall()
        output = []
        for conversation in conversations:
            job_id = conversation["active_job_id"]
            if job_id:
                job = connection.execute(
                    """
                    SELECT job_id, conversation_id, status, progress, response_content,
                           response_reasoning, error, updated_at
                    FROM ai_chat_jobs
                    WHERE job_id = %s
                    """,
                    (job_id,),
                ).fetchone()
            else:
                job = connection.execute(
                    """
                    SELECT job_id, conversation_id, status, progress, response_content,
                           response_reasoning, error, updated_at
                    FROM ai_chat_jobs
                    WHERE conversation_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (conversation["conversation_id"],),
                ).fetchone()
            if job:
                job["last_event_id"] = latest_event_id(job["job_id"])
            conversation["latest_job"] = job
            conversation["updated_at"] = conversation["updated_at"].isoformat()
            output.append(conversation)
        return output


def pending_jobs():
    with connect() as connection:
        return connection.execute(
            "SELECT job_id FROM ai_chat_jobs WHERE status = 'running' ORDER BY created_at"
        ).fetchall()


def create_artifact(artifact_id, conversation_id, job_id, name, content_type, content):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO ai_chat_artifacts
                (artifact_id, conversation_id, job_id, name, content_type, content)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (artifact_id, conversation_id, job_id, name, content_type, content),
        )


def get_artifact(artifact_id):
    with connect() as connection:
        return connection.execute(
            """
            SELECT artifact_id, name, content_type, content
            FROM ai_chat_artifacts
            WHERE artifact_id = %s
            """,
            (artifact_id,),
        ).fetchone()


def update_sandbox(conversation_id, vm_id, session_id):
    with connect() as connection:
        connection.execute(
            """
            UPDATE ai_chat_conversations
            SET vm_id = %s, vm_session_id = %s, updated_at = NOW()
            WHERE conversation_id = %s
            """,
            (vm_id, session_id, conversation_id),
        )