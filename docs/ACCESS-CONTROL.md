# Access Control

Arkon has a dual-realm permission system:

1. **Global realm** — department-based RBAC for organization-wide resources (documents, wiki, skills)
2. **Workspace realm** — membership-based roles for project-scoped resources

These two realms are independent. Global permissions do not grant workspace access, and workspace membership does not grant access to global resources by itself.

---

## Global Realm — Permissions

### Permission format

```
{resource}:{action}:{scope}

scope options:
  own_dept  →  your department + global (unscoped) resources
  all       →  all resources regardless of department
```

### Full permission list

**Documents**
| Permission | Description |
|---|---|
| `doc:read:own_dept` | View documents in your department and global documents |
| `doc:read:all` | View all documents across all departments |
| `doc:create:own_dept` | Upload documents to your department |
| `doc:create:all` | Upload documents to any department |
| `doc:edit:own_dept` | Edit document metadata in your department |
| `doc:edit:all` | Edit any document |
| `doc:delete:own_dept` | Delete documents in your department |
| `doc:delete:all` | Delete any document |

**Wiki**
| Permission | Description |
|---|---|
| `wiki:read:own_dept` | Read global wiki + wiki pages scoped to your dept |
| `wiki:read:all` | Read all wiki pages |
| `wiki:write:own_dept` | Propose wiki drafts for global pages |
| `wiki:write:all` | Direct edit any wiki page + approve/reject drafts on global pages |
| `wiki:delete:own_dept` | Delete wiki pages in your dept scope |
| `wiki:delete:all` | Delete any wiki page |

**AI Skills**
| Permission | Description |
|---|---|
| `skill:read:own_dept` | Use skills in your department + global skills |
| `skill:read:all` | Use all skills |
| `skill:create:own_dept` | Upload skills to your department |
| `skill:create:all` | Upload skills anywhere |
| `skill:edit:own_dept` | Edit skill metadata in your department |
| `skill:edit:all` | Edit any skill |
| `skill:delete:own_dept` | Delete skills in your department |
| `skill:delete:all` | Delete any skill |

**Organization (admin operations)**
| Permission | Description |
|---|---|
| `org:departments:read` | View departments |
| `org:departments:manage` | Create/edit/delete departments |
| `org:employees:read` | View employee directory |
| `org:employees:manage` | Create/edit/deactivate employees |
| `org:roles:read` | View roles and their permissions |
| `org:roles:manage` | Create/edit/delete roles |
| `org:settings:read` | View system settings |
| `org:settings:manage` | Modify system settings (AI providers, keys) |
| `org:audit:read` | View audit log |

**Workspaces**
| Permission | Description |
|---|---|
| `workspace:view:all` | View all workspaces without being a member |

---

### Roles

A **Role** is a named collection of permissions assigned to employees. Roles are created and managed in **Admin Portal → Roles**.

**Built-in role presets:**

| Preset | Included permissions |
|---|---|
| **Viewer** | `doc:read:own_dept`, `wiki:read:own_dept`, `skill:read:own_dept`, `org:departments:read` |
| **Contributor** | Viewer + `doc:create:own_dept`, `wiki:write:own_dept`, `skill:create:own_dept` |
| **Department Admin** | Contributor + edit/delete for own dept (docs, wiki, skills) |
| **Knowledge Admin** | All `:all` permissions for docs, wiki, and skills |

**Default employee permissions** (when no custom role is assigned):
`doc:read:own_dept`, `doc:create:own_dept`, `wiki:read:own_dept`, `wiki:write:own_dept`, `skill:read:own_dept`

---

### System Admin

The `admin` role (set on the Employee model) is a system-level override:
- Bypasses all permission checks
- Has workspace admin role in every workspace automatically
- Can create and delete workspaces
- Can view and manage all resources regardless of department

---

## Workspace Realm — Membership Roles

Each workspace (project) has its own member list. Membership roles are separate from global permissions.

### Roles

| Role | Level | What they can do |
|---|---|---|
| **Viewer** | 0 | Read wiki pages, sources, and member list of the workspace |
| **Contributor** | 1 | + Propose wiki drafts for workspace pages |
| **Editor** | 2 | + Direct edit wiki pages · Approve/reject drafts · Add/remove sources · Upload files to workspace |
| **Admin** | 3 | + Add/remove members · Change member roles · Rename/archive the workspace |

Roles are hierarchical — Editor can do everything Contributor can, and so on.

### Guards

- **Last admin protection** — the last workspace admin cannot be removed or demoted. Assign another admin first.
- **Workspace deletion** — only system admins can delete workspaces, regardless of workspace role.
- **Workspace creation** — only system admins can create workspaces.

---

## How scope resolution works

### Global documents

```
User has doc:read:own_dept?
  → Source has no departments (Global doc)?   → Accessible ✓
  → Source's departments include user's dept? → Accessible ✓
  → Otherwise                                 → Blocked ✗

User has doc:read:all?
  → Accessible regardless of source departments ✓
```

### Workspace resources

```
User is system admin?            → Full access ✓
User is workspace member?        → Access (filtered by workspace role)
Otherwise                        → 403 Forbidden
```

### Wiki pages

Wiki pages have three possible scopes — `global`, `department`, `project` — and the MCP read path resolves visibility by OR-ing the three branches the caller actually belongs to:

