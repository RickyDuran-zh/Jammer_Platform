# RK3588（LubanCat 5）部署手册

目标：在一块 16 GB / 128 GB 的 LubanCat 5 上，用纯 Python 后端替代 MATLAB，
复用现有 PyQt UI 与 RL 服务，驱动外置 USRP X310 完成感知—决策—干扰—遥测闭环。

> **射频安全**：本文所有步骤默认 **不发射**。只有 P6 第 7 步、在已授权的屏蔽箱或
> 充分衰减的有线链路中，才允许临时设置 `ZH_RF_TX_ENABLE=LAB_ONLY`。
> `power_db=0` 表示最小增益，**不是关闭发射**。

---

## 1. 目录布局

```
/opt/zh_jammer_v4/                 # 仓库根（git clone 或 scp 同步）
├── python/app/                    # 既有 PyQt UI（不改动）
├── python/rl_service/             # 既有 RL 服务（不改动）
└── arm_port/
    ├── .venv/                     # 后端专用虚拟环境（numpy）
    ├── config/jammer_arm.json     # 站点配置（可覆盖为 /etc/jammer/…）
    ├── python/jammer_arm/         # 移植实现
    ├── scripts/                   # 安装 / 构建 / 运行 / 验收
    ├── systemd/                   # 三个常驻单元
    └── docs/                      # 本文档集

/etc/jammer/jammer_arm.json        # 生产配置（优先级高于仓库内默认）
/etc/jammer/jammer.env             # systemd 环境变量（互锁默认关闭）
/run/jammer/status.json            # 运行时状态快照（tmpfs，1 Hz 刷新）
```

创建运行用户：

```bash
sudo useradd -r -m -G dialout,plugdev -s /bin/bash jammer
sudo mkdir -p /etc/jammer /opt
sudo chown -R "$USER" /opt/zh_jammer_v4
```

---

## 2. 网络：先让板子看得见 X310

X310 默认管理地址 `192.168.10.2`。**推荐直连**（板卡网口 ↔ X310），避免交换机吞掉巨帧。

```bash
# 1) 确认网口名（LubanCat 5 可能是 eth0/eth1/enP*）
ip -br link

# 2) 配置静态地址 + 巨帧（以 eth0 为例，Netplan 系统）
sudo tee /etc/netplan/60-jammer.yaml >/dev/null <<'EOF'
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: false
      addresses: [192.168.10.1/24]
      mtu: 9000
      optional: true
EOF
sudo netplan apply

# 3) 关闭中断合并与分段卸载（降低突发延迟）
sudo ethtool -K eth0 gro off lro off gso off tso off || true
sudo ethtool -G eth0 rx 4096 tx 4096 || true

# 4) 验证
ping -c 3 192.168.10.2
ip -br addr show eth0
```

同时确认 X310 侧 MTU 也是 9000（UHD 可用 `--args addr=192.168.10.2,mtu=9000`，
或在主机端设置；`uhd_usrp_probe` 会打印实际使用的 MTU）。

> 若板载网口只有 1 GbE 且实测 `recv overflow`：先确认 MTU，再考虑降采样率
> （见 `01_feasibility.md` §3.2）。含 2.5G/10G 口或走 SFP+ 的板卡不受此限。

---

## 3. 系统依赖与后端环境

```bash
cd /opt/zh_jammer_v4
bash arm_port/scripts/install_deps_rk3588.sh
```

脚本会完成：apt 依赖、`.venv`（仅 numpy）、`/etc/sysctl.d/90-jammer.conf`
（socket 缓冲）、`setcap cap_sys_nice+ep`（降低调度抖动）。

**CPU 调频**（对帧时延影响最大的一项）：

```bash
sudo cpupower frequency-set -g performance
# 或在部分内核上：
echo performance | sudo tee /sys/devices/system/cpu/cpufreq/policy*/scaling_governor
```

---

## 4. UHD 与 X310

### 4.1 构建 UHD（含 Python 绑定）

```bash
bash arm_port/scripts/build_uhd.sh            # 默认 v4.9.0.0
```

预计 25–45 min。结束后会自检 `import uhd`。

```bash
uhd_find_devices
uhd_usrp_probe --args addr=192.168.10.2
```

`uhd_usrp_probe` 必须**没有** FPGA 兼容性告警。若出现
`Expected FPGA compatibility number ...`，说明镜像与 UHD 版本不匹配，
需要用对应版本的 `uhd_image_loader` 更新 X310 的 FPGA 镜像。

### 4.2 绑定不可用时的兜底

```bash
bash arm_port/scripts/build_cpp.sh
sudo cmake --install arm_port/cpp/build
sudo jammer-rfio --port 5601 &          # 加 --sim 可先不接硬件
python -m jammer_arm run --radio rfio --rfio-port 5601 --frames 5
```

后端与守护进程之间只走本地 TCP，上层逻辑完全不变。

---

## 5. 配置

生产配置放 `/etc/jammer/jammer_arm.json`（结构同 `arm_port/config/jammer_arm.json`，
只需写与默认值不同的项）。**注意**：

- `safety` 段不可配置；`tx_enabled` 之类的键会被直接拒绝加载；
- 任何 `_` 开头的键视为注释；
- 命令行 `--set radio.num_samples=8192` 优先级最高。

```bash
sudo cp arm_port/config/jammer_arm.json /etc/jammer/jammer_arm.json
sudo -u jammer python -m jammer_arm show-config --config /etc/jammer/jammer_arm.json | head -30
```

