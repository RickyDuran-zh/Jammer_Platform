# Jammer Platform

智能频谱管控系统中的干扰机子系统，面向红蓝双方智能对抗实验。项目以单台 USRP X310 为射频后端，使用 MATLAB 完成频谱感知、波形生成和时分收发；Python 提供强化学习（RL）决策服务、实时监控界面及 Agent 自主控制入口。干扰机可根据实时频谱态势，由 RL 或 Agent 在信道、功率、带宽、波形和策略之间自主切换。

> **射频安全声明**：本项目默认禁止硬件发射。真实发射仅限获得授权的屏蔽箱或充分衰减的有线测试环境；使用者须自行遵守所在地频谱法规与实验室安全规范。

## 当前能力

- 围绕 **3.0 GHz、20 MHz 观察带宽、10 个逻辑信道**运行；每个逻辑信道宽度为 2 MHz。
- 支持 MATLAB Mock 后端，无需 USRP 即可验证 UI、RL、Agent 和 TCP 通信闭环。
- 支持单台 X310 的时分收发：先接收一帧 IQ 数据进行感知，再发送一帧干扰波形。
- 提供 PPO、A2C、BDQ 三种 RL 推理服务。
- 提供 PyQt5 实时监控界面：频谱、10 信道状态、趋势、动作日志与策略状态。
- 提供可选的 DeepSeek 兼容 Agent：依据实时态势自主切换控制模式、干扰参数和预定义策略，也支持自然语言查询与指令。
- 对真实 TX 提供显式环境变量互锁、复基带峰值限制和硬件增益限幅。

## 系统架构

~~~text
┌───────────────────── Python UI（127.0.0.1）──────────────────────┐
│ PyQt5 监控面板                                                     │
│  ├─ TelemetryServer :5555  接收 MATLAB 遥测                       │
│  ├─ CommandBridge   :5557  保存 Agent / UI 控制命令               │
│  └─ AgentBrain（可选） 本地策略编排 + DeepSeek 兼容工具调用       │
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
| RL 决策 | MATLAB ↔ Python RL 服务 | 6001 | state_req / action_rsp |
| 遥测 | MATLAB → Python UI | 5555 | telemetry |
| 控制 | Python UI → MATLAB | 5557 | MATLAB 轮询 cmd_poll，UI 回复 cmd_rsp |

三条链路都使用 UTF-8 JSON Lines（一条 JSON 一行）。MATLAB 和 Python 后端在连接失败后会重试；推荐仍按下面顺序启动，避免初始等待。

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
| 后端控制模式 | rl / agent | rl 由 RL 服务每帧给出动作；agent 根据实时遥测执行 Agent 参数或自主策略动作。 |
| RL 算法 | ppo / a2c / bdq | 三者都输出相同的四元动作。 |
| Agent 策略 | fixed | 固定动作。 |
|  | sweep | 依次遍历指定信道。 |
|  | track_busiest | 选择占用度最高的信道。 |
|  | track_weakest | 选择 SNR-like 指标最低的信道。 |
|  | alternating | 在给定信道之间轮换。 |
|  | reactive | 根据目标信道 SNR 在噪声/多音之间切换。 |

Agent 控制需要 DEEPSEEK_API_KEY；没有密钥时，UI、Mock 后端和 RL 模式仍可正常使用，但自然语言 Agent 不可用。

## 目录结构

~~~text
Jammer_Platform/
├─ README.md
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

## 快速开始：Mock 全链路

Mock 是首次运行与演示的推荐方式，不会连接或发射 USRP。

### 1. 启动 UI

在 PowerShell 新开一个终端：

~~~powershell
conda activate jammer_ui
Set-Location C:\Users\503\Desktop\zh_jammer_v4\python\app

# 可选：启用自然语言 Agent
$env:DEEPSEEK_API_KEY = "你的_API_密钥"

python .\main_window.py
~~~

UI 应显示两个服务正在监听：遥测 5555、命令桥接 5557。

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

1. 在启动 UI 的 PowerShell 中设置 DEEPSEEK_API_KEY。
2. 等待 MATLAB 遥测到达。
3. 在右侧 Agent 面板输入控制意图，例如“切换到 Agent 控制并扫描所有信道”或“固定在信道 5，功率 15 dB，带宽 4 MHz，使用多音”。
4. Agent 通过工具调用先切换到 agent 模式，再将策略/参数放入 :5557 命令队列；MATLAB 下一个轮询周期执行。
5. 使用“交还给 RL”取消策略并恢复 rl 模式。

Agent 参数控制依赖正确的控制模式顺序；如果只下发参数而未先切换为 agent，后续 RL 动作可能覆盖该参数。

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
