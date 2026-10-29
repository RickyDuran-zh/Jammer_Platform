# ARM 平台移植可行性分析（RK3588 / LubanCat 5 + USRP X310）

> 结论先行：**可行，且推荐“纯 Python 主路径 + C++ 射频守护进程兜底”的混合方案。**
> 需要硬件验证的风险集中在两处：UHD Python 绑定在板端的构建，以及 1 GbE 链路对
> 20 MS/s 的承载余量。两者都已有明确的规避路径（rfio 守护进程 / 降采样或万兆口）。
>
> 本文所有“实测”数据均来自移植开发机（Windows x86_64，Python 3.10 + numpy 2.2.5，
> 同一份 UHD 4.9.0.0 发行版），板端数字必须用 `jammer_arm bench` 在目标机上复测；
> 文中给出折算方法与判据。

---

## 1. 基线：被移植的系统到底做了什么

移植对象不是“一个 MATLAB 脚本”，而是三条 TCP 链路串起来的三个进程。可行性评估必须
以进程边界为颗粒度，否则很容易在“重写 DSP”上投入过多、在“协议与恢复逻辑”上翻车。

| 进程 | 现有实现 | 代码量 | 职责 |
| --- | --- | ---: | --- |
| 射频后端 | `matlab/`（6 个功能文件） | 约 600 行 | 收 IQ → FFT 感知 → 决策 → 生成波形 → TX → 发遥测 |
| RL 推理 | `python/rl_service/` | 约 900 行 | PPO/A2C/BDQ 单步推理，:6001 |
| UI + Agent | `python/app/` | 约 2800 行 | 遥测显示、命令桥 :5557、策略编排、工具调用 |

**移植范围**：只替换第一个进程（MATLAB）。RL 服务与 UI 完全不动——这正是三条 JSON-Lines
链路存在的价值：端口、报文结构、动作四元组不变，则 7000 行既有 Python 代码零改动复用。

MATLAB 侧需要被替代的能力共 8 项：

| # | MATLAB 能力 | 实现文件 | 替代难度 |
| ---: | --- | --- | --- |
| 1 | `comm.SDRuReceiver` 收流 | `radio/radio_rx_init.m` | 中（UHD API 差异） |
| 2 | `comm.SDRuTransmitter` 发流 | `radio/radio_tx_init.m`, `tx_send_frame.m` | 中 |
| 3 | `fft/fftshift` + 10 信道切片 | `sensing/sense_channel_grid.m` | 低（可直接等价复刻） |
| 4 | `fir1` + `filter` 限带 | `waveform/generate_jammer_signal.m` | 中（需自己实现 fir1） |
| 5 | `jsonencode` + `tcpclient` | `interface/*.m` | 低 |
| 6 | 定时循环 `pause` | `backend/jammer_backend_loop.m` | 低 |
| 7 | RX 超时→重建→缓存兜底 | `jammer_backend_loop.m` | 低（但必须逐条复刻） |
| 8 | 射频互锁 `ZH_RF_TX_ENABLE` | `jammer_config.m`, `tx_send_frame.m` | 低（但必须更严格） |

第 3、4 两项是“数值契约”：RL 检查点是在这套特征上训练的，波形幅度/带宽决定干扰效果。
**它们不允许“顺手优化”**，只能等价复刻——本次移植用 MATLAB 导出的黄金向量做了逐点校验
（见 `05_validation.md`，特征向量误差 ≤1e-12，多音波形逐样本一致）。

---

## 2. UHD 在 ARM（aarch64）上的编译与运行支持

### 2.1 结论

| 项目 | 结论 | 依据 |
| --- | --- | --- |
| UHD 核心库编译 | **支持**，aarch64 交叉/本机编译均为常规路径 | UHD 无 x86 专有指令，`uhd::convert` 有通用实现 |
| 运行期依赖 | 无内核模块，纯用户态 UDP + libusb | X310 走以太网，不需要 `uhd` 内核驱动 |
| Python 绑定 | **可用但最脆弱**：需 Boost.Python + numpy 头 | 发行版包普遍不带该模块 |
| 编译耗时（RK3588，8 核） | 约 25–45 min（`-j8`，Release，示例/测试关闭） | 源码规模与 A76 主频折算，板端实测为准 |
| 二进制体积 | 约 60–90 MB（含 FPGA 镜像包另计） | 128 GB eMMC 无压力 |

