"""Approvals, jobs, releases, model aliases and workflow leases.

Revision ID: 0003_approvals_jobs_releases
Revises: 0002_workflow_notifications
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_approvals_jobs_releases"
down_revision = "0002_workflow_notifications"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)
IDENT = sa.String(128)


def upgrade() -> None:
    with op.batch_alter_table("incidents") as batch:
        batch.add_column(sa.Column("workflow_lease_owner", sa.String(64), nullable=True))
        batch.add_column(sa.Column("workflow_lease_until", TS, nullable=True))

    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("gate", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("spec", JSONType, nullable=False),
        sa.Column("spec_digest", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("requested_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", TS, nullable=True),
        sa.Column("decision_note", sa.String(1000), nullable=True),
        sa.UniqueConstraint("incident_id", "gate", name="uq_approval_incident_gate"),
    )
    op.create_index("ix_approval_requests_incident_id", "approval_requests", ["incident_id"])
    op.create_index("ix_approval_requests_status", "approval_requests", ["status"])
    op.create_index("ix_approval_requests_expires_at", "approval_requests", ["expires_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column(
            "approval_id",
            sa.Uuid(),
            sa.ForeignKey("approval_requests.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("model_name", IDENT, nullable=False),
        sa.Column("pipeline_id", IDENT, nullable=False),
        sa.Column("runner", sa.String(16), nullable=False),
        sa.Column("spec", JSONType, nullable=False),
        sa.Column("spec_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claimed_by", sa.String(128), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("deadline_at", TS, nullable=False),
        sa.Column("result", JSONType, nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("logs_ref", sa.String(2048), nullable=True),
    )
    op.create_index("ix_jobs_incident_id", "jobs", ["incident_id"])
    op.create_index("ix_jobs_model_name", "jobs", ["model_name"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "model_releases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column(
            "approval_id",
            sa.Uuid(),
            sa.ForeignKey("approval_requests.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("model_name", IDENT, nullable=False),
        sa.Column("target", IDENT, nullable=False),
        sa.Column("adapter", sa.String(32), nullable=False),
        sa.Column("from_ref", sa.String(512), nullable=True),
        sa.Column("to_ref", sa.String(512), nullable=False),
        sa.Column("to_version", IDENT, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("details", JSONType, nullable=False),
    )
    op.create_index("ix_model_releases_incident_id", "model_releases", ["incident_id"])
    op.create_index("ix_model_releases_model_name", "model_releases", ["model_name"])

    op.create_table(
        "model_aliases",
        sa.Column("model_name", IDENT, primary_key=True),
        sa.Column("alias", IDENT, primary_key=True),
        sa.Column("model_ref", sa.String(512), nullable=False),
        sa.Column("model_version", IDENT, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("model_aliases")
    op.drop_table("model_releases")
    op.drop_table("jobs")
    op.drop_table("approval_requests")
    with op.batch_alter_table("incidents") as batch:
        batch.drop_column("workflow_lease_until")
        batch.drop_column("workflow_lease_owner")
