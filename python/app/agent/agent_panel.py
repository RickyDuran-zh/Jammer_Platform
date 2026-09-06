"""
PyQt5 chat panel widget for interacting with the AI agent.
"""
from PyQt5 import QtCore, QtGui, QtWidgets

MAX_BLOCKS = 500  # trim messages when exceeding this many text blocks


class AgentPanel(QtWidgets.QWidget):
    """Chat panel with message display, loading animation, and quick commands."""

    send_message = QtCore.pyqtSignal(str)
    clear_requested = QtCore.pyqtSignal()
    stop_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dot_timer = QtCore.QTimer(self)
        self._dot_timer.timeout.connect(self._tick_dots)
        self._dot_count = 0
        self._control_mode = "rl"  # "rl" or "agent"
        self._pending = False  # LLM call in progress
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # ── Header ─────────────────────────────────────────────
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("AI 智能助手")
        title.setObjectName("AgentTitle")

        self.status_label = QtWidgets.QLabel("就绪")
        self.status_label.setObjectName("AgentStatus")

        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.status_label)

        new_chat_btn = QtWidgets.QPushButton("新对话")
        new_chat_btn.setObjectName("AgentClearBtn")
        new_chat_btn.setFixedWidth(60)
        new_chat_btn.clicked.connect(self._on_new_chat)
        header.addWidget(new_chat_btn)

        clear_btn = QtWidgets.QPushButton("清屏")
        clear_btn.setObjectName("AgentClearBtn")
        clear_btn.setFixedWidth(50)
        clear_btn.clicked.connect(self._on_clear_screen)
        header.addWidget(clear_btn)

        layout.addLayout(header)

        # ── Message area ───────────────────────────────────────
        self.message_area = QtWidgets.QTextEdit()
        self.message_area.setReadOnly(True)
        self.message_area.setObjectName("AgentMessageArea")
        self.message_area.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
        layout.addWidget(self.message_area, stretch=1)

        # ── Quick commands (dynamic based on control mode) ─────
        self.quick_cmds_layout = QtWidgets.QHBoxLayout()
        self.quick_cmds_layout.setSpacing(4)
        self.quick_cmd_btns: list[QtWidgets.QPushButton] = []
        layout.addLayout(self.quick_cmds_layout)

        # Second row for control commands
        self.control_cmds_layout = QtWidgets.QHBoxLayout()
        self.control_cmds_layout.setSpacing(4)
        self.control_cmd_btns: list[QtWidgets.QPushButton] = []
        layout.addLayout(self.control_cmds_layout)

        self._refresh_quick_commands()

        # ── Input area ─────────────────────────────────────────
        input_layout = QtWidgets.QHBoxLayout()
        input_layout.setSpacing(6)

        self.input_field = QtWidgets.QLineEdit()
        self.input_field.setObjectName("AgentInput")
        self.input_field.setPlaceholderText("输入命令，例如：分析频谱 / 扫描所有信道 / 追踪最强信号...")
        self.input_field.returnPressed.connect(self._on_send)

        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.setObjectName("AgentClearBtn")
        self.stop_btn.setFixedWidth(50)
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setVisible(False)

        self.send_btn = QtWidgets.QPushButton("发送")
        self.send_btn.setObjectName("AgentSendBtn")
        self.send_btn.setFixedWidth(60)
        self.send_btn.clicked.connect(self._on_send)

        input_layout.addWidget(self.input_field)
        input_layout.addWidget(self.stop_btn)
        input_layout.addWidget(self.send_btn)
        layout.addLayout(input_layout)

    # ── Public API ─────────────────────────────────────────────

    def set_control_mode(self, mode: str):
        """Switch quick command buttons based on control mode."""
        if self._control_mode == mode:
            return  # already in this mode, avoid redundant refresh
        self._control_mode = mode
        self._refresh_quick_commands()

    # ── Quick commands ──────────────────────────────────────────

    def _refresh_quick_commands(self):
        # Clear existing — hide, remove from layout, then schedule deletion
        for btn in self.quick_cmd_btns:
            btn.hide()
            self.quick_cmds_layout.removeWidget(btn)
            btn.deleteLater()
        self.quick_cmd_btns.clear()
        self.quick_cmds_layout.activate()
        for btn in self.control_cmd_btns:
            btn.hide()
            self.control_cmds_layout.removeWidget(btn)
            btn.deleteLater()
        self.control_cmd_btns.clear()
        self.control_cmds_layout.activate()

        if self._control_mode == "agent":
            analysis = [
                ("频谱分析", "分析当前频谱态势"),
                ("趋势评估", "分析最近的趋势变化"),
            ]
            control = [
                ("固定当前", "固定干扰当前信道"),
                ("追踪最强", "切换到Agent控制，追踪占用度最高的信道"),
                ("信道扫描", "切换到Agent控制，扫描所有信道"),
                ("交还RL", "取消策略，切换回RL自动控制"),
            ]
        else:
            analysis = [
                ("频谱分析", "分析当前频谱态势"),
                ("趋势评估", "分析最近的趋势变化"),
                ("信道状态", "查看各信道状态分布"),
            ]
            control = [
                ("接管控制", "切换到Agent控制模式，固定干扰当前信道"),
                ("信道扫描", "切换到Agent控制，扫描所有信道，每个信道停留2秒"),
                ("追踪最强", "切换到Agent控制，追踪占用度最高的信道"),
            ]

        for label, cmd in analysis:
            btn = self._make_qcmd(label, cmd)
            self.quick_cmds_layout.addWidget(btn)
            self.quick_cmd_btns.append(btn)

        for label, cmd in control:
            btn = self._make_qcmd(label, cmd)
            self.control_cmds_layout.addWidget(btn)
            self.control_cmd_btns.append(btn)

        # Force immediate layout recalculation
        self.quick_cmds_layout.activate()
        self.control_cmds_layout.activate()

    def _make_qcmd(self, label: str, cmd: str):
        btn = QtWidgets.QPushButton(label)
        btn.setObjectName("QuickCmdBtn")
        btn.setFixedHeight(24)
        btn.clicked.connect(lambda checked, c=cmd: self._dispatch_message(c))
        return btn

    # ── Input handling ──────────────────────────────────────────

    def keyPressEvent(self, event):
        """Ctrl+Enter to send."""
        if event.key() == QtCore.Qt.Key_Return and (
            event.modifiers() & QtCore.Qt.ControlModifier
        ):
            self._on_send()
        else:
            super().keyPressEvent(event)

    def _on_send(self):
        text = self.input_field.text().strip()
        if not text:
            return
        self.input_field.clear()
        self._dispatch_message(text)

    def _dispatch_message(self, text: str):
        """Start loading UI and emit the message. Shared by text input and quick commands."""
        self._append_message("user", text)
        self._start_loading()
        self._pending = True
        self.send_btn.setEnabled(False)
        self.input_field.setEnabled(False)
        self.stop_btn.setVisible(True)
        self.send_message.emit(text)

    def _on_stop(self):
        """User requests to cancel the in-progress LLM call."""
        if self._pending:
            self._pending = False
            self._stop_loading()
            self._append_message("error", "请求已被用户取消。")
            self.send_btn.setEnabled(True)
            self.input_field.setEnabled(True)
            self.stop_btn.setVisible(False)
            self.stop_requested.emit()

    # ── Response handling ───────────────────────────────────────

    def show_response(self, response_text: str):
        self._pending = False
        self._stop_loading()
        self._append_message("agent", response_text)
        self.send_btn.setEnabled(True)
        self.input_field.setEnabled(True)
        self.stop_btn.setVisible(False)
        self.input_field.setFocus()

    def show_error(self, error_text: str):
        self._pending = False
        self._stop_loading()
        self._append_message("error", error_text)
        self.send_btn.setEnabled(True)
        self.input_field.setEnabled(True)
        self.stop_btn.setVisible(False)

    # ── Chat management ─────────────────────────────────────────

    def _on_new_chat(self):
        self.message_area.clear()
        self.clear_requested.emit()
        self._append_message("agent", "新对话已开始。请随时输入你的问题或命令。")

    def _on_clear_screen(self):
        self.message_area.clear()

    # ── Loading animation ──────────────────────────────────────

    def _start_loading(self):
        self._dot_count = 0
        self._tick_dots()
        self._dot_timer.start(400)

    def _stop_loading(self):
        self._dot_timer.stop()
        self.status_label.setText("就绪")
        self.status_label.setStyleSheet("color: #6ee7b7;")

    def _tick_dots(self):
        self._dot_count = (self._dot_count + 1) % 4
        dots = "." * self._dot_count if self._dot_count > 0 else ""
        self.status_label.setText(f"思考中{dots}")
        self.status_label.setStyleSheet("color: #ffb84d; font-weight: bold;")

    def set_connected(self, connected: bool):
        if connected:
            self.status_label.setText("已连接")
            self.status_label.setStyleSheet("color: #6ee7b7;")
        else:
            self.status_label.setText("未连接")
            self.status_label.setStyleSheet("color: #ff5c73;")

    # ── Message formatting ─────────────────────────────────────

    def _append_message(self, role: str, text: str):
        cursor = self.message_area.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)

        if role == "user":
            label, color = "你", "#66d9ef"
        elif role == "agent":
            label, color = "AI助手", "#4dff88"
        elif role == "error":
            label, color = "系统", "#ff5c73"
        else:
            label, color = role, "#98a6b8"

        fmt = QtGui.QTextCharFormat()
        fmt.setForeground(QtGui.QColor(color))
        fmt.setFontWeight(QtGui.QFont.Bold)
        cursor.insertText(f"\n[{label}] ", fmt)

        text_fmt = QtGui.QTextCharFormat()
        text_fmt.setForeground(QtGui.QColor("#e8eef5"))
        cursor.insertText(f"{text}\n", text_fmt)

        sep_fmt = QtGui.QTextCharFormat()
        sep_fmt.setForeground(QtGui.QColor("#243244"))
        cursor.insertText("─" * 50 + "\n", sep_fmt)

        self.message_area.setTextCursor(cursor)
        self._prune_if_needed()

    def _prune_if_needed(self):
        """Remove oldest messages when exceeding block limit."""
        doc = self.message_area.document()
        if doc.blockCount() > MAX_BLOCKS:
            # Remove first ~100 blocks (roughly 10 message pairs)
            root = doc.rootFrame()
            block = root.firstChild()
            removed = 0
            while block and removed < 100:
                cursor = QtGui.QTextCursor(block)
                cursor.select(QtGui.QTextCursor.BlockUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()  # remove trailing newline
                block = root.firstChild()
                removed += 1
