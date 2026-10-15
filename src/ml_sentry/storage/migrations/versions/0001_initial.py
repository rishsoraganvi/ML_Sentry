"""Initial schema: evaluation events, incidents, policy decisions, audit events.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)
IDENT = sa.String(128)


def upgrade() -> None:
    op.create_table(
        "evaluation_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", IDENT, nullable=False, unique=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("model_name", IDENT, nullable=False),
        sa.Column("model_version", IDENT, nullable=False),
        sa.Column("target_name", IDENT, nullable=False),
        sa.Column("dataset_id", IDENT, nullable=False),
        sa.Column("dataset_version", IDENT, nullable=False),
        sa.Column("evaluation_protocol_id", IDENT, nullable=False),
        sa.Column("sample_count", sa.BigInteger(), nullable=False),
        sa.Column("evaluation_timestamp", TS, nullable=False),
        sa.Column("metrics", JSONType, nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("received_at", TS, nullable=False),
        sa.Column("processing_status", sa.String(16), nullable=False),
        sa.Column("evidence_valid", sa.Boolean(), nullable=True),
    )
    op.create_index("ix_evaluation_events_model_name", "evaluation_events", ["model_name"])
    op.create_index(
        "ix_evaluation_events_comparability",
        "evaluation_events",
        [
            "model_name",
            "target_name",
            "dataset_id",
            "dataset_version",
            "evaluation_protocol_id",
            "role",
        ],
    )

    op.create_table(
        "incidents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "evaluation_event_id",
            sa.Uuid(),
            sa.ForeignKey("evaluation_events.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("model_name", IDENT, nullable=False),
        sa.Column("model_version", IDENT, nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("proposed_action", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_incidents_model_name", "incidents", ["model_name"])
    op.create_index("ix_incidents_severity", "incidents", ["severity"])
    op.create_index("ix_incidents_status", "incidents", ["status"])
    op.create_index("ix_incidents_created_at", "incidents", ["created_at"])

    op.create_table(
        "policy_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "evaluation_event_id",
            sa.Uuid(),
            sa.ForeignKey("evaluation_events.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id"), nullable=True),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("baseline_event_id", IDENT, nullable=True),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("proposed_action", sa.String(64), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=False),
        sa.Column("result", JSONType, nullable=False),
        sa.Column("result_digest", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )

    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id"), nullable=True),
        sa.Column(
            "evaluation_event_id",
            sa.Uuid(),
            sa.ForeignKey("evaluation_events.id"),
            nullable=True,
        ),
        sa.Column("policy_version", sa.String(64), nullable=True),
        sa.Column("evidence_refs", JSONType, nullable=False),
        sa.Column("outcome", sa.String(64), nullable=False),
        sa.Column("details", JSONType, nullable=False),
    )
    op.create_index("ix_audit_events_incident_id", "audit_events", ["incident_id"])

    if op.get_bind().dialect.name == "postgresql":
        # Defence in depth for the append-only audit log. A database superuser can
        # still drop the trigger; this is not cryptographic immutability.
        op.execute(
            """
            CREATE FUNCTION ml_sentry_audit_append_only() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'audit_events is append-only';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE TRIGGER audit_events_append_only
            BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION ml_sentry_audit_append_only();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
        op.execute("DROP FUNCTION IF EXISTS ml_sentry_audit_append_only()")
    op.drop_table("audit_events")
    op.drop_table("policy_decisions")
    op.drop_table("incidents")
    op.drop_table("evaluation_events")
