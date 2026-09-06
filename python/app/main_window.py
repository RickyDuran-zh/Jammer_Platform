import os
import sys
from collections import deque
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from telemetry_server import TelemetryServer
from agent.agent_brain import AgentBrain
from agent.agent_panel import AgentPanel
from agent.strategy_engine import StrategyEngine
from command_bridge import CommandBridge


def make_kpi_card(title: str, value: str = "--"):
    frame = QtWidgets.QFrame()
    frame.setObjectName("KpiCard")
    layout = QtWidgets.QVBoxLayout(frame)
    layout.setContentsMargins(14, 10, 14, 10)
    layout.setSpacing(4)
    title_label = QtWidgets.QLabel(title)
    title_label.setObjectName("CardTitle")
    value_label = QtWidgets.QLabel(value)
    value_label.setObjectName("CardValue")
    value_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
    layout.addWidget(title_label)
    layout.addWidget(value_label)
    return frame, value_label


class LogWidget(QtWidgets.QTableWidget):
    def __init__(self):
        super().__init__(0, 8)
        self.setHorizontalHeaderLabels(
            ["时间", "序号", "目标信道", "功率(dB)", "带宽(MHz)", "模式", "策略", "状态"]
        )
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)

    def append_log(self, row_data):
        row = self.rowCount()
        self.insertRow(row)
        for col, value in enumerate(row_data):
            self.setItem(row, col, QtWidgets.QTableWidgetItem(str(value)))
        self.scrollToBottom()
        while self.rowCount() > 200:
            self.removeRow(0)


