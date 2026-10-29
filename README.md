# Jammer Platform

智能频谱管控系统中的干扰机子系统，面向红蓝双方智能对抗实验。项目以单台 USRP X310 为射频后端，使用 MATLAB 完成频谱感知、波形生成和时分收发；Python 提供强化学习（RL）决策服务、实时监控界面及 Agent 自主控制入口。干扰机可根据实时频谱态势，由 RL 或 Agent 在信道、功率、带宽、波形和策略之间自主切换。

> **射频安全声明**：本项目默认禁止硬件发射。真实发射仅限获得授权的屏蔽箱或充分衰减的有线测试环境；使用者须自行遵守所在地频谱法规与实验室安全规范。

## 当前能力

- 围绕 **3.0 GHz、20 MHz 观察带宽、10 个逻辑信道**运行；每个逻辑信道宽度为 2 MHz。
- 支持 MATLAB Mock 后端，无需 USRP 即可验证 UI、RL、Agent 和 TCP 通信闭环。
- 支持单台 X310 的时分收发：先接收一帧 IQ 数据进行感知，再发送一帧干扰波形。
- 提供 PPO、A2C、BDQ 三种 RL 推理服务。
- 提供 PyQt5 实时监控界面：频谱、10 信道状态、趋势、动作日志与策略状态。
- 控制入口集中在右侧「AI 智能助手」面板顶部的控制卡片：控制方式（RL 自动 / 手动控制 / 聊天策略）与三种 Agent 策略类型分行并列，点击即切换；手动控制下可自由点选信道、功率、带宽与干扰模式，参数改动即时下发。
- 提供三种 Agent 接管预设、四种手动可选干扰战术、独立的波形/功率/带宽控制，以及可选的 DeepSeek 兼容自然语言工具；预设在有 RL 服务时逐帧取候选动作，断线时使用规则回退。
- DeepSeek 密钥保存在当前 Windows 用户的凭据管理器（credential vault）中，不出现在源码、脚本或仓库文件里。
- 对真实 TX 提供显式环境变量互锁、复基带峰值限制和硬件增益限幅。
- 提供 **ARM（RK3588）纯 Python 后端**（`arm_port/`），可替代 MATLAB 完成感知、决策、波形与遥测；与现有实现数值等价，UI 与 RL 服务无需改动（见下文「ARM 平台移植」）。

## 系统架构

~~~text
┌───────────────────── Python UI（127.0.0.1）──────────────────────┐
│ PyQt5 监控面板                                                     │
│  ├─ TelemetryServer :5555  接收 MATLAB 遥测                       │
│  ├─ CommandBridge   :5557  保存 Agent / UI 控制命令               │
│  ├─ 策略引擎         预设约束 + RL 候选 / 规则回退                │
│  └─ AgentBrain（可选） DeepSeek 兼容自然语言工具调用             │
└─────────────────────────────┬─────────────────────────────────────┘
         telemetry JSON-Line  │  cmd_poll / cmd_rsp JSON-Line
               :5555          │              :5557
                               │
┌─────────────────────────────▼─────────────────────────────────────┐
│ MATLAB 后端                                                        │
│  感知 IQ → 10 信道特征 → 选择动作 → 生成波形 → 时分 TX            │
│  ├─ Mock：合成频谱与信道状态                                       │
│  └─ Live：USRP X310 RX → TX                                        │
└─────────────────────────────┬─────────────────────────────────────┘
          state_req / action_rsp JSON-Line :6001
                               │
┌─────────────────────────────▼─────────────────────────────────────┐
│ Python RL 服务                                                     │
│  PPO / A2C / BDQ：信道特征 + 上一动作 → 下一动作                  │
└───────────────────────────────────────────────────────────────────┘
~~~

| 链路 | 方向 | 端口 | 内容 |
| --- | --- | ---: | --- |
| RL 决策 | MATLAB 或 UI Agent ↔ Python RL 服务 | 6001 | state_req / action_rsp；取决于控制模式 |
| 遥测 | MATLAB → Python UI | 5555 | telemetry |
| 控制 | Python UI → MATLAB | 5557 | MATLAB 轮询 cmd_poll，UI 回复 cmd_rsp |

