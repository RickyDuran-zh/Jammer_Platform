function [radio_tx, radio_rx] = radio_init(cfg)
% Initialize the single-X310 time-division radio chain.

radio_tx = radio_tx_init(cfg);
radio_rx = radio_rx_init(cfg);

disp("USRP initialized (single-radio time-division RX/TX, RF A TX/RX).");
end