class MainWindow(QtWidgets.QMainWindow):
    packet_received = QtCore.pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("频谱智能干扰控制前端")
        self.resize(1920, 1040)

        self.max_history = 120
        self.num_channels = 10
        self.channel_history = deque(
            [np.zeros(self.num_channels, dtype=np.int32) for _ in range(self.max_history)],
            maxlen=self.max_history,
        )
        self.snr_history = deque([0.0] * self.max_history, maxlen=self.max_history)
        self.bw_history = deque([0.0] * self.max_history, maxlen=self.max_history)
        self.power_history = deque([0.0] * self.max_history, maxlen=self.max_history)

        # ── Agent (API key comes from the environment; init in main thread) ─
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        self._agent_init_error: str | None = None
        try:
            self.agent_brain = AgentBrain(api_key=api_key)
        except Exception as e:
            import traceback
            self.agent_brain = None
            self._agent_init_error = f"Agent初始化失败:\n{traceback.format_exc()}"

        # ── Strategy engine ────────────────────────────────────
        self.strategy_engine = StrategyEngine()
        self._agent_control_mode = "rl"  # "rl" or "agent"
        self._rl_connected = False  # inferred from telemetry rl_meta
        self._frame_count = 0  # telemetry frame counter for diagnostics

        # ── Command bridge ─────────────────────────────────────
        self.cmd_bridge = CommandBridge("127.0.0.1", 5557)
        self.cmd_bridge.start()

        # ── Wire agent callbacks ───────────────────────────────
        # CRITICAL: Callbacks run in AgentWorker's bg thread (via LLM tool
        # execution). They use QMetaObject.invokeMethod with QueuedConnection
        # to safely defer all Qt widget ops to the main thread.
        if self.agent_brain:
            self.agent_brain.set_on_set_params(self._on_agent_set_params)
            self.agent_brain.set_on_control_mode(self._on_agent_control_mode)
            self.agent_brain.set_on_strategy(self._on_agent_strategy)
            self.agent_brain.set_on_cancel_strategy(self._on_agent_cancel_strategy)
            self.agent_brain.set_bridge_status_provider(lambda: self.cmd_bridge.is_connected)

        # ── Telemetry server ───────────────────────────────────
        self.packet_received.connect(self.handle_packet)
        self.server = TelemetryServer("127.0.0.1", 5555, self.packet_received.emit)
        self.server.start()

        self._build_ui()
        self._apply_style()
        self._wire_agent()
        self._update_mode_badge()  # apply initial badge color

    # ═══════════════════════════════════════════════════════════════
    # Layout:
    #  ┌──────────────────────────────────────┬─────────────────┐
    #  │  Header                              │                 │
    #  │  KPI Cards (2x4)                     │  AI 智能助手     │
    #  ├────────────────┬─────────────────────┤  (右侧全高)      │
    #  │  实时频谱      │  10信道状态图        │                 │
    #  │  (50%)         │  (50%)              │                 │
    #  ├────────────────┴─────────────────────┤                 │
    #  │  Tabs: 波形|趋势|日志|信道特征        │                 │
    #  └──────────────────────────────────────┴─────────────────┘
    # ═══════════════════════════════════════════════════════════════

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        # ── Master horizontal split: left (main) | right (AI) ──
        master_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        master_split.setHandleWidth(2)

        # ═══════════════════════════════════════════════════════
        # LEFT SIDE — all monitoring panels
        # ═══════════════════════════════════════════════════════
        left_widget = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(8)

        # ── Header ─────────────────────────────────────────
        header = QtWidgets.QHBoxLayout()
        title_col = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel("频谱智能干扰控制前端")
        title.setObjectName("MainTitle")
        subtitle = QtWidgets.QLabel("Python前端 · 10信道监视 · 信道/功率/带宽/模式回显 · AI智能分析")
        subtitle.setObjectName("SubTitle")
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch(1)

        self.conn_badge = QtWidgets.QLabel("监听中  127.0.0.1:5555")
        self.conn_badge.setObjectName("ConnBadge")
        header.addWidget(self.conn_badge)

        # ── Connection status dots ────────────────────────────
        self.dot_telem = QtWidgets.QLabel("●")
        self.dot_telem.setToolTip("遥测服务 (MATLAB→UI)")
        self.dot_telem.setStyleSheet("color: #555; font-size: 10pt;")
        self.dot_rl = QtWidgets.QLabel("●")
        self.dot_rl.setToolTip("RL 服务")
        self.dot_rl.setStyleSheet("color: #555; font-size: 10pt;")
        self.dot_cmd = QtWidgets.QLabel("●")
        self.dot_cmd.setToolTip("命令桥接 (UI→MATLAB)")
        self.dot_cmd.setStyleSheet("color: #555; font-size: 10pt;")
        header.addWidget(self.dot_telem)
        header.addWidget(self.dot_rl)
        header.addWidget(self.dot_cmd)

        # ── Control mode badge ────────────────────────────────
        self.mode_badge = QtWidgets.QLabel("RL 自动")
        self.mode_badge.setObjectName("ModeBadge")
        self.mode_badge.setAlignment(QtCore.Qt.AlignCenter)
        self.mode_badge.setMinimumWidth(110)
        self.mode_badge.adjustSize()
        header.addWidget(self.mode_badge)
        left_layout.addLayout(header)

        # ── Strategy status bar (hidden when no strategy active) ─
        self.strategy_bar = QtWidgets.QWidget()
        self.strategy_bar.setFixedHeight(36)
        strategy_bar_layout = QtWidgets.QHBoxLayout(self.strategy_bar)
        strategy_bar_layout.setContentsMargins(0, 0, 0, 0)
        strategy_bar_layout.setSpacing(8)
        self.strategy_label = QtWidgets.QLabel("")
        self.strategy_label.setObjectName("StrategyLabel")
        self.strategy_stop_btn = QtWidgets.QPushButton("停止策略")
        self.strategy_stop_btn.setObjectName("AgentClearBtn")
        self.strategy_stop_btn.setFixedWidth(70)
        self.strategy_stop_btn.clicked.connect(self._on_stop_strategy)
        strategy_bar_layout.addWidget(self.strategy_label)
        strategy_bar_layout.addWidget(self.strategy_stop_btn)
        strategy_bar_layout.addStretch()
        self.strategy_bar.setVisible(False)
        left_layout.addWidget(self.strategy_bar)

        # ── KPI Cards ──────────────────────────────────────
        cards = QtWidgets.QGridLayout()
        cards.setHorizontalSpacing(10)
        cards.setVerticalSpacing(8)
        self.card_snr, self.lbl_snr = make_kpi_card("全局信噪比", "-- dB")
        self.card_bw, self.lbl_bw = make_kpi_card("检测带宽", "-- MHz")
        self.card_ch, self.lbl_ch = make_kpi_card("目标信道", "--")
        self.card_pwr, self.lbl_pwr = make_kpi_card("功率控制", "-- dB")
        self.card_jbw, self.lbl_jbw = make_kpi_card("干扰带宽", "-- MHz")
        self.card_mode, self.lbl_mode = make_kpi_card("波形模式", "--")
        self.card_pol, self.lbl_pol = make_kpi_card("决策算法", "--")
        self.card_time, self.lbl_time = make_kpi_card("最近更新", "--")
        for i, widget in enumerate([
            self.card_snr, self.card_bw, self.card_ch, self.card_pwr,
            self.card_jbw, self.card_mode, self.card_pol, self.card_time,
        ]):
            cards.addWidget(widget, i // 4, i % 4)
        left_layout.addLayout(cards)

        # ── Middle: Spectrum | Channel Map (50/50 split) ────
        middle_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        # Spectrum
        spectrum_panel = self._panel("实时频谱", "3GHz 频段实时频谱与干扰覆盖")
        spec_layout = spectrum_panel.layout()
        self.spectrum_plot = pg.PlotWidget()
        self.spectrum_plot.setMinimumHeight(200)
        self._style_plot(self.spectrum_plot, "频率 (GHz)", "幅度")
        self.spectrum_curve = self.spectrum_plot.plot(pen=pg.mkPen("#66d9ef", width=2))
        spec_layout.addWidget(self.spectrum_plot)
        middle_split.addWidget(spectrum_panel)

        # Channel Map (X=channels, Y=time vertical flow)
        map_panel = self._panel("10信道状态图", "横轴=信道  纵轴=时间(↓新)  0空闲 1用户 2干扰 3重叠")
        map_layout = map_panel.layout()
        self.map_plot = pg.PlotWidget()
        self.map_plot.setMinimumHeight(200)
        self._style_plot(self.map_plot, "信道编号", "时间 (新→旧 ↓)")
        self.map_img = pg.ImageItem()
        self.map_plot.addItem(self.map_img)

        self.map_plot.setXRange(0, self.num_channels, padding=0)
        self.map_plot.setYRange(0, self.max_history, padding=0)
        self.map_plot.disableAutoRange()

        bottom_axis = self.map_plot.getAxis("bottom")
        bottom_ticks = [(i + 0.5, str(i + 1)) for i in range(self.num_channels)]
        bottom_axis.setTicks([bottom_ticks])

        left_axis = self.map_plot.getAxis("left")
        left_ticks = [(i, str(i)) for i in range(0, self.max_history + 1, 20)]
        left_axis.setTicks([left_ticks])

        cmap = pg.ColorMap(
            pos=np.array([0.0, 1 / 3, 2 / 3, 1.0]),
            color=np.array([
                [225, 228, 234, 255],
                [78, 235, 124, 255],
                [255, 91, 91, 255],
                [74, 74, 74, 255],
            ], dtype=np.ubyte),
        )
        self.map_img.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        self.map_img.setLevels([0, 3])
        map_layout.addWidget(self.map_plot)
        middle_split.addWidget(map_panel)

        middle_split.setSizes([720, 680])
        left_layout.addWidget(middle_split, stretch=4)

        # ── Bottom: Tabs ────────────────────────────────────
        tab_panel = self._panel("", "")
        tab_layout = tab_panel.layout()
        self.tabs = QtWidgets.QTabWidget()

        # Tab 1: Waveform
        waveform_tab = QtWidgets.QWidget()
        wf_layout = QtWidgets.QVBoxLayout(waveform_tab)
        self.wave_plot = pg.PlotWidget()
        self.wave_plot.setMinimumHeight(150)
        self._style_plot(self.wave_plot, "采样点", "幅度")
        self.wave_curve = self.wave_plot.plot(pen=pg.mkPen("#c792ea", width=2))
        wf_layout.addWidget(self.wave_plot)
        self.tabs.addTab(waveform_tab, "波形图")

        # Tab 2: Trend (moved from separate panel to tab)
        trend_tab = QtWidgets.QWidget()
        trend_tab_layout = QtWidgets.QVBoxLayout(trend_tab)
        self.trend_plot = pg.PlotWidget()
        self.trend_plot.setMinimumHeight(150)
        self._style_plot(self.trend_plot, "帧索引", "数值")
        self.trend_plot.addLegend(offset=(10, 10))
        self.snr_curve = self.trend_plot.plot(name="信噪比(dB)", pen=pg.mkPen("#4dff88", width=2))
        self.bw_curve = self.trend_plot.plot(name="带宽(MHz)", pen=pg.mkPen("#ffb84d", width=2))
        self.power_curve = self.trend_plot.plot(name="功率(dB)", pen=pg.mkPen("#ff5c73", width=2))
        trend_tab_layout.addWidget(self.trend_plot)
        self.tabs.addTab(trend_tab, "运行趋势")

        # Tab 3: Event Log
        log_tab = QtWidgets.QWidget()
        log_layout = QtWidgets.QVBoxLayout(log_tab)
        self.log_widget = LogWidget()
        log_layout.addWidget(self.log_widget)
        self.tabs.addTab(log_tab, "事件日志")

        # Tab 4: Channel Features
        channel_tab = QtWidgets.QWidget()
        channel_layout = QtWidgets.QVBoxLayout(channel_tab)
        self.channel_table = QtWidgets.QTableWidget(self.num_channels, 5)
        self.channel_table.setHorizontalHeaderLabels(["信道", "占用", "干扰", "效用", "SNR类指标"])
        self.channel_table.horizontalHeader().setStretchLastSection(True)
        self.channel_table.verticalHeader().setVisible(False)
        self.channel_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        channel_layout.addWidget(self.channel_table)
        self.tabs.addTab(channel_tab, "信道特征")

        self.tabs.setCurrentIndex(0)
        tab_layout.addWidget(self.tabs)
        left_layout.addWidget(tab_panel, stretch=4)

        master_split.addWidget(left_widget)

        # ═══════════════════════════════════════════════════════
        # RIGHT SIDE — AI Assistant (full height)
        # ═══════════════════════════════════════════════════════
        self.agent_panel = AgentPanel()
        master_split.addWidget(self.agent_panel)

        # Left ~75%, Right ~25%
        master_split.setSizes([1400, 460])

        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.addWidget(master_split)

    @staticmethod
    def _panel(title: str, desc: str):
        box = QtWidgets.QGroupBox(title)
        box.setObjectName("PanelBox")
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        if desc:
            desc_label = QtWidgets.QLabel(desc)
            desc_label.setObjectName("PanelDesc")
            layout.addWidget(desc_label)
        return box

    def _style_plot(self, plot, xlabel, ylabel):
        plot.setBackground("#0d1117")
        plot.showGrid(x=True, y=True, alpha=0.18)
        plot.setLabel("bottom", xlabel)
        plot.setLabel("left", ylabel)
        axis_pen = pg.mkPen("#9aa4b2")
        for axis_name in ["bottom", "left"]:
            plot.getAxis(axis_name).setPen(axis_pen)
            plot.getAxis(axis_name).setTextPen(axis_pen)

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget {
                background: #0f1722; color: #e8eef5;
                font-family: "Microsoft YaHei UI", "SimHei", "Segoe UI", sans-serif;
                font-size: 11pt;
            }
            #MainTitle { font-size: 20pt; font-weight: 700; color: #ffffff; }
            #SubTitle { color: #98a6b8; font-size: 9pt; }
            #ConnBadge {
                background: #17212b; border: 1px solid #243244; border-radius: 12px;
                padding: 6px 12px; color: #6ee7b7; font-weight: 600; font-size: 10pt;
            }
            #ModeBadge {
                border-radius: 10px; padding: 4px 10px; font-weight: 700; font-size: 9pt;
            }
            #StrategyLabel {
                color: #ffb84d; font-weight: 600; font-size: 10pt;
                background: #1a1f2e; border-radius: 6px; padding: 4px 10px;
            }
            #KpiCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #16202b, stop:1 #111923);
                border: 1px solid #243244; border-radius: 12px;
            }
            #CardTitle { color: #8ea0b5; font-size: 9pt; }
            #CardValue { color: #ffffff; font-size: 16pt; font-weight: 700; }
            #PanelBox {
                font-size: 11pt; font-weight: 600; border: 1px solid #223247;
                border-radius: 12px; margin-top: 10px; padding-top: 8px;
                background: #121a24;
            }
            #PanelBox QGroupBox::title {
                subcontrol-origin: margin; left: 14px; padding: 0 8px;
                color: #dbe6f1;
            }
            #PanelDesc { color: #7f8ea3; font-size: 8pt; margin-bottom: 2px; }
            QTabWidget::pane { border: 1px solid #243244; background: #121a24; }
            QTabBar::tab {
                background: #16202b; color: #c6d1dd; border: 1px solid #243244;
                padding: 7px 12px; min-width: 70px; font-size: 10pt;
            }
            QTabBar::tab:selected { background: #223247; color: #ffffff; }
            QHeaderView::section {
                background: #16202b; color: #dbe6f1; padding: 5px; border: none;
                border-bottom: 1px solid #243244; font-size: 10pt;
            }
            QTableWidget {
                background: #0f1722; alternate-background-color: #131d29;
                border: 1px solid #243244; color: #e8eef5; gridline-color: #223247;
                font-size: 10pt;
            }
            QSplitter::handle {
                background: #1e2d3d; margin: 0 2px;
            }
            #AgentTitle { font-size: 14pt; font-weight: 700; color: #c792ea; }
            #AgentStatus { color: #6ee7b7; font-size: 9pt; }
            #AgentClearBtn {
                background: #16202b; border: 1px solid #243244; border-radius: 8px;
                color: #c6d1dd; padding: 4px 10px; font-size: 9pt;
            }
            #AgentClearBtn:hover { background: #223247; }
            #AgentMessageArea {
                background: #0d1117; border: 1px solid #223247; border-radius: 8px;
                color: #e8eef5; font-size: 10pt;
            }
            #QuickCmdBtn {
                background: #1a2740; border: 1px solid #2d4a6f; border-radius: 8px;
                color: #8cb4ff; font-size: 8pt; padding: 3px 8px;
            }
            #QuickCmdBtn:hover { background: #223a5e; }
            #AgentInput {
                background: #0d1117; border: 1px solid #243244; border-radius: 8px;
                color: #e8eef5; padding: 7px 10px; font-size: 10pt;
            }
            #AgentSendBtn {
                background: #1a2740; border: 1px solid #2d4a6f; border-radius: 8px;
                color: #8cb4ff; font-weight: 600; padding: 7px; font-size: 10pt;
            }
            #AgentSendBtn:hover { background: #223a5e; }
        """)

    # ═══════════════════════════════════════════════════════════════
    # Agent wiring
    # ═══════════════════════════════════════════════════════════════

    def _wire_agent(self):
        self.agent_panel.send_message.connect(self._on_agent_message)
        self.agent_panel.clear_requested.connect(self._on_clear_agent)
        self.agent_panel.stop_requested.connect(self._on_stop_agent)
        if self._agent_init_error:
            self.agent_panel.show_error(f"Agent初始化失败:\n{self._agent_init_error}")
            self.agent_panel.set_connected(False)
        elif self.agent_brain:
            self.agent_panel.set_connected(self.agent_brain.enabled)
        else:
            self.agent_panel.set_connected(False)

    def _on_clear_agent(self):
        """Handle new-conversation request from agent panel."""
        if self.agent_brain:
            pass

    def _safe_worker_running(self) -> bool:
        """Check if agent worker is running without crashing on deleted C++ object."""
        try:
            return (hasattr(self, '_agent_worker')
                    and self._agent_worker is not None
                    and self._agent_worker.isRunning())
        except RuntimeError:
            self._agent_worker = None
            return False

    def _on_stop_agent(self):
        """Handle stop-request from agent panel (cancel in-progress LLM call)."""
        if self._safe_worker_running():
            self._agent_worker.quit()
            self._agent_worker.wait(500)

    def _on_agent_message(self, text: str):
        if self._agent_init_error:
            self.agent_panel.show_error(f"Agent初始化失败:\n{self._agent_init_error}")
            return
        if not self.agent_brain or not self.agent_brain.enabled:
            self.agent_panel.show_error("Agent未就绪，请检查API密钥。")
            return
        # Clean up previous worker if still running
        if self._safe_worker_running():
            self._agent_worker.quit()
            self._agent_worker.wait(500)
        self._agent_worker = AgentWorker(self.agent_brain, text)
        self._agent_worker.finished.connect(self.agent_panel.show_response)
        self._agent_worker.error.connect(self.agent_panel.show_error)
        self._agent_worker.finished.connect(self._agent_worker.deleteLater)
        self._agent_worker.destroyed.connect(lambda: setattr(self, '_agent_worker', None))
        self._agent_worker.start()

    def _on_agent_set_params(self, params: dict):
        """May be called from bg thread — defer to main thread via invokeMethod."""
        print(f"[Agent-BG] _on_agent_set_params called: {params}")
        QtCore.QMetaObject.invokeMethod(
            self, "_apply_set_params", QtCore.Qt.QueuedConnection,
            QtCore.Q_ARG(dict, params))

    @QtCore.pyqtSlot(dict)
    def _apply_set_params(self, params: dict):
        self.cmd_bridge.enqueue_command(params)
        print(f"[Agent] 已入队控制命令: {params}")

    def _on_agent_control_mode(self, mode: str):
        """May be called from bg thread — defer to main thread."""
        QtCore.QMetaObject.invokeMethod(
            self, "_apply_control_mode", QtCore.Qt.QueuedConnection,
            QtCore.Q_ARG(str, mode))

    @QtCore.pyqtSlot(str)
    def _apply_control_mode(self, mode: str):
        """Apply control mode switch on the main thread."""
        self._agent_control_mode = mode
        self._update_mode_badge()
        self.cmd_bridge.enqueue_control_mode(mode)
        self.agent_panel.set_control_mode(mode)
        print(f"[Agent] 控制模式切换 -> {mode}")

    def _on_agent_strategy(self, strategy_name: str, params: dict):
        """May be called from bg thread — defer to main thread."""
        print(f"[Agent-BG] _on_agent_strategy called: {strategy_name} {params}")
        QtCore.QMetaObject.invokeMethod(
            self, "_apply_strategy", QtCore.Qt.QueuedConnection,
            QtCore.Q_ARG(str, strategy_name),
            QtCore.Q_ARG(dict, params))

    @QtCore.pyqtSlot(str, dict)
    def _apply_strategy(self, strategy_name: str, params: dict):
        """Apply strategy start on the main thread."""
        if self._agent_control_mode != "agent":
            self._agent_control_mode = "agent"
            self._update_mode_badge()
            self.cmd_bridge.enqueue_control_mode("agent")
        self.strategy_engine.start_strategy(strategy_name, params)
        first_action = self.strategy_engine.tick()
        if first_action:
            self.cmd_bridge.enqueue_action(first_action)
            print(f"[Agent] 策略首帧动作已入队: {first_action}")
        self.strategy_bar.setVisible(True)
        self.agent_panel.set_control_mode("agent")
        print(f"[Agent] 启动策略 {strategy_name} 参数={params}")

    def _on_agent_cancel_strategy(self):
        """May be called from bg thread — defer to main thread."""
        QtCore.QMetaObject.invokeMethod(
            self, "_apply_cancel_strategy", QtCore.Qt.QueuedConnection)

    @QtCore.pyqtSlot()
    def _apply_cancel_strategy(self):
        """Apply strategy cancellation on the main thread."""
        self.strategy_engine.stop_strategy()
        self._agent_control_mode = "rl"
        self._update_mode_badge()
        self.strategy_bar.setVisible(False)
        self.cmd_bridge.enqueue_control_mode("rl")
        self.agent_panel.set_control_mode("rl")
        print("[Agent] 已取消策略，交还RL控制")

    def _push_state_to_agent(self):
        if not self.agent_brain:
            return
        system_state = {}
        if self.lbl_snr.text() != "-- dB":
            system_state["信噪比"] = self.lbl_snr.text()
        if self.lbl_bw.text() != "-- MHz":
            system_state["检测带宽"] = self.lbl_bw.text()
        if self.lbl_ch.text() != "--":
            system_state["目标信道"] = self.lbl_ch.text()
        if self.lbl_pwr.text() != "-- dB":
            system_state["功率控制"] = self.lbl_pwr.text()
        if self.lbl_jbw.text() != "-- MHz":
            system_state["干扰带宽"] = self.lbl_jbw.text()
        if self.lbl_mode.text() != "--":
            system_state["波形模式"] = self.lbl_mode.text()
        if self.lbl_pol.text() != "--":
            system_state["决策算法/延迟"] = self.lbl_pol.text()
        if self.lbl_time.text() != "--":
            system_state["最新更新"] = self.lbl_time.text()

        ch_feats = []
        for i in range(self.channel_table.rowCount()):
            row_data = []
            for j in range(1, 5):
                item = self.channel_table.item(i, j)
                if item:
                    try:
                        row_data.append(float(item.text()))
                    except ValueError:
                        row_data.append(0.0)
                else:
                    row_data.append(0.0)
            if any(v != 0.0 for v in row_data):
                ch_feats.append(row_data)

        self.agent_brain.update_state(
            system_state=system_state,
            channel_features=ch_feats if ch_feats else None,
            snr_history=list(self.snr_history),
            bw_history=list(self.bw_history),
            power_history=list(self.power_history),
            channel_map_history=list(self.channel_history),
        )

    # ═══════════════════════════════════════════════════════════════
    # UI helpers
    # ═══════════════════════════════════════════════════════════════

    def _update_mode_badge(self):
        if self._agent_control_mode == "agent":
            self.mode_badge.setText("Agent 手动")
            self.mode_badge.setStyleSheet(
                "#ModeBadge { color: #c792ea; background: #1a1525; "
                "border: 1px solid #3a2a5a; border-radius: 10px; "
                "padding: 4px 10px; font-weight: 700; font-size: 9pt; }")
        else:
            self.mode_badge.setText("RL 自动")
            self.mode_badge.setStyleSheet(
                "#ModeBadge { color: #6ee7b7; background: #11231a; "
                "border: 1px solid #1a4a3a; border-radius: 10px; "
                "padding: 4px 10px; font-weight: 700; font-size: 9pt; }")

    def _update_strategy_bar(self):
        """Update strategy status label with current progress."""
        eng = self.strategy_engine
        if not eng.active:
            self.strategy_bar.setVisible(False)
            return
        name = eng.strategy_name
        detail = ""
        s = eng._strategy
        if hasattr(s, '_idx') and hasattr(s, 'channels'):
            ch = s.channels[s._idx % len(s.channels)] if s.channels else "?"
            total = len(s.channels)
            dwell = getattr(s, 'dwell_frames', 0)
            remain = max(0, dwell - s._frame) if dwell else 0
            detail = f"| 信道 {int(ch)+1}/{total} | 剩余 {remain} 帧"
        elif hasattr(s, 'current_channel'):
            detail = f"| 目标: 信道 {int(s.current_channel)+1}"
        self.strategy_label.setText(f"策略: {name} {detail}")
        self.strategy_bar.setVisible(True)

    def _on_stop_strategy(self):
        """User manually stops the active strategy via the UI button."""
        self.strategy_engine.stop_strategy()
        self._agent_control_mode = "rl"
        self._update_mode_badge()
        self.strategy_bar.setVisible(False)
        self.cmd_bridge.enqueue_control_mode("rl")
        self.agent_panel.set_control_mode("rl")
        print("[UI] 手动停止策略，交还RL控制")

    # ═══════════════════════════════════════════════════════════════
    # Telemetry handling
    # ═══════════════════════════════════════════════════════════════

    def closeEvent(self, event):
        self.server.stop()
        self.cmd_bridge.stop()
        self.server.join(timeout=2.0)
        self.cmd_bridge.join(timeout=2.0)
        super().closeEvent(event)

    @QtCore.pyqtSlot(dict)
    def handle_packet(self, pkt: dict):
        self._frame_count += 1
        if self._frame_count % 30 == 1:
            mode_str = "Agent手动" if self._agent_control_mode == "agent" else "RL自动"
            print(f"[UI] 遥测帧#{self._frame_count} 模式={mode_str} 策略={self.strategy_engine.active}")
        telemetry = pkt.get("telemetry", {})
        action = pkt.get("action", {})
        rl_meta = pkt.get("rl_meta", {})
        seq = pkt.get("seq", "-")

        snr = float(telemetry.get("snr_est", 0.0))
        bw_est = float(telemetry.get("bw_est", 0.0)) / 1e6
        tx_state = telemetry.get("tx_state", "running")
        spectrum = np.asarray(telemetry.get("spectrum", []), dtype=float)
        freq_axis = np.asarray(telemetry.get("freq_axis_ghz", []), dtype=float)
        waveform = np.asarray(telemetry.get("jam_waveform_abs", []), dtype=float)
        ch_map = np.asarray(telemetry.get("channel_map", []), dtype=np.int32)
        ch_feat = np.asarray(telemetry.get("channel_features", []), dtype=float)

        channel_idx = int(action.get("channel_idx", 0))
        power_db = float(action.get("power_db", 0.0))
        bw_mhz = int(action.get("bw_mhz", 10))
        waveform_mode = int(action.get("waveform_mode", 0))
        policy = rl_meta.get("policy", "unknown")
        latency = rl_meta.get("latency_ms", 0)

        # KPI cards
        self.conn_badge.setText("已连接  127.0.0.1:5555")
        self.dot_telem.setStyleSheet("color: #6ee7b7; font-size: 10pt;")
        # RL status: inferred from rl_meta policy name
        rl_ok = policy not in ("", "unknown", "fallback", "compat_default", "agent")
        self.dot_rl.setStyleSheet(f"color: {'#6ee7b7' if rl_ok else '#ff5c73'}; font-size: 10pt;")
        # cmd bridge status
        cmd_ok = self.cmd_bridge.is_connected
        self.dot_cmd.setStyleSheet(f"color: {'#6ee7b7' if cmd_ok else '#ff5c73'}; font-size: 10pt;")

        self.lbl_snr.setText(f"{snr:.2f} dB")
        self.lbl_bw.setText(f"{bw_est:.1f} MHz")
        self.lbl_ch.setText(f"信道 {channel_idx + 1}")
        self.lbl_pwr.setText(f"{power_db:.1f} dB")
        self.lbl_jbw.setText(f"{bw_mhz} MHz")
        self.lbl_mode.setText("带限噪声" if waveform_mode == 0 else "多音干扰")
        self.lbl_pol.setText(f"{policy} / {latency} ms")
        self.lbl_time.setText(datetime.now().strftime("%H:%M:%S"))

        # Spectrum
        if spectrum.size > 0:
            self.spectrum_curve.setData(
                freq_axis if freq_axis.size == spectrum.size else None, spectrum
            )
            self.spectrum_plot.repaint()
        # Waveform
        if waveform.size > 0:
            self.wave_curve.setData(waveform)
            self.wave_plot.repaint()

        # Channel map — X=channels, Y=time (vertical flow downward)
        if ch_map.size > 0:
            if ch_map.size != self.num_channels:
                self.num_channels = int(ch_map.size)
                self.channel_history = deque(
                    [np.zeros(self.num_channels, dtype=np.int32) for _ in range(self.max_history)],
                    maxlen=self.max_history,
                )
                bottom_axis = self.map_plot.getAxis("bottom")
                bottom_ticks = [(i + 0.5, str(i + 1)) for i in range(self.num_channels)]
                bottom_axis.setTicks([bottom_ticks])
                self.map_plot.setXRange(0, self.num_channels, padding=0)
                self.channel_table.setRowCount(self.num_channels)
                self.map_img.setRect(QtCore.QRectF(0, 0, self.num_channels, self.max_history))

            self.channel_history.append(ch_map)
            mat = np.stack(list(self.channel_history), axis=0).T  # (channels, history)
            self.map_img.setImage(mat, autoLevels=False)
            self.map_plot.repaint()

        # Trend history
        self.snr_history.append(snr)
        self.bw_history.append(bw_est)
        self.power_history.append(power_db)
        x = np.arange(len(self.snr_history))
        self.snr_curve.setData(x, np.array(self.snr_history))
        self.bw_curve.setData(x, np.array(self.bw_history))
        self.power_curve.setData(x, np.array(self.power_history))

        # Event log
        self.log_widget.append_log([
            datetime.now().strftime("%H:%M:%S"),
            seq,
            channel_idx + 1,
            f"{power_db:.1f}",
            bw_mhz,
            "噪声" if waveform_mode == 0 else "多音",
            policy,
            self._tx_text(tx_state),
        ])

        # Channel feature table
        if ch_feat.size > 0 and ch_feat.ndim == 2:
            rows_in = ch_feat.shape[0]
            if rows_in != self.channel_table.rowCount():
                self.channel_table.setRowCount(rows_in)
            for i in range(rows_in):
                vals = [str(i + 1)] + [f"{float(v):.2f}" for v in ch_feat[i, :4]]
                for j, val in enumerate(vals):
                    self.channel_table.setItem(i, j, QtWidgets.QTableWidgetItem(val))

        # ── Strategy engine tick ────────────────────────────
        if self.strategy_engine.active:
            agent_action = self.strategy_engine.tick(
                channel_features=ch_feat.tolist() if ch_feat.size > 0 else None,
                channel_map=ch_map.tolist() if ch_map.size > 0 else None,
                snr=snr,
                bw_est=bw_est,
            )
            if agent_action:
                self.cmd_bridge.enqueue_action(agent_action)
                if self._frame_count % 10 == 0:
                    print(f"[Strategy] tick → enqueued: ch={agent_action.get('channel_idx', '?')}, "
                          f"pwr={agent_action.get('power_db', '?')}dB, "
                          f"bw={agent_action.get('bw_mhz', '?')}MHz")
            self._update_strategy_bar()
        elif self.strategy_bar.isVisible():
            self.strategy_bar.setVisible(False)

        self._push_state_to_agent()

    @staticmethod
    def _tx_text(tx_state: str) -> str:
        mapping = {
            "running": "运行中",
            "mock_running": "模拟运行中",
            "disabled": "未发射(互锁关闭)",
            "stopped": "已停止",
            "error": "异常",
        }
        return mapping.get(str(tx_state), str(tx_state))


# ═══════════════════════════════════════════════════════════════════
# Worker thread for non-blocking LLM calls
# ═══════════════════════════════════════════════════════════════════

class AgentWorker(QtCore.QThread):
    finished = QtCore.pyqtSignal(str)
    error = QtCore.pyqtSignal(str)

    def __init__(self, brain: AgentBrain, message: str):
        super().__init__()
        self.brain = brain
        self.message = message

    def run(self):
        try:
            result = self.brain.send_message(self.message)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


def main():
    app = QtWidgets.QApplication(sys.argv)
    font = QtGui.QFont("Microsoft YaHei UI")
    font.setPointSize(10)
    app.setFont(font)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
