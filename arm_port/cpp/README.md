# jammer-rfio — 可选 C++ 射频守护进程

用途：当板端的 **UHD Python 绑定无法构建/导入** 时，由本守护进程独占 UHD 流，
Python 后端通过本地 TCP 收发复数样本。上层逻辑（帧循环、DSP、动作、互锁、遥测）
完全留在 Python，不重复实现。

协议规范：[`../docs/04_rfio_protocol.md`](../docs/04_rfio_protocol.md)
Python 侧客户端：`../python/jammer_arm/radio/rfio.py`
协议参考实现（无硬件、合成 IQ）：`python -m jammer_arm.rfio_sim`

---

## 构建

```bash
bash ../scripts/build_uhd.sh      # 先有 libuhd 与头文件
bash ../scripts/build_cpp.sh      # 产出 cpp/build/jammer-rfio
sudo cmake --install cpp/build    # 安装到 /usr/local/bin
```

依赖：`cmake >= 3.16`、C++17 编译器、`libuhd-dev`（或源码安装的 UHD）。
**不需要** Boost.Python、不需要 Python 头文件——这正是它作为兜底的价值。

## 运行

```bash
jammer-rfio --port 5601            # 真设备
jammer-rfio --port 5601 --sim      # 合成 IQ，不打开设备、不发射
jammer-rfio --once                 # 服务一个连接后退出（调试）
```

```bash
# 后端切到 rfio
PYTHONPATH=../python python -m jammer_arm run --radio rfio --rfio-port 5601 --frames 5
```

## 实现要点

| 点 | 做法 | 原因 |
| --- | --- | --- |
| 收流模式 | `STREAM_MODE_NUM_SAMPS_AND_DONE`，每帧 N=4096 | 12 fps 帧循环下连续流必然溢出（见可行性分析 §3.3） |
| TDM | 仅 `rx_mode=continuous` 时在 TX 前后 stop/start 流 | 突发模式下发完 RX 链路已空闲 |
| 错误处理 | metadata 错误码映射为文本，随 `status:"error"` 返回 | Python 侧按文本区分 timeout/overflow 并触发恢复阶梯 |
| 增益 | 裁剪到 `[tx_gain_min, tx_gain_max]` 后下发 | 与 Python 侧双保险 |
| 短包 | `filled < n` 直接报错，绝不返回半填充缓冲 | 防止把陈旧尾部样本送进 DSP |
| JSON | 内置极简 flat-object 解析器（约 80 行） | 板端不引入第三方 JSON 库 |
| 线程 | 单线程 accept + serve，一次只服务一个后端 | 帧循环本来就是串行的，减少不确定性 |

## 安全

守护进程**不判断互锁**，只在 Python 侧校验。因此：

- 不要把它暴露到局域网（默认只监听 `127.0.0.1`）；
- 不要直接用它发 `tx` 命令做实验——那会绕过 `power_db`/峰值/互锁三重校验；
- `--sim` 模式可安全用于联调。

## 测试

```bash
bash ../scripts/test_cpp.sh          # 编译并运行线格式测试（无需 UHD / 无需 root）
# 40 checks, 0 failure(s)
```

`cpp/tests/test_json.cpp` 直接包含 `cpp/include/rfio_json.hpp` —— 守护进程与测试
共用同一份解析器，不存在“测试测的是另一份实现”的问题。

## 验证状态

| 项 | 结果 |
| --- | --- |
| `rfio_radio.cpp` 编译（真实 UHD 4.9.0.0 头文件） | ✅ 零错误、零告警 |
| `rfio_server.cpp` 编译 | ✅ 零错误、零告警 |
| 线格式测试（解析/转义/畸形输入） | ✅ 40 项断言通过 |
| 协议一致性（Python 参考服务，`tests/test_rfio.py`） | ✅ 8 项通过 |
| 板端链接 + `--sim` 运行 | ⏳ 待 `build_cpp.sh` 在 RK3588 上执行 |

编译验证期间修掉的真实缺陷：`UHD_VERSION_STRING` 宏不存在，已改为
`uhd::get_version_string()`（`uhd/version.hpp`）。
