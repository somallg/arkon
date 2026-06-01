"""remove_workspace_module

Revision ID: 53ddb764523d
Revises: dbf99a2f2ad9
Create Date: 2026-06-01 16:02:35.309875

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers
revision: str = '53ddb764523d'
down_revision: Union[str, None] = 'dbf99a2f2ad9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()

    # 1. Cascade delete all project-scoped resources
    # Delete from drafts depending on branches or pages scoped to 'project'
    connection.execute(sa.text("DELETE FROM wiki_page_drafts WHERE branch_id IN (SELECT id FROM wiki_branches WHERE scope_type = 'project')"))
    connection.execute(sa.text("DELETE FROM wiki_page_drafts WHERE page_id IN (SELECT id FROM wiki_pages WHERE scope_type = 'project')"))
    
    # Delete project-scoped branches
    connection.execute(sa.text("DELETE FROM wiki_branches WHERE scope_type = 'project'"))
    
    # Delete project-scoped pages (cascades to revisions and links)
    connection.execute(sa.text("DELETE FROM wiki_pages WHERE scope_type = 'project'"))
    
    # Delete project-scoped sources
    connection.execute(sa.text("DELETE FROM sources WHERE scope_type = 'project'"))
    
    # Delete project-scoped skills
    connection.execute(sa.text("DELETE FROM skills WHERE scope_type = 'project'"))

    # 2. Drop workspace related tables
    op.drop_table('project_sources')
    op.drop_table('project_members')
    op.drop_table('projects')


def downgrade() -> None:
    # Downgrade path: recreate tables
    op.create_table('projects',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('workspace_type', sa.String(length=20), nullable=False, server_default='project'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
        sa.Column('created_by_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['created_by_id'], ['employees.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table('project_members',
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('employee_id', sa.UUID(), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False, server_default='viewer'),
        sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('project_id', 'employee_id')
    )

    op.create_table('project_sources',
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('source_id', sa.UUID(), nullable=False),
        sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('project_id', 'source_id')
    )
