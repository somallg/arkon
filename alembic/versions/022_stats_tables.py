"""Add stats tables: mcp_query_log and stats_daily_rollup

Revision ID: 022
Revises: 021
Create Date: 2026-05-18 00:00:00.000000

Adds:
- mcp_query_log: per-call MCP tool invocation log (for usage analytics & gap detection)
- stats_daily_rollup: pre-aggregated daily metrics for the admin statistics dashboard
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = '022'
down_revision: Union[str, None] = '021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'mcp_query_log',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('employee_id', UUID(as_uuid=True), sa.ForeignKey('employees.id', ondelete='SET NULL'), nullable=True),
        sa.Column('tool_name', sa.String(80), nullable=False),
        sa.Column('query_text', sa.Text(), nullable=True),
        sa.Column('result_count', sa.Integer(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('scope_metadata', JSONB(), nullable=True),
        sa.Column('result_ids', JSONB(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='ok'),
        sa.Column('error_message', sa.Text(), nullable=True),
    )
    op.create_index('ix_mcp_query_log_created_at', 'mcp_query_log', ['created_at'])
    op.create_index('ix_mcp_query_log_employee_id', 'mcp_query_log', ['employee_id'])
    op.create_index('ix_mcp_query_log_tool_name', 'mcp_query_log', ['tool_name'])
    op.create_index('ix_mcp_query_log_zero_result', 'mcp_query_log', ['created_at', 'result_count'])

    op.create_table(
        'stats_daily_rollup',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('metric_key', sa.String(80), nullable=False),
        sa.Column('dimensions', JSONB(), nullable=True),
        sa.Column('dimensions_hash', sa.String(64), nullable=False, server_default=''),
        sa.Column('value_numeric', sa.Float(), nullable=True),
        sa.Column('value_json', JSONB(), nullable=True),
        sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_unique_constraint(
        'uq_stats_rollup_keys', 'stats_daily_rollup',
        ['date', 'metric_key', 'dimensions_hash'],
    )
    op.create_index('ix_stats_rollup_date', 'stats_daily_rollup', ['date'])
    op.create_index('ix_stats_rollup_metric', 'stats_daily_rollup', ['metric_key', 'date'])


def downgrade() -> None:
    op.drop_index('ix_stats_rollup_metric', table_name='stats_daily_rollup')
    op.drop_index('ix_stats_rollup_date', table_name='stats_daily_rollup')
    op.drop_constraint('uq_stats_rollup_keys', 'stats_daily_rollup', type_='unique')
    op.drop_table('stats_daily_rollup')

    op.drop_index('ix_mcp_query_log_zero_result', table_name='mcp_query_log')
    op.drop_index('ix_mcp_query_log_tool_name', table_name='mcp_query_log')
    op.drop_index('ix_mcp_query_log_employee_id', table_name='mcp_query_log')
    op.drop_index('ix_mcp_query_log_created_at', table_name='mcp_query_log')
    op.drop_table('mcp_query_log')
