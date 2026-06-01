"""simplify_auth_fixed_roles

Revision ID: dbf99a2f2ad9
Revises: e06a34a42582
Create Date: 2026-06-01 14:57:59.581465

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers
revision: str = 'dbf99a2f2ad9'
down_revision: Union[str, None] = 'e06a34a42582'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add global_role column as nullable first
    op.add_column('employees', sa.Column('global_role', sa.String(length=30), nullable=True, comment="viewer, contributor, knowledge_manager, or admin"))

    # 2. Perform data migration
    connection = op.get_bind()

    # Read all roles from 'roles' table
    try:
        roles = connection.execute(sa.text("SELECT id, permissions FROM roles")).fetchall()
        role_permissions = {str(r[0]): r[1] for r in roles}
    except Exception:
        # Table might not exist or empty
        role_permissions = {}

    # Read all employees
    employees = connection.execute(sa.text("SELECT id, role, custom_role_id FROM employees")).fetchall()

    for emp in employees:
        emp_id = emp[0]
        emp_role = emp[1]
        custom_role_id = str(emp[2]) if emp[2] else None

        global_role = 'viewer'
        if emp_role == 'admin':
            global_role = 'admin'
        elif custom_role_id and custom_role_id in role_permissions:
            perms = role_permissions[custom_role_id]
            if not isinstance(perms, list):
                perms = []
            
            # Check for knowledge_manager permissions (contains :all writes or skill review)
            km_indicators = {
                "doc:create:all", "doc:edit:all", "doc:delete:all",
                "wiki:write:all", "wiki:delete:all",
                "skill:create:all", "skill:edit:all", "skill:delete:all",
                "skill:contribution:review", "org:employees:manage", "org:roles:manage"
            }
            # Check for contributor permissions (contains :own_dept writes)
            contrib_indicators = {
                "doc:create:own_dept", "doc:edit:own_dept", "doc:delete:own_dept",
                "wiki:write:own_dept", "wiki:delete:own_dept",
                "skill:create:own_dept", "skill:edit:own_dept", "skill:delete:own_dept"
            }

            has_km = any(p in km_indicators for p in perms)
            has_contrib = any(p in contrib_indicators for p in perms)

            if has_km:
                global_role = 'knowledge_manager'
            elif has_contrib:
                global_role = 'contributor'
            else:
                global_role = 'viewer'
        else:
            global_role = 'viewer'

        connection.execute(
            sa.text("UPDATE employees SET global_role = :g_role WHERE id = :e_id"),
            {"g_role": global_role, "e_id": emp_id}
        )

    # 3. Set global_role nullable=False and default='viewer'
    op.alter_column('employees', 'global_role',
               existing_type=sa.String(length=30),
               nullable=False,
               server_default='viewer')

    # 4. Drop ForeignKey constraint and custom_role_id column
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.drop_column('custom_role_id')

    # 5. Drop roles table
    op.drop_table('roles')


def downgrade() -> None:
    # Downgrade path (restore roles table and custom_role_id column)
    op.create_table('roles',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('permissions', sa.JSON(), nullable=False),
        sa.Column('is_system', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
    )

    op.add_column('employees', sa.Column('custom_role_id', sa.UUID(), nullable=True))
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.create_foreign_key('fk_employees_custom_role_id_roles', 'roles', ['custom_role_id'], ['id'], ondelete='SET NULL')
        batch_op.drop_column('global_role')