### 2.2 版本选择：必须与 FPGA 镜像对齐

本机 Windows 站点使用 **UHD 4.9.0.0-release**（`D:\uhd`）。X310 的 FPGA 镜像与 UHD 版本
存在兼容矩阵，版本错配的典型表现是 `uhd_usrp_probe` 报
`Expected FPGA compatibility number ... got ...`，随后所有收发失败。

因此板上**固定构建同一版本**（`arm_port/scripts/build_uhd.sh` 默认 `UHD_REF=v4.9.0.0`），
避免“开发机 OK、板子不行”这类最难排查的问题。若必须换版本，先确认 FPGA 镜像能一起升级。

### 2.3 Python 绑定的真实风险与兜底

`-DENABLE_PYTHON_API=ON` 会在 `<prefix>/lib/python3.X/site-packages` 生成 `uhd` 模块。失败原因
几乎总是这三类：

1. 缺 `libboost-python-dev`（版本必须与 Python 主版本匹配，如 `libboost-python1.74-dev`）；
2. numpy 头文件缺失（`python3-numpy` 未装，且 venv 内 numpy 与系统 numpy 头不同源）；
3. 目标 Python 与编译时 Python 不一致（venv 指向 3.11，而 boost 模块编译给 3.10）。

`build_uhd.sh` 会安装依赖、写入 `.pth` 让 venv 也能 import，并在末尾自检 `import uhd`。
**即使这一步失败，系统仍然可用**：`--radio rfio` 把 UHD 收发交给 C++ 守护进程
（`arm_port/cpp`，`find_package(UHD)` 只需 C++ 库，不需要 Boost.Python），Python 侧只走
本地 TCP。这条兜底路径已在移植中实现，并用 Python 参考服务做过协议一致性测试。

### 2.4 运行期资源

- 单个 `jammer_arm` 进程常驻内存约 **80–120 MB**（numpy 为主），CPU 单核占用 **< 8%**（见 §4）。
- 若 RL 也跑在板上，torch（CPU 版）另需 **400–700 MB**，板载 16 GB 完全够用。
- 存储：venv + UHD + torch 约 **3.5 GB**，128 GB eMMC 充裕；建议把日志与黄金向量放 eMMC，
  IQ 抓取放外置存储。

---

## 3. X310 在该平台下的驱动与实时性

### 3.1 驱动模型：好消息

X310 是以太网设备，UHD 全程用户态收发，**没有内核模块、没有实时内核要求**。这与
USB 型 SDR（需要 `libusb` + 高优先级线程 + 大缓冲）有本质区别，也是“ARM 能跑 SDR”
的关键前提：实时性压力从“内核抢占延迟”转移到“用户态缓冲与调度抖动”，后者用
`performance` 调频策略 + 足够大的 socket 缓冲即可压住。

### 3.2 带宽预算：1 GbE 是真正的边界

| 项目 | 计算 | 结果 |
| --- | --- | --- |
| RX 采样率 | 20 MS/s，sc16（每样本 4 B：I/Q 各 2 B） | 80 MB/s |
| RX 线速占用 | 80 MB/s × 8 = 640 Mbps，加 UDP/IP 头约 5% | ≈ 672 Mbps |
| 1 GbE 实际可用 | 1000 Mbps 线速，UDP 实测 ≈ 940 Mbps（MTU 9000）/ ≈ 750 Mbps（MTU 1500） | — |
| 余量（MTU 9000） | 672 / 940 | **≈ 71% 占用，可行** |
| 余量（MTU 1500） | 672 / 750 | **≈ 90% 占用，临界** |
| TX 占用 | 8.3 fps × 4096 × 4 B ≈ 136 kB/s | 可忽略 |

