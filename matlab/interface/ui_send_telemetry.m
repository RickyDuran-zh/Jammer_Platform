function client = ui_send_telemetry(client, session_id, seq, fc, rx_signal, intersig, snr_est, bw_est, sig_type, tx_state, action, rl_meta, channel_map, channel_features, cfg)
try
    N = length(rx_signal);
    fs = cfg.sample_rate;
    spec = abs(fftshift(fft(rx_signal)));
    f = fc + (-N/2:N/2-1) * (fs / N);

    packet.type = "telemetry";
    packet.version = "2.0";
    packet.session_id = session_id;
    packet.seq = seq;
    packet.timestamp_ms = posixtime(datetime('now')) * 1000;

    ds = max(1, floor(N / 512));
    packet.telemetry.snr_est = double(snr_est);
    packet.telemetry.bw_est = double(bw_est);
    packet.telemetry.sig_type = double(sig_type);
    packet.telemetry.tx_state = tx_state;
    packet.telemetry.spectrum = double(spec(1:ds:end));
    packet.telemetry.freq_axis_ghz = double(f(1:ds:end) / 1e9);

    if ~isempty(intersig)
        w = abs(intersig(:));
        ds2 = max(1, floor(length(w) / 512));
        packet.telemetry.jam_waveform_abs = double(w(1:ds2:end)).';
    else
        packet.telemetry.jam_waveform_abs = [];
    end

    packet.telemetry.channel_map = double(channel_map);
    packet.telemetry.channel_features = double(channel_features);
    packet.action = action;
    packet.rl_meta = rl_meta;

    txt = jsonencode(packet);
    write(client, uint8([txt newline]), "uint8");
catch ME
    disp("遥测发送失败: " + ME.message);
    client = tcp_reconnect(client, cfg.ui_host, cfg.ui_port, cfg.ui_timeout);
end
end
