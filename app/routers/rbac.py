"""
Department & Employee router — RBAC management for admin portal.
Permission model v2: uses scoped permission format.
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.database.models import Department, Employee, EmployeeDepartment
from app.services.audit_service import log_audit
from app.services.auth_service import (
    get_current_user,
    hash_password,
    require_permission,
)
from app.services.mcp_auth_service import MCPAuthService

router = APIRouter()


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------

class DepartmentCreate(BaseModel):
    name: str
    description: Optional[str] = None


class DepartmentOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    employee_count: int = 0

    class Config:
        from_attributes = True


class EmployeeCreate(BaseModel):
    name: str
    email: str
    password: Optional[str] = None  # Optional on update
    role: str = "employee"  # "admin" or "employee"
    global_role: str = "viewer"  # "viewer", "contributor", "knowledge_manager", "admin"
    # All departments the employee belongs to. Empty list is allowed — such a
    # user can only see resources scoped to 'global'.
    department_ids: list[str] = []


class EmployeeOut(BaseModel):
    id: str
    name: str
    email: str
    role: str
    global_role: str
    department_ids: list[str] = []
    department_names: list[str] = []
    is_active: bool
    has_token: bool
    last_connected: Optional[str] = None

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    token: str
    employee_name: str
    instructions: str


# ---------------------------------------------------------------------------
# Department CRUD
# ---------------------------------------------------------------------------

@router.get("/departments")
async def list_departments(
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:departments:read"),
):
    """List all departments with employee counts."""
    stmt = select(Department).options(selectinload(Department.employees))
    result = await db.execute(stmt)
    departments = result.scalars().all()

    return [
        DepartmentOut(
            id=str(d.id),
            name=d.name,
            description=d.description,
            employee_count=len(d.employees),
        )
        for d in departments
    ]


@router.post("/departments", status_code=201)
async def create_department(
    body: DepartmentCreate,
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:departments:manage"),
):
    """Create a new department."""
    dept = Department(name=body.name, description=body.description)
    db.add(dept)
    await log_audit(db, _user, "create", "department", str(dept.id), reason=dept.name)
    await db.flush()
    return {"id": str(dept.id), "name": dept.name}


@router.put("/departments/{dept_id}")
async def update_department(
    dept_id: str,
    body: DepartmentCreate,
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:departments:manage"),
):
    dept = await db.get(Department, uuid.UUID(dept_id))
    if not dept:
        raise HTTPException(404, "Department not found")
    dept.name = body.name
    dept.description = body.description
    await log_audit(db, _user, "update", "department", str(dept.id), reason=dept.name)
    await db.flush()
    return {"id": str(dept.id), "name": dept.name}


@router.delete("/departments/{dept_id}")
async def delete_department(
    dept_id: str,
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:departments:manage"),
):
    dept = await db.get(Department, uuid.UUID(dept_id))
    if not dept:
        raise HTTPException(404, "Department not found")
    await log_audit(db, _user, "delete", "department", str(dept.id), reason=dept.name)
    await db.delete(dept)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Employee CRUD
# ---------------------------------------------------------------------------

@router.get("/employees")
async def list_employees(
    department_id: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:employees:read"),
):
    """List employees with pagination, optionally filtered by department or search."""
    from sqlalchemy import func as sa_func

    base = select(Employee).options(
        selectinload(Employee.employee_departments).selectinload(
            EmployeeDepartment.department
        ),
    )
    count_base = select(sa_func.count(Employee.id))

    if department_id:
        dept_uuid = uuid.UUID(department_id)
        dept_filter = Employee.id.in_(
            select(EmployeeDepartment.employee_id).where(
                EmployeeDepartment.department_id == dept_uuid
            )
        )
        base = base.where(dept_filter)
        count_base = count_base.where(dept_filter)
    if search:
        like = f"%{search}%"
        base = base.where(Employee.name.ilike(like) | Employee.email.ilike(like))
        count_base = count_base.where(Employee.name.ilike(like) | Employee.email.ilike(like))

    # Total count
    total = (await db.execute(count_base)).scalar() or 0

    # Paginated query
    offset = (max(page, 1) - 1) * page_size
    stmt = base.order_by(Employee.name).offset(offset).limit(page_size)
    result = await db.execute(stmt)
    employees = result.scalars().all()

    return {
        "items": [
            EmployeeOut(
                id=str(e.id),
                name=e.name,
                email=e.email,
                role=e.role,
                global_role=e.global_role,
                department_ids=[str(ed.department_id) for ed in e.employee_departments],
                department_names=[
                    ed.department.name for ed in e.employee_departments if ed.department
                ],
                is_active=e.is_active,
                has_token=bool(e.mcp_token_hash),
                last_connected=e.last_connected.isoformat() if e.last_connected else None,
            )
            for e in employees
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, -(-total // page_size)),  # ceil division
    }


@router.post("/employees", status_code=201)
async def create_employee(
    body: EmployeeCreate,
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:employees:manage"),
):
    """Create a new employee."""
    if not body.password:
        raise HTTPException(400, "Password is required")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if body.role not in ("admin", "employee"):
        raise HTTPException(400, "Role must be 'admin' or 'employee'")
    if body.global_role not in ("viewer", "contributor", "knowledge_manager", "admin"):
        raise HTTPException(400, "Global role must be one of: viewer, contributor, knowledge_manager, admin")

    dept_uuids = [uuid.UUID(d) for d in body.department_ids]
    if dept_uuids:
        existing = (await db.execute(
            select(Department.id).where(Department.id.in_(dept_uuids))
        )).scalars().all()
        if len(existing) != len(set(dept_uuids)):
            raise HTTPException(400, "One or more departments not found")

    emp = Employee(
        name=body.name,
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role,
        global_role=body.global_role,
    )
    db.add(emp)
    await db.flush()  # need emp.id before adding join rows

    for d_id in set(dept_uuids):
        db.add(EmployeeDepartment(employee_id=emp.id, department_id=d_id))

    await log_audit(db, _user, "create", "employee", str(emp.id), reason=emp.email)
    await db.flush()

    return {"id": str(emp.id), "name": emp.name, "email": emp.email}


@router.put("/employees/{emp_id}")
async def update_employee(
    emp_id: str,
    body: EmployeeCreate,
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:employees:manage"),
):
    emp = await db.get(Employee, uuid.UUID(emp_id))
    if not emp:
        raise HTTPException(404, "Employee not found")
    if body.global_role not in ("viewer", "contributor", "knowledge_manager", "admin"):
        raise HTTPException(400, "Global role must be one of: viewer, contributor, knowledge_manager, admin")
    emp.name = body.name
    emp.email = body.email
    emp.role = body.role
    emp.global_role = body.global_role
    if body.password:
        emp.password_hash = hash_password(body.password)

    # Reconcile department memberships against the incoming set.
    new_dept_uuids = {uuid.UUID(d) for d in body.department_ids}
    if new_dept_uuids:
        existing = (await db.execute(
            select(Department.id).where(Department.id.in_(new_dept_uuids))
        )).scalars().all()
        if len(existing) != len(new_dept_uuids):
            raise HTTPException(400, "One or more departments not found")

    current = (await db.execute(
        select(EmployeeDepartment).where(EmployeeDepartment.employee_id == emp.id)
    )).scalars().all()
    current_set = {ed.department_id for ed in current}

    for ed in current:
        if ed.department_id not in new_dept_uuids:
            await db.delete(ed)
    for d_id in new_dept_uuids - current_set:
        db.add(EmployeeDepartment(employee_id=emp.id, department_id=d_id))

    await log_audit(db, _user, "update", "employee", str(emp.id), reason=emp.email)
    await db.flush()
    return {"id": str(emp.id), "name": emp.name}


@router.delete("/employees/{emp_id}")
async def delete_employee(
    emp_id: str,
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:employees:manage"),
):
    emp = await db.get(Employee, uuid.UUID(emp_id))
    if not emp:
        raise HTTPException(404, "Employee not found")
    if emp.role == "admin":
        raise HTTPException(400, "Cannot delete an admin account")
    await log_audit(db, _user, "delete", "employee", str(emp.id), reason=emp.email)
    await db.delete(emp)
    return {"deleted": True}


class DepartmentMembership(BaseModel):
    department_id: str


@router.post("/employees/{emp_id}/departments", status_code=201)
async def add_employee_department(
    emp_id: str,
    body: DepartmentMembership,
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:employees:manage"),
):
    """Add an employee to an additional department."""
    emp = await db.get(Employee, uuid.UUID(emp_id))
    if not emp:
        raise HTTPException(404, "Employee not found")
    dept = await db.get(Department, uuid.UUID(body.department_id))
    if not dept:
        raise HTTPException(404, "Department not found")

    existing = (await db.execute(
        select(EmployeeDepartment).where(
            EmployeeDepartment.employee_id == emp.id,
            EmployeeDepartment.department_id == dept.id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "Employee already belongs to this department")

    db.add(EmployeeDepartment(employee_id=emp.id, department_id=dept.id))
    await log_audit(
        db, _user, "update", "employee", str(emp.id),
        reason=f"added to dept={dept.name}",
    )
    await db.flush()
    return {"id": str(emp.id), "department_id": str(dept.id)}


@router.delete("/employees/{emp_id}/departments/{dept_id}")
async def remove_employee_department(
    emp_id: str,
    dept_id: str,
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:employees:manage"),
):
    """Remove an employee from a department."""
    ed = (await db.execute(
        select(EmployeeDepartment).where(
            EmployeeDepartment.employee_id == uuid.UUID(emp_id),
            EmployeeDepartment.department_id == uuid.UUID(dept_id),
        )
    )).scalar_one_or_none()
    if not ed:
        raise HTTPException(404, "Membership not found")
    await db.delete(ed)
    await log_audit(
        db, _user, "update", "employee", str(emp_id),
        reason=f"removed from dept={dept_id}",
    )
    await db.flush()
    return {"removed": True}


@router.patch("/employees/{emp_id}/toggle")
async def toggle_employee(
    emp_id: str,
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:employees:manage"),
):
    """Activate or deactivate an employee."""
    emp = await db.get(Employee, uuid.UUID(emp_id))
    if not emp:
        raise HTTPException(404, "Employee not found")
    emp.is_active = not emp.is_active
    await log_audit(db, _user, "update", "employee", str(emp.id), reason=f"toggle active={emp.is_active}")
    await db.flush()
    return {"id": str(emp.id), "is_active": emp.is_active}


# ---------------------------------------------------------------------------
# MCP Token Management
# ---------------------------------------------------------------------------

@router.post("/employees/{emp_id}/token", response_model=TokenResponse)
async def generate_mcp_token(
    emp_id: str,
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:employees:manage"),
):
    """Generate (or regenerate) an MCP token for an employee."""
    emp = await db.get(Employee, uuid.UUID(emp_id))
    if not emp:
        raise HTTPException(404, "Employee not found")

    auth_svc = MCPAuthService(db)
    token = await auth_svc.generate_token(emp.id)

    return TokenResponse(
        token=token,
        employee_name=emp.name,
        instructions=(
            f"Add this to Claude Desktop config:\n"
            f'{{"mcpServers": {{"arkon": {{"url": "https://your-server/mcp", '
            f'"headers": {{"Authorization": "Bearer {token}"}}}}}}}}'
        ),
    )


@router.delete("/employees/{emp_id}/token")
async def revoke_mcp_token(
    emp_id: str,
    db: AsyncSession = Depends(get_db),
    _user: Employee = require_permission("org:employees:manage"),
):
    """Revoke an employee's MCP token."""
    auth_svc = MCPAuthService(db)
    revoked = await auth_svc.revoke_token(uuid.UUID(emp_id))
    if not revoked:
        raise HTTPException(404, "Employee not found or has no token")
    return {"revoked": True}


