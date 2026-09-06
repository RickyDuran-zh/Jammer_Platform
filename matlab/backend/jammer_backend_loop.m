function jammer_backend_loop(state, cfg)
while ~state.isStop
    try
        state.seq = state.seq + 1;

        % ── 轮询控制命令（来自 Python UI AI Agent 或 StrategyEngine）──
        try
            [cmds, state.cmd_client] = cmd_bridge_poll(state.cmd_client, cfg);
            for i = 1:length(cmds)
                cmd = cmds{i};
                state = apply_command(state, cmd, cfg);
            end
        catch
            % 命令桥接暂时不可用，跳过
        end

        % ── 接收（含超时重建与缓存兜底）──
        [state, rx_signal, rx_ok] = safe_receive(state, cfg);
        if rx_ok
            [channel_features, snr_est, bw_est, sig_type, channel_map] = ...
                sense_channel_grid(rx_signal, cfg, state.prev_action);
            state.last_rx_signal = rx_signal;
            state.last_channel_features = channel_features;
            state.last_channel_map = channel_map;
            state.last_snr_est = snr_est;
            state.last_bw_est = bw_est;
            state.last_sig_type = sig_type;
        else
            [rx_signal, channel_features, snr_est, bw_est, sig_type, channel_map] = ...
                cached_sensing(state, cfg);
        end

        % ── 决策: Agent 控制 vs RL 控制 ──
        if state.agent_control_mode
            % Agent 控制模式: 跳过 RL, 使用 Agent 预设的参数
            action = state.prev_action;
            rl_meta.policy = "agent";
            rl_meta.value = 0;
            rl_meta.latency_ms = 0;
        else
            % RL 控制模式: 正常调用 RL 服务
            [action, rl_meta, state.rl_client] = rl_bridge_step( ...
                state.rl_client, cfg, cfg.session_id, state.seq, channel_features, ...
                cfg.num_channels, state.prev_action, state.tx_state);
        end

        state.prev_action = action;

        [jam_sig, tx_gain] = generate_jammer_signal(action, cfg);
        state.last_signal = jam_sig;

        try
            state.tx_state = tx_send_frame(state.radio_tx, jam_sig, tx_gain, cfg);
        catch MEtx
            state.tx_state = "error";
            disp("TX warning:");
            disp(MEtx.message);
        end

        state.ui_client = ui_send_telemetry( ...
            state.ui_client, cfg.session_id, state.seq, cfg.center_freq, ...
            rx_signal, jam_sig, snr_est, bw_est, sig_type, state.tx_state, ...
            action, rl_meta, channel_map, channel_features, cfg);

        fprintf('[SEQ=%d] ch=%d, power=%.1f dB, bw=%d MHz, mode=%d, ctrl=%s\n', ...
            state.seq, action.channel_idx, action.power_db, action.bw_mhz, ...
            action.waveform_mode, iif(state.agent_control_mode, "agent", "rl"));

        pause(cfg.pause_sec);

    catch ME
        disp("MATLAB 后端错误：");
        disp(ME.message);
        disp("跳过当前帧，继续运行...");
        continue;
    end
end
end

function state = apply_command(state, cmd, cfg)
% apply_command  处理来自 Python AI Agent 的控制命令
%   支持: channel_idx, power_db, bw_mhz, waveform_mode
%   新增: set_control_mode (切换 RL/Agent 控制)

    % ── 控制模式切换 ──
    if isfield(cmd, 'type') && strcmp(cmd.type, 'set_control_mode')
        if strcmp(cmd.mode, 'agent')
            state.agent_control_mode = true;
            fprintf('[CMD] 控制模式 -> Agent 手动控制\n');
        else
            state.agent_control_mode = false;
            fprintf('[CMD] 控制模式 -> RL 自动决策\n');
        end
        return;
    end

    % ── 参数覆盖 (Agent 在控制模式下使用) ──
    if isfield(cmd, 'channel_idx')
        val = min(max(round(double(cmd.channel_idx)), 0), cfg.num_channels - 1);
        state.prev_action.channel_idx = val;
        fprintf('[CMD] Agent 切换目标信道 -> %d\n', val + 1);
    end
    if isfield(cmd, 'power_db')
        val = double(cmd.power_db);
        state.prev_action.power_db = max(0, min(20, val));
        fprintf('[CMD] Agent 调整功率 -> %.1f dB\n', state.prev_action.power_db);
    end
    if isfield(cmd, 'bw_mhz')
        val = double(cmd.bw_mhz);
        if ismember(val, [2, 4, 6, 8])
            state.prev_action.bw_mhz = val;
            fprintf('[CMD] Agent 调整带宽 -> %d MHz\n', val);
        else
            fprintf('[CMD] 无效带宽值 %d MHz，忽略 (允许 2/4/6/8)\n', val);
        end
    end
    if isfield(cmd, 'waveform_mode')
        val = double(cmd.waveform_mode);
        if val == 0 || val == 1
            state.prev_action.waveform_mode = val;
            fprintf('[CMD] Agent 切换波形模式 -> %s\n', ...
                iif(val == 0, "带限噪声", "多音干扰"));
        else
            fprintf('[CMD] 无效波形模式 %d，忽略 (允许 0/1)\n', val);
        end
    end
