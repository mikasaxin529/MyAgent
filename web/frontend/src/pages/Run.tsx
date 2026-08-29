import { FormEvent, useEffect, useRef, useState } from "react";
import {
  RunSocket,
  type AuditEntry,
  type BlackboardData,
  type ApprovalRequest,
  type DoneSummary,
} from "../api";

const EVENT_COLORS: Record<string, string> = {
  agent_step: "text-indigo-300 border-indigo-500/40 bg-indigo-500/10",
  llm_call: "text-violet-300 border-violet-500/40 bg-violet-500/10",
  tool_call: "text-emerald-300 border-emerald-500/40 bg-emerald-500/10",
  approval: "text-amber-300 border-amber-500/40 bg-amber-500/10",
};

type Status = "idle" | "connecting" | "running" | "awaiting_approval" | "done" | "error";

export default function RunPage() {
  const [task, setTask] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [blackboard, setBlackboard] = useState<BlackboardData | null>(null);
  const [approval, setApproval] = useState<ApprovalRequest | null>(null);
  const [summary, setSummary] = useState<DoneSummary | null>(null);
  const [decisionComment, setDecisionComment] = useState("");
  const [editArgs, setEditArgs] = useState("{}");
  const socketRef = useRef<RunSocket | null>(null);

  useEffect(() => {
    return () => socketRef.current?.close();
  }, []);

  function reset() {
    setAudit([]);
    setBlackboard(null);
    setApproval(null);
    setSummary(null);
    setDecisionComment("");
    setEditArgs("{}");
  }

  function onStart(e: FormEvent) {
    e.preventDefault();
    if (!task.trim()) return;
    reset();
    setStatus("connecting");

    const sock = new RunSocket({
      onOpen: () => {
        setStatus("running");
        sock.send({ task });
      },
      onFrame: (frame) => {
        switch (frame.type) {
          case "audit":
            setAudit((prev) => [...prev, frame.entry]);
            break;
          case "blackboard":
            setBlackboard(frame.data);
            break;
          case "approval_request":
            setApproval({
              action: frame.action,
              args: frame.args,
              reason: frame.reason,
            });
            setEditArgs(JSON.stringify(frame.args, null, 2));
            setStatus("awaiting_approval");
            break;
          case "done":
            setBlackboard(frame.blackboard);
            setSummary(frame.summary);
            setApproval(null);
            setStatus("done");
            socketRef.current?.close();
            break;
        }
      },
      onClose: () => {
        if (status !== "done") setStatus((s) => (s === "awaiting_approval" ? s : "idle"));
      },
      onError: () => setStatus("error"),
    });
    socketRef.current = sock;
    sock.connect();
  }

  function sendDecision(decision: "approve" | "reject" | "edit") {
    let args: Record<string, unknown> = {};
    if (decision === "edit") {
      try {
        args = JSON.parse(editArgs);
      } catch {
        alert("Edit args is not valid JSON");
        return;
      }
    }
    socketRef.current?.send({ decision, comment: decisionComment, args });
    setApproval(null);
    setDecisionComment("");
    setStatus("running");
  }

  return (
    <div className="h-full flex flex-col">
      <header className="px-6 py-4 border-b border-[var(--line)]">
        <h1 className="text-lg font-semibold">Run</h1>
        <p className="text-xs text-[var(--text3)]">
          Stream an agent run over WebSocket with a live audit timeline and blackboard.
        </p>
      </header>

      <form onSubmit={onStart} className="px-6 py-4 border-b border-[var(--line)] flex gap-3 items-center">
        <input
          value={task}
          onChange={(e) => setTask(e.target.value)}
          placeholder="Describe a task for the agent…"
          className="flex-1 px-4 py-2.5 rounded-md bg-[var(--sunken)] border border-[var(--line)] text-sm focus:outline-none focus:border-indigo-500"
        />
        <button
          type="submit"
          disabled={!task.trim() || status === "running" || status === "connecting" || status === "awaiting_approval"}
          className="px-5 py-2.5 rounded-md bg-indigo-600 hover:bg-indigo-500 disabled:bg-[var(--panel)] disabled:text-[var(--text3)] text-white text-sm font-medium"
        >
          {status === "running" || status === "connecting" ? "Running…" : "Start"}
        </button>
        <StatusBadge status={status} />
      </form>

      <div className="flex-1 grid grid-cols-1 lg:grid-cols-[1fr_1fr] gap-px bg-[var(--line)] overflow-hidden">
        <Timeline audit={audit} />
        <BlackboardPanel
          blackboard={blackboard}
          summary={summary}
          status={status}
        />
      </div>

      {approval && (
        <ApprovalModal
          approval={approval}
          comment={decisionComment}
          setComment={setDecisionComment}
          editArgs={editArgs}
          setEditArgs={setEditArgs}
          onDecide={sendDecision}
        />
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: Status }) {
  const map: Record<Status, string> = {
    idle: "bg-[var(--panel)] text-[var(--text2)]",
    connecting: "bg-amber-500/15 text-amber-300",
    running: "bg-indigo-500/15 text-indigo-300",
    awaiting_approval: "bg-amber-500/15 text-amber-300",
    done: "bg-emerald-500/15 text-emerald-300",
    error: "bg-rose-500/15 text-rose-300",
  };
  const label = status.replace(/_/g, " ");
  return (
    <span className={`px-3 py-1 rounded-full text-[11px] font-medium ${map[status]}`}>
      {label}
    </span>
  );
}

function Timeline({ audit }: { audit: AuditEntry[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight });
  }, [audit]);

  return (
    <section className="flex flex-col bg-[var(--ground)] min-h-0">
      <div className="px-4 py-2 text-xs uppercase tracking-wider text-[var(--text3)] border-b border-[var(--line)]">
        Audit Timeline
      </div>
      <div ref={ref} className="flex-1 overflow-auto px-4 py-3 space-y-2">
        {audit.length === 0 && (
          <div className="text-[var(--text3)] text-sm py-6 text-center">
            Waiting for audit frames…
          </div>
        )}
        {audit.map((entry, i) => (
          <div
            key={i}
            className={`px-3 py-2 rounded-md border text-xs ${
              EVENT_COLORS[entry.event] ?? "text-[var(--text2)] border-[var(--line)] bg-[var(--sunken)]"
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono uppercase tracking-wider">{entry.event}</span>
              <span className="text-[var(--text3)] text-[10px]">{entry.actor}</span>
            </div>
            <div className="mt-1 text-[var(--text2)] text-[11px] break-words">
              {JSON.stringify(entry.detail)}
            </div>
            <div className="mt-1 text-[var(--text3)] text-[10px] font-mono">
              {entry.timestamp} · {entry.trace_id}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function BlackboardPanel({
  blackboard,
  summary,
  status,
}: {
  blackboard: BlackboardData | null;
  summary: DoneSummary | null;
  status: Status;
}) {
  const empty = !blackboard;
  return (
    <section className="flex flex-col bg-[var(--ground)] min-h-0 overflow-auto">
      <div className="px-4 py-2 text-xs uppercase tracking-wider text-[var(--text3)] border-b border-[var(--line)]">
        Blackboard
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-px bg-[var(--line)] flex-1">
        <Panel title="Plan">
          {empty || blackboard.plan.length === 0 ? (
            <Empty />
          ) : (
            <ol className="list-decimal list-inside space-y-1 text-sm text-[var(--text2)]">
              {blackboard.plan.map((step, i) => (
                <li key={i}>{step}</li>
              ))}
            </ol>
          )}
        </Panel>
        <Panel title="Code Diff" mono>
          {empty || !blackboard.code_diff ? <Empty /> : (
            <pre className="text-[11px] text-[var(--ok)] whitespace-pre-wrap break-all">
              {blackboard.code_diff}
            </pre>
          )}
        </Panel>
        <Panel title="Review">
          {empty || !blackboard.review ? <Empty /> : (
            <pre className="text-sm text-[var(--text2)] whitespace-pre-wrap">{blackboard.review}</pre>
          )}
        </Panel>
        <Panel title="Test Result">
          {empty || !blackboard.test_result ? <Empty /> : (
            <pre className="text-sm text-[var(--text2)] whitespace-pre-wrap">{blackboard.test_result}</pre>
          )}
        </Panel>
      </div>
      {status === "done" && summary && (
        <div className="px-4 py-3 border-t border-[var(--line)] bg-[var(--panel)] text-xs">
          <div className="text-[var(--text2)] mb-1">
            Done · <span className="text-[var(--ok)]">{summary.total_events}</span> events
          </div>
          <div className="flex flex-wrap gap-2">
            {Object.entries(summary.by_event).map(([k, v]) => (
              <span key={k} className="px-2 py-0.5 rounded bg-[var(--panel)] text-[var(--text2)] text-[10px]">
                {k}: {v}
              </span>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function Panel({
  title,
  children,
  mono,
}: {
  title: string;
  children: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="bg-[var(--ground)] p-3 flex flex-col min-h-0">
      <div className="text-[10px] uppercase tracking-wider text-[var(--text3)] mb-2">{title}</div>
      <div className={`flex-1 overflow-auto ${mono ? "font-mono" : ""}`}>{children}</div>
    </div>
  );
}

function Empty() {
  return <div className="text-[var(--text3)] text-xs">—</div>;
}

function ApprovalModal({
  approval,
  comment,
  setComment,
  editArgs,
  setEditArgs,
  onDecide,
}: {
  approval: ApprovalRequest;
  comment: string;
  setComment: (v: string) => void;
  editArgs: string;
  setEditArgs: (v: string) => void;
  onDecide: (d: "approve" | "reject" | "edit") => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-lg rounded-xl border border-[var(--line)] bg-[var(--sunken)] shadow-2xl">
        <div className="px-5 py-4 border-b border-[var(--line)]">
          <div className="flex items-center gap-2">
            <span className="px-2 py-0.5 rounded bg-amber-500/15 text-amber-300 text-[10px] uppercase tracking-wider">
              Approval Required
            </span>
            <span className="font-mono text-sm text-[var(--text)]">{approval.action}</span>
          </div>
        </div>
        <div className="px-5 py-4 space-y-3">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--text3)] mb-1">Reason</div>
            <div className="text-sm text-[var(--text2)]">{approval.reason}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--text3)] mb-1">Args</div>
            <pre className="px-3 py-2 rounded bg-[var(--ground)] border border-[var(--line)] text-[11px] text-[var(--text2)] overflow-auto max-h-40">
              {JSON.stringify(approval.args, null, 2)}
            </pre>
          </div>
          <div>
            <label className="text-[10px] uppercase tracking-wider text-[var(--text3)] block mb-1">
              Comment
            </label>
            <input
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="optional note…"
              className="w-full px-3 py-2 rounded-md bg-[var(--ground)] border border-[var(--line)] text-sm focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div>
            <label className="text-[10px] uppercase tracking-wider text-[var(--text3)] block mb-1">
              Edit args (JSON, used only on "Edit")
            </label>
            <textarea
              value={editArgs}
              onChange={(e) => setEditArgs(e.target.value)}
              rows={5}
              className="w-full px-3 py-2 rounded-md bg-[var(--ground)] border border-[var(--line)] text-xs font-mono focus:outline-none focus:border-indigo-500"
            />
          </div>
        </div>
        <div className="px-5 py-4 border-t border-[var(--line)] flex justify-end gap-2">
          <button
            onClick={() => onDecide("reject")}
            className="px-4 py-2 rounded-md border border-[var(--line)] text-[var(--text2)] hover:bg-[var(--panel)] text-sm"
          >
            Reject
          </button>
          <button
            onClick={() => onDecide("edit")}
            className="px-4 py-2 rounded-md border border-indigo-500/40 text-indigo-300 hover:bg-indigo-500/10 text-sm"
          >
            Edit
          </button>
          <button
            onClick={() => onDecide("approve")}
            className="px-4 py-2 rounded-md bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium"
          >
            Approve
          </button>
        </div>
      </div>
    </div>
  );
}