三条链路都使用 UTF-8 JSON Lines（一条 JSON 一行）。`rl` 模式由 MATLAB 每帧请求 RL；三种预设由 UI Agent 根据遥测向同一个 RL 服务请求候选动作，施加类型约束后通过现有命令桥下发给 MATLAB。RL 服务不可用时，预设走规则回退。MATLAB 和 Python 后端在连接失败后会重试；推荐仍按下面顺序启动，避免初始等待。

## 运行模型与动作

### 信道与射频配置

实际配置以 [jammer_config.m](matlab/backend/jammer_config.m) 为准：

| 项目 | 当前值 |
| --- | --- |
| 中心频率 | 3.0 GHz |
| 采样率 | 20 MS/s（200 MHz 主时钟 / 10） |
| 观察带宽 | 20 MHz |
| 逻辑信道 | 10 个 |
| 单信道宽度 | 2 MHz |
| 信道编号 | 0–9（UI 展示为信道 1–10） |
| 可选带宽 | 2 / 4 / 6 / 8 MHz |
| 控制功率 | 0–20 dB |
| X310 默认地址 | 192.168.10.2 |

统一动作格式：

~~~json
{
  "channel_idx": 4,
  "power_db": 10,
  "bw_mhz": 4,
  "waveform_mode": 0
}
~~~

### 干扰波形模式

| waveform_mode | 名称 | 行为 |
| ---: | --- | --- |
| 0 | 带限噪声 | 生成复高斯噪声，经低通 FIR 限带后搬移到目标信道。 |
| 1 | 多音干扰 | 在目标信道及指定带宽内生成 4 个等间隔复正弦音调。 |

所有波形会归一到保守的复基带峰值 0.25。power_db 映射为硬件 TX Gain（当前限制为 0–10 dB，映射系数为 0.5）。因此 **power_db = 0 表示最小增益，并不表示停止发射**；只要启动时已经解除互锁，仍会发送帧。

边缘信道请求较大带宽时，波形生成器会为避免奈奎斯特混叠而裁剪实际带宽。例如最边缘信道最大只能保持约 2 MHz。遥测中的动作字段目前仍记录请求带宽，不表示已增加硬件执行确认。

### 控制模式与 Agent 策略

| 层级 | 可选项 | 含义 |
| --- | --- | --- |
| 后端控制模式 | rl / agent | `rl` 由 MATLAB 直连 RL 服务逐帧决策；`agent` 使用 UI/Agent 经命令桥下发的动作。 |
| RL 算法 | ppo / a2c / bdq | 三者都输出相同的四元动作。 |
| 基础干扰战术 | fixed（恒定） | 保持所选信道、功率、带宽和波形。 |
|  | sweep（扫频） | 从所选信道开始，依次走完 10 个信道并循环；每信道驻留指定遥测帧数。 |
|  | random（随机跳频） | 从所选信道开始，在 10 个信道中随机跳至不同的下一个信道；每信道驻留指定遥测帧数。 |
|  | target_track（目标跟踪） | 逐帧读取 10 信道占用度，跟踪达到最低占用阈值的最大占用信道；0.05 占用度滞回抑制频繁切换，低于阈值时保持现有目标。 |
| 其他 Agent 规则 | track_busiest | 选择占用度最高的信道。 |
|  | track_weakest | 选择 SNR-like 指标最低的信道。 |
|  | alternating | 在给定信道之间轮换。 |
|  | reactive | 根据目标信道 SNR 在噪声/多音之间切换。 |
| Agent 预设 | coerce（强制型） | 默认偏高功率、较宽带宽；RL 候选影响信道、功率和带宽，断线时按占用度追踪。 |
|  | energy_save（能量节约型） | 默认低功率、2 MHz；RL 候选受功率上限与占用阈值约束，低占用时请求最小增益。它不会关断 TX。 |
|  | deception（欺骗诱导型） | 在现有两种波形之间做周期变化，RL 候选影响信道、功率和带宽。这是实验性规则近似，尚非论文中的欺骗博弈策略。 |

控制入口在右侧「AI 智能助手」面板顶部的**控制策略卡片**，分两行并列显示：**控制方式**（RL 自动 / 手动控制 / 聊天策略）与**策略类型**（强制型 / 能量节约型 / 欺骗诱导型）各占一行、三个候选框等宽并排，点击即切换控制权，当前生效项高亮，卡片右上角同步显示当前状态（RL 自动 / Agent 策略 / 手动模式），无需 API 密钥。切换预设或手动控制会先接管控制权；点击「RL 自动」交还。卡片可用标题栏右侧的「收起 / 展开」按钮折叠，把空间让给聊天区。