end

function [state, rx_signal, rx_ok] = safe_receive(state, cfg)
% safe_receive  带超时重建的接收。X310 偶发 ACK timeout 时先重建 RX，
%   反复失败则重建整条收发链路；全部失败返回缓存感知兜底。
    rx_ok = false;
    rx_signal = [];

    state = ensure_recovery_state(state, cfg);

    try
        rx_signal = state.radio_rx();
        state.rx_timeout_count = 0;
        state.rx_reinit_fail_count = 0;
        rx_ok = true;
        return;
    catch ME
        state.rx_timeout_count = state.rx_timeout_count + 1;
        fprintf('[RX] receive failed (%d/%d): %s\n', ...
            state.rx_timeout_count, cfg.rx_timeout_reinit_threshold, ME.message);
    end

    if state.rx_timeout_count >= cfg.rx_timeout_reinit_threshold
        [state, rebuilt] = rebuild_rx(state, cfg);
        if rebuilt
            fprintf('[RX] receiver rebuilt after timeout burst.\n');
        else
            fprintf('[RX] receiver rebuild failed (%d/%d).\n', ...
                state.rx_reinit_fail_count, cfg.full_radio_reinit_threshold);
            if state.rx_reinit_fail_count >= cfg.full_radio_reinit_threshold
                [state, full_rebuilt] = rebuild_full_radio(state, cfg);
                if full_rebuilt
                    fprintf('[RX] full radio rebuilt after repeated RX rebuild failures.\n');
                else
                    fprintf('[RX] full radio rebuild failed; will retry next loop.\n');
                end
            end
        end
        state.rx_timeout_count = 0;
    end
end

function state = ensure_recovery_state(state, cfg)
    if ~isfield(state, 'rx_timeout_count')
        state.rx_timeout_count = 0;
    end
    if ~isfield(state, 'rx_reinit_fail_count')
        state.rx_reinit_fail_count = 0;
    end
    if ~isfield(state, 'last_rx_signal') || isempty(state.last_rx_signal)
        state.last_rx_signal = zeros(cfg.sample_num, 1);
    end
    if ~isfield(state, 'last_channel_features') || isempty(state.last_channel_features)
        state.last_channel_features = zeros(cfg.num_channels, 4);
    end
    if ~isfield(state, 'last_channel_map') || isempty(state.last_channel_map)
        state.last_channel_map = zeros(1, cfg.num_channels);
    end
    if ~isfield(state, 'last_snr_est'), state.last_snr_est = 0; end
    if ~isfield(state, 'last_bw_est'), state.last_bw_est = 0; end
    if ~isfield(state, 'last_sig_type'), state.last_sig_type = 0; end
end

function [state, rebuilt] = rebuild_rx(state, cfg)
    rebuilt = false;
    try
        fprintf('[RX] timeout threshold reached; releasing and rebuilding receiver...\n');
        try, release(state.radio_rx); catch, end
        pause(cfg.rx_reinit_pause_sec);
        state.radio_rx = radio_rx_init(cfg);
        state.rx_reinit_fail_count = 0;
        rebuilt = true;
    catch ME
        state.rx_reinit_fail_count = state.rx_reinit_fail_count + 1;
        disp("[RX] rebuild warning:");
        disp(ME.message);
    end
end

function [state, rebuilt] = rebuild_full_radio(state, cfg)
    rebuilt = false;
    try
        fprintf('[RX] rebuilding full radio chain (TX/RX)...\n');
        try, release(state.radio_rx); catch, end
        try, release(state.radio_tx); catch, end
        pause(cfg.rx_reinit_pause_sec);
        [state.radio_tx, state.radio_rx] = radio_init(cfg);
        state.rx_reinit_fail_count = 0;
        rebuilt = true;
    catch ME
        disp("[RX] full radio rebuild warning:");
        disp(ME.message);
    end
end

function [rx_signal, channel_features, snr_est, bw_est, sig_type, channel_map] = cached_sensing(state, cfg)
% cached_sensing  RX 不可用时，用上一帧缓存数据兜底，保证遥测与决策不中断。
    state = ensure_recovery_state(state, cfg);
    rx_signal = state.last_rx_signal;
    channel_features = state.last_channel_features;
    channel_map = state.last_channel_map;
    snr_est = state.last_snr_est;
    bw_est = state.last_bw_est;
    sig_type = state.last_sig_type;
end

function s = iif(cond, t, f)
    if cond
        s = t;
    else
        s = f;
    end
end
