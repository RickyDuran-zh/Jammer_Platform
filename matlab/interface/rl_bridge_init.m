function client = rl_bridge_init(host, port, timeout_sec)
if nargin < 1, host = "127.0.0.1"; end
if nargin < 2, port = 6001; end
if nargin < 3, timeout_sec = 2; end
client = [];
try
    client = tcpclient(host, port, "Timeout", timeout_sec, "ConnectTimeout", 5);
    disp("已连接 RL 服务。");
catch ME
    disp("[RL] 初始连接失败，将在运行中自动重连: " + ME.message);
end
end