选择「手动控制」后卡片内联展开动作参数区，可直接点选四元动作的每一项：**信道**（1–10）、**功率**（0–20 dB）、**带宽**（2/4/6/8 MHz）、**干扰模式**（带限噪声 / 多音干扰）。恒定战术下任何参数改动都会即时经命令桥下发，无需再点按钮；下方「自动战术」为可选叠加层，选 **扫频 / 随机跳频 / 目标跟踪** 时按每帧遥测自动生成动作，扫频和随机跳频可设置每信道驻留帧数，目标跟踪可设置最低占用度，这三种序列型战术改参数后按钮变为「应用参数（重启战术）」，点击才重启序列，避免扫频中途被重置到首个信道。四种战术不增加新的 RF 波形编号，也不改变 MATLAB 的动作格式。

切换控制权会清除尚未被 MATLAB 轮询的旧命令；逐帧动作仅保留最新候选，防止连接暂断后回放过时动作。面板模式表示 UI 发出的控制请求；后端已执行的四元动作以遥测回显为准。遥测中断时，UI 战术停止更新，MATLAB 暂时沿用最近一次动作；硬件 TX 是否工作仍由独立互锁决定。

Agent 聊天工具也可通过 `execute_strategy` 启动这四种战术，或通过 `call_rl_inference` 请求当前 RL 算法推理一次动作；后者会切换到 Agent 一次性控制并入队动作。要持续由 RL 候选自主决策，应选择三种 Agent 预设之一。

只有右侧自然语言交互需要 DeepSeek 密钥。密钥保存在当前 Windows 用户的凭据管理器（`ZHJammerV4/DeepSeekAPI`，相当于 Windows 的密码保险库），不写入源码、`.env`、启动脚本或仓库；换机器或换 Windows 账号后需要重新保存。读取顺序为 `DEEPSEEK_API_KEY` 环境变量（仅本进程可见）优先，其次凭据管理器。预设 Agent、手动模式、Mock 后端和 RL 自动模式均不依赖该密钥。

### 与参考论文及总系统方案的关系

`work2` 原型论文将强制、节能、欺骗视为长期目标类型，随后由分层策略逐时隙选择恒定、扫频、随机、目标跟踪等干扰战术以及频段和功率；总系统方案进一步包含蓝方通信与抗干扰闭环。**当前仓库只实现红方干扰机子系统**：四种基础战术已经可通过 UI 或 Agent 工具执行；三种类型仍是基于现有 PPO/A2C/BDQ 单策略服务与规则回退的工程预设，尚未学习如何在四种战术间自主选择。当前尚无类型条件化训练、对手预测、贝叶斯 Stackelberg 求解、蓝方内部通信指标或 PPO–TD3 蓝方控制。尤其不能把多音波形或周期切换等同于已验证的“欺骗效果”。后续接入论文方法时，应复用 `state_req/action_rsp` 与四元动作接口，并分别验证训练、回退和 Mock 闭环。

## ARM 平台移植（RK3588）

`arm_port/` 是本仓库的**扩展子项目**：把射频后端从 MATLAB 完整移植到纯 Python，运行在
RK3588（野火鲁班猫 5，16 GB / 128 GB）上，外接同一台 USRP X310。RL 服务与 PyQt UI 一行未改，
三条 JSON-Lines 链路（:6001 / :5555 / :5557）的端口和报文结构保持不变。

| 关注点 | MATLAB 后端 | ARM 后端 |
| --- | --- | --- |
| 收流 | `comm.SDRuReceiver` | UHD MultiUSRP + **定长突发**（`NUM_SAMPS_AND_DONE`） |
| 发流 | `comm.SDRuTransmitter` | UHD `tx_stream.send()` 单突发 |
| 感知/波形 | `fft` / `fir1` / `filter` | numpy 等价实现（偏差 ≤1e-15） |
| 协议 | `jsonencode` + `tcpclient` | `json` + `socket` |
| 容错 | 超时→重建→缓存兜底 | 逐条复刻，另有耗时/计数/状态快照 |
| 互锁 | `ZH_RF_TX_ENABLE=LAB_ONLY` | 同一条环境变量，且配置文件无法开锁 |

快速开始（无硬件，30 秒跑通全栈）：

~~~bash
bash arm_port/scripts/run_stack.sh mock
PYTHONPATH=arm_port/python python -m jammer_arm selftest
PYTHONPATH=arm_port/python python -m unittest discover -s arm_port/tests
~~~

