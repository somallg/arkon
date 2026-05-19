"use client";

import React from "react";
import { AiCheckItem, AiCheckResults, AiCheckStatus } from "@/types/wiki";

type Props = {
  status: AiCheckStatus | string;
  results: AiCheckResults | null;
};

function statusBadge(status: AiCheckStatus | string) {
  switch (status) {
    case "passed":
      return { label: "AI: all clear", classes: "bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-200", icon: "check_circle" };
    case "warned":
      return { label: "AI: needs attention", classes: "bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-200", icon: "warning" };
    case "failed":
      return { label: "AI: critical flags", classes: "bg-rose-100 dark:bg-rose-900/40 text-rose-700 dark:text-rose-200", icon: "report" };
    case "running":
      return { label: "AI: running…", classes: "bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-200", icon: "progress_activity" };
    case "skipped":
      return { label: "AI: skipped", classes: "bg-muted text-muted-foreground", icon: "skip_next" };
    case "pending":
    default:
      return { label: "AI: queued", classes: "bg-muted text-muted-foreground", icon: "schedule" };
  }
}

function checkIcon(check: AiCheckItem): { icon: string; cls: string } {
  switch (check.status) {
    case "pass":
      return { icon: "check_circle", cls: "text-emerald-600 dark:text-emerald-400" };
    case "warn":
      return { icon: "warning", cls: "text-amber-600 dark:text-amber-400" };
    case "fail":
      return { icon: "cancel", cls: "text-rose-600 dark:text-rose-400" };
    case "skipped":
    default:
      return { icon: "remove_circle", cls: "text-muted-foreground" };
  }
}

function formatMatch(m: AiCheckItem["matches"][number]): string {
  if (typeof m === "string") return m;
  if (m.slug && typeof m.score === "number") {
    return `${m.slug} (${(m.score * 100).toFixed(0)}%)`;
  }
  if (m.slug) return m.slug;
  if (m.snippet) return m.line ? `L${m.line}: ${m.snippet}` : m.snippet;
  return JSON.stringify(m);
}

export function WikiAiCheckPanel({ status, results }: Props) {
  const [expanded, setExpanded] = React.useState(false);
  const badge = statusBadge(status);
  const summary = results?.summary;
  const allChecks = results?.checks || [];
  const flagged = allChecks.filter((c) => c.status === "warn" || c.status === "fail");
  const passed = allChecks.filter((c) => c.status === "pass");
  const skipped = allChecks.filter((c) => c.status === "skipped");
  const [showPassed, setShowPassed] = React.useState(false);

  return (
    <div className="border-t border-border bg-muted/30">
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center gap-2 px-4 py-2 text-xs hover:bg-muted/50 transition-colors"
      >
        <span
          className={`material-symbols-outlined ${
            status === "running" ? "animate-spin" : ""
          }`}
          style={{ fontSize: 16 }}
        >
          {badge.icon}
        </span>
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wide ${badge.classes}`}>
          {badge.label}
        </span>
        {summary && (
          <span className="text-muted-foreground tabular-nums">
            {summary.pass} pass · {summary.warn} warn · {summary.fail} fail
            {summary.skipped > 0 && ` · ${summary.skipped} skipped`}
          </span>
        )}
        <span className="ml-auto material-symbols-outlined text-muted-foreground" style={{ fontSize: 16 }}>
          {expanded ? "expand_less" : "expand_more"}
        </span>
      </button>

      {expanded && (
        <div className="px-4 pb-3 space-y-3">
          {!results ? (
            <p className="text-xs text-muted-foreground italic">
              AI pre-review {status === "running" ? "in progress" : "has not run yet"}.
            </p>
          ) : (
            <>
              {/* Flagged checks first — these need attention */}
              {flagged.length === 0 ? (
                <p className="text-xs text-emerald-700 dark:text-emerald-300">
                  All checks passed.
                </p>
              ) : (
                <ul className="space-y-1.5">
                  {flagged.map((c) => {
                    const ico = checkIcon(c);
                    return (
                      <li key={c.id} className="flex gap-2 text-xs">
                        <span
                          className={`material-symbols-outlined shrink-0 mt-0.5 ${ico.cls}`}
                          style={{ fontSize: 14 }}
                        >
                          {ico.icon}
                        </span>
                        <div className="flex-1 min-w-0">
                          <p className="font-mono text-[11px] text-muted-foreground">
                            [{c.layer}] {c.id}
                          </p>
                          {c.message && <p>{c.message}</p>}
                          {c.matches.length > 0 && (
                            <ul className="mt-0.5 ml-2 text-muted-foreground text-[11px] list-disc list-inside">
                              {c.matches.slice(0, 5).map((m, i) => (
                                <li key={i} className="truncate">{formatMatch(m)}</li>
                              ))}
                              {c.matches.length > 5 && (
                                <li className="italic">+{c.matches.length - 5} more…</li>
                              )}
                            </ul>
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}

              {/* Passed + skipped checks — collapsed by default. Reviewers
                  can expand to see what the AI already verified. */}
              {(passed.length > 0 || skipped.length > 0) && (
                <div>
                  <button
                    type="button"
                    onClick={() => setShowPassed((s) => !s)}
                    className="text-[11px] text-muted-foreground hover:text-foreground flex items-center gap-1"
                  >
                    <span
                      className="material-symbols-outlined"
                      style={{ fontSize: 14 }}
                    >
                      {showPassed ? "expand_less" : "expand_more"}
                    </span>
                    {showPassed ? "Hide" : "Show"} {passed.length} pass
                    {skipped.length > 0 && ` · ${skipped.length} skipped`}
                  </button>
                  {showPassed && (
                    <ul className="mt-1.5 space-y-1 pl-1">
                      {[...passed, ...skipped].map((c) => {
                        const ico = checkIcon(c);
                        return (
                          <li
                            key={c.id}
                            className="flex gap-2 text-[11px] text-muted-foreground"
                          >
                            <span
                              className={`material-symbols-outlined shrink-0 mt-0.5 ${ico.cls}`}
                              style={{ fontSize: 12 }}
                            >
                              {ico.icon}
                            </span>
                            <div className="flex-1 min-w-0">
                              <span className="font-mono">
                                [{c.layer}] {c.id}
                              </span>
                              {c.message && (
                                <span className="ml-1 italic">— {c.message}</span>
                              )}
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              )}
            </>
          )}
          <p className="text-[10px] text-muted-foreground italic pt-1">
            AI checks are advisory — they do not block approval.
          </p>
        </div>
      )}
    </div>
  );
}
