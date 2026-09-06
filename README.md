# 面向 3GHz / 10 信道的频谱智能干扰控制系统（v3）

本版本将项目重构为 **三进程架构**，并把动作空间从“窄带/多音/音调数”重构为更贴近比赛与平台目标的：

- **干扰信道选择**：10 个信道之一
- **功率控制**：可由 DRL 直接决策
- **干扰带宽选择**：预设若干可选带宽
- **波形模式选择**：带限噪声 / 多音（可选）

## 一、三进程架构

1. **MATLAB 后端**
   - 连接 USRP
   - 接收 IQ、感知 10 个信道的状态
   - 向 RL 服务发送 `state_req`
   - 根据返回动作生成干扰并发射
   - 向 UI 发送 `telemetry`

2. **RL 服务**
   - 接收 MATLAB 的状态请求
   - 支持三类算法：
     - PPO（离散复合动作）
     - A2C（离散复合动作）
     - BDQ（分支离散动作）
   - 返回统一动作格式：
     - `channel_idx`
     - `power_db`
     - `bw_mhz`
     - `waveform_mode`

3. **UI 进程**
   - 只负责显示，不参与决策
   - 显示：
     - 当前 10 信道状态
     - 实时频谱
     - 历史趋势
     - 动作日志
     - RL 元信息

## 二、关键前提与当前实现假设

你明确提出的平台前提是：
- 通信系统工作在 3GHz 附近
- 预划分 **10 个信道**
- 每个信道约 **10–20 MHz**

本版本默认采用一个可直接实现的基线信道规划：
- **总带宽视野**：100 MHz
- **信道数**：10
- **默认信道宽度**：10 MHz
- **中心频率**：3.0 GHz
- **信道中心**：3.0 GHz ± 45 MHz，共 10 个

如果你后续需要把单信道改成 15/20 MHz，或者总带宽扩到 150/200 MHz，需要同步修改：
- `matlab/backend/jammer_config.m`
- USRP 采样率 / decimation / interpolation
- 以及可能的扫描式感知逻辑

## 三、动作空间

统一动作字段如下：

```json
{
  "channel_idx": 0,
  "power_db": 12.5,
  "bw_mhz": 10,
  "waveform_mode": 0
}
```

含义：
- `channel_idx`：选择干扰的信道（0~9）
- `power_db`：相对功率控制变量，当前用于映射波形幅度和 TX Gain 修正
- `bw_mhz`：干扰带宽，可选值见配置
- `waveform_mode`：
  - 0 = 带限噪声干扰
  - 1 = 多音干扰

### PPO / A2C
采用 **离散复合动作空间**：
- 10 个信道
- 若干功率档位
- 若干带宽档位
- 2 种波形模式

### BDQ
采用 **分支离散动作空间**，分别输出信道、功率、带宽和波形模式分支，
再组合为统一的可执行动作。

## 四、目录结构

```text
zh_jammer_spectrum_control_v3/
├─ README.md
├─ PROTOCOL.md
├─ matlab/
│  ├─ project_setup.m
│  ├─ backend/
│  │  ├─ jammer_config.m
│  │  ├─ run_jammer_backend.m
│  │  ├─ jammer_backend_loop.m
│  │  └─ run_mock_backend_spectrum_control.m
│  ├─ interface/
│  │  ├─ rl_bridge_init.m
│  │  ├─ rl_bridge_step.m
│  │  ├─ ui_bridge_init.m
│  │  └─ ui_send_telemetry.m
│  ├─ radio/
│  │  └─ radio_init.m
│  ├─ sensing/
│  │  └─ sense_channel_grid.m
│  └─ waveform/
│     └─ generate_jammer_signal.m
└─ python/
   ├─ app/
   │  ├─ requirements.txt
   │  ├─ telemetry_server.py
   │  └─ main_window.py
   └─ rl_service/
      ├─ requirements.txt
      ├─ action_codec.py
      ├─ compat_policy.py
      ├─ jammer_ctrl_env.py
      ├─ networks.py
      ├─ ppo_agent.py
      ├─ a2c_agent.py
      ├─ bdq_agent.py
      ├─ train_ppo.py
      ├─ train_a2c.py
      ├─ train_bdq.py
      ├─ rl_server.py
      └─ checkpoints/
```

## 五、当前项目怎么运行

### A. 真实 MATLAB + USRP 链路（当前建议先用兼容/随机策略验证硬件）

#### 1. 启动 UI
```bash
conda activate jammer_ui
cd python/app
pip install -r requirements.txt
python main_window.py
```

#### 2. 启动 RL 服务
PPO：
```bash
conda activate jammer_rl
cd python/rl_service
pip install -r requirements.txt
python rl_server.py --algo ppo
```

A2C：
```bash
python rl_server.py --algo a2c
```

BDQ：
```bash
python rl_server.py --algo bdq
```

#### 3. 启动 MATLAB 后端
```matlab
run('matlab/project_setup.m');
run_jammer_backend
```

### B. 无 USRP，先验证三进程框架（强烈建议先跑这个）
```matlab
run('matlab/project_setup.m');
run_mock_backend_spectrum_control
```

它会：
- 模拟 10 个信道的占用 / 干扰 / 价值
- 向 RL 请求动作
- 用 MATLAB 向 UI 推送遥测
- 验证三进程框架与动作链路

## 六、当前真实嵌入情况

### 已经真正嵌入的
- RL 服务已经能返回 **信道 / 功率 / 带宽 / 模式**
- UI 已经能显示这些动作
- MATLAB 模拟后端已经可以真正消费这些动作

### 需要你后续结合硬件继续落地的
- 真实 USRP 环境里，`sense_channel_grid.m` 目前是基于当前接收 FFT 的切片式感知
- 若真实采样率不足以同时覆盖 100 MHz 视野，需要切换成：
  - 更高采样率
  - 或逐信道扫描式接收再拼接状态

## 七、关于“智能决策”的真实含义

这版开始，RL 的动作不再是：
- 选窄带 / 多音
- 选音调数

而是更贴近你需要的：
- **选哪一个信道去干扰**
- **功率怎么设**
- **带宽怎么设**
- **波形模式怎么选**

这才是更像“频谱具身智能管控”的动作定义。

## 八、离线训练

### PPO
```bash
conda activate jammer_rl
cd python/rl_service
python train_ppo.py
```

### A2C
```bash
python train_a2c.py
```

### BDQ
```bash
python train_bdq.py
```

训练完成后会生成：
- `checkpoints/ppo_spectrum_ctrl.pt`
- `checkpoints/a2c_spectrum_ctrl.pt`
- `checkpoints/bdq_spectrum_ctrl.pt`


> 说明：当前随项目提供的 PPO/A2C 权重是 **bootstrap 离线训练权重**；BDQ 权重可通过训练脚本生成。它们用于先打通“状态 -> 决策 -> 执行 -> 显示”链路，便于后续使用实测数据继续训练与微调。

## 九、下一步建议
1. 先跑 `run_mock_backend_spectrum_control`
2. 看 UI、RL、MATLAB 三进程是否联通
3. 再切到真实 `run_jammer_backend`
4. 再根据硬件侧实际采样能力优化 `sense_channel_grid.m`
5. 最后再用真实日志做再训练/微调