**结论与操作要求**：

- 板子与 X310 **直连**（不经交换机），两端 MTU 设 9000；
- 若板载为 2.5 GbE 变体，或 X310 走 10 GbE SFP+，则余量充足，可放宽到 200 MS/s 全带宽；
- 若出现 `recv overflow`/`D`（dropped）计数，**第一优先级是降采样率**：
  `--set radio.sample_rate_hz=10e6 --set radio.master_clock_rate_hz=200e6`（此时逻辑信道
  宽度需同步改为 1 MHz，属于配置变更，必须同步更新 README 与 RL 环境），
  其次是查 MTU 与 `net.core.rmem_max`。

### 3.3 关键设计决策：定长突发收流（本移植最重要的修正）

**问题**：帧循环 12.5 fps，每帧只要 4096 点；而设备以 20 MS/s 连续产生样本。
若用 `STREAM_MODE_START_CONTINUOUS` 并在每帧调一次 `recv()`：

```
产生速率 20 000 000 Sa/s
消费速率 4096 × 12.5 ≈ 51 200 Sa/s      → 消费仅为产生的 0.26%
```

主机缓冲必然在两帧之间溢出，`recv` 返回 `ERROR_CODE_OVERFLOW`，数据不可用。
这正是把 MATLAB 的 `comm.SDRuReceiver`（每次 `step()` 内部按帧申请采样）直接翻译成
“连续流 + 懒读”会踩的坑。

**方案**：每帧下发 `STREAM_MODE_NUM_SAMPS_AND_DONE`，`num_samps = 4096`，设备发完即停。
主机端任何时刻最多只有一帧样本在队列里，**结构上不可能溢出**；同时天然满足
“先 RX 后 TX”的时分语义（发完 RX 突发，链路已空闲，再发 TX 突发，无需 stop/start 流）。

配置项 `radio.rx_mode`：`burst`（默认）/ `continuous`（仅供“消费速率高于采样率”的实验场景）。
两种模式在 `tests/test_uhd_device.py` 中都有断言覆盖（下发命令、TDM 暂停行为）。

### 3.4 帧时延预算（实测 + 折算）

| 阶段 | x86 实测 | RK3588 折算（×3–5） | 说明 |
| --- | ---: | ---: | --- |
| RX 定长突发（4096 点 @20 MS/s） | 0.2 ms 空中时间 + ~0.5 ms 调度 | ~1 ms | 与 CPU 关系不大 |
| FFT + 10 信道特征 | 0.54 ms | 1.6–2.7 ms | 4096 点复数 FFT + 10 次切片统计 |
| 波形生成（FIR 129 抽头） | 0.59 ms | 1.8–3.0 ms | `convolve` 528k 复数乘加 |
| RL 单步往返（loopback） | 0.3–1.5 ms | 2–8 ms | 128×128 MLP，含 JSON 编解码 |
| 遥测打包 + JSON | 0.80 ms | 2.4–4.0 ms | 28 KiB/帧 |
| TX 突发 | ~0.3 ms | ~0.5 ms | — |
| **合计** | **≈ 2–4 ms** | **≈ 8–19 ms** | 预算 80 ms，占用 **10–24%** |

结论：**算力不是瓶颈**。真正的抖动来源是 Python 运行时（GC、GIL 争用、页错误）与
操作系统调频，处理手段见 §5.3。

### 3.5 实时性风险清单

| 风险 | 表现 | 缓解 |
| --- | --- | --- |
| CPU 调频抖动 | 偶发帧耗时 3–5× | `cpupower frequency-set -g performance`；`doctor` 会检查 governor |
| Python GC 停顿 | P99 帧时延尖峰 | 帧循环内不分配大对象；`gc.freeze()`（可选）；阈值告警 |
| 内存不足换页 | 长时间运行后卡顿 | 16 GB 内存 + 关闭 swap 或 `vm.swappiness=10` |
| 网络中断合并 | 突发延迟 | 直连、关闭 offload（`ethtool -K eth0 gro off lro off`） |
| 1 GbE 饱和 | `recv overflow` | MTU 9000 直连；或降采样率 |
| eMMC 写入抖动 | 日志同步阻塞 | 日志走 `/run`（tmpfs）或 `--quiet`；状态快照 1 Hz 原子替换 |

