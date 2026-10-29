# 分阶段实施计划与执行状态

> 本文件既是**计划**，也是**执行记录**。每个阶段都写明交付物、验收标准与当前状态；
> 状态为「已完成」的条目都有可复现的证据（测试、脚本输出、端到端日志），
> 状态为「待硬件」的条目必须在 RK3588 板卡上执行，开发机无法替代。

总工作量估算：**约 19 人日**（不含硬件采购与射频场地审批）。其中 P0–P5 已完成，
P6 需要 3–5 人日的现场时间。

---

## 阶段总览

| 阶段 | 目标 | 主要交付物 | 验收标准 | 工作量 | 状态 |
| --- | --- | --- | --- | ---: | --- |
| P0 | 基线与环境确认 | 模块清单、接口契约、黄金向量 | MATLAB 能导出参考数据 | 1 d | ✅ 已完成 |
| P1 | 配置 + DSP 等价实现 | `config.py`, `dsp/sensing.py`, `dsp/waveform.py` | 与 MATLAB 逐点一致 | 4 d | ✅ 已完成 |
| P2 | 射频抽象层 | `radio/{base,uhd_device,mock,rfio}.py` | 三套设备同一契约，互锁 fail-closed | 3 d | ✅ 已完成 |
| P3 | 帧循环 + 三条链路 | `core/backend.py`, `net/*.py`, `cli.py` | 协议与既有 UI/RL 完全兼容 | 4 d | ✅ 已完成 |
| P4 | 端到端联调 | 与真实 PyQt UI、真实 RL 服务联跑 | UI 正常显示，控制权可切换 | 2 d | ✅ 已完成 |
| P5 | 部署工程化 | 构建/安装/验收脚本、systemd、CLI 测试、文档 | 一条命令拉起全栈 | 3 d | ✅ 已完成 |
| P6 | 板端硬件联调与调优 | 实测报告、调参记录 | 600 帧零溢出、零重建 | 3–5 d | ⏳ 待硬件 |
| P7 | C++ rfio 兜底（条件触发） | `cpp/` 守护进程 + 协议/线格式测试 | 编译零错误 + 线格式测试通过 | 2 d | ✅ 已完成（待板端链接） |

---

## P0 — 基线与环境确认

**做什么**

1. 盘点 MATLAB 侧职责边界：6 个功能文件、8 项能力（见 `01_feasibility.md` §1）。
2. 冻结接口契约：端口（6001/5555/5557）、报文结构、四元动作、互锁语义。
3. 在装有 MATLAB 的工作站上生成黄金向量：`arm_port/tests/data/reference.json`
   （4 组感知输入、3 组多音波形、12 组 FIR 设计、1 组滤波用例）。

**验收**：`matlab -batch "addpath('arm_port/tools/matlab_reference'); dump_reference"`
输出 `wrote .../reference.json (7 cases)`。

**状态**：✅ 已完成。参考数据 1.8 MB，随仓库提交（不含任何遥测采集数据或密钥）。

---

## P1 — 配置与 DSP 等价实现

**做什么**

| 模块 | 对应 MATLAB | 关键点 |
| --- | --- | --- |
| `config.py` | `jammer_config.m` | 唯一配置源；`safety` 段**不可配置**；`tx_enabled` 只读环境变量 |
| `dsp/sensing.py` | `sense_channel_grid.m` | `std(ddof=1)`；`N=max(4096,len)`；干扰重叠标注 |
| `dsp/waveform.py` | `generate_jammer_signal.m` | 自实现 `fir1` 等价设计；边缘信道带宽裁剪；峰值归一到 0.25 |

**验收**

- 感知：`channel_features` 误差 ≤ 1e-12，`channel_map` 完全一致，`snr_est/bw_est/sig_type` 一致；
- 波形：多音模式**逐样本一致**（rtol 1e-10）；噪声模式比对带内能量占比与峰值；
- FIR：12 组抽头误差 ≤ 1e-13。

**状态**：✅ 已完成，`tests/test_equivalence.py` 5 项全绿。

---

## P2 — 射频抽象层

**做什么**