板上部署：`install_deps_rk3588.sh` → `build_uhd.sh` → `acceptance_test.sh`。
完整文档见 [arm_port/README.md](arm_port/README.md)，其中
[可行性分析](arm_port/docs/01_feasibility.md)、[实施计划](arm_port/docs/02_plan.md)、
[部署手册](arm_port/docs/03_deployment.md)、[验证报告](arm_port/docs/05_validation.md) 分别对应
移植的评估、计划、上线与验收。

**当前状态**：Python 后端、部署脚本与 120 项测试已完成，与 MATLAB 数值等价（特征 8.9e-16、
多音波形 2.8e-14）并已与真实 UI/RL 端到端联调；C++ `rfio` 守护进程已通过编译验证
（真实 UHD 4.9 头文件）与 40 项线格式测试。RK3588 + X310 实机联调（`docs/02_plan.md` P6）
与 `rfio` 的板端链接仍待硬件。

## 目录结构

~~~text
Jammer_Platform/
├─ AGENTS.md                         # 架构与协作开发约束
├─ README.md
├─ scripts/
│  └─ run_platform.sh                # Git Bash 一键启动器
├─ matlab/
│  ├─ project_setup.m                 # MATLAB 路径初始化
│  ├─ backend/
│  │  ├─ jammer_config.m              # 统一运行与射频配置
│  │  ├─ run_mock_backend_spectrum_control.m
│  │  ├─ run_jammer_backend.m
│  │  └─ jammer_backend_loop.m
│  ├─ interface/                      # RL、UI、命令 TCP 桥接
│  ├─ radio/                          # X310 TX/RX 初始化与安全送帧
│  ├─ sensing/sense_channel_grid.m    # FFT 与 10 信道特征
│  └─ waveform/generate_jammer_signal.m
├─ arm_port/                          # ARM（RK3588）纯 Python 后端，见 arm_port/README.md
│  ├─ python/jammer_arm/              # 配置、DSP、设备层、帧循环、CLI
│  ├─ cpp/                            # 可选 C++ rfio 守护进程（UHD 兜底）
│  ├─ scripts/                        # 安装 / 构建 / 运行 / 验收
│  ├─ systemd/                        # 三个常驻单元
│  ├─ tests/                          # 120 项测试 + MATLAB 黄金向量
│  └─ docs/                           # 可行性、计划、部署、协议、验证
└─ python/
   ├─ app/
   │  ├─ main_window.py               # PyQt5 UI 入口
   │  ├─ telemetry_server.py          # :5555
   │  ├─ command_bridge.py            # :5557
   │  └─ agent/                       # Agent、工具与策略
   └─ rl_service/
      ├─ rl_server.py                 # :6001
      ├─ action_codec.py
      ├─ ppo_agent.py / a2c_agent.py / bdq_agent.py
      ├─ train_ppo.py / train_a2c.py / train_bdq.py
      └─ checkpoints/                 # 随附 PPO/A2C bootstrap 权重
~~~

## 环境要求

- Windows + Conda（示例使用 jammer_ui、jammer_rl 两个环境）。
- MATLAB R2025a 或兼容版本。
- 真实硬件模式需要 Communications Toolbox、Signal Processing Toolbox，以及可用的 USRP/X310 支持环境。
- Python UI 依赖见 [python/app/requirements.txt](python/app/requirements.txt)。
- RL 服务依赖见 [python/rl_service/requirements.txt](python/rl_service/requirements.txt)。

如未创建环境，可分别安装：

~~~powershell
conda create -n jammer_ui python=3.10 -y
conda activate jammer_ui
pip install -r C:\Users\503\Desktop\zh_jammer_v4\python\app\requirements.txt

conda create -n jammer_rl python=3.10 -y
conda activate jammer_rl
pip install -r C:\Users\503\Desktop\zh_jammer_v4\python\rl_service\requirements.txt
~~~

## Git Bash 一键启动

一键启动器会按 UI（5555）→ RL（6001）→ MATLAB 的顺序自动拉起三进程；MATLAB 后端退出或在终端按 Ctrl+C 后，脚本会终止它启动的其余进程。运行日志写入未纳入版本控制的 logs/ 目录。

在 **Git Bash** 中，从仓库根目录执行：

~~~bash
bash scripts/run_platform.sh mock
~~~

