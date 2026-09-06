function client = tcp_reconnect(client, host, port, timeout_sec)
% tcp_reconnect  尝试(重新)建立到 host:port 的 tcpclient。
%   若连接失败，保留原 client 以便后续继续重试；若成功则替换原 client。
%   用于在 UI / RL / 命令桥断线或启动时未就绪的情况下自动重连。

if nargin < 4, timeout_sec = 1; end

new_client = [];
try
    new_client = tcpclient(host, port, "Timeout", timeout_sec, "ConnectTimeout", 5);
catch
end

if isempty(new_client)
    return;   % 重连失败，保留旧 client（可能为 []）
end

if ~isempty(client)
    try
        delete(client);
    catch
    end
end
client = new_client;
disp("[TCP] 已连接 " + string(host) + ":" + string(port));
end
