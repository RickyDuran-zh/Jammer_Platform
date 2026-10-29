function dump_reference(out_dir)
%DUMP_REFERENCE  Export golden vectors from the ORIGINAL MATLAB implementation.
%
%   The ARM port (arm_port/python/jammer_arm) must reproduce these numbers.
%   Run from the repository root or pass an explicit output directory:
%
%       matlab -batch "addpath('arm_port/tools/matlab_reference'); dump_reference"
%
%   Output: <out_dir>/reference.json  (consumed by arm_port/tests/test_equivalence.py)
%
%   Notes
%   -----
%   * Sensing cases are fully deterministic (inputs are exported verbatim), so the
%     Python port can be compared sample-by-sample.
%   * The band-limited-noise waveform depends on MATLAB's randn stream, which
%     numpy cannot reproduce. For those cases we export the designed FIR taps and
%     a deterministic filter test instead of the noise realisation, and compare
%     the Python output statistically (band edges / peak / rejection).
%   * Multi-tone cases are deterministic and compared exactly.

if nargin < 1 || isempty(out_dir)
    here = fileparts(mfilename('fullpath'));
    out_dir = fullfile(here, '..', '..', 'tests', 'data');
end
if ~exist(out_dir, 'dir')
    mkdir(out_dir);
end

repo = fullfile(fileparts(mfilename('fullpath')), '..', '..', '..');
addpath(fullfile(repo, 'matlab', 'backend'));
addpath(fullfile(repo, 'matlab', 'sensing'));
addpath(fullfile(repo, 'matlab', 'waveform'));

cfg = jammer_config();
ref = struct();
ref.generated_by = 'matlab dump_reference.m';
ref.matlab_version = version();
ref.cfg = sanitize(cfg);      % plain JSON-encodable copy of jammer_config()
ref.cases = {};

rng(20261029, 'twister');

% ── Case 1: three user signals + noise, mixed occupancy ────────────────
N = cfg.sample_num;
fs = cfg.sample_rate;
t = (0:N-1).' / fs;
rx = 0.02 * (randn(N,1) + 1i*randn(N,1));
rx = rx + 0.6 * exp(1i*2*pi*(cfg.channel_centers_hz(3)-cfg.center_freq)*t);
rx = rx + 0.35 * exp(1i*2*pi*((cfg.channel_centers_hz(7)-cfg.center_freq)+0.4e6)*t);
rx = rx + 0.15 * exp(1i*2*pi*((cfg.channel_centers_hz(8)-cfg.center_freq)-0.5e6)*t);
ref.cases{end+1} = sensing_case('sensing_mixed', rx, cfg, ...
    struct('channel_idx', 6, 'power_db', 10, 'bw_mhz', 4, 'waveform_mode', 0));

% ── Case 2: noise only ────────────────────────────────────────────────
rx = 0.05 * (randn(N,1) + 1i*randn(N,1));
ref.cases{end+1} = sensing_case('sensing_noise_only', rx, cfg, ...
    struct('channel_idx', 0, 'power_db', 0, 'bw_mhz', 2, 'waveform_mode', 0));

% ── Case 3: single narrow tone (small bw_est) ─────────────────────────
rx = 0.01 * (randn(N,1) + 1i*randn(N,1));
rx = rx + 1.0 * exp(1i*2*pi*(cfg.channel_centers_hz(1)-cfg.center_freq)*t);
ref.cases{end+1} = sensing_case('sensing_narrow_tone', rx, cfg, ...
    struct('channel_idx', 0, 'power_db', 20, 'bw_mhz', 8, 'waveform_mode', 1));

% ── Case 4: wideband signal (large bw_est) + jam overlay at ch5 bw6 ───
rx = 0.01 * (randn(N,1) + 1i*randn(N,1));
rx = rx + 0.4 * (randn(N,1) + 1i*randn(N,1)) .* exp(1i*2*pi*0*t);
ref.cases{end+1} = sensing_case('sensing_wideband', rx, cfg, ...
    struct('channel_idx', 4, 'power_db', 5, 'bw_mhz', 6, 'waveform_mode', 0));

