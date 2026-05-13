# Wiki Department Hard Scoping

## Problem
Source documents gắn Department (IT, Sales, HR...) nhưng tất cả Wiki pages đều sinh ra ở scope `global`, ai có quyền `wiki:read` đều đọc được. User kỳ vọng: gắn department = chỉ department đó đọc được wiki.

## Solution: Department as Hard Scope
Sử dụng cơ chế scope hiện có (`scope_type` + `scope_id`) để cách ly wiki theo department. Mỗi department có "không gian wiki" riêng, slug riêng, không gộp chéo.

### Logic Rules
| Source departments | Wiki scope | Giải thích |
|---|---|---|
| Không có (global doc) | `scope_type=global, scope_id=NULL` | Hiện hành, không đổi |
| 1 department (ví dụ IT) | `scope_type=department, scope_id=IT_uuid` | Wiki riêng cho IT |
| 2+ departments (IT + Sales) | Tạo wiki pages ở **mỗi** department scope | LLM chạy 1 lần, commit nội dung vào từng scope |
| Thuộc Project | `scope_type=project, scope_id=proj_uuid` | Hiện hành, project > department |

### Multi-department Source
- Pipeline chạy LLM **1 lần duy nhất** để sinh nội dung
- Ở bước COMMIT, lặp qua từng department → commit cùng nội dung vào mỗi scope
- Chi phí LLM giữ nguyên, chỉ tốn thêm vài row DB
- Mỗi department scope có bản wiki riêng biệt, có thể tiến hóa độc lập nếu sau này có source khác contribute vào

### KB Reconciliation
- Semantic search chỉ tìm kiếm trong **cùng scope** (đã hoạt động đúng nhờ `_scope_filter`)
- Source IT → chỉ thấy wiki pages scope=department/IT → không bao giờ merge với Sales
- Cùng slug `concept/quy-trinh-nghi-phep` có thể tồn tại song song ở IT scope và Sales scope

### Department Change Flow
Khi admin đổi department của source (PATCH `/sources/{id}`):
1. Gọi `detach_source_from_wiki(source_id)` — gỡ source khỏi wiki pages cũ
2. Pages chỉ có 1 source → bị xóa luôn
3. Pages có nhiều sources → chỉ gỡ source_id, page vẫn sống
4. Regenerate index ở scope cũ (dọn dẹp)
5. Re-enqueue MRP pipeline → tạo wiki pages mới ở department scope mới
6. Đặt `source.status = "processing"` để UI biết đang chạy lại
7. UI hiện confirm dialog: *"Đổi phòng ban sẽ chạy lại quá trình phân tích AI. Wiki pages cũ sẽ được cập nhật sang phòng ban mới. Tiếp tục?"*

---

## Proposed Changes

### Backend Model
#### [MODIFY] [models.py](file:///d:/arkon/app/database/models.py)
- Thêm `DEPARTMENT = "department"` vào `ScopeType` enum (dòng 39-40)

---

### MRP Pipeline — Resolve department scope + multi-scope commit
#### [MODIFY] [pipeline.py](file:///d:/arkon/app/ai/mrp/pipeline.py)

**`run_commit_phase()`** — thay vì dùng trực tiếp `source.scope_type`:
```python
# Resolve effective wiki scopes from source
source_depts = await session.execute(
    select(SourceDepartment.department_id)
    .where(SourceDepartment.source_id == source.id)
)
dept_ids = [r[0] for r in source_depts.all()]

if source.scope_type == "project":
    wiki_scopes = [("project", source.scope_id)]
elif dept_ids:
    wiki_scopes = [("department", did) for did in dept_ids]
else:
    wiki_scopes = [("global", None)]
```

Sau đó lặp: `for scope_type, scope_id in wiki_scopes:` → commit tất cả pages vào mỗi scope. LLM content giữ nguyên, chỉ nhân bản DB rows.

#### [MODIFY] [reducer.py](file:///d:/arkon/app/ai/mrp/reducer.py)
- `reconcile_with_kb()`: thêm cùng logic resolve department scope
- Mỗi scope chạy reconciliation riêng → plan đúng per-scope

---

### Wiki Router — Filter department pages for users
#### [MODIFY] [wiki.py](file:///d:/arkon/app/routers/wiki.py)

**`_build_wiki_scope_filter()`** — cho `own_dept` scope:
```python
if scope_level == "own_dept":
    return or_(
        WikiPage.scope_type == "global",
        WikiPage.scope_id.in_(
            select(ProjectMember.project_id)
            .where(ProjectMember.employee_id == user.id)
        ),
        # NEW: user thấy wiki pages thuộc department mình
        and_(
            WikiPage.scope_type == "department",
            WikiPage.scope_id == user.department_id,
        ),
    )
```

**`get_wiki_page()`** — thêm access check:
```python
if page.scope_type == "department" and page.scope_id:
    if user.role != "admin":
        perms = _get_user_permissions(user)
        if "wiki:read:all" not in perms:
            if user.department_id != page.scope_id:
                raise HTTPException(403, "Access denied — this page belongs to another department")
```

---

### Source Edit — Detach + Re-ingest on department change
#### [MODIFY] [sources.py](file:///d:/arkon/app/routers/sources.py)

**`update_source()`** (PATCH) — khi `department_ids` thay đổi và source đã `ready`:
1. So sánh old departments vs new departments
2. Nếu khác → gọi `detach_source_from_wiki(source_id)`
3. Regenerate index ở scope cũ
4. Re-enqueue `ingest_map_reduce_task(source_id)`
5. Đặt `source.status = "processing"`

---

### MCP Tools — Department filter
#### [MODIFY] [tools.py](file:///d:/arkon/app/mcp/tools.py)
- `search_wiki()`: thêm department-scope pages vào search range nếu user thuộc department đó
- `list_wiki_pages()`: tương tự
- `read_wiki_page()`: thêm access check cho department scope

---

### Frontend
#### Đã có sẵn (không cần sửa)
- `ScopeBadge` component đã hỗ trợ `scope_type = "department"` (badge indigo) ✅
- `WikiPageSummary` response model đã có `scope_type` + `scope_id` fields ✅
- Wiki list/detail pages đã render scope badge tự động ✅

#### [MODIFY] [edit-source-dialog.tsx](file:///d:/arkon/frontend/src/components/knowledge/knowledge-table/edit-source-dialog.tsx)
- Thêm confirm dialog khi đổi department: *"Đổi phòng ban sẽ chạy lại quá trình phân tích AI. Wiki pages cũ sẽ được cập nhật sang phòng ban mới. Tiếp tục?"*

---

## Verification Plan

### Test Scenarios
1. Upload doc **không có department** → wiki pages ở global scope (không đổi so với hiện tại)
2. Upload doc **gán IT** → wiki pages có `scope_type=department, scope_id=IT_uuid`
3. Upload doc cùng chủ đề **gán Sales** → tạo page riêng ở Sales scope, **không merge** với IT
4. Upload doc **gán IT + Sales** → tạo wiki pages ở cả 2 scope
5. Login user IT → thấy global + IT wiki. **Không thấy** Sales wiki
6. Login admin → thấy tất cả
7. Đổi department IT → Sales → wiki cũ bị xóa, pipeline chạy lại, wiki mới ở Sales scope
8. Source thuộc Project → wiki scope = project (ưu tiên project, bỏ qua department)
