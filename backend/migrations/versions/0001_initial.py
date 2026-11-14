"""initial schema

Revision ID: 0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="engineer"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("last_login_at", TZ),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", TZ, nullable=False),
        sa.Column("ip", sa.String(64)),
        sa.Column("user_agent", sa.String(300)),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.create_table(
        "sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("config", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("secret_enc", sa.Text),
        sa.Column("visibility", sa.String(20), nullable=False, server_default="internal"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("schedule_minutes", sa.Integer),
        sa.Column("status", sa.String(20), nullable=False, server_default="idle"),
        sa.Column("last_sync_at", TZ),
        sa.Column("last_error", sa.Text),
        sa.Column("created_by", sa.String(36)),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("sources.id", ondelete="CASCADE")),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("doc_type", sa.String(20), nullable=False, server_default=""),
        sa.Column("uri", sa.Text),
        sa.Column("file_path", sa.Text),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("visibility", sa.String(20), nullable=False, server_default="internal"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("error", sa.Text),
        sa.Column("issues", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("page_count", sa.Integer),
        sa.Column("chunk_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("legacy", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("meta", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("uploaded_by", sa.String(36)),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", TZ),
    )
    op.create_index("ix_documents_source_id", "documents", ["source_id"])
    op.execute("CREATE INDEX ix_documents_uri ON documents USING hash (uri)")

    op.create_table(
        "chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ord", sa.Integer, nullable=False, server_default="0"),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("section_title", sa.Text),
        sa.Column("locator", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    # Generated tsvector: 'simple' configuration keeps part numbers, OBIS codes and Arabic tokens intact.
    op.execute(
        "ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS "
        "(to_tsvector('simple', coalesce(section_title, '') || ' ' || text)) STORED"
    )
    op.execute("CREATE INDEX ix_chunks_tsv ON chunks USING GIN (tsv)")

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("ref_id", sa.String(36)),
        sa.Column("label", sa.String(300), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("message", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column("stats", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(36)),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", TZ),
        sa.Column("finished_at", TZ),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False, server_default="New conversation"),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("citations", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("meta", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])

    op.create_table(
        "reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default="report"),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("data", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("citations", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_reports_user_id", "reports", ["user_id"])

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("secret_enc", sa.Text),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ts", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("user_id", sa.String(36)),
        sa.Column("user_email", sa.String(255)),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("target", sa.String(300)),
        sa.Column("detail", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("ip", sa.String(64)),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])


def downgrade() -> None:
    for table in ["audit_log", "app_settings", "reports", "messages", "conversations", "jobs",
                  "chunks", "documents", "sources", "sessions", "users"]:
        op.drop_table(table)
