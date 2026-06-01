"use client";

import { useEffect, useState } from "react";
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

type Department = { id: string; name: string };
type Role = { id: string; name: string };
type Employee = {
  id: string;
  name: string;
  email: string;
  role: string;
  global_role: string;
  department_ids: string[];
};

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  employee: Employee | null; // null = create mode
  departments: Department[];
  roles?: Role[];
  onSaved: () => void;
};

export function EmployeeDialog({
  open,
  onOpenChange,
  employee,
  departments,
  roles = [],
  onSaved,
}: Props) {
  const isEdit = !!employee;
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [globalRole, setGlobalRole] = useState("viewer");
  const [deptIds, setDeptIds] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [localDepartments, setLocalDepartments] = useState<Department[]>([]);
  const [inlinePrompt, setInlinePrompt] = useState({
    open: false,
    title: "",
    label: "",
    value: "",
    saving: false,
    error: "",
    onSubmit: async (val: string) => {},
  });

  useEffect(() => {
    setLocalDepartments(departments);
  }, [departments]);

  const handleCreateDepartment = () => {
    setInlinePrompt({
      open: true,
      title: "Create Department",
      label: "Department Name",
      value: "",
      saving: false,
      error: "",
      onSubmit: async (val) => {
        const newDept = await api<Department>("/api/departments", {
          method: "POST",
          body: { name: val, description: "" }
        });
        setLocalDepartments(prev => [...prev, newDept]);
        setDeptIds(prev => prev.includes(newDept.id) ? prev : [...prev, newDept.id]);
        setInlinePrompt(p => ({ ...p, open: false }));
      }
    });
  };

  const submitInlinePrompt = async () => {
    if (!inlinePrompt.value.trim()) return;
    setInlinePrompt(p => ({ ...p, saving: true, error: "" }));
    try {
      await inlinePrompt.onSubmit(inlinePrompt.value.trim());
    } catch (err) {
      setInlinePrompt(p => ({ ...p, saving: false, error: err instanceof Error ? err.message : String(err) }));
    }
  };

  useEffect(() => {
    if (employee) {
      setName(employee.name);
      setEmail(employee.email);
      setGlobalRole(employee.global_role || "viewer");
      setDeptIds(employee.department_ids ?? []);
      setPassword("");
    } else {
      setName("");
      setEmail("");
      setPassword("");
      setGlobalRole("viewer");
      setDeptIds(departments[0]?.id ? [departments[0].id] : []);
    }
    setError("");
  }, [employee, open, departments]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError("");

    try {
      const body: Record<string, unknown> = {
        name,
        email,
        role: globalRole === "admin" ? "admin" : "employee",
        global_role: globalRole,
        department_ids: deptIds,
      };
      if (password) body.password = password;

      if (isEdit) {
        await api(`/api/employees/${employee.id}`, { method: "PUT", body });
      } else {
        if (!password) {
          setError("Password is required");
          setSaving(false);
          return;
        }
        await api("/api/employees", { method: "POST", body });
      }

      onSaved();
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-xl">
            {isEdit ? "Edit Employee" : "Add Employee"}
          </DialogTitle>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4 mt-2">
          <div className="flex flex-col gap-2">
            <Label htmlFor="emp-name">Name</Label>
            <Input
              id="emp-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              className="bg-background"
            />
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="emp-email">Email</Label>
            <Input
              id="emp-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              className="bg-background"
            />
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="emp-password">
              Password {isEdit && "(leave blank to keep current)"}
            </Label>
            <Input
              id="emp-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={isEdit ? "••••••••" : "Min 8 characters"}
              className="bg-background"
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="flex flex-col gap-2">
              <Label>Role</Label>
              <Select value={globalRole} onValueChange={(v) => v && setGlobalRole(v)}>
                <SelectTrigger className="bg-background">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {roles.map((r) => (
                    <SelectItem key={r.id} value={r.id}>
                      {r.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="flex flex-col gap-2">
              <Label>Departments</Label>
              <Select
                value=""
                onValueChange={(v) => {
                  if (!v) return;
                  if (v === "__new__") {
                    handleCreateDepartment();
                    return;
                  }
                  setDeptIds(prev => prev.includes(v) ? prev : [...prev, v]);
                }}
              >
                <SelectTrigger className="bg-background">
                  <span className="text-muted-foreground">Add department…</span>
                </SelectTrigger>
                <SelectContent className="!w-max min-w-(--anchor-width)">
                  {localDepartments
                    .filter(d => !deptIds.includes(d.id))
                    .map((d) => (
                      <SelectItem key={d.id} value={d.id}>
                        {d.name}
                      </SelectItem>
                    ))}
                  <div className="h-px bg-border my-1 -mx-1" />
                  <SelectItem value="__new__" className="text-primary font-medium focus:text-primary">
                    <span className="flex items-center gap-2">
                      <span className="material-symbols-outlined text-sm">add</span>
                      Create new department...
                    </span>
                  </SelectItem>
                </SelectContent>
              </Select>
              <div className="flex flex-wrap gap-1.5 min-h-7">
                {deptIds.length === 0 ? (
                  <span className="text-xs text-muted-foreground italic">
                    No departments — user will only see global resources
                  </span>
                ) : (
                  deptIds.map((id) => {
                    const d = localDepartments.find(x => x.id === id);
                    return (
                      <span
                        key={id}
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-secondary text-secondary-foreground text-xs"
                      >
                        {d?.name || id}
                        <button
                          type="button"
                          onClick={() => setDeptIds(prev => prev.filter(x => x !== id))}
                          className="hover:text-destructive transition-colors"
                          aria-label={`Remove ${d?.name || id}`}
                        >
                          <span className="material-symbols-outlined text-sm leading-none">close</span>
                        </button>
                      </span>
                    );
                  })
                )}
              </div>
            </div>
          </div>

          {error && (
            <p className="text-destructive text-sm bg-destructive/10 px-3 py-2 rounded-lg">
              {error}
            </p>
          )}

          <div className="flex justify-end gap-2 mt-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={saving}
              className="bg-primary text-primary-foreground hover:bg-primary/90"
            >
              {saving ? "Saving..." : isEdit ? "Update" : "Create"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>

      <Dialog open={inlinePrompt.open} onOpenChange={(o) => setInlinePrompt(p => ({ ...p, open: o }))}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>{inlinePrompt.title}</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-4 py-2">
            <div className="flex flex-col gap-2">
              <Label>{inlinePrompt.label}</Label>
              <Input 
                value={inlinePrompt.value}
                onChange={e => setInlinePrompt(p => ({ ...p, value: e.target.value }))}
                autoFocus
                onKeyDown={e => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    submitInlinePrompt();
                  }
                }}
              />
            </div>
            {inlinePrompt.error && (
              <p className="text-destructive text-sm">{inlinePrompt.error}</p>
            )}
          </div>
          <div className="flex justify-end gap-2 mt-2">
            <Button type="button" variant="outline" onClick={() => setInlinePrompt(p => ({ ...p, open: false }))}>
              Cancel
            </Button>
            <Button 
              disabled={inlinePrompt.saving || !inlinePrompt.value.trim()} 
              onClick={submitInlinePrompt}
            >
              {inlinePrompt.saving ? "Saving..." : "Create"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
