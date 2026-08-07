import { useEffect, useMemo, useState } from "react";
import dagre from "dagre";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

// ============================================================================
// 动态图形流：根据 planner 生成的步骤计划动态画图
// 与固定节点图不同，节点数量/名称由 LLM 规划决定——
// "总结资讯"3 步、"写函数"1 步，图随任务而变。
// ============================================================================

/** 节点运行状态：idle 等待 / running 运行中 / done 完成 / error 出错 */
type NodeStatus = "idle" | "running" | "done" | "error";

/** planner 生成的单步计划（与后端 plan 帧的 step 结构对齐） */
export interface PlanStep {
  id: string;
  name: string;
  description: string;
  output: string;
  needs_search: boolean;
}

/** DevNode 自定义节点的 data 字段 */
interface DevNodeData {
  label: string;
  status: NodeStatus;
  kind: "planner" | "step";
  needsSearch?: boolean;
  [key: string]: unknown;
}

/** FlowGraph 接收的节点状态更新项（来自 node 帧） */
export interface NodeUpdate {
  node_id: string;
  status: NodeStatus;
}

interface FlowGraphProps {
  /** 节点状态更新列表，监听后写入对应 node.data.status */
  nodeUpdates?: NodeUpdate[];
  /** planner 生成的步骤计划：收到后据此动态重建节点图 */
  plan?: PlanStep[] | null;
}

// ============================================================================
// dagre 自动布局
// ============================================================================

function getLayoutedElements<N extends Node, E extends Edge>(
  nodes: N[],
  edges: E[],
  direction: "LR" | "TB" = "LR",
): { nodes: N[]; edges: E[] } {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: direction, nodesep: 40, ranksep: 90 });
  const nodeWidth = 150;
  const nodeHeight = 52;
  nodes.forEach((node) => g.setNode(node.id, { width: nodeWidth, height: nodeHeight }));
  edges.forEach((edge) => g.setEdge(edge.source, edge.target));
  dagre.layout(g);
  const layoutedNodes = nodes.map((node) => {
    const pos = g.node(node.id);
    return { ...node, position: { x: pos.x - nodeWidth / 2, y: pos.y - nodeHeight / 2 } };
  });
  return { nodes: layoutedNodes, edges };
}

// ============================================================================
// 自定义节点组件 DevNode
// 深色卡片 + 状态指示圆点 + 搜索图标 + 左右 Handle
// ============================================================================

function statusClasses(status: NodeStatus) {
  switch (status) {
    case "running":
      return { border: "border-amber-500", text: "text-amber-300", dot: "bg-amber-400", pulse: "animate-pulse" };
    case "done":
      return { border: "border-emerald-500", text: "text-emerald-300", dot: "bg-emerald-400", pulse: "" };
    case "error":
      return { border: "border-rose-500", text: "text-rose-300", dot: "bg-rose-400", pulse: "" };
    default:
      return { border: "border-slate-700", text: "text-slate-500", dot: "bg-slate-600", pulse: "" };
  }
}

