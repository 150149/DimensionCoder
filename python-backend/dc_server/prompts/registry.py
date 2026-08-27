
from . import load_prompt

STEP_TYPE_EXECUTOR = "executor"
STEP_TYPE_GATE = "gate"
STEP_TYPE_PLAN = "plan"
STEP_TYPE_CODE_REVIEW = "code_review"
STEP_TYPE_REVERSE = "reverse"    # 逆向专家（CTF/二进制分析/模拟器专用，2026-08-24）
STEP_TYPE_RESEARCHER = "researcher"  # 研究员（只读调研专家，2026-08-24）
STEP_TYPE_MONITOR = "monitor"    # 审查步骤（初始编排/步骤完成/介入）
STEP_TYPE_REVIEW = "review"      # 最终审查（独立转正，同 planner）
STEP_TYPE_REPORT = "report"      # 最终报告（独立转正，同 planner）
STEP_TYPES = (STEP_TYPE_EXECUTOR, STEP_TYPE_GATE, STEP_TYPE_PLAN, STEP_TYPE_CODE_REVIEW,
              STEP_TYPE_REVERSE, STEP_TYPE_RESEARCHER, STEP_TYPE_MONITOR, STEP_TYPE_REVIEW, STEP_TYPE_REPORT)


def is_virtual_step(step_id: str) -> bool:
    """判断是否为系统虚拟步骤（_ 前缀保留给系统，工具不得观测/操作）"""
    return isinstance(step_id, str) and step_id.startswith("_")


# ── 步骤类型 → agent 提示词文件映射 ───────────────────────────────
STEP_PROMPT_MAP = {
    STEP_TYPE_EXECUTOR: "step-executor",
    STEP_TYPE_GATE: "gate-reporter",
    STEP_TYPE_PLAN: "planner",
    STEP_TYPE_CODE_REVIEW: "code-reviewer",
    STEP_TYPE_REVERSE: "reverse-expert",
    STEP_TYPE_RESEARCHER: "researcher",
}


_HIDDEN_STEP_TYPES = (STEP_TYPE_MONITOR, STEP_TYPE_REVIEW, STEP_TYPE_REPORT)


def is_hidden_step(step: dict) -> bool:
    """审查/收尾步骤（实体化 2026-08-21）：type 命中或 id 匹配（monitor-*/review/
    report、历史 `_` 前缀）——对 AI 工具与 UI 不可见，但参与执行循环/状态机。"""
    sid = str(step.get("step_id", ""))
    return (step.get("type") in _HIDDEN_STEP_TYPES
            or sid.startswith("_") or sid.startswith("monitor-")
            or sid in ("review", "report"))


def prompt_for_step(step_id: str, step_type: str) -> str:
    """步骤提示词映射（2026-08-21 实体化）：type 优先——review → final-reviewer、
    report → final-reporter、monitor 类 → orchestrator；普通步骤走 STEP_PROMPT_MAP。"""
    if step_type == STEP_TYPE_REVIEW:
        return "final-reviewer"
    if step_type == STEP_TYPE_REPORT:
        return "final-reporter"
    if step_type == STEP_TYPE_MONITOR or step_id.startswith("monitor-"):
        return "orchestrator"
    return STEP_PROMPT_MAP.get(step_type, STEP_PROMPT_MAP[STEP_TYPE_EXECUTOR])


def get_step_prompt(step: dict) -> str:
    """执行步骤的 agent 提示词：
    - type=plan / type=code_review → 专属提示词（显式类型优先）
    - human_attention=gate → gate-reporter（覆盖迁移默认的 type=executor：
      旧数据 gate 步骤无 type，DB 迁移后 type 默认 executor，须按 gate 标记选提示词）
    - 其余 → type 映射或默认 step-executor
    """
    t = step.get("type")
    if t in (STEP_TYPE_PLAN, STEP_TYPE_CODE_REVIEW):
        return load_prompt(STEP_PROMPT_MAP[t])
    if step.get("human_attention") == "gate":
        return load_prompt(STEP_PROMPT_MAP[STEP_TYPE_GATE])
    return load_prompt(STEP_PROMPT_MAP.get(t, STEP_PROMPT_MAP[STEP_TYPE_EXECUTOR]))
