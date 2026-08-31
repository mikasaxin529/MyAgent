import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, MessageSquare } from "lucide-react";
import MessageBubble from "../components/MessageBubble";
import Composer from "../components/Composer";
import Timeline, { type TrackedStep } from "../components/Timeline";
import SessionList from "../components/SessionList";
import {
  chatSSE,
  fetchAgents,
  fetchStoredSessions,
  fetchStoredSession,
  putSession,
  deleteStoredSession,
  migrateLocalSessions,
  getLastAgent,
  setLastAgent,
  type AgentManifest,
  type Message,
  type FileItem as FileItemType,
  type SessionGroup,
  type StoredSession,
} from "../api";

const DEFAULT_AGENT: AgentManifest = {
  id: "general",
  display_name: "通用对话",
  description: "默认助手：搜索、写代码、规划多步任务",
  identity_color: "#615CED",
  placeholder: "输入需求，如「帮我总结7月最新AI资讯」",
};

function genId(): string {
  return `sess_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

export default function ChatPage() {
  const [agents, setAgents] = useState<AgentManifest[]>([DEFAULT_AGENT]);
  const [currentAgent, setCurrentAgent] = useState<AgentManifest>(DEFAULT_AGENT);
  const [sessions, setSessions] = useState<SessionGroup[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string>("");
  const [sessionListOpen, setSessionListOpen] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [steps, setSteps] = useState<TrackedStep[]>([]);
  const [files, setFiles] = useState<FileItemType[]>([]);
  // 每个智能体正在运行的流数量（多会话并行不互斥；loading 只看当前智能体）
  const [runningCount, setRunningCount] = useState<Record<string, number>>({});

  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<import("../components/Composer").ComposerHandle | null>(null);

  // 视图快照 refs：流回调据此判断"结果是否属于当前正在看的会话"。
  // 只写 ref + setState，绝不用流回调里的闭包状态猜用户当前视图。
  const agentIdRef = useRef<string>("general");
  const sidRef = useRef<string>("");
  const runningRef = useRef<Set<string>>(new Set());

  // 会话存储的服务端镜像：agentId → 摘要列表（按 updated_at 降序）。
  // 读写都走 /api/sessions（后端 SQLite），本地不再持有全量消息——
  // 选中会话时才 fetchStoredSession 拉全文。
  const sessionIndexRef = useRef<Map<string, StoredSession[]>>(new Map());

  useEffect(() => { agentIdRef.current = currentAgent.id; }, [currentAgent.id]);
  useEffect(() => { sidRef.current = activeSessionId; }, [activeSessionId]);
  // 偏好记录不放 effect（StrictMode remount 会用初始 general 覆写 localStorage），
  // 改为在每个真实切换入口显式 setLastAgent，见 switchAgent / handleNewSession / handleSelectSession。

  const loading = (runningCount[currentAgent.id] ?? 0) > 0;

  // ---- Load agents + server sessions on mount ----
  // 偏好恢复：优先回到上次使用的智能体（localStorage），失效则回退 general。
  // 会话索引来自服务端；首次使用则把 localStorage 旧会话一次性迁移上去。
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await fetchAgents();
        if (cancelled || list.length === 0) return;
        setAgents(list);
        await migrateLocalSessions();
        const stored = await fetchStoredSessions();
        if (cancelled) return;
        const idx = new Map<string, StoredSession[]>();
        for (const s of stored) {
          const arr = idx.get(s.agent_id) ?? [];
          arr.push(s);
          idx.set(s.agent_id, arr);
        }
        sessionIndexRef.current = idx;
        const last = getLastAgent();
        const remembered = last ? list.find((a) => a.id === last) : undefined;
        const def = remembered ?? list.find((a) => a.id === "general") ?? list[0];
        await switchAgent(def.id, list, idx);
      } catch {
        if (!cancelled) switchAgent(getLastAgent() ?? "general");
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- Auto-scroll（流式增量时跟随；用户手动上滚时不强拉，由 onToken 节流触发） ----
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  // ---- Rebuild session list (read-only snapshot for the drawer) ----
  function rebuildSessionList(activeAgentId: string, agentList?: AgentManifest[],
                              idx?: Map<string, StoredSession[]>) {
    const list = agentList ?? agents;
    const index = idx ?? sessionIndexRef.current;
    const groups: SessionGroup[] = [];
    for (const [aid, sessList] of index.entries()) {
      if (sessList.length === 0) continue;
      const am = list.find((a) => a.id === aid);
      groups.push({
        agentId: aid,
        displayName: am?.display_name ?? aid,
        identityColor: am?.identity_color ?? "#615CED",
        sessions: sessList.map((s) => ({ id: s.id, title: s.title, updatedAt: s.updated_at })),
      });
    }
    if (!groups.find((g) => g.agentId === activeAgentId)) {
      const cur = list.find((a) => a.id === activeAgentId) ?? DEFAULT_AGENT;
      groups.push({
        agentId: cur.id,
        displayName: cur.display_name,
        identityColor: cur.identity_color,
        sessions: [],
      });
    }
    setSessions(groups);
  }

  // ---- Switch agent ----
  async function switchAgent(agentId: string, agentList?: AgentManifest[],
                             idx?: Map<string, StoredSession[]>) {
    const list = agentList ?? agents;
    const agent = list.find((a) => a.id === agentId) ?? DEFAULT_AGENT;
    setCurrentAgent(agent);
    agentIdRef.current = agentId;
    setLastAgent(agent.id);

    // 服务端索引里该智能体最近的一条会话（原 localStorage activeIndex 语义：
    // 切回智能体时回到最近活跃的会话）。
    const sessList = idx?.get(agentId) ?? sessionIndexRef.current.get(agentId) ?? [];
    const activeSess = sessList[0] ?? null;

    if (activeSess) {
      sidRef.current = activeSess.id;
      setActiveSessionId(activeSess.id);
      const full = await fetchStoredSession(activeSess.id);
      // 切换期间用户又切走了：丢弃迟到结果
      if (agentIdRef.current !== agentId) return;
      if (full) {
        setMessages(full.messages);
        const lastAssistant = [...full.messages].reverse().find((m) => m.role === "assistant");
        setSteps((lastAssistant?.steps as TrackedStep[] | undefined) ?? []);
        setFiles(lastAssistant?.files ?? []);
      }
    } else {
      sidRef.current = "";
      setActiveSessionId("");
      setMessages([]);
      setSteps([]);
      setFiles([]);
    }
    rebuildSessionList(agentId, list, idx);
  }

  // ---- Persist messages for a given agent/session. 返回实际会话 id。
  //      只有"用户正看着这条会话"时才联动更新视图与 activeSessionId。 ----
  function persistFor(agentId: string, sid: string | null, msgs: Message[]): string {
    const title =
      msgs.length > 0 && msgs[0].role === "user"
        ? msgs[0].content.slice(0, 40)
        : "新对话";

    let finalSid = sid ?? "";
    if (!finalSid) {
      finalSid = genId();
      // 新会话：索引头部插入（本地立即反映，服务端稍后落盘）
      const arr = sessionIndexRef.current.get(agentId) ?? [];
      sessionIndexRef.current.set(agentId, [
        { id: finalSid, agent_id: agentId, title, created_at: Date.now() / 1000, updated_at: Date.now() / 1000 },
        ...arr,
      ]);
    } else {
      // 更新索引里的 updated_at（重新排到最前）
      const arr = sessionIndexRef.current.get(agentId) ?? [];
      const hit = arr.find((s) => s.id === finalSid);
      if (hit) {
        hit.updated_at = Date.now() / 1000;
        hit.title = title;
        sessionIndexRef.current.set(
          agentId,
          [hit, ...arr.filter((s) => s.id !== finalSid)],
        );
      }
    }

    // 落服务端（fire-and-forget，失败静默）
    void putSession(finalSid, agentId, title, msgs);

    if (agentIdRef.current === agentId && (sid === null || sid === "" || sid === sidRef.current)) {
      sidRef.current = finalSid;
      setActiveSessionId(finalSid);
    }
    rebuildSessionList(agentId);
    return finalSid;
  }

  // ---- New session ----
  function handleNewSession(agentId?: string) {
    const aid = agentId ?? currentAgent.id;
    if (aid !== currentAgent.id || agentId) {
      const agent = agents.find((a) => a.id === aid) ?? DEFAULT_AGENT;
      setCurrentAgent(agent);
      agentIdRef.current = aid;
      setLastAgent(aid);
    }
    sidRef.current = "";
    setActiveSessionId("");
    setMessages([]);
    setSteps([]);
    setFiles([]);
    setSessionListOpen(false);
  }

  // ---- Select session ----
  async function handleSelectSession(agentId: string, sessionId: string, keepOpen = false) {
    const agent = agents.find((a) => a.id === agentId) ?? DEFAULT_AGENT;
    setCurrentAgent(agent);
    agentIdRef.current = agentId;
    setLastAgent(agentId);
    sidRef.current = sessionId;
    setActiveSessionId(sessionId);
    // 索引里把选中项提到最前（原 activeIndex 语义的替身）
    const arr = sessionIndexRef.current.get(agentId) ?? [];
    const hit = arr.find((s) => s.id === sessionId);
    if (hit) {
      sessionIndexRef.current.set(agentId, [hit, ...arr.filter((s) => s.id !== sessionId)]);
    }
    const full = await fetchStoredSession(sessionId);
    // 拉取期间用户已切走：丢弃迟到结果
    if (agentIdRef.current !== agentId || sidRef.current !== sessionId) return;
    if (full) {
      setMessages(full.messages);
      const lastAssistant = [...full.messages].reverse().find((m) => m.role === "assistant");
      setSteps((lastAssistant?.steps as TrackedStep[] | undefined) ?? []);
      setFiles(lastAssistant?.files ?? []);
    }
    if (!keepOpen) setSessionListOpen(false);
  }

  // ---- Delete session：正在跑流的会话不让删；删当前会话则清空视图 ----
  async function handleDeleteSession(agentId: string, sessionId: string) {
    if (runningRef.current.has(agentId) && agentIdRef.current === agentId && sidRef.current === sessionId) {
      window.alert("该会话正在生成回复，完成后再删除。");
      return;
    }
    // 先改本地索引（立即反馈），再删服务端
    const arr = sessionIndexRef.current.get(agentId) ?? [];
    sessionIndexRef.current.set(agentId, arr.filter((s) => s.id !== sessionId));
    void deleteStoredSession(sessionId);

    if (agentIdRef.current === agentId && sidRef.current === sessionId) {
      // 删的是正在看的会话：回到该智能体剩余的首条，或空新会话视图
      const rest = sessionIndexRef.current.get(agentId) ?? [];
      if (rest.length > 0) {
        await handleSelectSession(agentId, rest[0].id, true); // 面板保持打开，便于连续删除
        rebuildSessionList(agentIdRef.current); // 列表内容变了，须重建（handleSelectSession 不做这件事）
        return;
      }
      sidRef.current = "";
      setActiveSessionId("");
      setMessages([]);
      setSteps([]);
      setFiles([]);
    }
    rebuildSessionList(agentIdRef.current);
  }

  // ---- Send message ----
  const handleSend = useCallback(
    (text: string, base?: Message[]) => {
      const agentId = currentAgent.id;
      // 同智能体串行：上一条还在跑就拒绝再发；不同智能体可并行。
      if (runningRef.current.has(agentId)) return;
      const msgs = base ?? messages;
      const history = msgs.map((m) => ({ role: m.role, content: m.content }));
      const startSid = sidRef.current || null;

      const userMsg: Message = {
        role: "user", content: text, reasoning: "", steps: [], files: [], done: false, ts: Date.now() / 1000,
      };
      const assistantMsg: Message = {
        role: "assistant", content: "", reasoning: "", steps: [], files: [], done: false, ts: Date.now() / 1000,
      };

      // 流私有状态：即使切走会话/智能体，帧也只写进它自己这份数组。
      let streamMsgs: Message[] = [...msgs, userMsg, assistantMsg];
      let mySid = startSid ?? "";
      let mySteps: TrackedStep[] = [];
      let myFiles: FileItemType[] = [];
      const starts = new Map<string, number>();

      runningRef.current.add(agentId);
      setRunningCount((c) => ({ ...c, [agentId]: (c[agentId] ?? 0) + 1 }));

      const patchAssistant = (fn: (m: Message) => Message) => {
        const last = streamMsgs[streamMsgs.length - 1];
        if (last?.role !== "assistant") return;
        streamMsgs = [...streamMsgs.slice(0, -1), fn(last)];
      };
      // 当前视图正是这条会话时，才把私有状态投影到界面
      const inView = () => agentIdRef.current === agentId && sidRef.current === (mySid || startSid || "");
      const project = () => {
        if (!inView()) return;
        setMessages([...streamMsgs]);
        setSteps([...mySteps]);
        setFiles([...myFiles]);
      };
      const finish = () => {
        runningRef.current.delete(agentId);
        setRunningCount((c) => ({ ...c, [agentId]: Math.max(0, (c[agentId] ?? 1) - 1) }));
      };

      mySid = persistFor(agentId, startSid, streamMsgs);

      chatSSE(text, history, agentId, {
        onToken: (delta, _stepId, chips) => {
          patchAssistant((m) => {
            const updated: Message = { ...m, content: m.content + delta };
            if (Array.isArray(chips) && chips.length > 0) updated.chips = chips;
            return updated;
          });
          project();
        },
        onStep: (step) => {
          if (step.status === "running") starts.set(step.id, step.ts);
          let tracked: TrackedStep = { ...step, duration: undefined };
          if (step.status === "done" && starts.has(step.id)) {
            const dur = Math.max(0, Math.round((step.ts - starts.get(step.id)!) * 100) / 100);
            tracked = { ...step, duration: dur };
            starts.delete(step.id);
          }
          const i = mySteps.findIndex((s) => s.id === step.id);
          if (i >= 0) mySteps[i] = tracked;
          else mySteps = [...mySteps, tracked];
          patchAssistant((m) => ({
            ...m,
            steps: [...m.steps.filter((s) => s.id !== step.id), tracked],
          }));
          project();
        },
        onFiles: (fileList) => {
          myFiles = fileList;
          patchAssistant((m) => ({ ...m, files: fileList }));
          project();
        },
        onAgentMeta: (meta) => {
          if (!inView()) return;
          setCurrentAgent((prev) => {
            if (prev.id !== meta.agent_id) return prev;
            return { ...prev, display_name: meta.display_name, description: meta.description ?? prev.description, identity_color: meta.identity_color, placeholder: meta.placeholder };
          });
        },
        onError: (errMsg) => {
          patchAssistant((m) => ({ ...m, error: errMsg, done: true }));
          mySid = persistFor(agentId, mySid || startSid, streamMsgs);
          project();
          finish();
        },
        onDone: (answer, meta) => {
          patchAssistant((m) => ({ ...m, content: m.content || answer, done: true, meta }));
          mySid = persistFor(agentId, mySid || startSid, streamMsgs);
          project();
          finish();
        },
      }, mySid)
        .catch(() => { finish(); })
        .finally(() => {
          // 流自然断掉（无 done 帧）也收尾落盘，防止半截输出丢失
          if (runningRef.current.has(agentId)) {
            patchAssistant((m) => (m.done ? m : { ...m, done: true }));
            persistFor(agentId, mySid || startSid, streamMsgs);
            project();
            finish();
          }
        });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [messages, currentAgent.id, agents],
  );

  // ---- 重新生成（千问式）：截掉最后一条 assistant 回复，重发上一条用户消息 ----
  const handleRegenerate = useCallback(() => {
    const last = messages[messages.length - 1];
    if (!last || last.role !== "assistant") return;
    const prev = messages[messages.length - 2];
    if (!prev || prev.role !== "user") return;
    if (runningRef.current.has(currentAgent.id)) return;
    handleSend(prev.content, messages.slice(0, -1));
  }, [messages, currentAgent.id, handleSend]);

  // ---- Agent select from composer：不中断任何流，切走后其结果仍写回自己会话 ----
  function handleAgentSelect(agentId: string) {
    if (agentId === currentAgent.id) return;
    switchAgent(agentId);
  }

  // ---- Total time / done count for the timeline header ----
  const doneSteps = steps.filter((s) => s.status === "done");
  const runningCountInView = steps.filter((s) => s.status === "running").length;
  let totalTime: string | undefined;
  if (doneSteps.length === steps.length && steps.length > 0) {
    const firstTs = Math.min(...steps.map((s) => s.ts));
    const lastTs = Math.max(...doneSteps.map((s) => s.ts));
    if (lastTs - firstTs > 0) totalTime = `${(lastTs - firstTs).toFixed(1)}s`;
  }
  const doneCount = steps.length > 0 ? `${doneSteps.length}/${steps.length}` : undefined;
  const isRunning = runningCountInView > 0 || loading;

  return (
    <div className="chat-layout" style={{ ["--identity-color" as string]: currentAgent.identity_color }}>
      <div className="chat-col">
        <header className="chat-head">
          <div className="seal">{currentAgent.display_name.charAt(0)}</div>
          <div className="who">
            <b>{currentAgent.display_name}</b>
            <span className="desc">{currentAgent.description}</span>
          </div>
          <div className="grow" />
          <span className="model-chip">deepseek-v3</span>
          <button className="newchat" type="button" onClick={() => setSessionListOpen((v) => !v)} title="会话历史">
            <MessageSquare size={14} style={{ verticalAlign: "middle", marginRight: 4 }} />会话
          </button>
          <button className="newchat" type="button" onClick={() => handleNewSession()}>+ 新会话</button>
        </header>

        <div ref={scrollRef} className="msgs">
          {messages.length === 0 && !loading && (
            <div className="msg" style={{ textAlign: "center", paddingTop: 60 }}>
              <div style={{ color: "var(--text3)", fontSize: 14, lineHeight: 1.8 }}>
                <Bot size={28} style={{ margin: "0 auto 12px", display: "block" }} />
                <p style={{ margin: 0 }}>输入需求开始对话</p>
                <p style={{ margin: "4px 0 0", fontSize: 12 }}>当前智能体：{currentAgent.display_name}</p>
              </div>
            </div>
          )}
          {messages.map((m, i) => (
            <MessageBubble
              key={i}
              message={m}
              identityColor={currentAgent.identity_color}
              isLast={i === messages.length - 1}
              onRegenerate={handleRegenerate}
              onChipClick={(chipText) => composerRef.current?.fill(chipText)}
            />
          ))}
        </div>

        <Composer
          ref={composerRef}
          agentId={currentAgent.id}
          placeholder={currentAgent.placeholder}
          loading={loading}
          onSend={handleSend}
          agents={agents}
          currentAgent={currentAgent}
          onAgentSelect={handleAgentSelect}
        />
      </div>

      <aside className="panel">
        <div className="panel-body">
          <Timeline steps={steps} files={files} totalTime={totalTime} doneCount={doneCount} isRunning={isRunning} />
        </div>
        <div className="legend" aria-label="状态图例">
          <i><span className="sw" style={{ background: "var(--run)" }}></span>运行中</i>
          <i><span className="sw" style={{ background: "var(--ok)" }}></span>完成</i>
          <i><span className="sw" style={{ background: "var(--wait)" }}></span>等待</i>
          <i><span className="sw" style={{ background: "var(--err)" }}></span>出错</i>
        </div>
      </aside>

      {sessionListOpen && (
        <SessionList
          sessions={sessions}
          activeAgentId={currentAgent.id}
          activeSessionId={activeSessionId}
          onSelectSession={handleSelectSession}
          onDeleteSession={handleDeleteSession}
          onNewSession={(agentId) => handleNewSession(agentId)}
          onClose={() => setSessionListOpen(false)}
        />
      )}
    </div>
  );
}