# ---------------------------------------------------------------------------
# Self-Service: Employee gets their own MCP token
# ---------------------------------------------------------------------------

@router.post("/my/mcp-token", response_model=TokenResponse)
async def get_my_mcp_token(
    db: AsyncSession = Depends(get_db),
    current_user: Employee = Depends(get_current_user),
):
    """
    Rotate the current employee's own MCP token.

    Tokens are now stored hashed, so we can never read back the plaintext.
    Every POST here issues a NEW token and invalidates any previous one —
    callers must reconfigure their Claude Desktop after every call.
    """
    auth_svc = MCPAuthService(db)
    token = await auth_svc.generate_token(current_user.id)

    return TokenResponse(
        token=token,
        employee_name=current_user.name,
        instructions=(
            f"Add this to Claude Desktop config:\n"
            f'{{"mcpServers": {{"arkon": {{"url": "https://your-server/mcp", '
            f'"headers": {{"Authorization": "Bearer {token}"}}}}}}}}'
        ),
    )


@router.delete("/my/mcp-token")
async def revoke_my_mcp_token(
    db: AsyncSession = Depends(get_db),
    current_user: Employee = Depends(get_current_user),
):
    """Revoke the current employee's own MCP token."""
    auth_svc = MCPAuthService(db)
    await auth_svc.revoke_token(current_user.id)
    return {"revoked": True}


@router.get("/my/mcp-token/status")
async def get_my_mcp_token_status(
    current_user: Employee = Depends(get_current_user),
):
    """Check if the current employee has an active MCP token (without revealing it)."""
    return {"has_token": bool(current_user.mcp_token_hash)}
