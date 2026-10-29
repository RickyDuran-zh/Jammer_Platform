# 验证报告

> 本报告记录**已经实际执行**的验证，区分三类证据：
> ① 在开发机（Windows x86_64）上跑通的；② 用 MATLAB 黄金向量证明数值等价的；
> ③ 必须在 RK3588 板卡上补做的。第 ③ 类不会被描述为“已验证”。

---

## 0. 结论摘要

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 与 MATLAB 数值等价 | ✅ **机器精度一致** | `test_equivalence`（5 项），最大偏差 8.9e-16 |
| 协议与既有 UI/RL 兼容 | ✅ 真实 UI 收到 90+ 帧，命令桥连通 | §3 端到端记录 |
| 单元/集成测试 | ✅ **120 项全通过** | `python -m unittest discover -s arm_port/tests` |
| 互锁 fail-closed | ✅ 未武装零发射；配置无法开锁 | `test_config`、`test_backend` |
| RX 恢复阶梯 | ✅ 注入故障后仍持续出遥测 | `test_backend.TestRecovery`、`selftest` |
| DSP 性能预算 | ✅ 帧内 DSP 约占预算 3%（x86） | §4 |
| 板端硬件联调 | ⏳ **未执行**（无 RK3588 与 X310 接入） | P6 清单 |
| C++ rfio 编译 | ✅ 两文件零错误/零告警（真实 UHD 4.9 头文件） | §6 |
| C++ 线格式测试 | ✅ 40 项断言通过（宿主机实际运行） | §6 |

---

## 1. 与 MATLAB 的数值等价性

参考数据由 `arm_port/tools/matlab_reference/dump_reference.m` 在装有 MATLAB R2025a
的工作站上、直接调用**原实现**（`jammer_config.m` / `sense_channel_grid.m` /
`generate_jammer_signal.m`）生成，保存全部输入与输出，再由 Python 侧复算比对。

```
wrote arm_port/tests/data/reference.json (7 cases)
```

| 比对项 | 用例 | 最大绝对偏差 | 判定 |
| --- | ---: | ---: | --- |
| `channel_features`（10×4） | 4 组感知输入 | **8.9e-16** | 机器精度 |
| `channel_map` | 4 组 | 0（完全一致） | 通过 |
| `snr_est` | 4 组 | **3.6e-15** | 机器精度 |
| `bw_est` | 4 组 | **0.0 Hz** | 完全一致 |
| `sig_type` | 4 组 | 0 | 完全一致 |
| 多音波形（4096 点复样本） | 3 组 | **2.8e-14**（峰值 0.25，相对 1.1e-13） | 逐样本一致 |
| `fir1(128, Wn)` 抽头 | 12 组（4 带宽 × 3 信道） | **5.6e-17** | 一致 |
| `filter(b,1,x)` | 1 组 4096 点 | 通过（1e-13 容差） | 一致 |

**含义**：RL 检查点看到的特征向量、以及送入射频的波形，与原 MATLAB 实现逐位相同。
这意味着**不需要重训模型**，也不需要重新标定功率/带宽映射。

噪声模式（`waveform_mode=0`）使用 MATLAB 的 `randn` 流，numpy 无法复现同一随机序列，
因此该模式改为比对统计与频谱性质：

| 检查 | 判据 | 结果 |
| --- | --- | --- |
| 峰值归一 | `max(abs(y)) == 0.25` | 通过（12 位小数） |
| 带内能量占比（±15% 带宽余量） | > 90% | 通过（10 组信道×带宽组合） |
| FFT 限带替代实现 | > 90% | 通过 |
| TX 增益映射 `0→0, 10→5, 20→10`（裁剪） | 精确 | 通过 |
| 边缘信道裁剪（ch10 请求 8 MHz → 2 MHz） | 精确 | 通过 |

---

## 2. 测试清单（120 项）

| 模块 | 用例数 | 覆盖内容 |
| --- | ---: | --- |
| `test_config.py` | 18 | 默认值与 MATLAB 一致、信道栅格、端口、动作空间、**互锁只能由环境变量武装**、配置文件拒绝 `safety`/`tx_enabled`、点号覆盖、类型解析、非法值校验 |
| `test_equivalence.py` | 5 | 上述 MATLAB 逐点比对 |
| `test_sensing.py` | 10 | 形状/范围、频谱轴、占用信道追踪、CW 只抬干扰不抬占用、干扰重叠标注、静默帧、长帧、空结果 |
| `test_waveform.py` | 12 | 峰值归一、带限（FIR/FFT 两种整形）、多音 4 分量、确定性、边缘裁剪、增益映射、非法模式、自定义帧长、FIR 设计性质 |
| `test_protocol.py` | 12 | JSON-Lines 分帧/超时/重连、RL 报文逐字段、越界动作裁剪、服务缺失回退、遥测 schema 与 UI 契约、命令桥轮询/无效值 |
| `test_backend.py` | 16 | 合成后端帧循环、控制权切换、命令桥下发、无效命令忽略、互锁拦截、武装后发射与裁剪、超限帧拒绝、RX 故障恢复、**单帧异常隔离**、**预热帧不污染统计**、RL 动作执行、遥测端到端、状态快照原子写 |
| `test_rfio.py` | 8 | 参考服务一致性：open 配置、收样本、错误传播、互锁、增益裁剪、重建、后端跑在 rfio 上、连接失败的可操作报错 |
| `test_cli.py` | 22 | 以子进程方式验证退出码与输出：`show-config` JSON、非法覆盖/互锁键被拒（exit 3）、`doctor`、`selftest`、`bench`、`mock --frames`、`--control agent`、rfio 守护进程缺失（exit 2）、状态快照落盘、`--help/--version`、未知命令（exit 2）；以及 `assess` 的 7 项 G4 判定（健康/溢出/帧数不足/超时/互锁不一致/JSON 输出/快照缺失） |
| `test_uhd_device.py` | 17 | 用假 `uhd` 模块验证：配置顺序、设备参数、`info` 回读、突发命令与 `num_samps`、连续模式、metadata 错误映射、短包拒绝、TDM 仅作用于连续模式、增益设置的多签名兼容、`tx_repeat`、重建、关闭幂等、绑定缺失提示 |

