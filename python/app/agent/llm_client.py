"""
LLM client for the spectrum jammer AI agent.
Uses OpenAI-compatible API (DeepSeek by default).
"""
import json
import os
from typing import Any, Callable

MAX_TOOL_ROUNDS = 5  # max rounds of tool-calling before forcing a final answer


class LLMClient:
    """Wraps OpenAI-compatible API with multi-round tool-calling support."""

    SYSTEM_PROMPT = """你是频谱智能干扰控制系统的AI智能体。你**不只是聊天**，你可以**主动控制干扰系统**。

## 你的能力
1. 读取实时频谱数据 (SNR/信道特征/趋势/热力图)
2. 控制干扰系统: 切换RL/Agent模式、执行干扰策略、修改参数
3. 回答通用技术问题 (通信/信号处理/机器学习)

## 如何控制干扰系统
用户说"切换到Agent控制，扫描所有信道"时，你应该:
  1. switch_control_mode("agent")  — 接管控制权
  2. execute_strategy("sweep", {channels:[0..9], dwell_frames:25, power_db:10}) — 启动策略

用户说"把干扰调到信道5，功率15dB"时:
  1. switch_control_mode("agent")  — 先接管
  2. execute_strategy("fixed", {channel_idx:4, power_db:15})  — 或 set_jamming_parameters

用户说"交还给RL"时:
  cancel_strategy()

## 4种基础干扰战术 + 扩展规则 + 3种预设策略
- fixed(恒定): 固定信道和参数；sweep(扫频): 按channels顺序并以dwell_frames驻留
- random(随机跳频): 在channels中随机选择下个信道并避免连续重复；target_track(目标跟踪): 根据10信道占用度及min_occupancy跟踪目标
- 扩展规则: track_busiest、track_weakest、alternating、reactive
- coerce(强制型): 偏高功率和宽带的预设
- energy_save(能量节约型): 偏低功率和窄带的预设；0 dB 是最小增益，不能关闭发射
- deception(欺骗诱导型): 波形/信道变化实验预设；不是已验证的诱骗算法
基础干扰战术由UI策略引擎逐帧生成四元动作交给MATLAB执行；waveform_mode仅控制噪声/多音波形，不等于上述战术。
以上三种预设持续调用当前RL服务取得候选动作，再施加类型约束；服务不可用时用规则回退。

## 调用RL决策算法
如需一次性学习型决策，可调用 call_rl_inference() 让 RL(PPO/A2C/BDQ)
根据实时信道特征推理动作并下发；此工具会切换成 Agent 一次性控制。
若用户要持续自主决策，执行三种预设之一。

## 原则
- 执行操作前先简要分析，操作后汇报结果
- 参数范围: 信道0-9(对应1-10), 功率0-20dB, 带宽2/4/6/8MHz, 模式0=噪声 1=多音
- 通用问题直接回答，不强行调用工具
- 中文回复，简洁专业 (控制在200字以内除非用户要求详细分析)
- **如果需要同时读取数据和控制，在同一个响应中调用多个工具**
"""

    def __init__(self, api_key: str | None = None,
                 model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com"):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.model = model
        self.base_url = base_url
        self._tools: list[dict] = []
        self._tool_handlers: dict[str, Callable] = {}

        import openai, certifi, httpx
        self._client = openai.OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            http_client=httpx.Client(verify=certifi.where(), timeout=20.0),
        )

    def register_tool(self, definition: dict, handler: Callable[[dict], str]):
        self._tools.append(definition)
        self._tool_handlers[definition["function"]["name"]] = handler

    def _call_llm(self, messages: list[dict]) -> dict:
        """Single LLM call. Returns parsed response."""
        kwargs = dict(model=self.model, messages=messages, max_tokens=1024)
        if self._tools:
            kwargs["tools"] = self._tools
            kwargs["tool_choice"] = "auto"

        response = self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        result = {"text": choice.message.content or "", "tool_calls": []}
        if choice.message.tool_calls:
            print(f"[LLM] ★ 返回 {len(choice.message.tool_calls)} 个工具调用: "
                  f"{[tc.function.name for tc in choice.message.tool_calls]}")
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                print(f"[LLM]    {tc.function.name}({json.dumps(args, ensure_ascii=False)})")
                result["tool_calls"].append({
                    "name": tc.function.name, "input": args,
                    "id": tc.id, "raw_args": tc.function.arguments,
                })
        else:
            text_preview = (result["text"] or "")[:120]
            print(f"[LLM] 纯文本回复 (无工具调用): {text_preview}")
        return result

    def _execute_tools(self, tool_calls: list[dict]) -> list[dict]:
        """Execute tool calls. Returns tool result messages."""
        tool_msgs = []
        for tc in tool_calls:
            print(f"[LLM] ▷ 执行工具: {tc['name']}")
            handler = self._tool_handlers.get(tc["name"])
            if handler:
                try:
                    output = handler(tc["input"])
                except Exception as e:
                    output = f"工具错误: {e}"
                    print(f"[LLM]   ✗ 工具执行异常: {e}")
            else:
                output = f"未知工具: {tc['name']}"
                print(f"[LLM]   ✗ 工具未注册: {tc['name']}")
            tool_msgs.append({
                "role": "tool", "tool_call_id": tc["id"], "content": output,
            })
        return tool_msgs

    def chat(self, user_message: str, conversation_history: list[dict] | None = None) -> dict:
        """Send a message, handle multi-round tool calls, return final response."""
        print(f"[LLM] 收到用户消息: {user_message[:80]}...")
        messages = [{"role": "system", "content": self.SYSTEM_PROMPT}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})

        final_text = ""

        for round_num in range(MAX_TOOL_ROUNDS):
            response = self._call_llm(messages)

            # If no tool calls, this is the final answer
            if not response["tool_calls"]:
                final_text = response["text"]
                break

            # Build assistant message with tool calls
            assistant_msg = {
                "role": "assistant",
                "content": response["text"],
                "tool_calls": [
                    {
                        "id": tc["id"], "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["raw_args"]},
                    }
                    for tc in response["tool_calls"]
                ],
            }
            messages.append(assistant_msg)

            # Execute tools
            tool_msgs = self._execute_tools(response["tool_calls"])
            messages.extend(tool_msgs)

            # If this was the last allowed round, ask LLM to summarize
            if round_num == MAX_TOOL_ROUNDS - 1:
                messages.append({
                    "role": "user",
                    "content": "请基于以上所有工具返回的数据，给出最终分析和建议。"
                })

        # If we exhausted rounds without final text, make one last call
        if not final_text:
            final_response = self._call_llm(messages)
            final_text = final_response["text"] or "[Agent] 未能生成回复，请重试。"

        return {"text": final_text, "messages": messages}