---

## 6. 首次运行（不接硬件）

```bash
cd /opt/zh_jammer_v4
bash arm_port/scripts/run_stack.sh mock
```

预期：

```
[UI] 遥测帧#31 模式=RL自动 策略=False
[SEQ=1] ch=3 power=0.0dB bw=6MHz mode=0 policy=ppo ctrl=rl rx=ok tx=mock_running
...
  frames : 84 in 10.1s (8.33 fps)
  dsp time: last 0.64 ms, max 1.39 ms
```

`bash arm_port/scripts/run_stack.sh mock --duration 10 --no-ui --no-rl` 可以只跑后端。

---

## 7. 接入 X310（仍不发射）

```bash
# 终端 A：UI
cd /opt/zh_jammer_v4/python/app && ../..//arm_port/.venv/bin/python main_window.py
# 终端 B：RL
cd /opt/zh_jammer_v4/python/rl_service && ./.venv/bin/python rl_server.py --algo ppo
# 终端 C：后端
cd /opt/zh_jammer_v4 && PYTHONPATH=arm_port/python \
  arm_port/.venv/bin/python -m jammer_arm run --radio uhd --status /run/jammer/status.json
```

或一条命令：`bash arm_port/scripts/run_stack.sh live`。

验收看三处：
1. 后端 summary 中 `rx failures 0`、`overruns 0`；
2. UI 频谱曲线随环境变化，10 信道热力图滚动；
3. `status.json` 中 `tx_state = "disabled"`。

---

## 8. 解除互锁（仅限授权环境）

```bash
# 屏蔽箱/衰减链路确认到位后，仅对本次进程生效：
ZH_RF_TX_ENABLE=LAB_ONLY PYTHONPATH=arm_port/python \
  arm_port/.venv/bin/python -m jammer_arm run --radio uhd
```

- 后端启动横幅会打印 `tx_armed=YES`；
- `status.json` 中 `tx_state = "running"`；
- 用频谱仪确认中心频率 3.0 GHz、占用带宽与 `hw_actuals.bw_mhz` 一致。

**结束实验立刻关闭该环境变量**。不要把它写进 shell profile、systemd 环境文件或脚本默认值。

---

## 9. systemd 常驻

```bash
sudo cp arm_port/systemd/jammer.env.example /etc/jammer/jammer.env
sudo cp arm_port/systemd/jammer-{backend,rl,ui}.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jammer-backend jammer-rl jammer-ui

systemctl status jammer-backend --no-pager
journalctl -u jammer-backend -f
```

要点：

- 三个单元互不 `Requires`：后端在 RL 缺失时回退到上一动作，在 UI 缺失时丢弃遥测继续跑；
- UI 单元需要图形会话（`DISPLAY=:0`）；无桌面环境的板子可以只跑 backend + rl，
  用 `status.json` 与日志观测；
- 互锁在 `jammer.env` 里**默认注释掉**，解除时需要显式编辑并 `systemctl restart`。

---

## 10. 验收

```bash
bash arm_port/scripts/acceptance_test.sh              # 含 live
bash arm_port/scripts/acceptance_test.sh --skip-live  # 无硬件时
```

按顺序执行 5 步：doctor → selftest → 全部单测 → bench → live(50 帧)。
任一步失败返回非零。判据见 `02_plan.md` 的 G4。

---

## 11. 故障排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `cannot open USRP with args ... no devices found` | IP/网口/MTU 不对；X310 未上电 | `ping`、`uhd_find_devices`、检查网线直连 |
| `Expected FPGA compatibility number` | 镜像与 UHD 版本不匹配 | 用对应版本 `uhd_image_loader` 更新镜像 |
| `recv overflow` 频发 | 1 GbE 饱和 / MTU 1500 / 缓冲小 | MTU 9000 直连 → 调 `rmem_max` → 降采样率 |
| `recv timeout` 每帧一次 | 未下发定长突发（旧代码）/ 流未启动 | 确认 `radio.rx_mode=burst`；看 `doctor` |
| `import uhd` 失败 | Boost.Python 未编/路径未注入 | 重跑 `build_uhd.sh`，或改用 `--radio rfio` |
| UI 无数据但后端在跑 | 5555 未监听 / 端口占用 | `ss -lntp \| grep 5555`；先起 UI 再起后端 |
| 控制权切不过去 | 命令桥 5557 断开 | 看后端日志 `[CMD]` 行与 `cmd_bridge.is_connected` |
| 帧时延高且抖动大 | governor 非 performance / 降频 | `cpupower frequency-set -g performance`，测温 |
| 长时间运行变慢 | swap / 内存增长 | `free -m`，`vm.swappiness=10`，`--quiet` 减少日志 |
| `tx_state=error` | TX 增益/天线/镜像问题 | 看后端 `[TX] warning`，`uhd_usrp_probe` 查 TX 链 |

日志位置：`logs/ui.log`、`logs/rl.log`、`logs/backend.log`（`run_stack.sh` 生成）。

---

## 12. 回滚

本移植**不修改**原 MATLAB 与既有 Python 代码，因此回滚 = 停止新后端、按原方式启动：

```bash
sudo systemctl disable --now jammer-backend
bash scripts/run_platform.sh mock      # 原 Windows/Git Bash 一键脚本
```

仓库层面：`arm_port/` 是独立目录，删除或 `git revert` 该目录的提交即可，
不影响 `matlab/`、`python/` 与既有 `scripts/`。
