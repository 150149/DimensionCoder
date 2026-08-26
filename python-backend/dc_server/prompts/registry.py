
from . import load_prompt

STEP_TYPE_EXECUTOR = "executor"
STEP_TYPE_GATE = "gate"
STEP_TYPE_PLAN = "plan"
STEP_TYPE_CODE_REVIEW = "code_review"
STEP_TYPE_REVERSE = "reverse"
STEP_TYPE_RESEARCHER = "researcher"
STEP_TYPE_MONITOR = "monitor"
STEP_TYPE_REVIEW = "review"
STEP_TYPE_REPORT = "report"
STEP_TYPES = (STEP_TYPE_EXECUTOR, STEP_TYPE_GATE, STEP_TYPE_PLAN, STEP_TYPE_CODE_REVIEW,
              STEP_TYPE_REVERSE, STEP_TYPE_RESEARCHER, STEP_TYPE_MONITOR, STEP_TYPE_REVIEW, STEP_TYPE_REPORT)

def is_virtual_step(step_id: str) -> bool:
    return isinstance(step_id, str) and step_id.startswith("_")

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
    sid = str(step.get("step_id", ""))
    return (step.get("type") in _HIDDEN_STEP_TYPES
            or sid.startswith("_") or sid.startswith("monitor-")
            or sid in ("review", "report"))

def prompt_for_step(step_id: str, step_type: str) -> str:
    if step_type == STEP_TYPE_REVIEW:
        return "final-reviewer"
    if step_type == STEP_TYPE_REPORT:
        return "final-reporter"
    if step_type == STEP_TYPE_MONITOR or step_id.startswith("monitor-"):
        return "orchestrator"
    return STEP_PROMPT_MAP.get(step_type, STEP_PROMPT_MAP[STEP_TYPE_EXECUTOR])

def get_step_prompt(step: dict) -> str:
    t = step.get("type")
    if t in (STEP_TYPE_PLAN, STEP_TYPE_CODE_REVIEW):
        return load_prompt(STEP_PROMPT_MAP[t])
    if step.get("human_attention") == "gate":
        return load_prompt(STEP_PROMPT_MAP[STEP_TYPE_GATE])
    return load_prompt(STEP_PROMPT_MAP.get(t, STEP_PROMPT_MAP[STEP_TYPE_EXECUTOR]))
