function [channel_features, snr_est, bw_est, sig_type, channel_map] = sense_channel_grid(rx_signal, cfg, prev_action)
% 将当前接收频谱按 10 个信道切片，生成每个信道的特征
% 特征：occupancy, interference, utility, snr_like

N = max(4096, length(rx_signal));
spec = fftshift(fft(rx_signal, N));
power_spec = abs(spec).^2;
power_norm = power_spec / (max(power_spec) + 1e-12);

% 全局粗指标（兼容 UI）
signal_region = power_norm > 0.3;
noise_region = power_norm < 0.3;
if sum(signal_region) < 10
    signal_power = mean(power_spec);
else
    signal_power = mean(power_spec(signal_region));
end
if sum(noise_region) < 10
    noise_power = mean(power_spec);
else
    noise_power = mean(power_spec(noise_region));
end
snr_est = 10 * log10(signal_power / (noise_power + 1e-12));

active_bins = find(power_norm > 0.1);
if isempty(active_bins)
    bw_est = 0;
    sig_type = 0;
else
    bw_bins = max(active_bins) - min(active_bins);
    bw_est = bw_bins * (cfg.sample_rate / N);
    if bw_est < 20e6
        sig_type = 1;
    elseif bw_est < 50e6
        sig_type = 2;
    else
        sig_type = 3;
    end
end

freq_axis = cfg.center_freq + (-N/2:N/2-1)' * (cfg.sample_rate / N);
channel_features = zeros(cfg.num_channels, 4);
channel_map = zeros(1, cfg.num_channels);

for k = 1:cfg.num_channels
    ch_center = cfg.channel_centers_hz(k);
    ch_half = cfg.channel_width_hz / 2;
    idx = find(freq_axis >= (ch_center - ch_half) & freq_axis < (ch_center + ch_half));
    if isempty(idx)
        continue;
    end

    ch_energy = mean(power_norm(idx));
    ch_peak = max(power_norm(idx));
    occ = min(1, ch_energy * 1.8);
    interf = min(1, std(power_norm(idx)) * 4 + 0.2 * ch_peak);
    util = max(0, min(1, 0.65 * occ + 0.35 * ch_peak - 0.25 * interf));
    snr_like = max(0, min(1, ch_peak - mean(power_norm(noise_region))));

    channel_features(k, :) = [occ, interf, util, snr_like];

    if occ < 0.15
        channel_map(k) = 0;
    elseif interf > 0.65
        channel_map(k) = 2;
    else
        channel_map(k) = 1;
    end
end

% 标注上一时隙已执行干扰的信道（根据实际干扰带宽计算覆盖范围）
if isfield(prev_action, 'channel_idx') && isfield(prev_action, 'bw_mhz')
    jam_center = cfg.channel_centers_hz(double(prev_action.channel_idx) + 1);
    jam_half_bw = double(prev_action.bw_mhz) * 1e6 / 2;
    ch_half = cfg.channel_width_hz / 2;

    for k = 1:cfg.num_channels
        ch_center = cfg.channel_centers_hz(k);
        % 判断干扰信号频率范围是否与该信道有重叠
        if abs(ch_center - jam_center) < (jam_half_bw + ch_half)
            if channel_map(k) == 1
                channel_map(k) = 3;  % 用户占用 + 干扰重叠
            elseif channel_map(k) == 0
                channel_map(k) = 2;  % 仅干扰
            end
            % 如果已经是 2 或 3，保持不变
        end
    end
end
end
