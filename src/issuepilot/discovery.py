"""Proactive bug discovery (read-only). Static AST heuristics find candidates; the model triages.

Findings are *reports*, never actions: nothing is written to the repo or to GitHub. Each finding
carries file, line, evidence and a ready-to-file issue title so a human decides what happens next.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from issuepilot.budget import BudgetExceeded, Cancelled, CancelToken, Limits
from issuepilot.config import Settings
from issuepilot.llm.base import ChatMessage, LLMProvider
from issuepilot.modes import Capability, Mode
from issuepilot.persistence.tasks import TaskStore
from issuepilot.runtime import MeteredProvider, TaskContext
from issuepilot.tools.repo import RepoTools, ToolError

Severity = Literal["high", "medium", "low"]
SECRET_ASSIGN = re.compile(
    r"(?i)(password|passwd|secret|api_?key|token)\s*=\s*['\"][^'\"\s]{8,}['\"]"
)
PLACEHOLDER = re.compile(r"(?i)(example|changeme|placeholder|your[_-]|xxx|<.*>|test)")


class Finding(BaseModel):
    rule: str
    severity: Severity
    file: str
    line: int
    snippet: str
    message: str
    title: str = ""  # suggested issue title (set by triage)
    verdict: Literal["unreviewed", "real", "false_positive"] = "unreviewed"
    reasoning: str = ""


class Triage(BaseModel):
    class Item(BaseModel):
        id: int
        verdict: Literal["real", "false_positive"]
        severity: Severity
        title: str = Field(max_length=120)
        reasoning: str = Field(max_length=400)

    items: list[Item]


class DiscoveryReport(BaseModel):
    files_scanned: int
    findings: list[Finding]
    triaged: bool = False

    def markdown(self) -> str:
        real = [f for f in self.findings if f.verdict != "false_positive"]
        out = [
            f"# IssuePilot discovery report\n\nScanned {self.files_scanned} files; "
            f"{len(real)} finding(s) to review"
            f"{'' if self.triaged else ' (not model-triaged)'}.\n"
        ]
        for f in real:
            out.append(
                f"## [{f.severity}] {f.title or f.message}\n`{f.file}:{f.line}` · rule `{f.rule}`"
                f"\n\n```python\n{f.snippet}\n```\n{f.reasoning}\n"
            )
        return "\n".join(out)


def _line(src: list[str], n: int) -> str:
    return src[n - 1].strip()[:160] if 0 < n <= len(src) else ""


def scan_source(rel: str, text: str) -> list[Finding]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [
            Finding(
                rule="syntax-error",
                severity="high",
                file=rel,
                line=1,
                snippet="",
                message="file does not parse",
            )
        ]
    src = text.splitlines()
    found: list[Finding] = []

    def add(rule: str, sev: Severity, node: ast.AST, msg: str) -> None:
        ln = getattr(node, "lineno", 1)
        found.append(
            Finding(rule=rule, severity=sev, file=rel, line=ln, snippet=_line(src, ln), message=msg)
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                add("bare-except", "medium", node, "bare except swallows every error incl. Ctrl-C")
            elif len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                add("swallowed-exception", "medium", node, "exception caught and silently ignored")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for d in [*node.args.defaults, *node.args.kw_defaults]:
                if isinstance(d, ast.List | ast.Dict | ast.Set):
                    add(
                        "mutable-default", "medium", d, f"mutable default argument in {node.name}()"
                    )
        elif isinstance(node, ast.Call):
            fn = ast.unparse(node.func)
            if fn in ("eval", "exec"):
                add("eval-exec", "high", node, f"{fn}() on dynamic input is code injection")
            if fn.startswith("subprocess.") and any(
                k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                for k in node.keywords
            ):
                add(
                    "shell-true", "high", node, "subprocess with shell=True risks command injection"
                )
            if fn in ("yaml.load", "pickle.load", "pickle.loads"):
                add("unsafe-deserialize", "high", node, f"{fn} on untrusted data executes code")
            if (
                fn.endswith(".execute")
                and node.args
                and isinstance(node.args[0], ast.JoinedStr | ast.BinOp)
            ):
                add(
                    "sql-injection",
                    "high",
                    node,
                    "SQL built with string formatting; use parameters",
                )
            if (
                fn.split(".")[0] in ("requests", "httpx")
                and fn.split(".")[-1]
                in (
                    "get",
                    "post",
                    "put",
                    "delete",
                    "request",
                )
                and not any(k.arg == "timeout" for k in node.keywords)
            ):
                add("no-timeout", "low", node, f"{fn}() without timeout can hang forever")
        elif isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators, strict=False):
                if (
                    isinstance(op, ast.Eq | ast.NotEq)
                    and isinstance(right, ast.Constant)
                    and right.value is None
                ):
                    add("eq-none", "low", node, "compare to None with 'is', not '=='")
    for i, line in enumerate(src, 1):
        m = SECRET_ASSIGN.search(line)
        if m and not PLACEHOLDER.search(line):
            found.append(
                Finding(
                    rule="hardcoded-secret",
                    severity="high",
                    file=rel,
                    line=i,
                    snippet=re.sub(r"=\s*['\"].*", "= '<redacted>'", line.strip())[:160],
                    message="possible hardcoded credential",
                )
            )
    return found


def scan_repo(tools: RepoTools, max_files: int = 400) -> tuple[int, list[Finding]]:
    files = [
        f for f in tools.list_files() if "/tests/" not in f"/{f}" and not f.startswith("test_")
    ]
    out: list[Finding] = []
    for rel in files[:max_files]:
        try:
            out += scan_source(rel, tools.read_file(rel))
        except ToolError:
            continue
    return len(files[:max_files]), out


TRIAGE_PROMPT = """You triage static-analysis findings in a Python/FastAPI repository.
For each finding decide if it is a REAL bug/risk or a FALSE POSITIVE given the code context.
Be conservative: only 'real' when you can explain a concrete failure or exploit.
Return ONLY JSON matching: {schema}"""


def triage(provider: LLMProvider, tools: RepoTools, findings: list[Finding]) -> list[Finding]:
    ranked = sorted(findings, key=lambda f: {"high": 0, "medium": 1, "low": 2}[f.severity])[:25]
    lines = []
    for i, f in enumerate(ranked):
        try:
            code = tools.read_file(f.file).splitlines()
            ctx = "\n".join(code[max(0, f.line - 6) : f.line + 5])
        except ToolError:
            ctx = f.snippet
        lines.append(f"[{i}] {f.rule} {f.file}:{f.line} - {f.message}\n```\n{ctx}\n```")
    resp = provider.complete(
        [
            ChatMessage(
                role="system",
                content=TRIAGE_PROMPT.format(schema=json.dumps(Triage.model_json_schema())),
            ),
            ChatMessage(role="user", content="\n\n".join(lines)),
        ],
        json_mode=True,
    )
    try:
        result = Triage.model_validate_json(resp.content)
    except ValidationError:
        return findings
    for item in result.items:
        if 0 <= item.id < len(ranked):
            f = ranked[item.id]
            f.verdict, f.severity, f.title, f.reasoning = (
                item.verdict,
                item.severity,
                item.title,
                item.reasoning,
            )
    return findings


def run_discovery(
    settings: Settings,
    provider: LLMProvider,
    store: TaskStore,
    task_id: str,
    *,
    use_model: bool = True,
) -> DiscoveryReport | None:
    """Execute a 'discover' task in Observe mode (read-only, metered, cancellable)."""
    rec = store.get(task_id)
    assert rec is not None
    ctx = TaskContext(
        task_id, Mode.OBSERVE, settings, Limits(**json.loads(rec.limits_json)), store, CancelToken()
    )
    store.update(task_id, status="running")
    try:
        tools = RepoTools(rec.repo)
        with ctx.tool("static_scan", Capability.READ, repo=rec.repo):
            n, findings = scan_repo(tools)
        ctx.event("step", f"{len(findings)} candidate findings in {n} files")
        triaged = False
        if use_model and findings:
            ctx.event("step", "model triage")
            findings = triage(MeteredProvider(provider, ctx), tools, findings)
            triaged = True
        report = DiscoveryReport(files_scanned=n, findings=findings, triaged=triaged)
        store.update(
            task_id, status="completed", outcome="report", result_json=report.model_dump_json()
        )
        ctx.event("task_end", "completed", findings=len(findings))
        return report
    except BudgetExceeded as exc:
        store.update(task_id, status="budget_exceeded", outcome="budget_exceeded", error=exc.reason)
    except Cancelled:
        store.update(task_id, status="cancelled", outcome="cancelled")
    except (ToolError, OSError) as exc:
        store.update(task_id, status="failed", outcome="failed", error=str(exc)[:500])
    finally:
        ctx.flush()
    return None
