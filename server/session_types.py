"""Types for the WebSocket bridge between Claude Code CLI and the browser.

Ported from companion/web/server/session-types.ts.
Field names are kept identical to the TypeScript version for wire compatibility.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, TypedDict, Union


# ─── CLI Message Types (NDJSON from Claude Code CLI) ──────────────────────────


class McpServer(TypedDict):
    name: str
    status: str


class _CLISystemInitMessageOptional(TypedDict, total=False):
    agents: List[str]
    skills: List[str]


class CLISystemInitMessage(_CLISystemInitMessageOptional):
    type: Literal["system"]
    subtype: Literal["init"]
    cwd: str
    session_id: str
    tools: List[str]
    mcp_servers: List[McpServer]
    model: str
    permissionMode: str
    apiKeySource: str
    claude_code_version: str
    slash_commands: List[str]
    output_style: str
    uuid: str


class _CLISystemStatusMessageOptional(TypedDict, total=False):
    permissionMode: str


class CLISystemStatusMessage(_CLISystemStatusMessageOptional):
    type: Literal["system"]
    subtype: Literal["status"]
    status: Optional[Literal["compacting"]]
    uuid: str
    session_id: str


class _UsageTokens(TypedDict):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


class _AssistantInnerMessage(TypedDict):
    id: str
    type: Literal["message"]
    role: Literal["assistant"]
    model: str
    content: List[ContentBlock]
    stop_reason: Optional[str]
    usage: _UsageTokens


class _CLIAssistantMessageOptional(TypedDict, total=False):
    error: str


class CLIAssistantMessage(_CLIAssistantMessageOptional):
    type: Literal["assistant"]
    message: _AssistantInnerMessage
    parent_tool_use_id: Optional[str]
    uuid: str
    session_id: str


class _ModelUsageEntry(TypedDict):
    inputTokens: int
    outputTokens: int
    cacheReadInputTokens: int
    cacheCreationInputTokens: int
    contextWindow: int
    maxOutputTokens: int
    costUSD: float


class _CLIResultMessageOptional(TypedDict, total=False):
    result: str
    errors: List[str]
    modelUsage: Dict[str, _ModelUsageEntry]
    total_lines_added: int
    total_lines_removed: int


class CLIResultMessage(_CLIResultMessageOptional):
    type: Literal["result"]
    subtype: Literal[
        "success",
        "error_during_execution",
        "error_max_turns",
        "error_max_budget_usd",
        "error_max_structured_output_retries",
    ]
    is_error: bool
    duration_ms: float
    duration_api_ms: float
    num_turns: int
    total_cost_usd: float
    stop_reason: Optional[str]
    usage: _UsageTokens
    uuid: str
    session_id: str


class CLIStreamEventMessage(TypedDict):
    type: Literal["stream_event"]
    event: Any
    parent_tool_use_id: Optional[str]
    uuid: str
    session_id: str


class CLIToolProgressMessage(TypedDict):
    type: Literal["tool_progress"]
    tool_use_id: str
    tool_name: str
    parent_tool_use_id: Optional[str]
    elapsed_time_seconds: float
    uuid: str
    session_id: str


class CLIToolUseSummaryMessage(TypedDict):
    type: Literal["tool_use_summary"]
    summary: str
    preceding_tool_use_ids: List[str]
    uuid: str
    session_id: str


class _ControlRequestPayloadOptional(TypedDict, total=False):
    permission_suggestions: List[PermissionUpdate]
    description: str
    agent_id: str


class _ControlRequestPayload(_ControlRequestPayloadOptional):
    subtype: Literal["can_use_tool"]
    tool_name: str
    input: Dict[str, Any]
    tool_use_id: str


class CLIControlRequestMessage(TypedDict):
    type: Literal["control_request"]
    request_id: str
    request: _ControlRequestPayload


class CLIKeepAliveMessage(TypedDict):
    type: Literal["keep_alive"]


class _CLIAuthStatusMessageOptional(TypedDict, total=False):
    error: str


class CLIAuthStatusMessage(_CLIAuthStatusMessageOptional):
    type: Literal["auth_status"]
    isAuthenticating: bool
    output: List[str]
    uuid: str
    session_id: str


CLIMessage = Union[
    CLISystemInitMessage,
    CLISystemStatusMessage,
    CLIAssistantMessage,
    CLIResultMessage,
    CLIStreamEventMessage,
    CLIToolProgressMessage,
    CLIToolUseSummaryMessage,
    CLIControlRequestMessage,
    CLIKeepAliveMessage,
    CLIAuthStatusMessage,
]


# ─── Content Block Types ─────────────────────────────────────────────────────


class TextBlock(TypedDict):
    type: Literal["text"]
    text: str


class ToolUseBlock(TypedDict):
    type: Literal["tool_use"]
    id: str
    name: str
    input: Dict[str, Any]


class _ToolResultBlockOptional(TypedDict, total=False):
    is_error: bool


class ToolResultBlock(_ToolResultBlockOptional):
    type: Literal["tool_result"]
    tool_use_id: str
    content: Union[str, List[ContentBlock]]


class _ThinkingBlockOptional(TypedDict, total=False):
    budget_tokens: int


class ThinkingBlock(_ThinkingBlockOptional):
    type: Literal["thinking"]
    thinking: str


ContentBlock = Union[TextBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock]


# ─── Browser Message Types (browser <-> bridge) ──────────────────────────────

# -- Messages the browser sends to the bridge --


class _ImageData(TypedDict):
    media_type: str
    data: str


class _BrowserUserMessageOptional(TypedDict, total=False):
    session_id: str
    images: List[_ImageData]


class BrowserUserMessage(_BrowserUserMessageOptional):
    type: Literal["user_message"]
    content: str


class _BrowserPermissionResponseOptional(TypedDict, total=False):
    updated_input: Dict[str, Any]
    updated_permissions: List[PermissionUpdate]
    message: str


class BrowserPermissionResponse(_BrowserPermissionResponseOptional):
    type: Literal["permission_response"]
    request_id: str
    behavior: Literal["allow", "deny"]


class BrowserInterrupt(TypedDict):
    type: Literal["interrupt"]


class BrowserSetModel(TypedDict):
    type: Literal["set_model"]
    model: str


class BrowserSetPermissionMode(TypedDict):
    type: Literal["set_permission_mode"]
    mode: str


BrowserOutgoingMessage = Union[
    BrowserUserMessage,
    BrowserPermissionResponse,
    BrowserInterrupt,
    BrowserSetModel,
    BrowserSetPermissionMode,
]

# -- Messages the bridge sends to the browser --


class BrowserSessionInit(TypedDict):
    type: Literal["session_init"]
    session: SessionState


class BrowserSessionUpdate(TypedDict):
    type: Literal["session_update"]
    session: Dict[str, Any]  # Partial<SessionState>


class BrowserAssistant(TypedDict):
    type: Literal["assistant"]
    message: _AssistantInnerMessage
    parent_tool_use_id: Optional[str]


class BrowserStreamEvent(TypedDict):
    type: Literal["stream_event"]
    event: Any
    parent_tool_use_id: Optional[str]


class BrowserResult(TypedDict):
    type: Literal["result"]
    data: CLIResultMessage


class BrowserPermissionRequest(TypedDict):
    type: Literal["permission_request"]
    request: PermissionRequest


class BrowserPermissionCancelled(TypedDict):
    type: Literal["permission_cancelled"]
    request_id: str


class BrowserToolProgress(TypedDict):
    type: Literal["tool_progress"]
    tool_use_id: str
    tool_name: str
    elapsed_time_seconds: float


class BrowserToolUseSummary(TypedDict):
    type: Literal["tool_use_summary"]
    summary: str
    tool_use_ids: List[str]


class BrowserStatusChange(TypedDict):
    type: Literal["status_change"]
    status: Optional[Literal["compacting", "idle", "running"]]


class _BrowserAuthStatusOptional(TypedDict, total=False):
    error: str


class BrowserAuthStatus(_BrowserAuthStatusOptional):
    type: Literal["auth_status"]
    isAuthenticating: bool
    output: List[str]


class BrowserError(TypedDict):
    type: Literal["error"]
    message: str


class BrowserCliDisconnected(TypedDict):
    type: Literal["cli_disconnected"]


class BrowserCliConnected(TypedDict):
    type: Literal["cli_connected"]


class BrowserUserMessageIncoming(TypedDict):
    type: Literal["user_message"]
    content: str
    timestamp: float


class BrowserMessageHistory(TypedDict):
    type: Literal["message_history"]
    messages: List[BrowserIncomingMessage]


class BrowserSessionNameUpdate(TypedDict):
    type: Literal["session_name_update"]
    name: str


BrowserIncomingMessage = Union[
    BrowserSessionInit,
    BrowserSessionUpdate,
    BrowserAssistant,
    BrowserStreamEvent,
    BrowserResult,
    BrowserPermissionRequest,
    BrowserPermissionCancelled,
    BrowserToolProgress,
    BrowserToolUseSummary,
    BrowserStatusChange,
    BrowserAuthStatus,
    BrowserError,
    BrowserCliDisconnected,
    BrowserCliConnected,
    BrowserUserMessageIncoming,
    BrowserMessageHistory,
    BrowserSessionNameUpdate,
]


# ─── Session State ────────────────────────────────────────────────────────────

BackendType = Literal["claude", "codex", "terminal"]


class _SessionStateOptional(TypedDict, total=False):
    backend_type: BackendType


class SessionState(_SessionStateOptional):
    session_id: str
    model: str
    cwd: str
    tools: List[str]
    permissionMode: str
    claude_code_version: str
    mcp_servers: List[McpServer]
    agents: List[str]
    slash_commands: List[str]
    skills: List[str]
    total_cost_usd: float
    num_turns: int
    context_used_percent: float
    is_compacting: bool
    git_branch: str
    is_worktree: bool
    repo_root: str
    git_ahead: int
    git_behind: int
    total_lines_added: int
    total_lines_removed: int


# ─── Permission Types ────────────────────────────────────────────────────────

PermissionDestination = Literal[
    "userSettings",
    "projectSettings",
    "localSettings",
    "session",
    "cliArg",
]


class _PermissionRuleOptional(TypedDict, total=False):
    ruleContent: str


class _PermissionRule(_PermissionRuleOptional):
    toolName: str


class PermissionAddRules(TypedDict):
    type: Literal["addRules"]
    rules: List[_PermissionRule]
    behavior: Literal["allow", "deny", "ask"]
    destination: PermissionDestination


class PermissionReplaceRules(TypedDict):
    type: Literal["replaceRules"]
    rules: List[_PermissionRule]
    behavior: Literal["allow", "deny", "ask"]
    destination: PermissionDestination


class PermissionRemoveRules(TypedDict):
    type: Literal["removeRules"]
    rules: List[_PermissionRule]
    behavior: Literal["allow", "deny", "ask"]
    destination: PermissionDestination


class PermissionSetMode(TypedDict):
    type: Literal["setMode"]
    mode: str
    destination: PermissionDestination


class PermissionAddDirectories(TypedDict):
    type: Literal["addDirectories"]
    directories: List[str]
    destination: PermissionDestination


class PermissionRemoveDirectories(TypedDict):
    type: Literal["removeDirectories"]
    directories: List[str]
    destination: PermissionDestination


PermissionUpdate = Union[
    PermissionAddRules,
    PermissionReplaceRules,
    PermissionRemoveRules,
    PermissionSetMode,
    PermissionAddDirectories,
    PermissionRemoveDirectories,
]


class _PermissionRequestOptional(TypedDict, total=False):
    permission_suggestions: List[PermissionUpdate]
    description: str
    agent_id: str


class PermissionRequest(_PermissionRequestOptional):
    request_id: str
    tool_name: str
    input: Dict[str, Any]
    tool_use_id: str
    timestamp: float
