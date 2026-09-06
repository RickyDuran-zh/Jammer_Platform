function run_jammer_backend()
project_setup();
cfg = jammer_config();

[state.radio_tx, state.radio_rx] = radio_init(cfg);
cleanupObj = onCleanup(@() release_radios(state.radio_tx, state.radio_rx)); %#ok<NASGU>
state.rl_client = rl_bridge_init(cfg.rl_host, cfg.rl_port, cfg.rl_timeout);
state.ui_client = ui_bridge_init(cfg.ui_host, cfg.ui_port, cfg.ui_timeout);
state.cmd_client = cmd_bridge_init(cfg.cmd_host, cfg.cmd_port, cfg.cmd_timeout);

state.isStop = false;
state.seq = 0;
state.tx_state = "disabled";
state.agent_control_mode = false;  % false=RL自动, true=Agent手动
state.prev_action.channel_idx = 0;
state.prev_action.power_db = 0;
state.prev_action.bw_mhz = 4;
state.prev_action.waveform_mode = 0;
state.last_signal = [];
state.rx_timeout_count = 0;
state.rx_reinit_fail_count = 0;
state.last_rx_signal = zeros(cfg.sample_num, 1);
state.last_channel_features = zeros(cfg.num_channels, 4);
state.last_channel_map = zeros(1, cfg.num_channels);
state.last_snr_est = 0;
state.last_bw_est = 0;
state.last_sig_type = 0;

pause(0.5);  % USRP PLL lock & calibration settling time

disp("真实 MATLAB 后端已启动，按 Ctrl+C 停止。");
jammer_backend_loop(state, cfg);
end

function release_radios(radio_tx, radio_rx)
try, release(radio_tx); catch, end
try, release(radio_rx); catch, end
end