% ── Case 5/6: deterministic multi-tone waveforms (exact compare) ──────
ref.cases{end+1} = waveform_case('waveform_tone_ch0_bw8', ...
    struct('channel_idx', 0, 'power_db', 20, 'bw_mhz', 8, 'waveform_mode', 1), cfg);
ref.cases{end+1} = waveform_case('waveform_tone_ch9_bw8', ...
    struct('channel_idx', 9, 'power_db', 0, 'bw_mhz', 8, 'waveform_mode', 1), cfg);
ref.cases{end+1} = waveform_case('waveform_tone_ch4_bw2', ...
    struct('channel_idx', 4, 'power_db', 12, 'bw_mhz', 2, 'waveform_mode', 1), cfg);

% ── Case 7: FIR taps for every bandwidth the action space can request ─
fir_entries = {};
for bw = cfg.bw_choices_mhz
    for ch = [0, 4, 9]
        f0 = cfg.channel_centers_hz(ch+1) - cfg.center_freq;
        bw_hz = bw * 1e6;
        max_half_bw = fs/2 - abs(f0);
        bw_hz = min(bw_hz, max(0, 2 * max_half_bw));
        norm_cutoff = min(max((bw_hz/2) / (fs/2), 0.001), 0.95);
        b = fir1(128, norm_cutoff, 'low');
        fir_entries{end+1} = struct( ...
            'bw_mhz', bw, 'channel_idx', ch, ...
            'bw_hz_effective', bw_hz, 'norm_cutoff', norm_cutoff, ...
            'taps', b(:).');
    end
end
ref.fir_designs = struct('entries', {fir_entries});

% ── Case 8: deterministic FIR filtering (filter(b,1,x)) ───────────────
b = fir1(128, 0.2, 'low');
x = (1:N).' / N;                       % deterministic ramp + tone
x = complex(x, sin(2*pi*0.05*(0:N-1).'));
y = filter(b, 1, x);
ref.fir_filter_test = struct( ...
    'cutoff', 0.2, 'taps', b(:).', ...
    'x_re', real(x).', 'x_im', imag(x).', ...
    'y_re', real(y).', 'y_im', imag(y).');

path_out = fullfile(out_dir, 'reference.json');
fid = fopen(path_out, 'w');
fprintf(fid, '%s', jsonencode(ref));
fclose(fid);
fprintf('wrote %s (%d cases)\n', path_out, numel(ref.cases));
end

% ══════════════════════════════════════════════════════════════════════

function c = sensing_case(name, rx, cfg, prev_action)
[channel_features, snr_est, bw_est, sig_type, channel_map] = ...
    sense_channel_grid(rx, cfg, prev_action);
N = numel(rx);
spec = abs(fftshift(fft(rx, max(4096, N))));
c = struct( ...
    'kind', 'sensing', 'name', name, ...
    'rx_re', real(rx(:)).', 'rx_im', imag(rx(:)).', ...
    'prev_action', prev_action, ...
    'snr_est', snr_est, 'bw_est', bw_est, 'sig_type', double(sig_type), ...
    'channel_features', channel_features, 'channel_map', double(channel_map(:)).', ...
    'spectrum_abs', spec(:).');
end

function c = waveform_case(name, action, cfg)
[y, tx_gain] = generate_jammer_signal(action, cfg);
c = struct( ...
    'kind', 'waveform', 'name', name, 'action', action, ...
    'tx_gain', tx_gain, ...
    'y_re', real(y(:)).', 'y_im', imag(y(:)).', ...
    'peak', max(abs(y)));
end

function s = sanitize(cfg)
% Strip non-JSON-encodable types (function handles, strings stay as char).
s = struct();
f = fieldnames(cfg);
for k = 1:numel(f)
    v = cfg.(f{k});
    if isstring(v)
        s.(f{k}) = char(v);
    elseif ischar(v) || isnumeric(v) || islogical(v)
        s.(f{k}) = v;
    end
end
end