- `RadioDevice` 统一契约：`open/close/receive/transmit/rebuild_rx/rebuild`。
- `UhdRadio`：UHD Python API；**定长突发收流**（`NUM_SAMPS_AND_DONE`）；增益设置多版本兼容；
  元数据错误分类（timeout/overflow/late）；TDM 仅在连续模式下暂停 RX。
- `MockRadio`：合成 IQ + 故障注入（`fail_next`/`fail_always`）+ TX 记录（不辐射）。
- `RfioRadio`：C++ 守护进程客户端。
- 互锁：`guard_transmit()` 在每次发送前**重读环境变量**，并校验帧非空/有限/峰值。

**验收**

- 未武装时任何发送路径都抛 `InterlockError`，且 `tx_state="disabled"`；
- 已武装时帧峰值恰为配置值，增益被裁剪到 `[0,10]`；
- 使用假 `uhd` 模块验证配置顺序、突发命令、错误映射、重建行为（17 项测试）。

**状态**：✅ 已完成，`tests/test_uhd_device.py`、`test_rfio.py`、`test_backend.py` 覆盖。

---

## P3 — 帧循环与三条链路

**做什么**

逐条复刻 `jammer_backend_loop.m` 的行为顺序与容错阶梯：

```
轮询命令 → RX（含恢复阶梯）→ 感知（含缓存兜底）→ 决策（agent/rl）
        → 生成波形 → TX（互锁）→ 遥测 → 定周期等待
```

新增（不改变语义）：每帧耗时/DSP 耗时/超时/重建计数、JSON 状态快照、
遥测中的 `hw_actuals`（实际带宽与增益，追加字段，旧 UI 忽略）、`executed_action`。

**验收**

- 12 项协议测试 + 13 项后端测试：报文逐字段比对、越界动作裁剪、RL 断线回退、命令桥无效值忽略、单帧异常隔离；
- 帧循环：控制权切换不重放旧动作、RX 故障后继续出遥测、`tx_state` 正确。

**状态**：✅ 已完成。

---

## P4 — 端到端联调

**做什么**：不启动 MATLAB，改为启动**真实**的 PyQt UI + **真实**的 PPO/A2C/BDQ 服务 +
本移植后端（`--radio mock`，真实 DSP 跑在合成 IQ 上）。

**实测结果**

```
[Telemetry] MATLAB connected from ('127.0.0.1', 65530)
[CmdBridge] MATLAB connected from ('127.0.0.1', 65531)
[UI] 遥测帧#31 模式=RL自动 策略=False
[UI] 遥测帧#61 ...
[UI] 遥测帧#91 ...
[jammer_arm] frames: 84 in 10.1s (8.33 fps) | dsp max 1.39 ms | rx failures 0 | overruns 0
```

**验收**：UI 连续收到 90+ 帧遥测、命令桥双向连通、RL 服务返回 `policy=ppo`。

**状态**：✅ 已完成（`05_validation.md` 有完整记录）。

**顺带发现的问题（非本次移植引入）**：仓库随附的 PPO/A2C/BDQ 检查点对输入状态不敏感
（同一 argmax 动作，value 随状态变化）。这是 bootstrap 权重的性质，与后端无关；
本移植在验收中以「协议与算术正确」为准，不宣称策略有效性。

---

## P5 — 部署工程化

**做什么**

| 交付物 | 用途 |
| --- | --- |
| `scripts/install_deps_rk3588.sh` | apt 依赖、venv、`sysctl` 网络调优、`setcap` 实时优先级 |
| `scripts/build_uhd.sh` | 源码构建 UHD 4.9 + Python 绑定 + `.pth` 注入 + 自检 |
| `scripts/build_cpp.sh` | 构建 `jammer-rfio` |
| `scripts/run_stack.sh` | 一条命令拉起 UI + RL + 后端（mock / live） |
| `scripts/acceptance_test.sh` | 5 步验收：doctor → selftest → 单测 → bench → live |
| `systemd/*.service` | 三个常驻单元 + 环境文件模板（互锁默认注释掉） |
| `docs/03_deployment.md` | 现场部署手册 + 故障排查表 |

**验收**：`run_stack.sh --dry-run` 打印计划；`acceptance_test.sh --skip-live` 全绿。

**状态**：✅ 已完成（脚本在开发机以 `--dry-run`/`--skip-live` 验证；Linux 专属部分
如 `setcap`/`sysctl`/systemd 需在板上执行）。

