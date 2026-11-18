"""Agent 的安全护栏：路径约束、危险命令拦截、环境变量清洗。

为什么单独一个模块：
文件工具和 shell 工具都要做「不能跑出 workspace」这件事，写在工具里会重复，
写在 Agent 主循环里又会把业务与安全搅在一起。抽出来之后，
任何新增工具只要调 `resolve_in_workspace()` 就自动获得同样的保护。

重要说明（安全边界必须说清楚）：
本模块是**黑名单 + 目录约束 + 超时 + 环境清洗**的组合，属于「降低风险」，
不是「彻底沙箱」。真要跑不可信代码，需要容器 / 低权限用户 / seccomp 级别的隔离。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# ---------------------------------------------------------------- 异常

class SafetyViolation(Exception):
    """触碰安全规则时抛出，由工具注册中心统一转换为 ToolResult.error。"""

    def __init__(self, message: str, code: str = "safety_violation") -> None:
        self.message = message
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------- 路径约束

def resolve_in_workspace(
    workspace: Path,
    raw_path: str | None,
    *,
    must_exist: bool = False,
    expect: str | None = None,
) -> Path:
    """把用户/LLM 给的路径解析为 workspace 内的绝对路径。

    关键做法是先 `resolve()` 再判断包含关系：`resolve()` 会展开 `..` 和符号链接，
    因此 `../etc/passwd`、`C:\\Windows\\System32`、指向外部的软链接都会被拦下，
    而「字符串前缀比较」做不到这一点。

    Args:
        workspace: 沙箱根目录（绝对路径）。
        raw_path: 相对路径或绝对路径；空值代表 workspace 根。
        must_exist: 是否要求路径已存在。
        expect: "file" / "dir" / None，存在时额外校验类型。

    Returns:
        解析后的绝对路径。

    Raises:
        SafetyViolation: 路径越界、类型不符或不存在。
    """
    root = workspace.resolve()
    text = (raw_path or ".").strip() or "."
    if "\x00" in text:
        raise SafetyViolation("路径中包含非法字符")

    candidate = Path(text)
    target = candidate if candidate.is_absolute() else (root / candidate)
    try:
        target = target.resolve(strict=False)
    except OSError as exc:  # 极少数平台会在超长路径上抛错
        raise SafetyViolation(f"路径解析失败：{exc}") from exc

    if target != root and root not in target.parents:
        raise SafetyViolation(
            f"路径越界：{raw_path!r} 不在工作区 {root.name}/ 内，已拒绝访问"
        )

    if must_exist and not target.exists():
        raise SafetyViolation(f"路径不存在：{raw_path!r}", code="not_found")

    if expect == "file" and target.exists() and not target.is_file():
        raise SafetyViolation(f"不是文件：{raw_path!r}", code="not_found")
    if expect == "dir" and target.exists() and not target.is_dir():
        raise SafetyViolation(f"不是目录：{raw_path!r}", code="not_found")

    return target


def to_workspace_relative(workspace: Path, path: Path) -> str:
    """把绝对路径转成相对 workspace 的 POSIX 风格路径，作为对外展示的稳定标识。"""
    try:
        relative = path.relative_to(workspace.resolve())
    except ValueError:
        return path.as_posix()
    text = relative.as_posix()
    return "." if text in ("", ".") else text


# ---------------------------------------------------------------- 命令拦截

# 每条规则 = (正则, 拒绝原因)。用 re.IGNORECASE 匹配。
# 说明：这是「常见破坏性操作」的护栏，不是完备的恶意命令检测器。
_DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+-[a-z]*[rf]", "禁止使用 rm 递归/强制删除"),
    (r"\brmdir\s+/s", "禁止递归删除目录"),
    (r"\bdel\s+/[sfq]", "禁止强制删除文件"),
    (r"\bformat\s+[a-z]:", "禁止格式化磁盘"),
    (r"\bdiskpart\b|\bmkfs\b|\bdd\s+if=", "禁止磁盘级操作"),
    (r"\bshutdown\b|\breboot\b|\bhalt\b", "禁止关机/重启"),
    (r"\btaskkill\b|\bkillall\b|\bkill\s+-9\b", "禁止结束系统进程"),
    (r"\breg\s+(add|delete|import|save)\b", "禁止修改注册表"),
    (r"\bsc\s+(config|delete|stop|start)\b", "禁止操作系统服务"),
    (r"\bnet\s+(user|localgroup|share|start)\b", "禁止操作用户/共享/服务"),
    (r"\bschtasks\b|\bcrontab\b", "禁止创建计划任务"),
    (r"\b(wmic|powershell|pwsh)\b", "禁止调用 PowerShell / WMIC"),
    (r"\bsudo\b|\brunas\b|\bicacls\b|\btakeown\b", "禁止提权或修改文件权限"),
    (r"\b(curl|wget|iwr|invoke-webrequest|invoke-restmethod)\b[^|]*\|\s*(sh|bash|zsh|python|node|powershell)",
     "禁止下载后直接执行远程脚本"),
    (r"\bcertutil\b.*-urlcache", "禁止借 certutil 下载文件"),
    (r"\b(chmod|chown)\s+(-r\s+)?777", "禁止开放全局读写权限"),
    (r"\b(pip|pip3|npm|yarn|pnpm|apt|apt-get|yum|brew)\s+(install|add)\b",
     "禁止在沙箱内安装依赖"),
    (r"\bpython3?\s+-c\b|\bnode\s+-e\b|\bperl\s+-e\b", "禁止用单行脚本执行任意代码"),
    (r"\bcd\s+\.\.|\bcd\s+[/~\\]", "禁止切换到工作区之外的目录"),
    (r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-z]*f)", "禁止破坏性 git 操作"),
    (r">\s*/dev/sd|>\s*[a-z]:\\\\", "禁止直接写裸设备/系统盘"),
)

_COMPILED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in _DANGEROUS_PATTERNS
)

# 只是「名字里带敏感词」的路径白名单豁免：避免检查 "grep -r password ."
_SENSITIVE_ENV_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")


def ensure_safe_command(command: str) -> None:
    """校验命令字符串，命中危险规则则抛 SafetyViolation。

    Args:
        command: 待执行的原始命令。

    Raises:
        SafetyViolation: 命中危险模式或格式非法。
    """
    text = (command or "").strip()
    if not text:
        raise SafetyViolation("命令不能为空", code="invalid_arguments")
    if len(text) > 2000:
        raise SafetyViolation("命令过长（>2000 字符），请拆分后执行", code="invalid_arguments")
    if "\n" in text or "\r" in text:
        raise SafetyViolation("命令中不允许包含换行（避免多行脚本逃逸）", code="invalid_arguments")

    for pattern, reason in _COMPILED_PATTERNS:
        if pattern.search(text):
            raise SafetyViolation(f"命令被安全策略拦截：{reason}", code="safety_violation")


def build_safe_env(workspace: Path) -> dict[str, str]:
    """构造子进程环境变量：去掉密钥类变量，固定工作目录相关变量。

    为什么要清洗：LLM 生成的命令完全可以用 `echo $LLM_API_KEY` 或 `printenv`
    把宿主环境里的密钥读出来，再通过 stdout 回传给模型。
    这里直接不把敏感变量传给子进程，从源头掐断这条链路。
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if any(marker in key.upper() for marker in _SENSITIVE_ENV_MARKERS):
            continue
        env[key] = value

    env["PWD"] = str(workspace)
    # 关闭 Python 的字节码缓存，避免沙箱里到处是 __pycache__
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # 统一输出编码，降低 Windows 控制台乱码概率
    env["PYTHONIOENCODING"] = "utf-8"
    return env
