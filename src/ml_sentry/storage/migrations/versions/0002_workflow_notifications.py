"""Workflow tracking on incidents and notification delivery records.

Revision ID: 0002_workflow_notifications
Revises: 0001_initial
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_workflow_notifications"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    with op.batch_alter_table("incidents") as batch:
        batch.add_column(sa.Column("workflow_thread_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("workflow_status", sa.String(16), nullable=True))
        batch.add_column(sa.Column("workflow_error", sa.String(256), nullable=True))
    op.create_index(
        "ix_incidents_workflow_thread_id", "incidents", ["workflow_thread_id"], unique=True
    )
    op.create_index("ix_incidents_workflow_status", "incidents", ["workflow_status"])
    # Incidents opened before workflows existed get governed after the upgrade: the
    # startup recovery sweep picks up 'pending' ones and assigns their thread id.
    op.execute("UPDATE incidents SET workflow_status = 'pending' WHERE status = 'open'")

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("delivered_at", TS, nullable=True),
    )
    op.create_index(
        "ix_notification_deliveries_incident_id", "notification_deliveries", ["incident_id"]
    )


def downgrade() -> None:
    op.drop_table("notification_deliveries")
    op.drop_index("ix_incidents_workflow_status", "incidents")
    op.drop_index("ix_incidents_workflow_thread_id", "incidents")
    with op.batch_alter_table("incidents") as batch:
        batch.drop_column("workflow_error")
        batch.drop_column("workflow_status")
        batch.drop_column("workflow_thread_id")