上面的命令启动 Mock 全链路，默认使用 PPO。切换算法：

~~~bash
bash scripts/run_platform.sh mock --algo a2c
bash scripts/run_platform.sh mock --algo bdq
~~~

真实 X310 后端默认仍保持禁发，只做硬件初始化、感知、决策、波形生成与遥测：

~~~bash
bash scripts/run_platform.sh live
~~~

只有在已授权的屏蔽箱或充分衰减的有线测试环境中，才可显式解除脚本为 MATLAB 后端设置的射频互锁：

~~~bash
bash scripts/run_platform.sh live --allow-rf --algo ppo
~~~

常用参数：

| 参数 | 作用 |
| --- | --- |
| --algo ppo\|a2c\|bdq | 选择 RL 推理算法。 |
| --allow-rf | 仅允许与 live 同时使用；为 MATLAB 进程设置 ZH_RF_TX_ENABLE=LAB_ONLY。 |
| --ui-env 名称 / --rl-env 名称 | 覆盖默认 Conda 环境 jammer_ui / jammer_rl。 |
| --matlab-bin 路径 / --conda-bin 路径 | 覆盖 MATLAB 或 Conda 可执行文件。 |
| --no-ui / --no-rl | 调试时不启动对应的 Python 进程。 |
| --dry-run | 只验证参数并打印启动计划，不启动任何进程。 |

如果 Git Bash 未能从 PATH 找到 MATLAB，可传入 Windows 安装路径：

~~~bash
MATLAB_BIN='/c/Program Files/MATLAB/R2025a/bin/matlab.exe' \
  bash scripts/run_platform.sh mock
~~~

脚本会将当前的 DEEPSEEK_API_KEY 环境变量自动传给 UI 进程；如果已把密钥保存到凭据管理器，则无需设置该变量，脚本也不会读取或写入仓库文件。

## 手动启动：Mock 全链路

Mock 是首次运行与演示的推荐方式，不会连接或发射 USRP。

### 1. 启动 UI

在 PowerShell 新开一个终端：

~~~powershell
conda activate jammer_ui
Set-Location C:\Users\503\Desktop\zh_jammer_v4\python\app

python .\main_window.py
~~~

UI 应显示两个服务正在监听：遥测 5555、命令桥接 5557。

### 1.1 保存自然语言 Agent 的密钥（可选）

密钥不写在代码里。UI 启动后点击右侧面板标题栏的「密钥」按钮，粘贴 DeepSeek Key 并保存，密钥会存入当前 Windows 用户的凭据管理器（`ZHJammerV4/DeepSeekAPI`），保存后立即生效，无需重启。同一对话框可查看掩码状态或清除密钥。

也可以在终端录入，避免密钥出现在命令历史里：

~~~powershell
# 在 python\app 目录下
python -m agent.secret_store set     # 交互式隐藏输入
python -m agent.secret_store status  # 查看是否已配置
python -m agent.secret_store delete  # 清除
~~~

`DEEPSEEK_API_KEY` 环境变量仍受支持，且优先级高于凭据管理器；它只影响该终端启动的进程，不会落盘。

### 2. 启动 RL 服务

在第二个 PowerShell 终端：

~~~powershell
conda activate jammer_rl
Set-Location C:\Users\503\Desktop\zh_jammer_v4\python\rl_service
python .\rl_server.py --algo ppo
~~~

可将 ppo 替换为 a2c 或 bdq。当前仓库带有 PPO/A2C bootstrap 权重；若 BDQ 权重尚未训练，服务会提示并使用随机初始化。

### 3. 启动 MATLAB Mock 后端

在 MATLAB 命令窗口：

~~~matlab
cd('C:\Users\503\Desktop\zh_jammer_v4');
run('matlab/project_setup.m');
run_mock_backend_spectrum_control
~~~

Mock 后端约每 0.12 秒生成一帧随机频谱状态，向 RL 请求动作，再将遥测推送到 UI。按 Ctrl+C 停止。

## 真实 X310 模式

仅在已授权、屏蔽或充分衰减的测试链路中使用。

1. 按 Mock 的步骤先启动 UI 和 RL 服务。
2. 确认 X310 的地址与 [jammer_config.m](matlab/backend/jammer_config.m) 中的 cfg.ip_addr 一致。
3. 在启动后端**之前**设置发射互锁。可在 MATLAB 中执行：

