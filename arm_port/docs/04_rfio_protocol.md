# RFIO 协议规范（v1）

`rfio` 是可选的 C++ 射频守护进程，用于在 **UHD Python 绑定不可用** 时仍能把
X310 收发射频数据接进 Python 后端。它只搬运复数样本，**不做任何决策**：
帧节奏、动作选择、波形生成、互锁判断全部留在 Python 侧。

两个实现遵循本规范：

| 实现 | 路径 | 用途 |
| --- | --- | --- |
| C++ 守护进程 | `arm_port/cpp/` → `jammer-rfio` | 生产兜底，独占 UHD 流 |
| Python 参考服务 | `jammer_arm/rfio_sim.py` → `python -m jammer_arm.rfio_sim` | 开发/回归，合成 IQ，无硬件 |

一致性测试：`arm_port/tests/test_rfio.py`（8 项，客户端 ↔ 参考服务）。

---

## 1. 传输层

- TCP，默认 `127.0.0.1:5601`（`--port` 可改）。
- 仅本机监听；不建议暴露到局域网（无认证，等同一台射频发射机的控制面）。
- 单客户端；新连接到来时旧连接被丢弃（守护进程一次只服务一个后端）。
- 建议 `TCP_NODELAY`（两端默认开启）。

## 2. 分帧

控制消息：**一行一个 JSON 对象**（UTF-8，`\n` 结尾，与平台其余链路一致）。

数据面（`rx` / `tx`）用「JSON 头 + 定长二进制负载」：

```
┌─────────────── JSON 头（\n 结尾）───────────────┐┌──── 负载 N×8 字节 ────┐
{"status":"ok","nsamples":4096}                    <float32 LE interleaved>
```

- 样本格式固定为 **complex64 小端交错**（I 在前），即 UHD 的 `fc32`；
  两端都不得插入填充、换行或长度前缀。
- 负载长度 = `nsamples × 8`；接收方必须按此长度精确读取（不能依赖一次 `recv` 读完）。

## 3. 命令

### 3.1 `open`

```json
{"cmd":"open",
 "device_args":"type=x300,addr=192.168.10.2",
 "master_clock_rate":200000000.0,
 "center_freq":3000000000.0,
 "sample_rate":20000000.0,
 "num_samples":4096,
 "rx_gain":15.0,"tx_gain":0.0,
 "rx_antenna":"TX/RX","tx_antenna":"TX/RX",
 "clock_source":"internal",
 "tdm":true,"rx_mode":"burst","settle_sec":0.5}
```

成功：

```json
{"status":"ok","info":{"rx_rate":20000000.0,"tx_rate":20000000.0,"rx_freq":3000000000.0,
 "rx_gain":15.0,"tx_gain":0.0,"rx_antenna":"TX/RX","tx_antenna":"TX/RX",
 "rx_overruns":0,"tx_underruns":0,"mboard":"X310","uhd_version":"UHD 4.9.0.0"}}
```

`info` 中的值是**设备实际生效值**（UHD 可能对采样率做取整），客户端据此回填遥测。

配置顺序与 `radio/uhd_device.py` 一致：主时钟 → 时钟源 → RX(速率/频率/增益/天线) →
TX(速率/频率/增益/天线) → 建流 → 稳定等待。

### 3.2 `rx`

```json
{"cmd":"rx","nsamples":4096,"timeout":1.0}
```

成功：头 `{"status":"ok","nsamples":4096}` + 4096×8 字节负载。

`rx_mode=burst` 时守护进程下发 `STREAM_MODE_NUM_SAMPS_AND_DONE`，设备发完即停
（这是 12 fps 帧循环不溢出的前提）；`rx_mode=continuous` 时保持连续流。

### 3.3 `tx`

```json
{"cmd":"tx","nsamples":4096,"gain":5.0,"start_of_burst":true,"end_of_burst":true}
```

紧接 4096×8 字节负载。成功：`{"status":"ok","sent":4096}`。

客户端 **必须**先在本侧完成互锁与峰值校验再调用；守护进程不做策略判断，
但它对 `gain` 做 `[tx_gain_min, tx_gain_max]` 裁剪。

### 3.4 维护命令

| 命令 | 回复 | 说明 |
| --- | --- | --- |
| `{"cmd":"rebuild_rx"}` | `{"status":"ok"}` | 释放并重建 RX 流（对应 MATLAB 的 `radio_rx_init`） |
| `{"cmd":"rebuild"}` | `{"status":"ok"}` | 整链重建 |
| `{"cmd":"status"}` | `{"status":"ok","info":{...}}` | 计数器与配置回读 |
| `{"cmd":"close"}` | `{"status":"ok"}` | 释放设备，连接保持 |
| `{"cmd":"shutdown"}` | `{"status":"ok"}` | 关闭守护进程 |

## 4. 错误语义

```json
{"status":"error","error":"recv timeout (no samples from device)"}
```

| 错误串包含 | 客户端行为 |
| --- | --- |
| `timeout` | 计入 RX 超时；达到阈值触发重建阶梯 |
| `overflow` | 计入 `rx_overruns`；触发重建阶梯 |
| `short recv` | 视为失败，**不得**使用部分填充的缓冲 |
| `device not open` | 重新 `open` 或上报致命错误 |
| 其他 | 记录并计入 RX 失败 |

**顺序约束**：`rx`/`tx` 出错后必须先把该帧的负载读干净（或直接关连接）再发下一条命令，
否则字节流错位。参考实现选择「出错即关连接」，由客户端重连，代价是 1 次重连而非错帧。

## 5. 超时建议

| 场景 | 建议值 |
| --- | --- |
| `rx` 的 `timeout` | 1.0 s（与 `radio.recv_timeout_sec` 一致） |
| 客户端整体读超时 | `max(4 × recv_timeout, 2 s)` |
| `tx` 完成 | 与 `rx` 相同；期间设备在发空中帧 |

## 6. 版本与兼容

- 协议无版本号字段，靠字段增删保持向后兼容；**新增字段必须可选**。
- 现有 Python 客户端只依赖 `status`、`nsamples`、`sent`、`info` 四个键。
- 若未来需要多通道/多流，新增 `channel` 字段并保持默认 0，不改变现有语义。

## 7. 安全边界（必读）

1. 守护进程**不判断**互锁；互锁在 Python 侧的 `RadioDevice.transmit()` 与
   `JammerBackend._transmit()` 两处校验。直接对守护进程发 `tx` 会绕过这一层，
   因此**不要**把它当成独立可用的发射工具，也不要在网络上暴露端口。
2. 建议用 systemd 以专用用户运行，并限制 `IPAddress=127.0.0.1`（见 `systemd/`）。
3. 调试用 `--sim` 模式只产生合成样本，不打开设备、不发射。