---

## 4. 性能瓶颈分析（含“不是瓶颈”的项）

按“若它慢 10 倍会不会死”排序，逐项给出量化结论：

| 候选瓶颈 | 量化 | 判定 |
| --- | --- | --- |
| 4096 点复数 FFT | 0.54 ms/帧，占预算 0.7% | **非瓶颈** |
| FIR 限带（129 抽头） | 0.59 ms/帧 | 非瓶颈；需要时切 `shaping="fft"`（0.55 ms） |
| 遥测 JSON 序列化 | 0.77 ms/帧，28 KiB | 非瓶颈；12.5 fps → 350 KiB/s |
| torch 推理 | 0.3–1.5 ms（x86），板上 CPU 约 1–5 ms | 非瓶颈；**不建议上 NPU**（算子/精度迁移成本 >> 收益） |
| numpy 内存分配 | 每帧约 6 次 4096 点复数数组（≈200 kB） | 非瓶颈；如需极致可复用缓冲 |
| **UHD 传输 / 1 GbE** | 71% 线速占用 | **真瓶颈**，见 §3.2 |
| **Python 调度抖动** | P99 可能是均值的 3–5× | **真瓶颈**，用调频/亲和性/优先级压 |
| USB / 内核驱动 | 不适用（X310 走以太网） | 无此风险 |

板端复测方法（不依赖主观估计）：

```bash
python -m jammer_arm bench --iterations 500        # DSP 各阶段均值/P95/max
python -m jammer_arm run --radio uhd --frames 600  # 看 summary 的 max/overruns
```

**验收判据**：`overruns = 0`、`max_frame_ms < 0.5 × 帧周期`、连续 600 帧无 `RX rebuild`。

---

## 5. MATLAB 模块迁移的替代方案

### 5.1 逐模块对照

| MATLAB | 替代实现 | 等价性保证 |
| --- | --- | --- |
| `comm.SDRuReceiver` (MasterClockRate 200e6, DecimationFactor 10) | `uhd.usrp.MultiUSRP` + `set_rx_rate(20e6)` | UHD 自动选择抽取因子；`info.details` 回读实际值 |
| `comm.SDRuTransmitter` + `radio_tx(frame)` | `tx_stream.send()` 单突发 | `start_of_burst/end_of_burst` 标记 |
| `fftshift(fft(x, N))` | `np.fft.fftshift(np.fft.fft(x, N))` | 逐点一致（黄金向量校验） |
| `abs(spec).^2`, `mean/std` | numpy，`std(ddof=1)` | MATLAB `std` 默认 N-1，**必须显式 ddof=1** |
| `fir1(128, Wn, 'low')` | 自实现 `design_lowpass(129, Wn)`（sinc×Hamming×DC 归一） | 抽头误差 < 1e-10，无需 scipy |
| `filter(b, 1, x)` | `np.convolve(b, x)[:N]` | 逐点一致（零初始状态等价） |
| `jsonencode` / `jsondecode` | `json.dumps/loads` | 字段名/嵌套结构手工对齐，逐字段断言 |
| `tcpclient` + `writeline/readline` | `socket` + 自建行缓冲 | 分帧、半包、超时、重连均有测试 |
| `pause(0.08)` 定时 | 单调时钟 deadline 调度 | 慢帧后重新对齐，避免累积漂移 |
| `try/catch` + `continue` 单帧容错 | 每帧异常隔离 + 计数 | 单帧错误不终止循环 |

### 5.2 为什么不用 `scipy.signal`

