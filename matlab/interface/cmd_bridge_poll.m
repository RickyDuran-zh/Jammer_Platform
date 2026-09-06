function [commands, client] = cmd_bridge_poll(client, cfg)
% cmd_bridge_poll  轮询Python命令桥接，获取待执行的控制命令
%   [commands, client] = cmd_bridge_poll(client, cfg)
%   commands 为 cell 数组的命令结构体；无待处理命令时返回空 cell。
%   client 为可能重连后的 tcpclient（断线时自动重连，失败则保留原对象）。

    commands = {};
    try
        % 发送轮询请求
        poll_msg = jsonencode(struct("type", "cmd_poll"));
        writeline(client, poll_msg);

        % 读取本次响应。readline 会按 tcpclient Timeout 等待，避免刚写完
        % poll 后响应尚未到达时被 NumBytesAvailable=0 误判为空。
        rsp_str = readline(client);
        rsp = jsondecode(rsp_str);
        if strcmp(rsp.type, "cmd_rsp") && ~isempty(rsp.commands)
            cmds = rsp.commands;
            if isstruct(cmds)
                cmds = num2cell(cmds);
            end
            commands = cmds;
        end
    catch ME
        % 连接断开会抛出异常 - 静默处理并尝试重连
        if ~strcmp(ME.identifier, "transport:client:connectFailed")
            disp("[CMD] 轮询警告: " + ME.message);
        end
        client = tcp_reconnect(client, cfg.cmd_host, cfg.cmd_port, cfg.cmd_timeout);
    end
end
