// 思考事件分发与活动步骤定位（对齐 ChatFlow useChat.applyThinkingEvent / activeStep）。
import type { Message, StepRecord, ThinkingPhase } from '../types'

/** 找当前活动步骤：第一个 running 的，否则最后一条（chat 路由 steps 空时返 null）。 */
export function activeStep(m: Message): StepRecord | null {
  if (!m.steps.length) return null
  const running = m.steps.find((s) => s.status === 'running')
  return running ?? m.steps[m.steps.length - 1]
}

/**
 * 把 thinking 帧分发到对应 segment：按 (node, stepIndex, phase) 三元组累积。
 * - stepIndex 非空且 steps[stepIndex] 存在 → 进该步的 thinkingSegments（步骤级推理）。
 * - 否则 → 进消息级 thinkingSegments（route/planner/reflector）。
 * 栈顶三元组相同则 append，否则 push 新段——保证顺序 = SSE 到达顺序。
 */
export function applyThinkingEvent(
  m: Message,
  node: string,
  phase: ThinkingPhase,
  delta: string,
  stepIndex: number | null,
): void {
  const target =
    stepIndex !== null && m.steps[stepIndex] ? m.steps[stepIndex] : null
  const segs = target ? target.thinkingSegments : m.thinkingSegments
  const top = segs[segs.length - 1]
  if (top && top.node === node && top.stepIndex === stepIndex && top.phase === phase) {
    top.content += delta
  } else {
    segs.push({ node, stepIndex, phase, content: delta })
  }
}
