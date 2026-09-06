function [y, tx_gain] = generate_jammer_signal(action, cfg)
% 根据动作生成针对某个信道的干扰波形
% action:
%   channel_idx
%   power_db
%   bw_mhz
%   waveform_mode  (0=带限噪声, 1=多音)

N = cfg.sample_num;
fs = cfg.sample_rate;
t = (0:N-1)' / fs;

channel_idx = double(action.channel_idx) + 1;
channel_idx = min(max(channel_idx, 1), cfg.num_channels);

power_db = min(max(double(action.power_db), cfg.power_db_min), cfg.power_db_max);
bw_mhz = double(action.bw_mhz);
waveform_mode = double(action.waveform_mode);

f0 = cfg.channel_centers_hz(channel_idx) - cfg.center_freq;    % 基带偏移
bw_hz = bw_mhz * 1e6;

% 防止边缘信道频移后超出奈奎斯特区间导致频谱混叠。
% 有效带宽受限于 |f0| + bw_hz/2 <= fs/2。
max_half_bw = fs/2 - abs(f0);
bw_hz = min(bw_hz, max(0, 2 * max_half_bw));

tx_gain = min(max(cfg.tx_gain_base + power_db * 0.5, cfg.tx_gain_min), cfg.tx_gain_max);

if waveform_mode == 0
    % 带限噪声
    base_noise = randn(N,1) + 1i * randn(N,1);
    norm_cutoff = min(max((bw_hz/2) / (fs/2), 0.001), 0.95);
    b = fir1(128, norm_cutoff, 'low');
    shaped = filter(b, 1, base_noise);
    y = shaped .* exp(1i * 2 * pi * f0 * t);
else
    % 多音干扰
    tone_count = 4;
    tones = zeros(N,1);
    offsets = linspace(-bw_hz/2, bw_hz/2, tone_count);
    for k = 1:tone_count
        tones = tones + exp(1i * 2 * pi * (f0 + offsets(k)) * t);
    end
    y = tones / sqrt(tone_count);
end

% Keep samples in a conservative linear range. Requested power maps to TX gain.
peak = max(abs(y));
if peak < 1e-12
    y = zeros(N, 1);
else
    y = cfg.tx_baseband_peak * y / peak;
end
end
