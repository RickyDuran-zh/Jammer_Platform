function tx_state = tx_send_frame(radio_tx, frame, tx_gain, cfg)
% Send one bounded laboratory frame after checking the explicit RF interlock.

if ~cfg.tx_enabled
    tx_state = "disabled";
    return;
end
if ~strcmp(getenv(cfg.tx_interlock_env), cfg.tx_interlock_value)
    error('RF TX interlock is not armed. Set %s=%s only in an authorized shielded setup.', ...
        cfg.tx_interlock_env, cfg.tx_interlock_value);
end
if isempty(frame) || any(~isfinite(frame))
    error('TX frame must be non-empty and finite.');
end
if max(abs(frame)) > cfg.tx_baseband_peak + 1e-9
    error('TX frame exceeds configured baseband peak %.3f.', cfg.tx_baseband_peak);
end

radio_tx.Gain = min(max(tx_gain, cfg.tx_gain_min), cfg.tx_gain_max);
for k = 1:cfg.tx_repeat
    radio_tx(frame);
end
tx_state = "running";
end
