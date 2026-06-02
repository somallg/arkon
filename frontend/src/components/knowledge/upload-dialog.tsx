"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { api, apiUpload } from "@/lib/api";
import { Button } from "@/components/ui/button";
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

type KnowledgeType = {
  id: string;
  slug: string;
  name: string;
  color: string;
};

type Department = {
  id: string;
  name: string;
};

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  types: KnowledgeType[];
  departments: Department[];
  onUploaded: () => void;
};

const ACCEPTED_EXTENSIONS = ["pdf", "docx", "doc", "xlsx", "xls", "csv", "txt", "md", "pptx"];
const ACCEPTED_MIMES = [
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.ms-excel",
  "text/plain",
  "text/csv",
  "text/markdown",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
];
const ACCEPT_STRING = ACCEPTED_EXTENSIONS.map((e) => `.${e}`).join(",");

function getFileExtension(name: string): string {
  return (name.split(".").pop() || "").toLowerCase();
}

function validateFile(f: File): string | null {
  const ext = getFileExtension(f.name);
  if (!ACCEPTED_EXTENSIONS.includes(ext) && !ACCEPTED_MIMES.includes(f.type)) {
    return `Unsupported file type ".${ext}". Accepted: ${ACCEPTED_EXTENSIONS.join(", ")}`;
  }
  if (f.size > 50 * 1024 * 1024) {
    return "File too large. Maximum size is 50 MB.";
  }
  return null;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function UploadDialog({ open, onOpenChange, types, departments, onUploaded }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [typeId, setTypeId] = useState("");
  const [selectedDepts, setSelectedDepts] = useState<string[]>([]);
  const [scopeType, setScopeType] = useState("global");
  const [scopeId, setScopeId] = useState("");
  const [keepVerbatim, setKeepVerbatim] = useState(false);
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const toggleDept = (deptId: string) => {
    setSelectedDepts((prev) =>
      prev.includes(deptId) ? prev.filter((d) => d !== deptId) : [...prev, deptId]
    );
  };

  useEffect(() => {
    if (!open) return;
    api<{ id: string; name: string }[]>("/api/projects")
      .then((data) => setProjects(Array.isArray(data) ? data : []))
      .catch(() => setProjects([]));
  }, [open]);

  const handleFile = useCallback((f: File) => {
    const validationError = validateFile(f);
    if (validationError) {
      setError(validationError);
      setFile(null);
      return;
    }
    setError("");
    setFile(f);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setDragOver(false);
      const f = e.dataTransfer.files?.[0];
      if (f) handleFile(f);
    },
    [handleFile]
  );

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(false);
  }, []);

  const handleUpload = async () => {
    if (!file) return;
    setUploading(true);
    setError("");

    try {
      const formData = new FormData();
      formData.append("file", file);
      if (typeId) formData.append("knowledge_type_id", typeId);
      
      if (selectedDepts.length > 0) {
        formData.append("department_ids", selectedDepts.join(","));
      }
      formData.append("scope_type", scopeType);
      if (scopeType !== "global" && scopeId) {
        formData.append("scope_id", scopeId);
      }
      if (keepVerbatim) formData.append("preserve_verbatim", "true");

      await apiUpload("/api/sources/upload", formData);
      onUploaded();
      onOpenChange(false);
      setFile(null);
      setTypeId("");
      setSelectedDepts([]);
      setScopeType("global");
      setScopeId("");
      setKeepVerbatim(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-xl font-heading">Upload Document</DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-4 mt-2 overflow-hidden">
          {/* Drag & drop zone */}
          <div className="flex flex-col gap-2 min-w-0">
            <Label>File</Label>
            <div
              onDrop={handleDrop}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onClick={() => fileInputRef.current?.click()}
              className={`
                relative flex flex-col items-center justify-center gap-2 px-4 py-6
                rounded-xl border-2 border-dashed cursor-pointer transition-all duration-200 overflow-hidden
                ${dragOver
                  ? "border-primary bg-primary/5 scale-[1.01]"
                  : file
                    ? "border-primary/40 bg-primary/[0.02]"
                    : "border-border hover:border-primary/40 hover:bg-accent/30"
                }
              `}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept={ACCEPT_STRING}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) handleFile(f);
                  // Reset input so same file can be re-selected
                  e.target.value = "";
                }}
                className="hidden"
              />

              {file ? (
                /* File selected state */
                <div className="flex items-center gap-3 w-full min-w-0">
                  <div className="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center shrink-0">
                    <span className="material-symbols-outlined text-primary" style={{ fontSize: 20 }}>
                      description
                    </span>
                  </div>
                  <div className="flex-1 min-w-0 overflow-hidden">
                    <p className="text-sm font-medium text-foreground truncate">{file.name}</p>
                    <p className="text-xs text-muted-foreground">
                      {formatFileSize(file.size)} · .{getFileExtension(file.name).toUpperCase()}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setFile(null);
                      setError("");
                    }}
                    className="p-1 rounded-md hover:bg-accent/50 text-muted-foreground hover:text-foreground transition-colors"
                  >
                    <span className="material-symbols-outlined" style={{ fontSize: 16 }}>close</span>
                  </button>
                </div>
              ) : (
                /* Empty state — prompt */
                <>
                  <div className={`w-10 h-10 rounded-full flex items-center justify-center transition-colors ${
                    dragOver ? "bg-primary/15" : "bg-accent/60"
                  }`}>
                    <span className={`material-symbols-outlined transition-colors ${
                      dragOver ? "text-primary" : "text-muted-foreground"
                    }`} style={{ fontSize: 22 }}>
                      upload_file
                    </span>
                  </div>
                  <div className="text-center">
                    <p className="text-sm text-foreground font-medium">
                      {dragOver ? "Drop file here" : "Drag & drop or click to browse"}
                    </p>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      PDF, DOCX, XLSX, CSV, TXT, MD, PPTX · Max 50 MB
                    </p>
                  </div>
                </>
              )}
            </div>
          </div>

          {/* Knowledge Type */}
          <div className="flex flex-col gap-2">
            <Label>Knowledge Type</Label>
            <Select value={typeId} onValueChange={(v) => setTypeId(v ?? "")}>
              <SelectTrigger className="bg-background w-full">
                {typeId ? (() => { const t = types.find(x => x.id === typeId); return t ? (
                  <div className="flex items-center gap-2">
                    <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: t.color }} />
                    <span>{t.name}</span>
                  </div>
                ) : <SelectValue placeholder="Select type (optional)" />; })() : <SelectValue placeholder="Select type (optional)" />}
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="">None</SelectItem>
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

          {/* Verbatim mode — skip wiki, keep original exact */}
          <div className="flex flex-col gap-1.5">
            <label className="flex items-start gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={keepVerbatim}
                onChange={(e) => setKeepVerbatim(e.target.checked)}
                className="rounded border-border mt-0.5"
              />
              <span className="text-sm font-medium text-foreground">
                Keep verbatim — skip wiki generation
              </span>
            </label>
            <p className="text-xs text-muted-foreground ml-6">
              Use for official or high-fidelity documents (contracts, decrees, regulations).
              The document is stored and searched exactly as-is, skipping AI summarization or rewriting.
            </p>
          </div>

          {/* Department access control */}
          <div className="flex flex-col gap-1.5">
            <Label>Departments</Label>
            <p className="text-xs text-muted-foreground">
              Select which departments can access this document. Leave empty for global access.
            </p>
            <div className="border rounded-lg p-2 max-h-40 overflow-y-auto bg-background">
              {departments.length === 0 ? (
                <span className="text-xs text-muted-foreground">No departments available</span>
              ) : (
                departments.map((d) => (
                  <label
                    key={d.id}
                    className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-muted cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      checked={selectedDepts.includes(d.id)}
                      onChange={() => toggleDept(d.id)}
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
                      <button type="button" onClick={() => toggleDept(id)} className="hover:text-destructive">×</button>
                    </span>
                  );
                })}
              </div>
            )}
          </div>

          {/* Visibility / Scope */}
          <div className="flex flex-col gap-2">
            <Label>Visibility</Label>
            <Select value={scopeType} onValueChange={(v) => {
              const val = v ?? "global";
              setScopeType(val);
              if (val === "global") setScopeId("");
            }}>
              <SelectTrigger className="bg-background w-full">
                <div className="flex items-center gap-2">
                  <span className="material-symbols-outlined" style={{ fontSize: 14 }}>
                    {scopeType === "global" ? "public" : "folder_special"}
                  </span>
                  <span className="capitalize">{scopeType === "project" ? "Workspace" : scopeType}</span>
                </div>
              </SelectTrigger>
              <SelectContent className="min-w-[220px]">
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
                Document content will be compiled into the shared wiki and visible to all employees — including those without access to the original file. Only upload if the content is not sensitive.
              </p>
            )}
          </div>

          {scopeType === "project" && (
            <div className="flex flex-col gap-1.5">
              <Label>Target Workspace</Label>
              <Select value={scopeId} onValueChange={(v) => setScopeId(v ?? "")}>
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

          {error && (
            <p className="text-destructive text-sm bg-destructive/10 px-3 py-2 rounded-lg flex items-center gap-2">
              <span className="material-symbols-outlined" style={{ fontSize: 16 }}>error</span>
              {error}
            </p>
          )}

          <div className="flex justify-end gap-2 mt-2">
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              disabled={!file || uploading}
              onClick={handleUpload}
              className="bg-primary text-primary-foreground hover:bg-primary/90"
            >
              {uploading ? (
                <span className="flex items-center gap-2">
                  <span className="material-symbols-outlined animate-spin text-sm">
                    progress_activity
                  </span>
                  Uploading...
                </span>
              ) : (
                "Upload"
              )}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

