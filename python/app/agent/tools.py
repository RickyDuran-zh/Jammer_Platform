"""
Tool definitions (OpenAI function-calling format) and handler factories for the jammer agent.
"""
import json
import inspect
import math
from typing import Callable

from .rl_client import request_rl_action
from .strategy_engine import STRATEGY_REGISTRY


# ── Helper to build an OpenAI-function-format tool definition ─────

def _tool(name: str, desc: str, params: dict | None = None,
          required: list[str] | None = None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": params or {},
                "required": required or [],
            },
        },
    }


# ── Tool definitions ──────────────────────────────────────────────

TOOL_GET_SYSTEM_STATE = _tool(
    "get_system_state",
    "获取当前系统实时状态，包括信噪比、检测带宽、目标信道、功率、干扰带宽、波形模式、决策算法、延迟和最新更新时间等KPI指标。",
)

TOOL_GET_CHANNEL_FEATURES = _tool(
    "get_channel_features",
    "获取所有10个信道的详细特征数据，包括每个信道的占用度(occupancy 0-1)、干扰水平(interference 0-1)、效用值(utility 0-1)和类信噪比指标(SNR-like 0-1)。",
)

TOOL_GET_TREND_HISTORY = _tool(
    "get_trend_history",
    "获取最近N帧(默认60)的历史趋势数据，包括信噪比序列、检测带宽序列(MHz)和功率序列(dB)，用于分析系统运行趋势。",
    {
        "num_frames": {
            "type": "integer",
            "description": "获取最近多少帧的数据，默认60帧，最大120帧",
        },
    },
)

TOOL_GET_CHANNEL_MAP_HISTORY = _tool(
    "get_channel_map_history",
    "获取信道状态热力图的历史数据。返回最近N帧的10信道状态矩阵，状态值: 0=空闲 1=用户占用 2=干扰 3=用户+干扰。用于分析频谱占用模式和干扰效果。",
    {
        "num_frames": {
            "type": "integer",
            "description": "获取最近多少帧的数据，默认30帧，最大120帧",
        },
    },
)

TOOL_SET_JAMMING_PARAMETERS = _tool(
    "set_jamming_parameters",
    "设置一次性干扰动作，自动切换到 Agent 控制并经现有命令桥入队给 MATLAB。可修改目标信道、功率、带宽和波形。",
    {
        "channel_idx": {
            "type": "integer",
            "description": "目标信道编号 0-9 (对应信道1-10)，不修改则省略",
        },
        "power_db": {
            "type": "number",
            "description": "发射功率 0-20 dB，步进5dB，不修改则省略",
        },
        "bw_mhz": {
            "type": "integer",
            "description": "干扰带宽 2/4/6/8 MHz，不修改则省略",
        },
        "waveform_mode": {
            "type": "integer",
            "description": "波形模式: 0=带限噪声(宽带压制) 1=多音干扰(精准干扰)，不修改则省略",
        },
    },
)

TOOL_ANALYZE_SITUATION = _tool(
    "analyze_current_situation",
    "获取当前完整的频谱态势数据(系统状态+信道特征+近期趋势+信道热力图)，用于综合分析。调用此工具后你会收到一份结构化的态势报告。",
)

TOOL_SWITCH_CONTROL_MODE = _tool(
    "switch_control_mode",
    "切换控制权：rl 为 MATLAB 逐帧直连 RL；agent 为 UI/Agent 下发动作。切换为 agent 后还需执行策略或设置参数。",
    {
        "mode": {
            "type": "string",
            "description": "控制模式: 'rl' 表示交给RL自动决策, 'agent' 表示由AI Agent控制",
            "enum": ["rl", "agent"],
        },
    },
    required=["mode"],
)