~~~matlab
setenv('ZH_RF_TX_ENABLE', 'LAB_ONLY');
cd('C:\Users\503\Desktop\zh_jammer_v4');
run('matlab/project_setup.m');
run_jammer_backend
~~~

未设置 ZH_RF_TX_ENABLE=LAB_ONLY 时，后端仍会进行硬件初始化、接收感知、决策、波形生成和 UI 遥测，但 [tx_send_frame.m](matlab/radio/tx_send_frame.m) 不会调用实际 TX，状态为 disabled。

真实后端每轮按以下顺序执行：

~~~text
轮询 UI 命令 → RX 一帧 → FFT 感知 → RL/Agent 决策
→ 生成波形 → TX 一帧（仅已解除互锁）→ 上报遥测
~~~

RX 连续失败时，后端会先重建接收机；多次重建失败后会重建整条收发链路，并在无法接收时使用上一帧感知缓存维持遥测和控制循环。

## Agent 使用说明

1. 点击右侧面板的「密钥」按钮保存 DeepSeek Key（存入 Windows 凭据管理器），或在启动终端设置 DEEPSEEK_API_KEY。
2. 等待 MATLAB 遥测到达。
3. 在右侧 Agent 面板输入控制意图，例如“切换到 Agent 控制并扫描所有信道”或“固定在信道 5，功率 15 dB，带宽 4 MHz，使用多音”。
4. Agent 通过工具调用将策略/参数放入 :5557 命令队列；`execute_strategy` 与单次 `set_jamming_parameters` 会切换到 agent 控制，MATLAB 下一个轮询周期执行。
5. 使用“交还给 RL”取消策略并恢复 rl 模式。

持续运行的三种预设通过 UI 后台任务调用 RL 服务；额外规则策略在 UI 本地逐帧计算。右侧聊天只负责用户意图、工具调用和解释，不参与逐帧推理。

以上控制动作（切换预设策略、手动设参、RL 自动）也可不经聊天，直接通过右侧面板顶部的「控制策略」候选框完成。

## 离线训练

在 jammer_rl 环境中：

~~~powershell
Set-Location C:\Users\503\Desktop\zh_jammer_v4\python\rl_service
python .\train_ppo.py
python .\train_a2c.py
python .\train_bdq.py
~~~

PPO/A2C 训练默认输出到对应的 checkpoint 文件。训练模型应基于与当前 2/4/6/8 MHz 动作空间和 10×2 MHz 信道划分一致的数据/环境；变更射频配置后应重新训练或验证。

## 已知限制

- 真实发射链路尚需在实际 X310 与频谱仪环境下完成稳定性、中心频率、带宽和增益映射验收。
- 10 个逻辑信道位于 20 MHz 采样带宽边缘；大带宽动作会在边缘信道被自动裁剪。
- 遥测目前缺少“实际发射带宽”和硬件执行确认字段，UI 显示的是请求动作。
- 当前 power_db=0 不等于关闭 TX；不要用它代替射频互锁。
- 能量节约型只限制请求增益与带宽；低占用时请求 `power_db=0`，仍可能发射，不能用于停止发射。
- 自然语言 Agent 调用 `call_rl_inference` 依赖 RL 服务并发连接（现已支持多客户端）；RL 服务未运行时该工具返回错误并建议回退规则策略。
- 三种预设均调用启动时选定的单个 RL 模型，并非按三种类型分别训练；欺骗模式切换目前是规则原型，尚无蓝方反应或欺骗效果评估。
- 仓库随附的 PPO/A2C/BDQ 检查点对输入状态不敏感（同一 argmax 动作，value 仍随状态变化），属 bootstrap 权重性质；演示“状态驱动决策”前需重新训练或替换权重。
- ARM 后端的 RK3588 + X310 实机联调尚未执行；1 GbE 链路在 20 MS/s 下需 MTU 9000 直连，边缘余量见 [arm_port/docs/01_feasibility.md](arm_port/docs/01_feasibility.md) §3.2。
- ARM 后端的 C++ `rfio` 守护进程已通过编译检查（宿主机 + 真实 UHD 头文件）与线格式测试，但尚未在 aarch64 上链接、未运行 `--sim`。
- 本机 MATLAB R2025a 未安装 Communications Toolbox（`license('test','Communications_Toolbox')=0`），因此本仓库的 `live` 模式在该工作站上实际不可用；ARM 后端不依赖该工具箱。
