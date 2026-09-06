function client = ui_bridge_init(host, port, timeout_sec)
if nargin < 1, host = "127.0.0.1"; end
if nargin < 2, port = 5555; end
if nargin < 3, timeout_sec = 1; end
client = [];
try
    client = tcpclient(host, port, "Timeout", timeout_sec, "ConnectTimeout", 5);
    disp("已连接 UI 遥测服务。");
catch ME
    disp("[UI] 初始连接失败，将在运行中自动重连: " + ME.message);
end
end
