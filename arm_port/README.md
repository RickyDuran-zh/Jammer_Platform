# ARM 平台干扰机后端（RK3588 / LubanCat 5）

本目录把干扰机子系统的**射频后端从 MATLAB 完整移植到纯 Python**，运行在
RK3588（野火鲁班猫 5，16 GB / 128 GB）上，外接同一台 USRP X310。
既有的 PyQt 监控界面、RL 推理服务、Agent 工具链**一行未改**，直接复用。

```
                     ┌──────────── 既有 Python（未改动）────────────┐
                     │ PyQt UI :5555 遥测 / :5557 命令桥            │
                     │ RL 服务 :6001（PPO / A2C / BDQ）             │
                     └───────▲───────────────────────┬─────────────┘
                telemetry    │                       │ cmd_poll
                             │                       ▼
┌────────────────────────────┴───────────────────────────────────────┐
│ jammer_arm（本目录）                                                │
│  帧循环 core/backend.py                                             │
│    ├─ 命令 poll → apply（控制权 / 四元动作）                        │
│    ├─ RX 定长突发（NUM_SAMPS_AND_DONE）→ FFT 感知 → 10 信道特征      │
│    ├─ 决策：agent 保持 / RL 请求（失败回退上一动作）                │
│    ├─ 波形生成（带限噪声 / 多音，峰值 0.25）→ TX（互锁校验）        │
│    └─ 遥测打包（≤512 点抽稀）+ 状态快照                             │
│  设备层 radio/：uhd（主） | rfio（C++ 兜底） | mock（无硬件）        │
└────────────────────────────────────────────────────────────────────┘
```

---

## 快速开始

```bash
# 无硬件，30 秒跑通全栈（真实 DSP + 真实 UI + 真实 RL）
bash arm_port/scripts/run_stack.sh mock

# 只跑后端（不需要 UI/RL）
PYTHONPATH=arm_port/python python -m jammer_arm mock --duration 10

# 自检 + 单测
PYTHONPATH=arm_port/python python -m jammer_arm selftest
PYTHONPATH=arm_port/python python -m unittest discover -s arm_port/tests

# 板上：装依赖 → 编 UHD → 验收
bash arm_port/scripts/install_deps_rk3588.sh
bash arm_port/scripts/build_uhd.sh
bash arm_port/scripts/acceptance_test.sh
```

依赖只有 **numpy**（`requirements-arm.txt`）。torch 仍归 RL 服务、PyQt5 仍归 UI，
三者各自独立虚拟环境，互不牵连。

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/01_feasibility.md](docs/01_feasibility.md) | 可行性分析：UHD 在 ARM 的编译/运行、X310 驱动与实时性、带宽与时延预算、瓶颈、MATLAB 替代方案、方案选型、风险表 |
| [docs/02_plan.md](docs/02_plan.md) | 分阶段实施计划（P0–P7）与执行状态、里程碑门禁 |
| [docs/03_deployment.md](docs/03_deployment.md) | 板端部署手册：网络/巨帧、UHD 构建、systemd、调优、故障排查、回滚 |
| [docs/04_rfio_protocol.md](docs/04_rfio_protocol.md) | 可选 C++ 守护进程协议规范 |
| [docs/05_validation.md](docs/05_validation.md) | 验证报告：与 MATLAB 的逐点偏差、120 项测试、端到端记录、性能、未完成项 |

---

## 命令行

```
python -m jammer_arm <command>

run         实时后端：--radio uhd|rfio|mock（设备可换，DSP 不变）
mock        全合成后端（无设备），对应原 run_mock_backend_spectrum_control.m
doctor      上板前体检：python/numpy/uhd 绑定/端口/互锁/调频策略/socket 缓冲
selftest    8 项内置自检（配置、感知、波形、互锁、链路、循环、恢复）
bench       DSP 各阶段耗时与预算占用
assess      按 G4 判据评估状态快照（溢出/失败/帧时延/互锁一致性），返回非零即不合格
show-config 打印生效配置
```

常用参数：`--config FILE`、`--set key=value`（可重复）、`--frames N`、`--duration S`、
`--warmup N`（前 N 帧不计入稳定性统计）、`--status PATH`、`--control rl|agent`、
`--no-rl / --no-telemetry / --no-cmd`、`--quiet`。

板上验收：`run --status s.json --warmup 5` 之后用
`python -m jammer_arm assess --status s.json --min-frames 570` 判定，
7 条判据（溢出/接收失败/帧时延/帧错误/互锁一致性/TX 错误）任一不满足即返回非零。

---

## 模块地图

