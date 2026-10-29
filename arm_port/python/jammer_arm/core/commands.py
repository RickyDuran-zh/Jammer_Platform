"""Command application: port of ``apply_command`` in ``jammer_backend_loop.m``.

The UI/Agent side is unchanged, so the accepted messages are identical:

* ``{"type": "set_control_mode", "mode": "agent" | "rl"}`` - hand over control
* ``{"channel_idx": 4}`` / ``{"power_db": 12.5}`` / ``{"bw_mhz": 4}`` /
  ``{"waveform_mode": 1}`` - partial overrides applied to the held action.

Invalid values are ignored with a log line instead of being applied, matching the
MATLAB behaviour (bandwidth must be one of 2/4/6/8 MHz, waveform 0/1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import JammerConfig


@dataclass
class ControlState:
    """Controller ownership plus the action held by the agent."""

    agent_control_mode: bool = False
    messages: list[str] = field(default_factory=list)

    @property
    def control_label(self) -> str:
        return "agent" if self.agent_control_mode else "rl"


def apply_command(cmd: dict, action: dict, control: ControlState,
                  cfg: JammerConfig) -> dict:
    """Apply one command to ``action`` in place and return it."""
    if not isinstance(cmd, dict):
        return action

    if cmd.get("type") == "set_control_mode":
        mode = cmd.get("mode")
        if mode == "agent":
            control.agent_control_mode = True
            control.messages.append("[CMD] control mode -> agent")
        elif mode == "rl":
            control.agent_control_mode = False
            control.messages.append("[CMD] control mode -> rl")
        else:
            control.messages.append(f"[CMD] unsupported control mode {mode!r} ignored")
        return action

    num_channels = cfg.spectrum.num_channels

    if "channel_idx" in cmd:
        try:
            value = int(round(float(cmd["channel_idx"])))
        except (TypeError, ValueError):
            value = action["channel_idx"]
        value = max(0, min(value, num_channels - 1))
        action["channel_idx"] = value
        control.messages.append(f"[CMD] target channel -> {value + 1}")

    if "power_db" in cmd:
        try:
            value = float(cmd["power_db"])
        except (TypeError, ValueError):
            value = float(action["power_db"])
        action["power_db"] = max(cfg.actions.power_db_min,
                                 min(cfg.actions.power_db_max, value))
        control.messages.append(f"[CMD] power -> {action['power_db']:.1f} dB")

    if "bw_mhz" in cmd:
        raw = cmd["bw_mhz"]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = -1
        if value in cfg.actions.bw_choices_mhz:
            action["bw_mhz"] = value
            control.messages.append(f"[CMD] bandwidth -> {value} MHz")
        else:
            control.messages.append(
                f"[CMD] invalid bandwidth {raw!r} ignored "
                f"(allowed {list(cfg.actions.bw_choices_mhz)})"
            )

    if "waveform_mode" in cmd:
        raw = cmd["waveform_mode"]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = -1
        if value in cfg.actions.waveform_modes:
            action["waveform_mode"] = value
            label = "band-limited noise" if value == 0 else "multi-tone"
            control.messages.append(f"[CMD] waveform -> {label}")
        else:
            control.messages.append(
                f"[CMD] invalid waveform_mode {raw!r} ignored "
                f"(allowed {list(cfg.actions.waveform_modes)})"
            )

    return action