---

## P6 — 板端硬件联调与调优（待执行）

按此顺序推进，任一环节不通过就停在该环节排查，**不要跳步**：

| 步骤 | 命令 | 通过判据 |
| ---: | --- | --- |
| 1 | `uhd_find_devices` | 打印 X310 的 IP 与序列号 |
| 2 | `uhd_usrp_probe --args addr=192.168.10.2` | 无 FPGA 兼容性告警；RX/TX 子板正常 |
| 3 | `python -m jammer_arm doctor` | 仅「互锁未武装」为预期状态，其余 PASS |
| 4 | `python -m jammer_arm run --radio uhd --frames 20` | 20 帧 `rx=ok`，`tx=disabled` |
| 5 | `--frames 600` | `overruns=0`，`rx_failures=0`，`max_frame_ms < 40` |
| 6 | `--radio rfio` + `jammer-rfio --sim` | 协议连通；再换真设备 |
| 7 | 屏蔽箱/衰减链路内 `ZH_RF_TX_ENABLE=LAB_ONLY` | `tx_state=running`，频谱仪测得中心频率/带宽符合动作 |
| 8 | 1 小时长跑 | 无内存增长、无掉帧趋势、温度稳定 |

判定由 `jammer_arm assess` 自动完成（7 条判据，见 §P6 的 gate G4）；`--warmup 5`
把连接建立、PPO 加载、USRP 稳定等一次性开销排除在统计之外，否则首帧的 300–500 ms
会把 `max_frame_ms` 判成不合格（实测踩过这个坑）。

**若第 5 步超时/溢出**：按 `01_feasibility.md` §3.2 的顺序处理
（MTU → `rmem_max` → 降采样率），每次只改一个变量并重测 600 帧。

---

## P7 — C++ rfio 兜底

**做什么**：`cpp/` 下约 600 行守护进程，独占 UHD 流，通过本地 TCP 提供
`open/rx/tx/rebuild/status/close`。协议与 Python 参考服务
（`python -m jammer_arm.rfio_sim`）完全一致，后者已被 8 项一致性测试覆盖。

**验收**

1. 两个源文件编译零错误、零自有告警；
2. 线格式（JSON 解析/转义）在宿主机上跑通 40 项断言；
3. 板上 `bash scripts/build_cpp.sh` 可链接出二进制；
4. `jammer-rfio --sim` + `--radio rfio --frames 5` 跑通，再切真设备复测 P6 第 5 步。

**已完成的验证（宿主机）**

用 zig 0.16 自带的 clang 前端，对**真实 UHD 4.9.0.0 头文件**做 `-c` 编译（不链接）：

| 文件 | 结果 |
| --- | --- |
| `rfio_radio.cpp`（UHD API 封装） | 编译通过，`-Wall -Wextra` 零告警 |
| `rfio_server.cpp`（协议 + 主循环） | 编译通过，零告警；POSIX 头用按 glibc 语义的垫片替代（宿主机无 `<sys/socket.h>`） |
| `tests/test_json.cpp` | 编译并**运行**，40 项断言全通过 |

编译检查当场发现并修复了一个真实缺陷：`UHD_VERSION_STRING` 宏并不存在，
正确接口是 `uhd::get_version_string()`（见 `uhd/version.hpp`）。这类错误在板上
第一次构建时才会暴露，现在提前消除了。

**状态**：✅ 编译与线格式验证完成；⏳ 板端链接与真机运行待 P6。

---

## 里程碑门禁（Gate）

| Gate | 判据 | 作用 |
| --- | --- | --- |
| G1 数值等价 | `test_equivalence` 全绿 | 允许进入集成 |
| G2 协议兼容 | 真实 UI/RL 端到端 90+ 帧 | 允许替换 MATLAB |
| G3 安全 | 未武装时零发射；配置不可开锁 | 允许接触硬件 |
| G4 稳定性 | 600 帧零溢出/零重建 | 允许进入正式实验 |
| G5 射频符合性 | 频谱仪验证频率/带宽/增益映射 | 允许进入红蓝对抗实验 |

G1–G3 已在开发机通过；G4–G5 必须在板上完成。
