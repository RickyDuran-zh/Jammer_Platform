function radio_tx = radio_tx_init(cfg)
% Build the transmitter using the hardware settings validated on the X310.

radio_tx = comm.SDRuTransmitter( ...
    'Platform', 'X310', ...
    'IPAddress', cfg.ip_addr);

radio_tx.CenterFrequency = cfg.center_freq;
radio_tx.Gain = cfg.tx_gain_base;
radio_tx.MasterClockRate = cfg.MasterClockRate;
radio_tx.InterpolationFactor = cfg.InterpolationFactor;
radio_tx.ClockSource = 'Internal';
radio_tx.ChannelMapping = 1;
if isprop(radio_tx, 'Antenna')
    radio_tx.Antenna = cfg.tx_antenna;
end
end
