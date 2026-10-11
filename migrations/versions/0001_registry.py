"""Immutable release registry. Roles are provisioned outside migrations by the fixture harness."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("cluster", sa.Column("id", sa.String(64), primary_key=True))
    op.create_table(
        "release",
        sa.Column("cluster_id", sa.String(64), sa.ForeignKey("cluster.id"), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        *[
            sa.Column(n, sa.String(16 if n == "kind" else 64), nullable=False)
            for n in (
                "kind",
                "name",
                "version",
                "platform",
                "build_id",
                "content_digest",
                "manifest_digest",
            )
        ],
        sa.Column("payload", JSONB, nullable=False),
        sa.CheckConstraint("kind IN ('software', 'database')"),
        sa.UniqueConstraint("cluster_id", "kind", "name", "version", "platform", "build_id"),
    )
    op.create_table(
        "database_index",
        sa.Column("cluster_id", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        *[
            sa.Column(n, sa.String(64), nullable=False)
            for n in ("release_id", "tool_release_id", "parameters_digest", "index_digest")
        ],
        sa.ForeignKeyConstraint(["cluster_id", "release_id"], ["release.cluster_id", "release.id"]),
        sa.ForeignKeyConstraint(
            ["cluster_id", "tool_release_id"], ["release.cluster_id", "release.id"]
        ),
        sa.UniqueConstraint("cluster_id", "release_id", "tool_release_id", "parameters_digest"),
    )
    op.create_table(
        "channel",
        sa.Column("cluster_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("release_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.BigInteger, nullable=False),
        sa.ForeignKeyConstraint(["cluster_id", "release_id"], ["release.cluster_id", "release.id"]),
    )
    op.execute(
        "CREATE VIEW registry_public WITH (security_barrier=true) AS SELECT cluster_id, id, kind, name, version, platform, payload FROM release"
    )
    op.execute(
        "CREATE VIEW registry_indexes WITH (security_barrier=true) AS SELECT * FROM database_index"
    )
    op.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore an approved backup")