function DevNode({ data }: { id: string; data: DevNodeData }) {
  const { label, status, kind, needsSearch } = data;
  const s = statusClasses(status);
  return (
    <div
      className={`min-w-[140px] rounded-lg border-2 bg-slate-900 px-3 py-2 ${s.border} ${s.pulse}`}
    >
      <Handle type="target" position={Position.Left} style={{ background: "#475569", width: 6, height: 6, border: "none" }} />
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 shrink-0 rounded-full ${s.dot}`} />
        <span className={`font-medium text-xs ${s.text}`}>{label}</span>
        {/* 联网搜索标记：planner 标记或 step 的 needs_search */}
        {needsSearch && (
          <span className="ml-auto text-[10px] text-sky-400" title="联网搜索">
            🌐
          </span>
        )}
        {kind === "planner" && (
          <span className="ml-auto text-[10px] text-violet-400" title="规划">
            ⛳
          </span>
        )}
      </div>
      <Handle type="source" position={Position.Right} style={{ background: "#475569", width: 6, height: 6, border: "none" }} />
    </div>
  );
}

const nodeTypes = { devNode: DevNode };

// ============================================================================
// 主组件 FlowGraph（动态）
// ============================================================================

/**
 * 动态图形流：收到 plan 帧的步骤列表后，构建 [planner, step1, step2, ...] 节点，
 * 用 dagre 布局，边连成顺序链 planner→step1→step2→...。
 * node 帧按 node_id 更新对应节点状态色。
 */
export default function FlowGraph({ nodeUpdates, plan }: FlowGraphProps) {
  // 据当前 plan 动态生成初始节点/边。plan 变化（新对话）时重建图。
  const { nodes: baseNodes, edges: baseEdges } = useMemo(() => {
    if (!plan || plan.length === 0) {
      // 无 plan：占位单节点（等待规划）。
      return getLayoutedElements(
        [{ id: "planner", type: "devNode", data: { label: "Planner", status: "idle" as NodeStatus, kind: "planner" as const }, position: { x: 0, y: 0 } }] as Node<DevNodeData>[],
        [] as Edge[],
      );
    }
    const plannerNode: Node<DevNodeData> = {
      id: "planner",
      type: "devNode",
      data: { label: "Planner", status: "idle", kind: "planner" },
      position: { x: 0, y: 0 },
    };
    const stepNodes: Node<DevNodeData>[] = plan.map((s) => ({
      id: s.id,
      type: "devNode",
      data: { label: s.name, status: "idle", kind: "step", needsSearch: s.needs_search },
      position: { x: 0, y: 0 },
    }));
    // 边：planner→step1→step2→...
    const edges: Edge[] = [];
    edges.push({ id: "planner-step1", source: "planner", target: plan[0].id });
    for (let i = 0; i < plan.length - 1; i++) {
      edges.push({ id: `${plan[i].id}-${plan[i + 1].id}`, source: plan[i].id, target: plan[i + 1].id });
    }
    return getLayoutedElements([plannerNode, ...stepNodes], edges);
  }, [plan]);

  const [nodes, setNodes, onNodesChange] = useNodesState(baseNodes);
  const [edges, , onEdgesChange] = useEdgesState(baseEdges);

  // plan 变化时重置图（新对话）。
  const [planVersion, setPlanVersion] = useState(0);
  useEffect(() => {
    setNodes(baseNodes);
    setPlanVersion((v) => v + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan]);

  // 监听 nodeUpdates：按 node_id 更新对应节点状态（取最新）。
  useEffect(() => {
    if (!nodeUpdates || nodeUpdates.length === 0) return;
    setNodes((nds) =>
      nds.map((n) => {
        let upd: NodeUpdate | undefined;
        for (let i = nodeUpdates.length - 1; i >= 0; i--) {
          if (nodeUpdates[i].node_id === n.id) {
            upd = nodeUpdates[i];
            break;
          }
        }
        if (!upd) return n;
        return { ...n, data: { ...(n.data as DevNodeData), status: upd.status } };
      }),
    );
  }, [nodeUpdates, setNodes]);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      nodeTypes={nodeTypes}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
      fitView
      proOptions={{ hideAttribution: true }}
      style={{ height: "100%" }}
      className="bg-slate-950"
      key={planVersion}
    >
      <Background gap={16} size={1} color="#1e293b" />
      <Controls showInteractive={false} className="!bg-slate-900 !border-slate-800" />
      <MiniMap
        pannable
        zoomable
        nodeColor={(n) => {
          const data = n.data as DevNodeData;
          if (data.status === "running") return "#f59e0b";
          if (data.status === "done") return "#10b981";
          if (data.status === "error") return "#f43f5e";
          return "#334155";
        }}
        maskColor="rgba(2, 6, 23, 0.7)"
        className="!bg-slate-900 !border-slate-800"
      />
    </ReactFlow>
  );
}
