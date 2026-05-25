import React from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Source, KnowledgeType, Department } from "./types";

export function EditSourceDialog({
  source,
  types,
  departments,
  onClose,
  onSaved,
}: {
  source: Source;
  types: KnowledgeType[];
  departments: Department[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [title, setTitle] = React.useState(source.title);
  const [typeId, setTypeId] = React.useState(source.knowledge_type_id || "");
  const [selectedDepts, setSelectedDepts] = React.useState<string[]>(source.department_ids || []);
  const originalDepts = React.useRef<string[]>(source.department_ids || []);

  React.useEffect(() => {
    setSelectedDepts(source.department_ids || []);
    originalDepts.current = source.department_ids || [];
  }, [source.id]);
  const [scopeType, setScopeType] = React.useState(source.scope_type || "global");
  const [scopeId, setScopeId] = React.useState(source.scope_id || "");
  const [projects, setProjects] = React.useState<{ id: string; name: string }[]>([]);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState("");
  const [pendingConfirm, setPendingConfirm] = React.useState(false);

  // Scope and departments decide where wiki pages get committed; the worker
  // reads them at commit time. Allowing edits mid-pipeline could land pages
  // in the wrong scope (visibility leak). Backend enforces this too — UI just
  // makes it obvious so users don't get a 409 on save.
  const inFlight = ["pending", "processing", "awaiting_approval", "plan_ready"].includes(
    source.status,
  );

  // Fetch projects for workspace scope picker
  React.useEffect(() => {
    api<{ id: string; name: string }[]>("/api/projects")
      .then((data) => setProjects(Array.isArray(data) ? data : []))
      .catch(() => setProjects([]));
  }, []);

  const toggleDept = (deptId: string) => {
    setSelectedDepts((prev) =>
      prev.includes(deptId) ? prev.filter((d) => d !== deptId) : [...prev, deptId]
    );
  };

  const deptChanged = () => {
    const orig = new Set(originalDepts.current);
    const cur = new Set(selectedDepts);
    return orig.size !== cur.size || selectedDepts.some((d) => !orig.has(d));
  };

  const doSave = async () => {
    setSaving(true);
    setError("");
    setPendingConfirm(false);
    try {
      // While in-flight, only send cosmetic fields. Backend rejects any
      // scope/dept payload in those statuses, even if the value is unchanged.
      const body: Record<string, unknown> = {
        title: title || undefined,
        knowledge_type_id: typeId || null,
      };
      if (!inFlight) {
        body.department_ids = selectedDepts;
        body.scope_type = scopeType;
        body.scope_id = scopeType === "global" ? null : (scopeId || null);
      }
      await api(`/api/sources/${source.id}`, { method: "PATCH", body });
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  const handleSave = () => {
    if (source.status === "ready" && deptChanged()) {
      setPendingConfirm(true);
    } else {
      doSave();
    }
  };

  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-xl">Edit Document</DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-4 mt-2">
          <div className="flex flex-col gap-1.5">
            <Label>Title</Label>
            <Input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="bg-background"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Knowledge Type</Label>
            <Select value={typeId} onValueChange={(v) => setTypeId(v ?? "")}>
              <SelectTrigger className="bg-background">
                {typeId ? (() => { const t = types.find(x => x.id === typeId); return t ? (
                  <div className="flex items-center gap-2">
                    <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: t.color }} />
                    <span>{t.name}</span>
                  </div>
                ) : <SelectValue placeholder="No type" />; })() : <SelectValue placeholder="No type" />}
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="">No type</SelectItem>
                {types.map((t) => (
                  <SelectItem key={t.id} value={t.id}>
                    <div className="flex items-center gap-2">
                      <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: t.color }} />
                      {t.name}
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {inFlight && (
            <div className="rounded-lg border border-amber-300 bg-amber-50 dark:border-amber-700 dark:bg-amber-950/30 px-3 py-2 text-xs text-amber-800 dark:text-amber-300 flex items-start gap-1.5">
              <span className="material-symbols-outlined shrink-0" style={{ fontSize: 14, marginTop: 1 }}>info</span>
              <span>
                Tài liệu đang được xử lý. Bạn có thể đổi <strong>tên</strong> và <strong>loại tri thức</strong>, nhưng <strong>phòng ban</strong> và <strong>phạm vi</strong> chỉ đổi được sau khi xử lý xong (hoặc thất bại) — để tránh wiki page bị ghi nhầm phạm vi.
              </span>
            </div>
          )}

          {/* Multi-department selection */}
          <div className="flex flex-col gap-1.5">
            <Label className={inFlight ? "text-muted-foreground" : ""}>Departments</Label>
            <p className="text-xs text-muted-foreground">
              Select which departments can access this document. Leave empty for global access.
            </p>
            <div className={`border rounded-lg p-2 max-h-40 overflow-y-auto bg-background ${inFlight ? "opacity-60" : ""}`}>
              {departments.length === 0 ? (
                <span className="text-xs text-muted-foreground">No departments available</span>
              ) : (
                departments.map((d) => (
                  <label
                    key={d.id}
                    className={`flex items-center gap-2 px-2 py-1.5 rounded hover:bg-muted ${inFlight ? "cursor-not-allowed" : "cursor-pointer"}`}
                  >
                    <input
                      type="checkbox"
                      checked={selectedDepts.includes(d.id)}
                      onChange={() => toggleDept(d.id)}
                      disabled={inFlight}
                      className="rounded border-border"
                    />
                    <span className="text-sm">{d.name}</span>
                  </label>
                ))
              )}
            </div>
            {selectedDepts.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1">
                {selectedDepts.map((id) => {
                  const name = departments.find((d) => d.id === id)?.name ?? id;
                  return (
                    <span
                      key={id}
                      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-primary/10 text-primary"
                    >
                      {name}
                      <button
                        type="button"
                        onClick={() => toggleDept(id)}
                        className="hover:text-destructive"
                      >
                        ×
                      </button>
                    </span>
                  );
                })}
              </div>
            )}
          </div>

          {/* Visibility / Scope */}
          <div className="flex flex-col gap-1.5">
            <Label className={inFlight ? "text-muted-foreground" : ""}>Visibility</Label>
            <Select value={scopeType} disabled={inFlight} onValueChange={(v) => {
              const val = v ?? "global";
              setScopeType(val);
              if (val === "global") setScopeId("");
            }}>
              <SelectTrigger className="bg-background">
                <div className="flex items-center gap-2">
                  <span className="material-symbols-outlined" style={{ fontSize: 14 }}>
                    {scopeType === "global" ? "public" : "folder_special"}
                  </span>
                  <span className="capitalize">{scopeType === "project" ? "Workspace" : scopeType}</span>
                </div>
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="global">
                  <div className="flex items-center gap-2">
                    <span className="material-symbols-outlined" style={{ fontSize: 14 }}>public</span>
                    Global
                  </div>
                </SelectItem>
                <SelectItem value="project">
                  <div className="flex items-center gap-2">
                    <span className="material-symbols-outlined" style={{ fontSize: 14 }}>folder_special</span>
                    Workspace
                  </div>
                </SelectItem>
              </SelectContent>
            </Select>
            {scopeType === "global" && (
              <p className="text-xs text-amber-600 dark:text-amber-400 flex items-start gap-1.5 mt-0.5">
                <span className="material-symbols-outlined shrink-0" style={{ fontSize: 13, marginTop: 1 }}>warning</span>
                Document content will be compiled into the shared wiki and visible to all employees — including those without access to the original file. Only use Global if the content is not sensitive.
              </p>
            )}
          </div>

          {scopeType === "project" && (
            <div className="flex flex-col gap-1.5">
              <Label className={inFlight ? "text-muted-foreground" : ""}>Target Workspace</Label>
              <Select value={scopeId} disabled={inFlight} onValueChange={(v) => setScopeId(v ?? "")}>
                <SelectTrigger className="bg-background">
                  <span>{scopeId ? (projects.find(p => p.id === scopeId)?.name ?? "Select...") : "Select workspace..."}</span>
                </SelectTrigger>
                <SelectContent>
                  {projects.map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          {pendingConfirm && (
            <div className="rounded-lg border border-amber-300 bg-amber-50 dark:border-amber-700 dark:bg-amber-950/30 p-3 flex flex-col gap-3">
              <p className="text-sm text-amber-800 dark:text-amber-300">
                Đổi phòng ban sẽ chạy lại quá trình phân tích AI. Wiki pages cũ sẽ được cập nhật sang phòng ban mới. Tiếp tục?
              </p>
              <div className="flex justify-end gap-2">
                <Button variant="outline" size="sm" onClick={() => setPendingConfirm(false)}>Huỷ</Button>
                <Button size="sm" onClick={doSave} className="bg-amber-600 hover:bg-amber-700 text-white">
                  Xác nhận
                </Button>
              </div>
            </div>
          )}

          {error && (
            <p className="text-destructive text-sm bg-destructive/10 px-3 py-2 rounded-lg">
              {error}
            </p>
          )}

          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" onClick={onClose}>Cancel</Button>
            <Button
              disabled={saving || pendingConfirm}
              onClick={handleSave}
              className="bg-primary text-primary-foreground hover:bg-primary/90"
            >
              {saving ? (
               <span className="flex items-center gap-2">
                 <span className="material-symbols-outlined animate-spin text-sm">progress_activity</span>
                 Saving...
               </span>
              ) : "Save"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
