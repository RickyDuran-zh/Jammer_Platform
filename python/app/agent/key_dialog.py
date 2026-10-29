"""Dialog for storing the DeepSeek key in the Windows Credential Manager.

The key never lands in the repository, an environment file, or the UI source.
It is written to the current Windows user's credential vault under
``ZHJammerV4/DeepSeekAPI`` and read back at UI start-up or after saving here.
"""

from PyQt5 import QtCore, QtWidgets

from .secret_store import delete_api_key, read_api_key, save_api_key


class APIKeyDialog(QtWidgets.QDialog):
    """Save, replace, or remove this user's stored API key."""

    key_changed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI 助手密钥")
        self.setModal(True)
        self.setMinimumWidth(420)
        self._build_ui()
        self._refresh_status()

    def _build_ui(self):
        self.setStyleSheet("""
            QDialog { background: #121a24; }
            QLabel { color: #dbe6f1; font-size: 10pt; }
            QLineEdit {
                background: #0d1117; border: 1px solid #243244; border-radius: 7px;
                color: #e8eef5; padding: 6px 9px; font-size: 10pt;
            }
            QPushButton {
                background: #172436; border: 1px solid #30465f; border-radius: 7px;
                color: #b9c9da; padding: 5px 12px; font-size: 9pt;
            }
            QPushButton:hover { background: #223a55; }
            QPushButton:default {
                background: #1d3a2b; border-color: #2f6a4c; color: #7ef2b0;
                font-weight: 600;
            }
            QPushButton:disabled { color: #6b7a8c; }
        """)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        intro = QtWidgets.QLabel(
            "密钥保存在当前 Windows 用户的凭据管理器中，不写入仓库、脚本或 "
            "环境文件。更换电脑或账号后需要重新保存。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self.key_edit = QtWidgets.QLineEdit()
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.key_edit.setPlaceholderText("粘贴 DeepSeek API Key（sk-...）")
        self.key_edit.returnPressed.connect(self._on_save)
        row.addWidget(self.key_edit, stretch=1)

        self.show_btn = QtWidgets.QPushButton("显示")
        self.show_btn.setCheckable(True)
        self.show_btn.setFixedWidth(52)
        self.show_btn.toggled.connect(self._on_toggle_echo)
        row.addWidget(self.show_btn)
        layout.addLayout(row)

        self.message_label = QtWidgets.QLabel("")
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(8)
        self.delete_btn = QtWidgets.QPushButton("清除已存密钥")
        self.delete_btn.clicked.connect(self._on_delete)
        self.save_btn = QtWidgets.QPushButton("保存")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        close_btn = QtWidgets.QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(self.delete_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.save_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    # ── helpers ────────────────────────────────────────────────

    def _refresh_status(self):
        try:
            stored = read_api_key()
        except OSError as exc:
            self.status_label.setText(f"无法读取凭据管理器：{exc}")
            self.status_label.setStyleSheet("color: #ff5c73;")
            self.delete_btn.setEnabled(False)
            return
        if stored:
            masked = f"{stored[:6]}…{stored[-4:]}" if len(stored) > 12 else "已保存"
            self.status_label.setText(f"当前状态：已配置（{masked}）")
            self.status_label.setStyleSheet("color: #6ee7b7;")
        else:
            self.status_label.setText("当前状态：未配置，右侧自然语言交互不可用。")
            self.status_label.setStyleSheet("color: #ffb84d;")
        self.delete_btn.setEnabled(bool(stored))

    def _on_toggle_echo(self, checked: bool):
        self.key_edit.setEchoMode(
            QtWidgets.QLineEdit.Normal if checked else QtWidgets.QLineEdit.Password)
        self.show_btn.setText("隐藏" if checked else "显示")

    def _on_save(self):
        key = self.key_edit.text().strip()
        if not key:
            self.message_label.setText("请输入密钥后再保存。")
            self.message_label.setStyleSheet("color: #ff5c73;")
            return
        try:
            save_api_key(key)
        except (OSError, ValueError) as exc:
            self.message_label.setText(f"保存失败：{exc}")
            self.message_label.setStyleSheet("color: #ff5c73;")
            return
        self.key_edit.clear()
        self.message_label.setText("已保存到 Windows 凭据管理器，AI 助手立即可用。")
        self.message_label.setStyleSheet("color: #6ee7b7;")
        self._refresh_status()
        self.key_changed.emit(key)

    def _on_delete(self):
        try:
            delete_api_key()
        except OSError as exc:
            self.message_label.setText(f"清除失败：{exc}")
            self.message_label.setStyleSheet("color: #ff5c73;")
            return
        self.message_label.setText("已清除本机保存的密钥。")
        self.message_label.setStyleSheet("color: #98a6b8;")
        self._refresh_status()
        self.key_changed.emit("")
