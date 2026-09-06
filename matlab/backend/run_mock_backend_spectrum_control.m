function run_mock_backend_spectrum_control()
project_setup();
cfg = jammer_config();

rl_client = rl_bridge_init(cfg.rl_host, cfg.rl_port, cfg.rl_timeout);
ui_client = ui_bridge_init(cfg.ui_host, cfg.ui_port, cfg.ui_timeout);
cmd_client = cmd_bridge_init(cfg.cmd_host, cfg.cmd_port, cfg.cmd_timeout);

session_id = "mock_spectrum_ctrl_001";
seq = 0;
agent_control_mode = false;  % false=RL自动, true=Agent手动

prev_action.channel_idx = 0;
prev_action.power_db = 0;
prev_action.bw_mhz = 4;
prev_action.waveform_mode = 0;

disp("模拟 MATLAB 后端已启动 (支持 Agent 控制模式)，按 Ctrl+C 停止。");

while true
    seq = seq + 1;

    % ── 轮询控制命令 ──
    try
        [cmds, cmd_client] = cmd_bridge_poll(cmd_client, cfg);
        for i = 1:length(cmds)
            cmd = cmds{i};
            % 控制模式切换
            if isfield(cmd, 'type') && strcmp(cmd.type, 'set_control_mode')
                if strcmp(cmd.mode, 'agent')
                    agent_control_mode = true;
                    fprintf('[CMD] 控制模式 -> Agent 手动控制\n');
                else
                    agent_control_mode = false;
                    fprintf('[CMD] 控制模式 -> RL 自动决策\n');
                end
                continue;
            end
            % 参数覆盖
            if isfield(cmd, 'channel_idx')
                prev_action.channel_idx = min(max(round(double(cmd.channel_idx)), 0), cfg.num_channels - 1);
                fprintf('[CMD] Agent 切换目标信道 -> %d\n', prev_action.channel_idx + 1);
            end
            if isfield(cmd, 'power_db')
                prev_action.power_db = max(0, min(20, double(cmd.power_db)));
                fprintf('[CMD] Agent 调整功率 -> %.1f dB\n', prev_action.power_db);
            end
            if isfield(cmd, 'bw_mhz')
                val = double(cmd.bw_mhz);
                if ismember(val, [2, 4, 6, 8])
                    prev_action.bw_mhz = val;
                    fprintf('[CMD] Agent 调整带宽 -> %d MHz\n', val);
                end
            end
            if isfield(cmd, 'waveform_mode')
                val = double(cmd.waveform_mode);
                if val == 0 || val == 1
                    prev_action.waveform_mode = val;
                    fprintf('[CMD] Agent 切换波形模式 -> %d\n', val);
                end
            end
        end
    catch
    end

    [channel_features, channel_map] = build_mock_channel_features(cfg, prev_action);
    snr_est = 5 + 15 * rand();
    bw_est = 10e6 + 10e6 * rand();
    sig_type = 1;
    tx_state = "mock_running";

    % ── 决策: Agent 控制 vs RL 控制 ──
    if agent_control_mode
        action = prev_action;
        rl_meta.policy = "agent";
        rl_meta.value = 0;
        rl_meta.latency_ms = 0;
    else
        [action, rl_meta, rl_client] = rl_bridge_step( ...
            rl_client, cfg, session_id, seq, channel_features, ...
            cfg.num_channels, prev_action, tx_state);
    end

    prev_action = action;

    rx_signal = build_mock_rx_signal(cfg, channel_features);
    [jam_sig, ~] = generate_jammer_signal(action, cfg);

    ui_client = ui_send_telemetry( ...
        ui_client, session_id, seq, cfg.center_freq, ...
        rx_signal, jam_sig, snr_est, bw_est, sig_type, tx_state, ...
        action, rl_meta, channel_map, channel_features, cfg);

    ctrl_str = "rl";
    if agent_control_mode
        ctrl_str = "agent";
    end
    fprintf('[MOCK][SEQ=%d] ch=%d, power=%.1f dB, bw=%d MHz, mode=%d, policy=%s, ctrl=%s\n', ...
        seq, action.channel_idx, action.power_db, action.bw_mhz, ...
        action.waveform_mode, string(rl_meta.policy), ctrl_str);

    pause(0.12);
end
end

function [features, channel_map] = build_mock_channel_features(cfg, prev_action)
features = zeros(cfg.num_channels, 4);
channel_map = zeros(1, cfg.num_channels);

for k = 1:cfg.num_channels
    occ = rand();
    interf = rand();
    util = 0.2 + 0.8 * rand();
    snr_like = max(0, min(1, occ * (1 - interf) + 0.1 * rand()));

    features(k, :) = [occ, interf, util, snr_like];

    if occ < 0.25
        channel_map(k) = 0;
    elseif interf > 0.65
        channel_map(k) = 2;
    else
        channel_map(k) = 1;
    end
end

% 根据实际干扰带宽计算覆盖范围，标注所有被干扰的信道
if isfield(prev_action, 'channel_idx') && isfield(prev_action, 'bw_mhz')
    jam_center = cfg.channel_centers_hz(double(prev_action.channel_idx) + 1);
    jam_half_bw = double(prev_action.bw_mhz) * 1e6 / 2;
    ch_half = cfg.channel_width_hz / 2;

    for k = 1:cfg.num_channels
        ch_center = cfg.channel_centers_hz(k);
        if abs(ch_center - jam_center) < (jam_half_bw + ch_half)
            if channel_map(k) == 1
                channel_map(k) = 3;
            elseif channel_map(k) == 0
                channel_map(k) = 2;
            end
        end
    end
end
end

function rx_signal = build_mock_rx_signal(cfg, channel_features)
N = cfg.sample_num;
fs = cfg.sample_rate;
t = (0:N-1)' / fs;
rx_signal = 0.05 * (randn(N,1) + 1i * randn(N,1));

for k = 1:cfg.num_channels
    occ = channel_features(k,1);
    interf = channel_features(k,2);
    amp = 0.1 + 0.5 * occ * (1 - 0.5 * interf);
    f0 = cfg.channel_centers_hz(k) - cfg.center_freq;
    rx_signal = rx_signal + amp * exp(1i * 2 * pi * f0 * t);
end
end
