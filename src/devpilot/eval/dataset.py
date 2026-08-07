"""评测数据集：Golden set 构建与加载。

评测数据集构建。
来源：人工标注的真实任务 + Agent 真实运行案例（数据飞轮回流）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GoldenCase:
    id: str
    task: str
    expected: str          # 期望结果/关键断言
    rubric: list[str]      # 评分维度（供 LLM-judge 用）
    tags: list[str]        # 分类标签，便于分维度统计


class GoldenSet:
    """Golden 评测集。"""

    def __init__(self) -> None:
        self._cases: list[GoldenCase] = []

    def load_jsonl(self, path: str | Path) -> None:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                self._cases.append(GoldenCase(
                    id=obj["id"], task=obj["task"],
                    expected=obj.get("expected", ""),
                    rubric=obj.get("rubric", []),
                    tags=obj.get("tags", []),
                ))

    def cases(self) -> list[GoldenCase]:
        return list(self._cases)

    def add(self, case: GoldenCase) -> None:
        """数据飞轮：把人工标注的新案例追加进评测集。"""
        self._cases.append(case)
