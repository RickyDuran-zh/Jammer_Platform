"""
Agent orchestrator: connects LLM, tools, and UI data.
Each message is treated independently (no conversation history retention).
"""
from .llm_client import LLMClient
from .tools import ALL_TOOL_DEFS, ToolHandlers


class AgentBrain:
    """Central agent that manages LLM conversation with tool-use capabilities.

    Each user message starts a fresh conversation — no history retained between messages.
    """

    def __init__(self, api_key: str | None = None,
                 model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com"):
        self.tools = ToolHandlers()
        self.llm = LLMClient(api_key=api_key, model=model, base_url=base_url)
        self._enabled = bool(api_key)

        _handler_map = {
            "get_system_state": self.tools.handle_get_system_state,
            "get_channel_features": self.tools.handle_get_channel_features,
            "get_trend_history": self.tools.handle_get_trend_history,
            "get_channel_map_history": self.tools.handle_get_channel_map_history,
            "set_jamming_parameters": self.tools.handle_set_jamming_parameters,
            "analyze_current_situation": self.tools.handle_analyze_situation,
            "switch_control_mode": self.tools.handle_switch_control_mode,
            "execute_strategy": self.tools.handle_execute_strategy,
            "call_rl_inference": self.tools.handle_call_rl_inference,
            "cancel_strategy": self.tools.handle_cancel_strategy,
        }
        for tool_def in ALL_TOOL_DEFS:
            name = tool_def["function"]["name"]
            if name in _handler_map:
                self.llm.register_tool(tool_def, _handler_map[name])

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_api_key(self, key: str):
        self.llm.api_key = key
        self._enabled = bool(key)
        if key:
            import openai, certifi, httpx
            self.llm._client = openai.OpenAI(
                base_url=self.llm.base_url,
                api_key=key,
                http_client=httpx.Client(verify=certifi.where(), timeout=30.0),
            )

    def update_state(self, **kwargs):
        """Forward telemetry data to tool handlers."""
        self.tools.update_state(**kwargs)

    def send_message(self, user_message: str) -> str:
        """Send a single user message as a fresh conversation.

        No history is retained — each message is independent.
        The tool-calling loop runs within this single call.
        """
        if not self._enabled:
            return "[Agent] API密钥未配置。"

        try:
            result = self.llm.chat(user_message, None)  # None = no history
            return result.get("text", "[Agent] 未能获取回复。")
        except Exception as e:
            import traceback
            detail = traceback.format_exc()
            lines = detail.strip().split('\n')
            short = '\n'.join(lines[-4:]) if len(lines) > 4 else detail
            return f"[Agent] 调用LLM出错: {e}\n{short}"

    def set_on_set_params(self, callback):
        """Set callback for parameter change commands."""
        self.tools.set_on_set_params(callback)

    def set_on_control_mode(self, callback):
        """Set callback for switch_control_mode tool."""
        self.tools.set_on_control_mode(callback)

    def set_on_strategy(self, callback):
        """Set callback for execute_strategy tool."""
        self.tools.set_on_strategy(callback)

    def set_on_cancel_strategy(self, callback):
        """Set callback for cancel_strategy tool."""
        self.tools.set_on_cancel_strategy(callback)

    def set_bridge_status_provider(self, callback):
        """Set callback used by tools to report MATLAB command bridge status."""
        self.tools.set_bridge_status_provider(callback)