TOOL_EXECUTE_STRATEGY = _tool(
    "execute_strategy",
    "执行规则干扰战术：fixed 恒定、sweep 扫频、random 随机跳频、target_track 按占用度目标跟踪；另有 track_busiest/track_weakest/alternating/reactive。coerce/energy_save/deception 为逐帧调用 RL 并施加约束的实验预设。waveform_mode 是独立的噪声/多音波形选择。",
    {
        "strategy": {
            "type": "string",
            "description": "策略名称: fixed/sweep/random/target_track/track_busiest/track_weakest/alternating/reactive/coerce/energy_save/deception",
            "enum": ["fixed", "sweep", "random", "target_track", "track_busiest", "track_weakest", "alternating", "reactive", "coerce", "energy_save", "deception"],
        },
        "channel_idx": {"type": "integer", "description": "目标/初始信道 0-9 (fixed/target_track/reactive)"},
        "start_channel": {"type": "integer", "description": "随机跳频首个信道 0-9"},
        "channels": {"type": "array", "items": {"type": "integer"}, "description": "信道列表 (sweep/random/alternating，如[0,1,2,3,4,5,6,7,8,9])"},
        "dwell_frames": {"type": "integer", "description": "每信道驻留遥测帧数 (sweep/random/alternating)"},
        "power_db": {"type": "number", "description": "功率 0-20 dB，默认10"},
        "bw_mhz": {"type": "integer", "description": "带宽 2/4/6/8 MHz，默认4"},
        "waveform_mode": {"type": "integer", "description": "波形模式: 0=带限噪声 1=多音干扰，默认0"},
        "min_occupancy": {"type": "number", "description": "最低占用度阈值 0-1 (target_track/track_busiest策略)"},
        "switch_margin": {"type": "number", "description": "目标跟踪切换滞回阈值 0-1，默认0.05"},
        "noise_below_snr": {"type": "number", "description": "低于此SNR使用噪声 (reactive策略)"},
        "tone_above_snr": {"type": "number", "description": "高于此SNR使用多音 (reactive策略)"},
    },
    required=["strategy"],
)

TOOL_CALL_RL_INFERENCE = _tool(
    "call_rl_inference",
    "调用当前运行的RL决策算法(PPO/A2C/BDQ)推理一次动作，切换至 Agent 一次性控制并入队给 MATLAB。连续自主决策请使用 execute_strategy 的三种预设。",
)

TOOL_CANCEL_STRATEGY = _tool(
    "cancel_strategy",
    "取消当前正在执行的干扰策略，并将控制权交还给RL强化学习算法自动决策。",
)

ALL_TOOL_DEFS = [
    TOOL_GET_SYSTEM_STATE,
    TOOL_GET_CHANNEL_FEATURES,
    TOOL_GET_TREND_HISTORY,
    TOOL_GET_CHANNEL_MAP_HISTORY,
    TOOL_SET_JAMMING_PARAMETERS,
    TOOL_ANALYZE_SITUATION,
    TOOL_SWITCH_CONTROL_MODE,
    TOOL_EXECUTE_STRATEGY,
    TOOL_CALL_RL_INFERENCE,
    TOOL_CANCEL_STRATEGY,
]


# ── Handler registry ──────────────────────────────────────────────