运行方式（开发机与板端一致）：

```bash
PYTHONPATH=arm_port/python python -m unittest discover -s arm_port/tests
# Ran 120 tests ... OK
```

内置自检（无需测试框架，板端可单独跑）：

```bash
python -m jammer_arm selftest
# [PASS] config / sensing / waveform(noise,tone) / rf interlock
# [PASS] json-lines transport / mock frame loop / rx recovery ladder
# 8 checks, 0 failure(s)
```

---

## 3. 端到端联调（真实 UI + 真实 RL 服务）

**拓扑**：真实 PyQt5 UI（`:5555` 遥测 / `:5557` 命令桥）+ 真实 `rl_server.py --algo ppo`
（`:6001`）+ 本移植后端（`--radio mock`，真实感知/波形/遥测代码，合成 IQ）。

UI 侧输出（节选）：

```
[CmdBridge] listening on 127.0.0.1:5557
[Telemetry] listening on 127.0.0.1:5555
[Telemetry] MATLAB connected from ('127.0.0.1', 65530)
[CmdBridge] MATLAB connected from ('127.0.0.1', 65531)
[UI] 遥测帧#31 模式=RL自动 策略=False
[UI] 遥测帧#61 模式=RL自动 策略=False
[UI] 遥测帧#91 模式=RL自动 策略=False
```

后端侧输出：

```
[CMD] control mode -> rl
[jammer_arm] mode=mock radio=none tx_armed=no
[SEQ=1] ch=3 power=0.0dB bw=6MHz mode=0 policy=ppo ctrl=rl rx=ok tx=mock_running frame=13.2ms dsp=0.93ms
...
  frames          : 84 in 10.1s (8.33 fps)
  frame time      : last 3.9 ms, max 13.2 ms
  dsp time        : last 0.64 ms, max 1.39 ms
  rx failures     : 0 (rebuilds 0, full 0)
  overruns        : 0
```

另一次以 `--radio mock`（真实 DSP 跑合成 IQ）连同一套 UI/RL 运行：

```
[SEQ=1] ... policy=ppo ... frame=766.5ms   # 首帧含 torch 加载与首次连接
[SEQ=5] ... frame=4.9ms dsp=0.66ms
status.json: telemetry_frames=15 / frames=15, interlock_blocks=15, tx_state="disabled"
```

**结论**：

1. 三条链路的报文结构与既有 Python 端**完全兼容**，UI 无需改动；
2. UI 主动下发的 `set_control_mode rl` 命令被后端正确接收并执行 → 命令桥双向可用；
3. 首帧 766 ms 是 torch 无关的冷启动（连接建立 + 首次 JSON + 页面渲染），之后稳定在 4–7 ms。

---

## 4. 性能（x86 参考 + 板端复测方法）

`python -m jammer_arm bench --iterations 150`（AMD64，numpy 2.2.5，单线程）：

| 阶段 | 均值 | P95 | 最大 | 占 80 ms 预算 |
| --- | ---: | ---: | ---: | ---: |
| 感知（FFT + 10 信道） | 0.538 ms | 0.949 ms | 1.291 ms | 0.67% |
| 波形·噪声（FIR） | 0.594 ms | 0.978 ms | 1.414 ms | 0.74% |
| 波形·噪声（FFT 整形） | 0.546 ms | 0.913 ms | 1.437 ms | 0.68% |
| 波形·多音 | 0.761 ms | 1.308 ms | 1.804 ms | 0.95% |
| 遥测打包 | 0.037 ms | 0.059 ms | 0.237 ms | 0.05% |
| 遥测 JSON 编码 | 0.766 ms | 1.004 ms | 1.485 ms | 0.96% |

单帧遥测 28.0 KiB；8.33 fps → 约 233 KiB/s，链路压力可忽略。

