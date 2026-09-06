function cfg = jammer_config()
% 单 USRP 稳态调试版
% 目标：一台 USRP 用 RX 先感知环境，再用 TX 发射干扰（时分收发）
% 为了降低 ACK timeout 风险，先把观测总带宽和采样率都收紧。

cfg.session_id = "jammer_spectrum_ctrl_single_usrp";

cfg.center_freq = 3.0e9;

% ---------- 频谱观测与信道划分 ----------
% 单台 USRP 稳态版先把总视野缩到 20 MHz
% 10 个逻辑信道，每个 2 MHz
cfg.total_span_hz = 20e6;
cfg.num_channels = 10;
cfg.channel_width_hz = 2e6;
cfg.channel_centers_hz = linspace(cfg.center_freq - 9e6, cfg.center_freq + 9e6, cfg.num_channels);

% ---------- 动作空间 ----------
% 干扰带宽候选先收紧到更安全的范围，避免边缘信道越界
cfg.bw_choices_mhz = [2, 4, 6, 8];

% 功率控制（相对量）
cfg.power_levels_db = [0, 5, 10, 15, 20];
cfg.power_db_min = 0;
cfg.power_db_max = 20;

% 波形模式：0=带限噪声，1=多音
cfg.waveform_modes = [0, 1];

% ---------- 单台 USRP ----------
cfg.ip_addr = '192.168.10.2';

% 采样率从 100 MHz 降到 20 MHz
cfg.MasterClockRate = 200e6;
cfg.InterpolationFactor = 10;     % 200e6 / 10 = 20 MHz
cfg.sample_rate = cfg.MasterClockRate / cfg.InterpolationFactor;
cfg.sample_num = 4096;

% TX / RX 增益分开
cfg.tx_antenna = 'TX/RX';
cfg.rx_antenna = 'TX/RX';
cfg.tx_baseband_peak = 0.25;
cfg.tx_gain_base = 0;
cfg.tx_gain_min = 0;
cfg.tx_gain_max = 10;
cfg.rx_gain = 15;

% RX 超时重建阈值（单机时分收发下 X310 可能偶发 ACK timeout）
cfg.rx_timeout_reinit_threshold = 3;
cfg.rx_reinit_pause_sec = 0.2;
cfg.full_radio_reinit_threshold = 2;

% RF transmission is opt-in. Arm only in an authorized shielded/cabled setup.
cfg.tx_interlock_env = 'ZH_RF_TX_ENABLE';
cfg.tx_interlock_value = 'LAB_ONLY';
cfg.tx_enabled = strcmp(getenv(cfg.tx_interlock_env), cfg.tx_interlock_value);

% 单机时分收发时，发射重复次数先降到 1
cfg.tx_repeat = 1;
cfg.pause_sec = 0.08;

% ---------- RL 服务 ----------
cfg.rl_host = "127.0.0.1";
cfg.rl_port = 6001;
cfg.rl_timeout = 2;

% ---------- UI 遥测 ----------
cfg.ui_host = "127.0.0.1";
cfg.ui_port = 5555;
cfg.ui_timeout = 1;

% ---------- 命令桥接（Python -> MATLAB 控制参数）----------
cfg.cmd_host = "127.0.0.1";
cfg.cmd_port = 5557;
cfg.cmd_timeout = 1;
end