class ToolHandlers:
    """Registry of tool handler functions that bridge LLM tools to UI data."""

    def __init__(self):
        self._system_state: dict = {}
        self._channel_features: list = []
        self._snr_history: list = []
        self._bw_history: list = []
        self._power_history: list = []
        self._channel_map_history: list = []
        self._prev_action: dict = {}
        self._on_set_params: Callable | None = None
        self._on_control_mode: Callable | None = None
        self._on_strategy: Callable | None = None
        self._on_cancel_strategy: Callable | None = None
        self._bridge_status_provider: Callable[[], bool] | None = None

    def update_state(self, *, system_state: dict | None = None,
                     channel_features: list | None = None,
                     snr_history: list | None = None,
                     bw_history: list | None = None,
                     power_history: list | None = None,
                     channel_map_history: list | None = None,
                     prev_action: dict | None = None):
        """Update cached state from the UI telemetry handler. Called each frame."""
        if system_state is not None:
            self._system_state = system_state
        if channel_features is not None:
            self._channel_features = channel_features
        if snr_history is not None:
            self._snr_history = snr_history
        if bw_history is not None:
            self._bw_history = bw_history
        if power_history is not None:
            self._power_history = power_history
        if channel_map_history is not None:
            self._channel_map_history = channel_map_history
        if prev_action is not None:
            self._prev_action = prev_action

    def set_on_set_params(self, callback: Callable):
        self._on_set_params = callback

    def set_on_control_mode(self, callback: Callable):
        self._on_control_mode = callback

    def set_on_strategy(self, callback: Callable):
        self._on_strategy = callback

    def set_on_cancel_strategy(self, callback: Callable):
        self._on_cancel_strategy = callback

    def set_bridge_status_provider(self, callback: Callable[[], bool]):
        self._bridge_status_provider = callback

    def _bridge_status_fields(self) -> dict:
        if self._bridge_status_provider is None:
            return {}
        try:
            if self._bridge_status_provider():
                return {"命令桥接状态": "MATLAB命令桥已连接"}
        except Exception:
            pass
        return {
            "warning": (
                "命令已生成，但MATLAB命令桥未连接；左侧KPI和信道状态图不会更新，"
                "直到MATLAB后端连接并回传遥测。"
            )
        }

    # ── Handler methods ──────────────────────────────────────────

    def handle_get_system_state(self, _input: dict) -> str:
        if not self._system_state:
            return json.dumps({"error": "暂无系统状态数据，请等待遥测数据到达"}, ensure_ascii=False)
        return json.dumps(self._system_state, ensure_ascii=False, indent=2)

    def handle_get_channel_features(self, _input: dict) -> str:
        if not self._channel_features:
            return json.dumps({"error": "暂无信道特征数据"}, ensure_ascii=False)
        feats = []
        for i, f in enumerate(self._channel_features):
            if len(f) >= 4:
                feats.append({
                    "信道": i + 1,
                    "占用度": round(float(f[0]), 3),
                    "干扰水平": round(float(f[1]), 3),
                    "效用值": round(float(f[2]), 3),
                    "SNR指标": round(float(f[3]), 3),
                })
        return json.dumps(feats, ensure_ascii=False, indent=2)

    def handle_get_trend_history(self, inp: dict) -> str:
        n = min(inp.get("num_frames", 60), 120)
        snr_list = list(self._snr_history)[-n:] if self._snr_history else []
        bw_list = list(self._bw_history)[-n:] if self._bw_history else []
        pw_list = list(self._power_history)[-n:] if self._power_history else []
        if not snr_list:
            return json.dumps({"error": "暂无趋势数据"}, ensure_ascii=False)
        result = {
            "帧数": len(snr_list),
            "信噪比_dB": [round(v, 2) for v in snr_list],
            "带宽_MHz": [round(v, 2) for v in bw_list],
            "功率_dB": [round(v, 2) for v in pw_list],
            "信噪比统计": {
                "当前": round(snr_list[-1], 2) if snr_list else 0,
                "均值": round(sum(snr_list) / len(snr_list), 2),
                "最小": round(min(snr_list), 2),
                "最大": round(max(snr_list), 2),
                "趋势": "上升" if len(snr_list) >= 2 and snr_list[-1] > snr_list[0] * 1.05 else (
                    "下降" if len(snr_list) >= 2 and snr_list[-1] < snr_list[0] * 0.95 else "稳定"),
            },
            "功率统计": {
                "当前": round(pw_list[-1], 2) if pw_list else 0,
                "均值": round(sum(pw_list) / len(pw_list), 2),
            },
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    def handle_get_channel_map_history(self, inp: dict) -> str:
        n = min(inp.get("num_frames", 30), 120)
        history = list(self._channel_map_history)[-n:] if self._channel_map_history else []
        if not history:
            return json.dumps({"error": "暂无信道状态历史数据"}, ensure_ascii=False)
        mat = []
        for frame in history:
            if hasattr(frame, 'tolist'):
                mat.append(frame.tolist())
            else:
                mat.append(list(frame))
        summary = []
        for ch in range(len(mat[0]) if mat else 0):
            states = [int(row[ch]) for row in mat if ch < len(row)]
            state_names = {0: "空闲", 1: "用户占用", 2: "干扰", 3: "用户+干扰"}
            counts = {}
            for s in states:
                counts[state_names.get(s, str(s))] = counts.get(state_names.get(s, str(s)), 0) + 1
            dominant = max(counts, key=counts.get) if counts else "未知"
            summary.append({
                "信道": ch + 1,
                "主要状态": dominant,
                "状态分布": {k: f"{v}/{len(states)}" for k, v in counts.items()},
            })
        return json.dumps({
            "帧数": len(mat),
            "信道数": len(mat[0]) if mat else 0,
            "逐信道摘要": summary,
        }, ensure_ascii=False, indent=2)

    def handle_set_jamming_parameters(self, inp: dict) -> str:
        if self._on_set_params is None:
            return json.dumps({"error": "控制通道未就绪，无法发送参数"}, ensure_ascii=False)
        params = {}
        for key in ["channel_idx", "power_db", "bw_mhz", "waveform_mode"]:
            if key in inp:
                val = inp[key]
                if key in ("channel_idx", "bw_mhz", "waveform_mode"):
                    params[key] = int(val)
                else:
                    params[key] = float(val)
        if not params:
            return json.dumps({"error": "未提供要修改的参数"}, ensure_ascii=False)
        if ("channel_idx" in params and not 0 <= params["channel_idx"] < 10
                or "power_db" in params and not (math.isfinite(params["power_db"])
                                                 and 0 <= params["power_db"] <= 20)
                or "bw_mhz" in params and params["bw_mhz"] not in (2, 4, 6, 8)
                or "waveform_mode" in params and params["waveform_mode"] not in (0, 1)):
            return json.dumps({"error": "动作越界：信道0-9、功率0-20、带宽2/4/6/8、波形0/1"}, ensure_ascii=False)
        try:
            self._on_set_params(params)
            result = {"success": True, "已发送参数": params}
            result.update(self._bridge_status_fields())
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": f"发送失败: {e}"}, ensure_ascii=False)

    def handle_analyze_situation(self, _input: dict) -> str:
        state = self._system_state or {}
        feats = self._channel_features or []
        snr_list = list(self._snr_history)[-60:] if self._snr_history else []
        bw_list = list(self._bw_history)[-60:] if self._bw_history else []
        pw_list = list(self._power_history)[-60:] if self._power_history else []
        chmap = list(self._channel_map_history)[-30:] if self._channel_map_history else []

        report = {}
        if state:
            report["系统状态"] = state

        if feats:
            ch_data = []
            for i, f in enumerate(feats):
                if len(f) >= 4:
                    ch_data.append({
                        "信道": i + 1,
                        "占用度": round(float(f[0]), 3),
                        "干扰水平": round(float(f[1]), 3),
                        "效用值": round(float(f[2]), 3),
                        "SNR指标": round(float(f[3]), 3),
                    })
            report["信道特征"] = ch_data

        if snr_list:
            snr_current = snr_list[-1]
            snr_mean = sum(snr_list) / len(snr_list)
            report["趋势分析"] = {
                "SNR当前": round(snr_current, 2),
                "SNR均值": round(snr_mean, 2),
                "SNR变化": "改善中" if len(snr_list) >= 5 and snr_list[-1] > snr_list[-5] * 1.03 else (
                    "恶化中" if len(snr_list) >= 5 and snr_list[-1] < snr_list[-5] * 0.97 else "基本稳定"),
                "功率当前": round(pw_list[-1], 2) if pw_list else 0,
                "带宽当前": round(bw_list[-1], 2) if bw_list else 0,
            }

        if chmap:
            last_frame = chmap[-1]
            if hasattr(last_frame, 'tolist'):
                last_frame = last_frame.tolist()
            jammed_channels = sum(1 for s in last_frame if s in (2, 3))
            user_channels = sum(1 for s in last_frame if s in (1, 3))
            report["干扰覆盖"] = {
                "被干扰信道数": jammed_channels,
                "用户占用信道数": user_channels,
                "总信道数": len(last_frame),
            }

        return json.dumps(report, ensure_ascii=False, indent=2)

    # ── New: Control mode & strategy handlers ─────────────────

    def handle_switch_control_mode(self, inp: dict) -> str:
        mode = inp.get("mode", "rl")
        if mode not in ("rl", "agent"):
            return json.dumps({"error": f"无效模式: {mode}，允许 rl 或 agent"}, ensure_ascii=False)
        if self._on_control_mode:
            self._on_control_mode(mode)
            result = {
                "success": True,
                "已请求控制模式": "UI/Agent下发动作" if mode == "agent" else "MATLAB直连RL自动决策",
            }
            result.update(self._bridge_status_fields())
            return json.dumps(result, ensure_ascii=False, indent=2)
        return json.dumps({"error": "控制模式回调未就绪"}, ensure_ascii=False)

    def handle_execute_strategy(self, inp: dict) -> str:
        strategy_name = inp.get("strategy", "")
        if strategy_name not in STRATEGY_REGISTRY:
            return json.dumps({"error": f"未知策略: {strategy_name}"}, ensure_ascii=False)

        # Build params from input, filtering None values
        params = {}
        param_keys = ["channel_idx", "start_channel", "channels", "dwell_frames", "power_db",
                      "bw_mhz", "waveform_mode", "min_occupancy", "switch_margin",
                      "noise_below_snr", "tone_above_snr"]
        for k in param_keys:
            if k in inp and inp[k] is not None:
                params[k] = inp[k]

        try:
            inspect.signature(STRATEGY_REGISTRY[strategy_name]).bind(**params)
            if ("channel_idx" in params and not 0 <= int(params["channel_idx"]) < 10
                    or "start_channel" in params and not 0 <= int(params["start_channel"]) < 10
                    or "channels" in params and
                    (not params["channels"] or any(not 0 <= int(ch) < 10 for ch in params["channels"]))
                    or "dwell_frames" in params and not 1 <= int(params["dwell_frames"]) <= 200
                    or "power_db" in params and not (math.isfinite(float(params["power_db"]))
                                                     and 0 <= float(params["power_db"]) <= 20)
                    or "bw_mhz" in params and int(params["bw_mhz"]) not in (2, 4, 6, 8)
                    or "waveform_mode" in params and int(params["waveform_mode"]) not in (0, 1)
                    or "min_occupancy" in params and not 0 <= float(params["min_occupancy"]) <= 1
                    or "switch_margin" in params and not 0 <= float(params["switch_margin"]) <= 1):
                raise ValueError("策略参数超出动作范围")
            if strategy_name == "random" and "channels" in params:
                if len(set(params["channels"])) < 2:
                    raise ValueError("随机跳频至少需要两个不同信道")
                if ("start_channel" in params and
                        params["start_channel"] not in params["channels"]):
                    raise ValueError("首个信道必须包含在随机跳频信道列表中")
        except (TypeError, ValueError) as exc:
            return json.dumps({"error": f"策略参数无效: {exc}"}, ensure_ascii=False)

        if self._on_strategy:
            try:
                self._on_strategy(strategy_name, params)
                result = {
                    "success": True,
                    "策略": strategy_name,
                    "参数": params,
                }
                result.update(self._bridge_status_fields())
                return json.dumps(result, ensure_ascii=False, indent=2)
            except Exception as e:
                return json.dumps({"error": f"策略执行失败: {e}"}, ensure_ascii=False)
        return json.dumps({"error": "策略回调未就绪"}, ensure_ascii=False)

    def handle_call_rl_inference(self, _inp: dict) -> str:
        if self._on_set_params is None:
            return json.dumps({"error": "控制通道未就绪，无法发送动作"}, ensure_ascii=False)
        if (len(self._channel_features) != 10 or
                any(len(row) != 4 for row in self._channel_features)):
            return json.dumps({"error": "需先收到完整的10信道特征，才能进行RL推理"}, ensure_ascii=False)
        result = request_rl_action(self._channel_features, self._prev_action)
        if result is None:
            return json.dumps({
                "error": "RL 服务不可用或未运行，可回退到 execute_strategy 规则策略",
            }, ensure_ascii=False)
        action, rl_meta = result
        try:
            self._on_set_params(action)
            resp = {
                "success": True,
                "已下发RL动作": action,
                "决策算法": rl_meta.get("policy", "unknown"),
                "value": rl_meta.get("value"),
                "latency_ms": rl_meta.get("latency_ms"),
            }
            resp.update(self._bridge_status_fields())
            return json.dumps(resp, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": f"RL动作下发失败: {e}"}, ensure_ascii=False)

    def handle_cancel_strategy(self, _inp: dict) -> str:
        if self._on_cancel_strategy:
            self._on_cancel_strategy()
            result = {"success": True, "message": "策略已取消，控制权交还RL"}
            result.update(self._bridge_status_fields())
            return json.dumps(result, ensure_ascii=False, indent=2)
        return json.dumps({"error": "取消回调未就绪"}, ensure_ascii=False)