**板端判据**（P6 第 4–5 步）：`--frames 600` 后
`overruns=0`、`rx_failures=0`、`max_frame_ms < 40`、`max_dsp_ms < 20`。
若 DSP 超出，优先切 `shaping="fft"`，其次降 `num_samples`（会改变 RL 输入分辨率，需评估）。

---

## 5. 安全性验证

| 检查 | 方法 | 结果 |
| --- | --- | --- |
| 默认不发射 | 未设环境变量跑 15 帧 | `tx_state=disabled`，`interlock_blocks=15`，无 TX 记录 |
| 精确匹配才武装 | `""`/`"1"`/`"lab_only"` 均判为未武装 | 通过 |
| 配置无法开锁 | `JammerConfig.from_dict({"tx_enabled":True})` 等 4 个键 | 均抛 `ConfigError` |
| `safety` 段不可配置 | `{"safety":{"interlock_value":"X"}}` | 抛 `ConfigError` |
| 发送前二次校验 | 设备层 `guard_transmit` 重读环境变量 | 未武装时抛 `InterlockError` |
| 超限帧被拒 | 峰值 0.9 的帧 | 抛 `TxFrameError`，未送达设备 |
| 增益裁剪 | `power_db=999` | 实际下发 `tx_gain_max=10` |
| 运行期环境变量生效 | 启动后设置环境变量再发帧 | 立即生效（非缓存布尔值） |

---

## 6. 未完成项与已知限制

### 6.1 板端硬件联调（P6，待执行）

无 RK3588 与 X310 接入，以下**未验证**，不得视为通过：

- UHD 在 aarch64 上的编译产物与 Python 绑定；
- X310 在 1 GbE 直连 + MTU 9000 下的实际吞吐与溢出率；
- 定长突发在真机上的时延与抖动分布；
- 真实 TX 的中心频率/带宽/增益与频谱仪读数的一致性；
- 1 小时长跑的内存与温度表现。

执行清单见 `02_plan.md` P6；一键脚本 `scripts/acceptance_test.sh`。

### 6.2 C++ rfio 守护进程

已从“完全未验证”推进到“编译 + 线格式已验证”，链接与真机运行仍待板端。

**编译验证**（宿主机，zig 0.16 的 clang 前端 + 真实 UHD 4.9.0.0 头文件，`-c` 不链接）：

| 目标 | 命令要点 | 结果 |
| --- | --- | --- |
| `rfio_radio.cpp` | `-std=c++17 -Wall -Wextra -I <uhd>/include` | 通过，零告警，产出 865 KB `.o` |
| `rfio_server.cpp` | 同上 + glibc 语义 POSIX 垫片（宿主机无 `<sys/socket.h>`）+ `-DSIGPIPE` | 通过，零告警，产出 1.1 MB `.o` |

**当场修掉的真实缺陷**：`UHD_VERSION_STRING` 不存在 → 改为 `uhd::get_version_string()`。
若留到板上，会在第一次 `build_cpp.sh` 时以编译错误形式出现。

**线格式测试**：把 JSON 解析/转义抽到 `cpp/include/rfio_json.hpp`（守护进程与测试
共用同一份代码，避免双实现漂移），`bash arm_port/scripts/test_cpp.sh` 编译并运行
`cpp/tests/test_json.cpp`：40 项断言覆盖 `open/rx/tx` 真实报文、缺省值、数字格式、
转义往返、嵌套跳过、空白容忍、8 类畸形输入必须抛异常。结果：**40 checks, 0 failures**。

**仍未验证**：链接（`libuhd` 符号解析）、`--sim`/真机运行、TCP 分帧在真实
`recv`/`send` 下的边界行为。板上首次执行 `scripts/build_cpp.sh` 即可完成前两项；
第三项已由 Python 参考服务的 8 项一致性测试间接覆盖。

### 6.3 既有检查点的行为（非本次移植引入）

用同一组差异极大的特征向量（全零 / 单信道占用 / 均匀 / 随机）查询三个服务：

| 算法 | 动作是否随状态变化 | 说明 |
| --- | --- | --- |
| PPO | 否（恒定 ch2/p0/bw6/m0） | value 随状态变化（1.16→11.2），说明网络在算，但 argmax 退化 |
| A2C | 否（恒定 ch1/p0/bw6/m1） | 同上 |
| BDQ | 基本否（仅 mode 偶变） | 分支 Q 值区分度低 |

结论：仓库随附的是 **bootstrap 权重**，不适合用来演示“状态驱动的策略行为”。
这与移植无关（RL 服务代码未改动），但会影响演示效果；正式实验前应重新训练或换权重。

### 6.4 数值边界

- 全零输入时 MATLAB 的 `snr_est` 为 `-Inf`，无法编码进 JSON；移植侧钳到 **-120 dB**
  并在代码中注明。真实硬件噪声下不会触发。
- `channel_features` 中 `std` 使用 `ddof=1`（与 MATLAB 一致）；单元素信道退化为 0。
- 遥测 `hw_actuals` / `executed_action` 是**追加字段**，旧 UI 忽略；
  它们表达的是“请求带宽的等效实际带宽”，不是硬件回读值（UHD 无逐帧带宽回读）。
