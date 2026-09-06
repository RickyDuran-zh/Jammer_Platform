# Jammer Platform 开发指南

## 项目定位

本仓库是智能频谱管控系统中的干扰机子系统，服务于红蓝双方智能对抗实验，不是通用的应急通信应用。子系统负责感知频谱、通过 RL 或 Agent 选择干扰动作、在显式解除互锁后发射对应波形，并向监控界面回传遥测。

## 架构契约

系统由三个协作进程组成：

| 进程 | 主要入口 | 职责 |
| --- | --- | --- |
| MATLAB 后端 | matlab/backend/run_mock_backend_spectrum_control.m 或 matlab/backend/run_jammer_backend.m | Mock/真实感知、动作执行、波形生成、TX 与遥测。 |
| Python RL 服务 | python/rl_service/rl_server.py | 基于信道特征与上一动作进行 PPO、A2C 或 BDQ 推理。 |
| Python UI 与 Agent | python/app/main_window.py | 监控显示、遥测处理、命令队列、Agent 工具和策略编排。 |

传输接口约定：

- MATLAB ↔ RL 使用 127.0.0.1:6001 上的 JSON Lines：state_req / action_rsp。
- MATLAB → UI 使用 127.0.0.1:5555 上的 JSON Lines：telemetry。
- MATLAB 轮询 UI 的 127.0.0.1:5557：cmd_poll / cmd_rsp。
- 通用动作字段为：channel_idx（0–9）、power_db（0–20）、bw_mhz（2/4/6/8）、waveform_mode（0=带限噪声，1=多音）。

动作结构、端口、JSON-Line 分帧以及 10 信道配置，必须在 MATLAB、UI、Agent 工具、RL 编码器、环境、训练脚本和 README 中保持一致；不得只修改其中一端。

## 运行与射频约束

- matlab/backend/jammer_config.m 是射频、信号、端口与安全配置的唯一事实来源。
- 当前真实后端使用一台 X310，采用先 RX 后 TX 的时分方式；中心频率 3 GHz、采样率 20 MS/s、10 个逻辑 2 MHz 信道。
- 硬件 TX 默认禁用。只有在取得授权的屏蔽环境或充分衰减的有线测试环境中，才能通过 ZH_RF_TX_ENABLE=LAB_ONLY 解除互锁。
- 不得在代码或启动工具中弱化、移除、绕过或静默设置 TX 互锁。
- power_db=0 表示最小 TX 增益，不表示关闭射频发射。
- 不得提交 API 密钥、凭据、.env 文件、遥测采集数据、生成缓存、日志或未经审查的模型产物。

## 开发与验证流程

1. 修改子系统前，先检查既有配置、接口和调用方。
2. 变更运行契约时必须保持 Mock 模式可运行；在没有 USRP 时，它是首选端到端回归路径。
3. 使用 bash scripts/run_platform.sh mock 启动完整 Mock 栈；只有在已批准的射频测试环境中才能使用 live --allow-rf。
4. 代码修改后应运行相关 Python 导入/静态检查；条件允许时运行 MATLAB Mock 路径，并验证 UI、RL、MATLAB 的端口兼容性。
5. 除非变更明确调整架构，否则必须保持 MATLAB 的恢复机制、UI 遥测处理和 RL 回退行为。

## 文档强制规则

任何新增模块或功能，只要改变了架构、进程归属、数据流、端口、动作结构、射频配置、运行模式、启动/开发流程或安全行为，**必须在同一次变更中更新 README.md**。README 必须反映当前实际系统，不能保留过时设计或仅描述计划方案。

项目级开发约束或架构契约发生变化时，也必须同步更新本 AGENTS.md。

## 变更边界

- 优先进行聚焦修改，避免大范围重写，并保留无关的用户改动。
- 应复用既有的显式接口，不要创建重复的控制或遥测链路。
- 生成文件应写入 .gitignore，而不是提交到仓库。
- 新增 RL 算法时，应同步更新服务端 CLI、编码器/环境兼容性、训练入口、检查点行为、README；若架构契约变化，也要更新本指南。
- 新增波形模式时，应同步更新 MATLAB 波形生成与校验、UI 标签、Agent 工具定义、动作说明、测试和 README。