```
Visible to a non-admin caller:
  scope=global                                                  → always
  scope=department AND scope_id == user.department_id           → yes
  scope=project    AND scope_id ∈ user.project_ids (member of)  → yes
  otherwise                                                     → no

System admin                                                    → all scopes
```

> **History note.** Before commit `7676df5`, the MCP scope filter only OR-ed `global + own_dept` and silently dropped project-scoped pages — workspace members couldn't find their own workspace's wiki via `search_wiki`. The helper is now `_scope_filter_for_identity(department_id, project_ids)`; the older `_scope_filter_with_dept` is deprecated and kept only for the pipeline write path.

### Out-of-scope discovery hint

When a non-admin caller searches or reads a slug that exists in a scope they cannot access (a different department, a workspace they aren't a member of), the MCP tools return a *hint* instead of pretending the page does not exist:

```
search_wiki  → appends "Out-of-scope matches" section listing
               (scope_type, scope_name, count) groups
read_wiki_page → returns "exists in department X / workspace Y but
                 you don't have access, contact the scope admin"
```

The hint deliberately leaks **only** the scope label + count. Page titles, summaries, and content are never surfaced across a permission boundary — a title can itself be sensitive (e.g. "Q1 layoffs"). A future opt-in (Tier 2) could expose titles via a config flag, but the default behaviour is conservative.

### Wiki write permissions (global pages)

| Action | Required permission |
|---|---|
| Propose a draft | `wiki:write:own_dept` or `wiki:write:all` |
| Direct edit | `wiki:write:all` |
| Approve / reject draft | `wiki:write:all` |

For **workspace-scoped** pages, global permissions are not used — workspace roles apply:

| Action | Required workspace role |
|---|---|
| Propose a draft | Contributor+ |
| Direct edit | Editor+ |
| Approve / reject draft | Editor+ |

---

## Setting up access control (step by step)

### 1. Create departments

**Admin Portal → Departments → New Department**

Departments define the scope boundary for `own_dept` permissions. Every employee belongs to one department.

### 2. Create roles

**Admin Portal → Roles → New Role**

Select the permissions this role should grant. You can start from a built-in preset and customize.

### 3. Create employees

**Admin Portal → Employees → New Employee**

Assign each employee to a department and a role.

### 4. Assign knowledge types to sources

When uploading documents, assign them a knowledge type. This determines which employees can see them via MCP (based on their MCP token's `allowed_knowledge_types`).

### 5. Create workspaces and add members

**Admin Portal → Workspaces → New Workspace**

Add employees as workspace members and assign their workspace role (Viewer, Contributor, Editor, or Admin).

---

## MCP token scoping

When an MCP token is generated for an employee, it captures their current permission scope:
- Which knowledge types they can access
- Their department
- The list of workspaces they are a member of (`project_ids`)
- The list of source IDs reachable through those workspaces (`project_source_ids`)

This scope is re-evaluated on each request from the live state of their role, department, and workspace memberships. Revoking a token or changing an employee's role takes effect immediately on the next MCP call — there is no in-memory cache to invalidate.

### Tokens are hashed at rest

Plaintext tokens are never stored. When a token is issued, Arkon stores:

| Column | What it holds |
|---|---|
| `employees.mcp_token_hash` | `HMAC-SHA256(MCP_TOKEN_PEPPER, plaintext)`, hex-encoded |
| `employees.mcp_token_prefix` | First 12 chars of the plaintext (for UI display only) |
| `employees.mcp_token_rotated_at` | When the current token was issued |

Verification recomputes the HMAC of the bearer token in each request and looks it up by `mcp_token_hash`. A DB dump alone — without the pepper — cannot forge tokens. Tokens are also URL-safe random 256-bit strings, so a plain HMAC is sufficient (no bcrypt necessary).

> **Migration `027` zeroed every legacy plaintext token in place.** Every user must rotate via the Admin Portal or `POST /api/my/mcp-token` after deploying 0.7.2. The `MCP_TOKEN_PEPPER` env var is required to start the server — pick a strong random value at deploy time and never rotate it (rotating invalidates every token).

`generate_token` calls return the plaintext exactly once. There is no read-back path; lost tokens must be regenerated.

---

## Workspace-scoped picker endpoints

Workspace admins need to invite members and link sources but **do not** automatically inherit org-level `org:employees:read` / `doc:read:all` permissions. Two scoped endpoints exist so the frontend pickers don't need org-wide access:

| Endpoint | Returns | Required role |
|---|---|---|
| `GET /api/projects/{id}/members/candidates` | Employees not yet in the workspace, with name + email + department | Workspace admin |
| `GET /api/projects/{id}/sources/candidates` | Sources not yet linked to the workspace, with title + KT + status | Workspace editor+ |

Both accept `?search=` for substring filtering and cap at 500 rows. The bulk-add endpoint `POST /api/projects/{id}/members/bulk` (workspace admin) accepts `{employee_ids, role}` and processes each row in its own savepoint, so a duplicate or stale employee_id in the batch doesn't poison the rest.

The frontend `ProjectDetail` page uses `Promise.allSettled` for these calls — a 403 on the candidate fetches (e.g. a workspace viewer opening the page) no longer sinks the whole load. The "Add members" UI is gated on `isOrgAdmin || workspaceRole === 'admin'` so workspace admins see the picker even when they aren't org admins.
