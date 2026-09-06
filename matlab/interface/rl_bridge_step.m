function [action, rl_meta, client] = rl_bridge_step(client, cfg, session_id, seq, channel_features, num_channels, prev_action, tx_state)
packet.type = "state_req";
packet.version = "2.0";
packet.session_id = session_id;
packet.seq = seq;
packet.timestamp_ms = posixtime(datetime('now')) * 1000;

packet.state.task = "jammer_spectrum_ctrl";
packet.state.num_channels = num_channels;
packet.state.channel_features = channel_features;
packet.state.prev_action = prev_action;
packet.state.tx_state = tx_state;

try
    txt = jsonencode(packet);
    write(client, uint8([txt newline]), "uint8");
    response = readline(client);
    resp = jsondecode(char(response));
catch
    % 连接断开或服务未就绪：回退到上一帧动作，并尝试重连，避免整帧被跳过。
    disp("RL 服务不可用，回退到上一帧动作。");
    client = tcp_reconnect(client, cfg.rl_host, cfg.rl_port, cfg.rl_timeout);
    action = prev_action;
    rl_meta.policy = "fallback";
    rl_meta.value = 0;
    rl_meta.latency_ms = 0;
    return;
end

if ~isfield(resp, 'status') || ~strcmp(resp.status, 'ok')
    disp("RL 服务返回异常，使用上一帧动作。");
    action = prev_action;
    rl_meta.policy = "fallback";
    rl_meta.value = 0;
    rl_meta.latency_ms = 0;
    return;
end

action = resp.action;
rl_meta = resp.rl_meta;
end