| 路径 | 作用 | 对应 MATLAB |
| --- | --- | --- |
| `python/jammer_arm/config.py` | 唯一配置源、校验、环境变量互锁 | `backend/jammer_config.m` |
| `python/jammer_arm/dsp/sensing.py` | FFT + 10 信道特征 + 干扰重叠标注 | `sensing/sense_channel_grid.m` |
| `python/jammer_arm/dsp/waveform.py` | 限带噪声 / 多音、FIR 设计、峰值归一 | `waveform/generate_jammer_signal.m` |
| `python/jammer_arm/radio/base.py` | 设备契约 + 发送前 fail-closed 校验 | `radio/tx_send_frame.m` |
| `python/jammer_arm/radio/uhd_device.py` | UHD MultiUSRP 收发 | `radio/radio_*_init.m` |
| `python/jammer_arm/radio/rfio.py` | C++ 守护进程客户端 | — |
| `python/jammer_arm/radio/mock.py` | 合成 IQ + 故障注入 | — |
| `python/jammer_arm/net/*.py` | 三条 JSON-Lines 链路 | `interface/*.m` |
| `python/jammer_arm/core/backend.py` | 帧循环、恢复阶梯、指标 | `backend/jammer_backend_loop.m` |
| `python/jammer_arm/core/mock_backend.py` | 全合成后端 | `run_mock_backend_spectrum_control.m` |
| `python/jammer_arm/cli.py` | 命令行入口 | `run_jammer_backend.m` |
| `cpp/` | 可选 C++ rfio 守护进程（UHD 直连） | — |
| `scripts/` | 安装 / 构建 / 运行 / 验收 | `scripts/run_platform.sh`（原） |
| `systemd/` | 三个常驻单元 + 环境模板 | — |
| `tools/matlab_reference/` | 从 MATLAB 导出黄金向量 | — |
| `tests/` | 120 项测试 + `data/reference.json` | — |

---

## 配置

默认值写在 `config.py`（等价于 `jammer_config.m`），站点差异写在
`config/jammer_arm.json`（或 `/etc/jammer/jammer_arm.json`），命令行可再覆盖。
`_` 开头的键是注释，会被忽略。

**射频互锁只有一条路**：进程环境变量 `ZH_RF_TX_ENABLE=LAB_ONLY`。

- 配置文件里的 `safety` 段、`tx_enabled`、`tx_baseband_peak` 等键会被**直接拒绝**；
- 每次发送前都会重读环境变量（不是启动时缓存的布尔值）；
- 未武装时 `tx_state="disabled"`，但感知、决策、遥测照常运行；
- `power_db=0` 是**最小增益，不是关闭发射**。

关键项（其余见 `--help` 与代码注释）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `radio.device_args` | `type=x300,addr=192.168.10.2` | UHD 设备参数 |
| `radio.sample_rate_hz` | 20e6 | 200e6 主时钟 / 10，与原 MATLAB 一致 |
| `radio.num_samples` | 4096 | 每帧样本数 |
| `radio.rx_mode` | `burst` | 定长突发；`continuous` 仅供高频消费实验 |
| `radio.tdm` | `true` | 连续模式下 TX 期间暂停 RX |
| `radio.tx_gain_max_db` | 10 | 增益硬上限 |
| `loop.frame_period_sec` | 0.08 | 12.5 fps（原 MATLAB 为 `pause(0.08)`） |
| `loop.mock_frame_period_sec` | 0.12 | 合成后端节奏（与原 mock 一致） |

---

## 行为等价性（关键）

移植按“等价替换”而非“重新设计”执行，因此：

| 项 | 偏差 |
| --- | --- |
| 10 信道特征（`channel_features`） | 最大 8.9e-16 |
| `channel_map` / `sig_type` | 完全一致 |
| `snr_est` / `bw_est` | 3.6e-15 / 0 Hz |
| 多音波形（4096 点） | 最大 2.8e-14（峰值 0.25） |
| `fir1(128, Wn)` 抽头 | 5.6e-17 |

因此 **RL 检查点无需重训**。详见 `docs/05_validation.md`。

帧循环保留原 MATLAB 的全部容错语义：RX 超时计数 → 重建接收 → 重建整链 → 缓存感知兜底；
单帧异常不终止循环；控制权切换不重放旧动作。新增的只有可观测性（耗时/计数/状态快照）。

---

## 与现有系统的关系

- **不修改** `matlab/`、`python/app/`、`python/rl_service/`、`scripts/run_platform.sh`；
- 三条链路的端口与报文结构保持不变，UI 与 RL 服务可同时服务两种后端（不要同时启动两个后端）；
- 原 Windows + MATLAB 路径仍然可用，回滚只需停掉新后端。

---

## 状态

| 项 | 状态 |
| --- | --- |
| 可行性分析 / 实施计划 | ✅ 完成 |
| Python 后端（配置、DSP、设备层、帧循环、CLI） | ✅ 完成，120 项测试通过 |
| 与 MATLAB 数值等价 | ✅ 机器精度一致 |
| 与真实 UI/RL 端到端 | ✅ 已验证（90+ 帧） |
| 部署脚本 / systemd / 验收脚本 | ✅ 完成 |
| RK3588 + X310 实机联调 | ⏳ 待执行（`docs/02_plan.md` P6） |
| C++ rfio 守护进程 | ✅ 两文件编译零错误/零告警（真实 UHD 4.9 头文件），线格式 40 项测试通过；⏳ 板端链接待执行 |

> 射频安全：真实发射仅限获得授权的屏蔽箱或充分衰减的有线链路。
> 使用者须自行遵守所在地频谱法规与实验室安全规范。
