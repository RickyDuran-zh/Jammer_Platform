# Jammer Platform Development Guide

## System role

This repository contains the jammer subsystem of an intelligent spectrum-control system for red-blue intelligent confrontation experiments. It is not a general-purpose emergency-communications application. The subsystem senses the spectrum, chooses an interference action through RL or Agent control, emits the selected waveform when explicitly armed, and returns telemetry to the monitoring UI.

## Architecture contract

The system has three cooperating processes:

| Process | Primary entry point | Responsibility |
| --- | --- | --- |
| MATLAB backend | matlab/backend/run_mock_backend_spectrum_control.m or matlab/backend/run_jammer_backend.m | Mock/live sensing, action consumption, waveform generation, TX and telemetry. |
| Python RL service | python/rl_service/rl_server.py | PPO, A2C or BDQ inference over channel features and the previous action. |
| Python UI and Agent | python/app/main_window.py | Monitoring, telemetry display, command queue, Agent tools and strategy orchestration. |

Transport contracts:

- MATLAB ↔ RL uses JSON Lines on 127.0.0.1:6001: state_req / action_rsp.
- MATLAB → UI uses JSON Lines on 127.0.0.1:5555: telemetry.
- MATLAB polls UI on 127.0.0.1:5557: cmd_poll / cmd_rsp.
- The common action schema is channel_idx (0–9), power_db (0–20), bw_mhz (2/4/6/8), and waveform_mode (0 noise, 1 multitone).

Keep the action schema, ports, JSON-Line framing, and the 10-channel configuration aligned across MATLAB, UI, Agent tools, RL codec, environment, training scripts, and README. Do not modify one side in isolation.

## Runtime and RF constraints

- matlab/backend/jammer_config.m is the source of truth for RF, signal, port, and safety configuration.
- The current live backend is one X310 using time-division RX then TX at 3 GHz, 20 MS/s and 10 logical 2 MHz channels.
- Hardware TX is disabled by default. It may only be armed in a legally authorized shielded or sufficiently attenuated cabled test setup through ZH_RF_TX_ENABLE=LAB_ONLY.
- Never weaken, remove, bypass, or silently set the TX interlock in code or startup tooling.
- Treat power_db=0 as minimum TX gain, not as an RF-off command.
- Do not commit API keys, credentials, .env files, telemetry captures, generated caches, logs, or unreviewed model artifacts.

## Development workflow

1. Inspect the existing config, interfaces, and callers before changing a subsystem.
2. Keep Mock mode runnable whenever changing runtime contracts. It is the primary end-to-end regression path when USRP hardware is unavailable.
3. Start the stack with bash scripts/run_platform.sh mock; use live --allow-rf only in an approved RF test environment.
4. For code changes, run relevant Python imports/static checks and, when possible, the MATLAB Mock path. Validate UI/RL/MATLAB port compatibility.
5. Keep MATLAB recovery behavior, UI telemetry handling, and RL fallback behavior intact unless the change intentionally revises the architecture.

## Documentation rule

Any new module or feature that changes architecture, process ownership, data flow, ports, action schema, RF configuration, operating modes, startup/development workflow, or safety behavior **must update README.md in the same change**. The README must describe the resulting current system, not a planned or obsolete design.

Update this AGENTS.md as well whenever project-level development constraints or architectural contracts change.

## Change boundaries

- Prefer focused changes over broad rewrites; preserve unrelated user work.
- Use the existing explicit interfaces instead of creating duplicate control or telemetry paths.
- Add generated files to .gitignore rather than committing them.
- For a new RL algorithm, update the server CLI, codec/environment compatibility, training entry point, checkpoint behavior, README, and this guide if the architecture contract changes.
- For new waveform modes, update MATLAB generation, validation, UI labels, Agent tool definitions, action documentation, tests, and README together.