`scipy.signal.firwin` 与本项目的 `fir1` 结果一致，但 scipy 在 aarch64 上多一个约 40 MB 的
依赖，且 `lfilter` 对 4096 点、129 抽头的场景并无优势（`np.convolve` 已足够快）。
最终选择**只用 numpy**，把运行期依赖压到最小——这也让 venv 构建在板上只需 30 秒。

### 5.3 为什么不保留 MATLAB 的任何部分

- **许可**：本机 MATLAB R2025a **未安装 Communications Toolbox**（实测 `license('test','Communications_Toolbox') = 0`），
  也就是说现有 `live` 后端在这台机器上根本无法运行；继续依赖它等于把系统绑在一个无法验证的组件上。
- **部署**：MATLAB Runtime 在 aarch64 上的可用性与体积（>2 GB）都不适合嵌入式交付。
- **可维护性**：DSP 逻辑一旦用 Python 表达，就能用同一套测试（含 MATLAB 黄金向量）守住数值契约。

### 5.4 方案选型：纯 Python vs C++ + Python

| 维度 | 纯 Python | C++ + Python | 本移植选择 |
| --- | --- | --- | --- |
| 开发/维护成本 | 低 | 高（两套构建、两套调试） | Python 为主 |
| DSP 性能 | 足够（3% 预算） | 更快但无必要 | Python |
| UHD 绑定风险 | 依赖 Boost.Python | 无（直接链 libuhd） | **C++ 兜底** |
| 实时性上限 | 受 GIL/GC 影响 | 更可控 | 现阶段无差异 |
| 调试可观测性 | 强 | 弱 | Python |

**决策**：`RadioDevice` 抽象出三套实现——`uhd`（主）、`rfio`（C++ 守护进程兜底）、
`mock`（无硬件）。上层帧循环、DSP、遥测、互锁完全共享，切换只改一个命令行参数。
这样既拿到 Python 的开发效率，又保留一条“绑定编译失败也能上板”的退路。

---

## 6. 风险登记表

| # | 风险 | 概率 | 影响 | 缓解措施 | 验收方式 |
| ---: | --- | --- | --- | --- | --- |
| R1 | UHD Python 绑定在板上编译失败 | 中 | 高 | `--radio rfio` 兜底；构建脚本自检 | `doctor` 显示 uhd 版本 或 rfio 连通 |
| R2 | FPGA 镜像与 UHD 版本不匹配 | 中 | 高 | 固定 4.9.0.0；`uhd_usrp_probe` 校验 | probe 无兼容性告警 |
| R3 | 1 GbE 承载 20 MS/s 不足 | 中 | 中 | MTU 9000 直连；降采样率预案 | 600 帧 `overruns=0` |
| R4 | Python 抖动导致帧超时 | 中 | 中 | performance governor、nice、CPU 亲和 | P99 帧时延 < 40 ms |
| R5 | 数值不一致导致 RL 决策偏移 | 低 | 高 | MATLAB 黄金向量逐点校验 | `test_equivalence` 全绿 |
| R6 | 互锁被绕过/误开 | 低 | **极高** | 仅环境变量、配置拒绝、发送前二次校验 | `test_config` / `test_backend` |
| R7 | RX 超时后不恢复 | 低 | 高 | 两级重建 + 缓存兜底 | `test_backend.TestRecovery` |
| R8 | 板端散热降频 | 低 | 中 | 无风扇场景限制占空比；监控温度 | 1 小时长跑无降频 |

---

## 7. 明确不做的范围

- 不修改 RL 训练算法、奖励函数、动作空间；不在板上引入类型条件化训练。
- 不实现蓝方闭环、不宣称“欺骗效果”已被验证（与主 README 的边界保持一致）。
- 不改动 PyQt UI 与 Agent 工具的任何交互契约。
- 不新增 RF 波形模式；不弱化、不绕过、不可配置化 TX 互锁。
- 不在本次移植中做“性能优化型”算法替换（例如把感知改成 Welch PSD）——那会破坏 RL 输入分布。

以上边界意味着：**移植是“等价替换”，不是“重新设计”**。这也是能用黄金向量做验收的前提。
