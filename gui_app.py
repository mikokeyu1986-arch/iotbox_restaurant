"""
IoT Box Desktop - 桌面 GUI 管理工具

功能：
- 系统托盘图标 & 右键菜单
- 服务控制（启动/停止/重启）
- HTTP / HTTPS 协议切换
- 服务器 URL 配置（Odoo 连接）
- 电子秤配置（串口 / 品牌 / 协议）
- 在线更新检查 & 安装
- 服务状态实时监控
"""

from __future__ import annotations

import logging
import json
import os
import platform
import subprocess
import socket
import ssl
import sys
import threading
import time
import webbrowser
import ctypes
import traceback
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from pathlib import Path
import tkinter as tk
from tkinter import Tk, StringVar, IntVar, BooleanVar
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText
from typing import Any

# 将项目根目录加入 path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from app.config_store import ConfigStore
from app.updater import UpdateManager, UpdateSource, GitHubUpdateSource, DEFAULT_UPDATE_MANIFEST_URL
from app.version import APP_VERSION

_logger = logging.getLogger(__name__)

# ============================================================================
# 常量
# ============================================================================

APP_NAME = "IoT Box Desktop"
# PyInstaller replaces _internal during updates. Keep mutable settings and
# generated TLS certificates beside the installed GUI instead.
INSTALL_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BASE_DIR
_CONFIG_DEFAULT = INSTALL_DIR / "runtime_config.json"
_CERTS_DIR = INSTALL_DIR / "certs"
# One configuration source for both HTTP and HTTPS runtimes.
CONFIG_FILE = _CONFIG_DEFAULT

DEFAULT_SCALE_PORT = "COM3"
DEFAULT_SCALE_BAUDRATE = 9600

# IoT Box 服务端口（与 run_https.py 一致）。
# 运行时只有 HTTPS —— HTTP 服务已移除。
HTTPS_PORT = 8398

# 电子秤品牌预设 —— 与 app/drivers/scale.py 中 ScaleConfig 支持的品牌一致。
# runtime 只认 zfoc 和 epelsa 两个 brand，其他品牌不会被 ScaleMonitor 正确解析。
SCALE_PRESETS: dict[str, dict[str, Any]] = {
    "Gram Zfoc-p (ZFOC)": {
        "brand": "zfoc",
        "baudrate": 9600,
        "timeout": 1.2,
        "inter_command_delay": 0.05,
    },
    "Epelsa 56 PPI": {
        "brand": "epelsa",
        "baudrate": 9600,
        "timeout": 1.2,
        "inter_command_delay": 0.05,
    },
}

# 服务控制脚本。运行时只有 HTTPS —— HTTP 服务已移除。
HTTPS_SCRIPT = BASE_DIR / "run_https.py"
REDSYS_HOME = INSTALL_DIR / "redsys"
REDSYS_SCRIPT = REDSYS_HOME / "server" / "main.py"
REDSYS_CONFIG = REDSYS_HOME / "config.yaml"
REDSYS_EXECUTABLE = INSTALL_DIR / "runtime" / "redsys_service" / "redsys_service.exe"
REDSYS_PORT = 6971


# ============================================================================
# 设置窗口
# ============================================================================


class SettingsWindow(tk.Toplevel):
    """主设置窗口（从托盘菜单打开）"""

    def __init__(self, master: tk.Tk | None, config_store: ConfigStore) -> None:
        super().__init__(master)
        self.config_store = config_store
        self.title("IoT Box — 设备管理器")
        self.geometry("750x580")
        self.minsize(680, 480)
        self.resizable(True, True)
        self._minimize_to_tray_enabled = False

        # 居中
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self.bind("<Unmap>", self._on_window_unmap, add="+")

        # 图标（如可用）
        self._set_icon()

        # 构建 UI
        self._build_notebook()
        self._load_config()

        # 自动启动服务
        if self.auto_start_var.get():
            self.after(500, self._on_start)
        # REDSYS is independent of the printing service, so make the local
        # card terminal available after every normal GUI launch.
        self.after(700, self._start_redsys_automatically)
        self.after(60000, self._auto_update_check)

    # ------------------------------------------------------------------

    def _set_icon(self) -> None:
        try:
            ico = BASE_DIR / "web" / "favicon.ico"
            if ico.exists():
                self.iconbitmap(str(ico))
        except Exception:
            pass

    def _on_window_close(self) -> None:
        """退出 IoT Box 时同步关闭独立客户显示屏。"""
        if self._minimize_to_tray_enabled:
            self.withdraw()
            return
        self._close_customer_display_screen()
        self._stop_redsys_from_gui()
        self.destroy()

    def enable_minimize_to_tray(self) -> None:
        self._minimize_to_tray_enabled = True

    def _on_window_unmap(self, _event=None) -> None:
        if self._minimize_to_tray_enabled and self.state() == "iconic":
            self.after_idle(self.withdraw)

    # ------------------------------------------------------------------

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=5, pady=5)

        # ---- Tab 1: 服务控制 ----
        self.tab_service = ttk.Frame(self.notebook)
        self.tab_server = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_service, text="  服务控制  ")
        self._build_service_tab()

        self.notebook.add(self.tab_server, text="  Odoo Server Binding  ")
        self._build_server_tab()

        self.tab_redsys = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_redsys, text="  💳 REDSYS 刷卡  ")
        self._build_redsys_tab()

        # ---- Tab 2: 服务器配置 ----

        # ---- Tab 3: 电子秤配置 ----
        self.tab_scale = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_scale, text="  电子秤  ")
        self._build_scale_tab()
        self.tab_vfd = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_vfd, text="  VFD 客显  ")
        self._build_vfd_tab()
        # ---- Tab 4: 关于 & 更新 ----
        self.tab_about = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_about, text="  关于 & 更新  ")
        self._build_about_tab()

        self.tab_diagnostics = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_diagnostics, text="  诊断日志  ")
        self._build_diagnostics_tab()

    def _build_diagnostics_tab(self) -> None:
        frame = self.tab_diagnostics
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", padx=10, pady=10)
        ttk.Button(toolbar, text="刷新日志", command=self._refresh_diagnostics).pack(side="left", padx=(0, 8))
        ttk.Button(toolbar, text="清空显示", command=self._clear_diagnostics).pack(side="left", padx=(0, 8))
        ttk.Button(toolbar, text="导出日志", command=self._export_diagnostics).pack(side="left")
        ttk.Label(frame, text="仅用于开发排错；日志可能包含设备地址等运行信息，请勿直接公开。", foreground="gray").pack(anchor="w", padx=10)
        self.diagnostics_text = ScrolledText(frame, state="disabled", wrap="none", font=("Consolas", 9))
        self.diagnostics_text.pack(fill="both", expand=True, padx=10, pady=10)
        self._refresh_diagnostics()

    def _diagnostic_files(self) -> list[Path]:
        paths = [BASE_DIR / "logs", BASE_DIR / "redsys" / "data"]
        result: list[Path] = []
        for directory in paths:
            if directory.exists():
                result.extend(p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in {".log", ".txt", ".json"})
        return result

    def _refresh_diagnostics(self) -> None:
        chunks = []
        for path in self._diagnostic_files():
            try:
                chunks.append(f"===== {path.relative_to(BASE_DIR)} =====\n{path.read_text(encoding='utf-8', errors='replace')[-200000:]}")
            except OSError as exc:
                chunks.append(f"===== {path.name} =====\n读取失败: {exc}")
        text = "\n\n".join(chunks) or "当前没有可用日志。"
        self.diagnostics_text.config(state="normal")
        self.diagnostics_text.delete("1.0", "end")
        self.diagnostics_text.insert("1.0", text)
        self.diagnostics_text.config(state="disabled")

    def _clear_diagnostics(self) -> None:
        self.diagnostics_text.config(state="normal")
        self.diagnostics_text.delete("1.0", "end")
        self.diagnostics_text.config(state="disabled")

    def _export_diagnostics(self) -> None:
        target = filedialog.asksaveasfilename(defaultextension=".log", filetypes=[("Log files", "*.log"), ("All files", "*.*")])
        if target:
            Path(target).write_text(self.diagnostics_text.get("1.0", "end"), encoding="utf-8")

    # ==================================================================
    # 服务控制 Tab
    # ==================================================================

    def _build_service_tab(self) -> None:
        f = self.tab_service

        # -- 状态区 --
        status_frame = ttk.LabelFrame(f, text="服务状态", padding=10)
        status_frame.pack(fill="x", padx=10, pady=10)

        self.lbl_status = ttk.Label(status_frame, text="● 服务状态: 未运行", font=("Segoe UI", 11))
        self.lbl_status.pack(anchor="w")

        self.lbl_protocol = ttk.Label(status_frame, text="协议: --", font=("Segoe UI", 10))
        self.lbl_protocol.pack(anchor="w", pady=(5, 0))

        self.lbl_uptime = ttk.Label(status_frame, text="运行时间: --", font=("Segoe UI", 10))
        self.lbl_uptime.pack(anchor="w")

        # -- 协议切换区 --
        proto_frame = ttk.LabelFrame(f, text="协议切换", padding=10)
        proto_frame.pack(fill="x", padx=10, pady=10)

        # 只有 HTTPS：HTTP 服务已移除，8399 端口不再存在。
        self.proto_var = StringVar(value="https")
        rb_https = ttk.Radiobutton(proto_frame, text=f"HTTPS (端口 {HTTPS_PORT})", variable=self.proto_var, value="https")
        rb_https.pack(side="left")

        self.auto_start_var = BooleanVar(value=True)
        ttk.Checkbutton(proto_frame, text="启动 GUI 后自动启动服务", variable=self.auto_start_var,
                        command=self._save_service_preferences).pack(side="left", padx=(20, 0))
        ttk.Button(proto_frame, text="保存协议设置", command=self._save_service_preferences).pack(side="right")

        # -- 动作按钮 --
        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill="x", padx=10, pady=10)

        self.btn_start = ttk.Button(btn_frame, text="▶  启动服务", command=self._on_start)
        self.btn_start.pack(side="left", padx=(0, 8))

        self.btn_stop = ttk.Button(btn_frame, text="■  停止服务", command=self._on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 8))

        self.btn_restart = ttk.Button(btn_frame, text="↻  重启服务", command=self._on_restart, state="disabled")
        self.btn_restart.pack(side="left", padx=(0, 8))

        self.btn_open_web = ttk.Button(btn_frame, text="🌐  打开网页界面", command=self._on_open_web)
        self.btn_open_web.pack(side="left")

        # -- 日志区 --
        log_frame = ttk.LabelFrame(f, text="服务日志", padding=5)
        log_frame.pack(fill="both", expand=True, padx=10, pady=10)

        self.log_text = ScrolledText(log_frame, height=10, state="disabled", wrap="word",
                                      font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self.log_text.pack(fill="both", expand=False, ipady=35)

        self._append_log("IoT Box Desktop 已启动")

    # ------------------------------------------------------------------

    def _build_redsys_tab(self) -> None:
        """Build the local REDSYS terminal configuration and diagnostics view."""
        frame = self.tab_redsys
        title = ttk.Frame(frame)
        title.pack(fill="x", padx=12, pady=(12, 0))
        try:
            card_icon = tk.PhotoImage(file=str(BASE_DIR / "web" / "nfc_override.png"))
            self._redsys_card_icon = card_icon.subsample(16, 16)
            ttk.Label(title, text="REDSYS 刷卡", image=self._redsys_card_icon, compound="left", font=("Segoe UI", 12, "bold")).pack(side="left")
        except tk.TclError:
            ttk.Label(title, text="💳 REDSYS 刷卡", font=("Segoe UI", 12, "bold")).pack(side="left")

        settings = ttk.LabelFrame(frame, text="REDSYS 服务配置", padding=12)
        settings.pack(fill="x", padx=10, pady=10)
        self.redsys_merchant_var = StringVar()
        self.redsys_terminal_var = StringVar()
        self.redsys_com_var = StringVar()
        self.redsys_password_var = StringVar()
        self.redsys_version_var = StringVar()
        self.redsys_simulate_var = BooleanVar(value=False)
        for row, (label, variable) in enumerate((
            ("商户号", self.redsys_merchant_var),
            ("终端号", self.redsys_terminal_var),
            ("刷卡串口", self.redsys_com_var),
            ("签名密码", self.redsys_password_var),
            ("版本号", self.redsys_version_var),
        )):
            ttk.Label(settings, text=label).grid(row=row, column=0, sticky="w", pady=4)
            if variable is self.redsys_com_var:
                self.redsys_com_combo = ttk.Combobox(settings, textvariable=variable, width=31, state="readonly")
                self.redsys_com_combo.grid(row=row, column=1, sticky="ew", pady=4)
                ttk.Button(settings, text="刷新", command=self._refresh_redsys_ports).grid(row=row, column=2, padx=(6, 0))
            else:
                ttk.Entry(settings, textvariable=variable, width=34, show="*" if variable is self.redsys_password_var else "").grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Checkbutton(settings, text="模拟模式（不连接真实刷卡机）", variable=self.redsys_simulate_var).grid(row=5, column=0, columnspan=2, sticky="w", pady=6)
        settings.columnconfigure(1, weight=1)

        self.redsys_status_var = StringVar(value="状态：未运行")
        ttk.Label(frame, textvariable=self.redsys_status_var, font=("Segoe UI", 11)).pack(anchor="w", padx=12)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", padx=10, pady=10)
        ttk.Button(buttons, text="测试读卡器", command=self._test_redsys_reader).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="测试刷卡", command=self._test_redsys_payment).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="保存配置", command=self._save_redsys_config).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="启动 REDSYS", command=self._start_redsys_from_gui).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="停止 REDSYS", command=self._stop_redsys_from_gui).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="打开刷卡界面", command=lambda: webbrowser.open(f"http://127.0.0.1:{REDSYS_PORT}/ui")).pack(side="left")
        self._load_redsys_config()

    def _load_redsys_config(self) -> None:
        text = REDSYS_CONFIG.read_text(encoding="utf-8") if REDSYS_CONFIG.exists() else ""

        def value(key: str) -> str:
            match = re.search(rf"^\s*{re.escape(key)}:\s*['\"]?([^'\"\r\n]+)", text, re.MULTILINE)
            return match.group(1).strip() if match else ""

        self.redsys_merchant_var.set(value("comercio"))
        self.redsys_terminal_var.set(value("terminal") or "1")
        self.redsys_com_var.set(value("puerto"))
        self.redsys_password_var.set(value("clave_firma"))
        self.redsys_version_var.set(value("version") or "6.1")
        self.redsys_simulate_var.set(value("simulate").lower() == "true")
        self._refresh_redsys_ports()

    def _refresh_redsys_ports(self) -> None:
        configured = self._normalize_windows_com_port(self.redsys_com_var.get())
        ports = {normalized for port in [*self._list_serial_ports(), configured] if (normalized := self._normalize_windows_com_port(port))}
        self.redsys_com_combo["values"] = sorted(ports, key=self._serial_port_sort_key)
        if configured:
            self.redsys_com_var.set(configured)

    def _save_redsys_config(self) -> None:
        if not REDSYS_CONFIG.exists():
            self.redsys_status_var.set("状态：找不到 REDSYS 配置文件")
            return
        text = REDSYS_CONFIG.read_text(encoding="utf-8")
        for key, new_value in {
            "comercio": self.redsys_merchant_var.get().strip(),
            "terminal": self.redsys_terminal_var.get().strip() or "1",
            "puerto": self.redsys_com_var.get().strip(),
            "clave_firma": self.redsys_password_var.get().strip(),
            "version": self.redsys_version_var.get().strip() or "6.1",
            "simulate": "true" if self.redsys_simulate_var.get() else "false",
        }.items():
            text = re.sub(rf"^(\s*{re.escape(key)}:\s*)[^\r\n]+", rf"\g<1>'{new_value}'", text, flags=re.MULTILINE)
        REDSYS_CONFIG.write_text(text, encoding="utf-8")
        self.redsys_status_var.set("状态：配置已保存")

    def _start_redsys_from_gui(self) -> None:
        self._save_redsys_config()
        self._ensure_redsys_service()
        self.redsys_status_var.set("状态：运行中" if self._is_local_port_open(REDSYS_PORT) else "状态：启动失败")

    def _start_redsys_automatically(self) -> None:
        self._ensure_redsys_service()
        self.redsys_status_var.set("状态：运行中" if self._is_local_port_open(REDSYS_PORT) else "状态：启动失败")

    def _test_redsys_reader(self) -> None:
        self._save_redsys_config()
        self._ensure_redsys_service()
        if not self._is_local_port_open(REDSYS_PORT):
            self.redsys_status_var.set("状态：读卡器测试失败（REDSYS 服务未运行）")
            return
        self.redsys_status_var.set("状态：正在测试读卡器…")

        def run_test() -> None:
            try:
                with urlopen(Request(f"http://127.0.0.1:{REDSYS_PORT}/connect", method="GET"), timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not payload.get("connected"):
                    raise RuntimeError(str(payload.get("error") or "读卡器未连接"))
                self.after(0, self.redsys_status_var.set, "状态：读卡器连接成功（未发起付款）")
            except Exception as exc:
                self.after(0, self.redsys_status_var.set, f"状态：读卡器测试失败（{exc}）")

        threading.Thread(target=run_test, daemon=True).start()

    def _test_redsys_payment(self) -> None:
        if not messagebox.askyesno("测试刷卡", "将发起一笔 0.01 EUR 的真实测试付款。\n请只在可撤销的测试交易中继续。", parent=self):
            return
        self._save_redsys_config()
        self._ensure_redsys_service()
        if not self._is_local_port_open(REDSYS_PORT):
            self.redsys_status_var.set("状态：测试刷卡失败（REDSYS 服务未运行）")
            return
        invoice = f"GUI-TEST-{int(time.time())}"
        self.redsys_status_var.set("状态：等待读卡器完成 0.01 EUR 测试付款…")

        def run_payment() -> None:
            try:
                request = Request(
                    f"http://127.0.0.1:{REDSYS_PORT}/pay",
                    data=json.dumps({"type": "PAGO", "amount": 0.01, "invoice": invoice}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=130) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                operation = payload.get("operation") or {}
                if not payload.get("success"):
                    raise RuntimeError(str(operation.get("message") or payload.get("warning") or "付款未完成"))
                self.after(0, self.redsys_status_var.set, "状态：测试刷卡成功")
            except Exception as exc:
                self.after(0, self.redsys_status_var.set, f"状态：测试刷卡失败（{exc}）")

        threading.Thread(target=run_payment, daemon=True).start()

    def _stop_redsys_from_gui(self) -> None:
        proc = getattr(self, "_redsys_proc", None)
        if proc and proc.poll() is None:
            proc.terminate()
        self._redsys_proc = None
        self.redsys_status_var.set("状态：已停止")

    def _ensure_redsys_service(self) -> None:
        if self._is_local_port_open(REDSYS_PORT):
            return
        if not REDSYS_SCRIPT.exists() or not REDSYS_CONFIG.exists():
            self._append_log("REDSYS 启动失败: 找不到 redsys/server/main.py 或 config.yaml")
            return
        try:
            self._redsys_proc = subprocess.Popen(
                [str(REDSYS_EXECUTABLE), "--config", str(REDSYS_CONFIG)]
                if getattr(sys, "frozen", False) and REDSYS_EXECUTABLE.exists()
                else [sys.executable, str(REDSYS_SCRIPT), "--config", str(REDSYS_CONFIG)],
                cwd=str(INSTALL_DIR),
                env=os.environ.copy(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
            )
            deadline = time.time() + 8.0
            while time.time() < deadline and self._redsys_proc.poll() is None:
                if self._is_local_port_open(REDSYS_PORT):
                    return
                time.sleep(0.2)
            self._append_log("REDSYS 启动失败: 6971 端口未就绪")
        except Exception as exc:
            self._append_log(f"REDSYS 启动失败: {exc}")

    def _on_start(self) -> None:
        self._ensure_redsys_service()
        proto = self.proto_var.get()
        self._activate_protocol_config(proto)
        port = HTTPS_PORT
        if self._is_local_service_ready(proto, port):
            # A service started outside this GUI is still a valid running service.
            self._on_service_started(proto)
            return
        if self._is_local_port_open(port):
            self._append_log(f"端口 {port} 已被其他或未就绪的服务占用")
            return
        self._append_log(f"正在以 {proto.upper()} 模式启动服务…")
        script = HTTPS_SCRIPT
        threading.Thread(target=self._run_service, args=(script, proto), daemon=True).start()

    def _save_service_preferences(self) -> None:
        protocol = self.proto_var.get().strip().lower()
        if protocol not in {"http", "https"}:
            protocol = "https"
        self._activate_protocol_config(protocol)
        preferences = {
            "service_protocol": protocol,
            "auto_start_service": bool(self.auto_start_var.get()),
        }
        self.config_store.update_local_config(**preferences)
        if self.config_store.config_path.resolve() != _CONFIG_DEFAULT.resolve():
            # The default store is also the GUI bootstrap store.  Keep only
            # launcher preferences there so the next run can reopen the
            # selected protocol without mixing the two runtime stores.
            ConfigStore(_CONFIG_DEFAULT).update_local_config(**preferences)
        self._append_log(f"服务设置已保存: protocol={protocol}, auto_start={self.auto_start_var.get()}")

    def _activate_protocol_config(self, protocol: str) -> None:
        target = _CONFIG_DEFAULT
        if self.config_store.config_path.resolve() == target.resolve():
            return
        previous_connection = self.config_store.get_connection()
        previous_local = self.config_store.get_local_config()
        target_existed = target.exists()
        next_store = ConfigStore(target)
        if not target_existed:
            portable_local = {
                key: value
                for key, value in previous_local.items()
                if key not in {"ssl_engine", "local_url", "service_protocol"}
            }
            if portable_local:
                next_store.update_local_config(**portable_local)
            if previous_connection.get("url"):
                next_store.update_connection(**previous_connection)
        self.config_store = next_store

    def _run_service(self, script: Path, proto: str) -> None:
        try:
            port = HTTPS_PORT
            if self._is_local_service_ready(proto, port):
                self.after(0, self._append_log, f"端口 {port} 已有服务运行，不重复启动")
                return
            service_env = os.environ.copy()
            service_env["IOT_CONFIG_PATH"] = str(_CONFIG_DEFAULT)
            service_env["IOT_CERTS_DIR"] = str(_CERTS_DIR)
            # European thermal-printer code page with a real Euro symbol.
            service_env["IOT_ESCPOS_ENCODING"] = "cp858"
            if getattr(sys, "frozen", False):
                service_exe = Path(sys.executable).parent / "runtime" / "run_https.exe"
                service_command = [str(service_exe)]
                service_cwd = service_exe.parent
            else:
                service_command = [sys.executable, str(script)]
                service_cwd = BASE_DIR
            self._service_proc = subprocess.Popen(
                service_command,
                cwd=str(service_cwd),
                env=service_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
            )
            deadline = time.time() + 20.0
            while time.time() < deadline and self._service_proc.poll() is None:
                if self._is_local_service_ready(proto, port):
                    self.after(0, self._on_service_started, proto)
                    break
                time.sleep(0.2)
            else:
                return_code = self._service_proc.poll()
                self.after(
                    0,
                    self._append_log,
                    f"启动失败: 健康检查未通过 (protocol={proto}, port={port}, exit={return_code})",
                )
                if self._service_proc.poll() is None:
                    self._service_proc.terminate()
                return
            for line in self._service_proc.stdout:
                self.after(0, self._append_log, line.strip())
        except Exception as e:
            self.after(0, self._append_log, f"启动失败: {e}")

    @staticmethod
    def _is_local_port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
                return True
        except OSError:
            return False

    @staticmethod
    def _is_local_service_ready(proto: str, port: int) -> bool:
        url = f"{proto}://127.0.0.1:{int(port)}/healthz"
        context = ssl._create_unverified_context() if proto == "https" else None
        try:
            with urlopen(url, timeout=0.8, context=context) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError):
            return False
        return payload.get("status") == "ready" and payload.get("protocol") == proto

    def _on_service_started(self, proto: str) -> None:
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_restart.config(state="normal")
        self.lbl_status.config(text=f"● 服务状态: 运行中 ({proto.upper()})", foreground="green")
        self.lbl_protocol.config(text=f"协议: {proto.upper()}")
        self._start_time = time.time()
        self._update_uptime()
        # Native POS display actions are rendered locally.  There is no URL
        # or manual launch setting: the customer screen appears whenever a
        # second monitor is connected.
        self.after(200, self._launch_customer_display_screen)

    def _stop_service_process(self) -> None:
        """停止服务子进程并等待退出（线程安全，不操作 GUI widget）。

        terminate() 后等待进程真正退出并释放端口，避免重启时端口仍被占用。
        """
        proc = getattr(self, "_service_proc", None)
        if not proc:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self._service_proc = None

    def _update_service_stopped_ui(self) -> None:
        """更新 GUI 显示服务已停止（仅主线程调用）"""
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.btn_restart.config(state="disabled")
        self.lbl_status.config(text="● 服务状态: 已停止", foreground="red")
        self.lbl_uptime.config(text="运行时间: --")

    def _on_stop(self) -> None:
        self._stop_service_process()
        self._update_service_stopped_ui()
        self._append_log("服务已停止")

    def _on_restart(self) -> None:
        self._on_stop()
        self.after(1000, self._on_start)

    def _on_open_web(self) -> None:
        proto = self.proto_var.get()
        port = HTTPS_PORT
        url = f"{proto}://127.0.0.1:{port}"
        webbrowser.open(url)

    def _update_uptime(self) -> None:
        if hasattr(self, "_start_time") and self._start_time:
            elapsed = int(time.time() - self._start_time)
            h, m = divmod(elapsed, 3600)
            m, s = divmod(m, 60)
            self.lbl_uptime.config(text=f"运行时间: {h:02d}:{m:02d}:{s:02d}")
        if self.btn_start.cget("state") == "disabled":
            self.after(1000, self._update_uptime)

    def _append_log(self, text: str) -> None:
        # The GUI log is reserved for actionable failures.  Normal service
        # output (startup, status, successful prints, health checks, etc.) is
        # intentionally kept out of the panel so errors remain visible.
        message = str(text or "")
        error_markers = (
            "error", "exception", "traceback", "failed", "failure",
            "fatal", "critical", "stderr", "错误", "失败", "异常", "报错",
        )
        if not any(marker in message.casefold() for marker in error_markers):
            return
        self.log_text.config(state="normal")
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {message}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ==================================================================
    # 服务器配置 Tab（精简：单一 URL + 绑定状态）
    # ==================================================================

    def _build_server_tab(self) -> None:
        f = self.tab_server

        # -- Token 连接 URL --
        url_frame = ttk.LabelFrame(f, text="Odoo 连接地址", padding=15)
        url_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(url_frame, text="粘贴 Odoo 生成的 Token 连接地址:", font=("Segoe UI", 10)).pack(anchor="w")
        self.token_url_var = StringVar()
        url_entry = ttk.Entry(url_frame, textvariable=self.token_url_var, width=60)
        url_entry.pack(fill="x", pady=(5, 5))
        url_entry.bind("<Control-v>", lambda event: self._paste_token_url(url_entry))
        url_entry.bind("<Control-V>", lambda event: self._paste_token_url(url_entry))
        ttk.Label(url_frame, text="格式: http://地址:端口?token=xxx&db_uuid=xxx&enterprise_code=&db_name=xxx",
                  foreground="gray", font=("Segoe UI", 8)).pack(anchor="w")

        btn_row = ttk.Frame(url_frame)
        btn_row.pack(fill="x", pady=(10, 0))
        ttk.Button(btn_row, text="📋 粘贴 Token", command=lambda: self._paste_token_url(url_entry)).pack(side="left", padx=(0, 8))
        self.btn_connect = ttk.Button(btn_row, text="🔗  连接并绑定", command=self._on_connect_server)
        self.btn_connect.pack(side="left", padx=(0, 8))
        self.btn_disconnect = ttk.Button(btn_row, text="断开", command=self._on_disconnect_server)
        self.btn_disconnect.pack(side="left")

        # -- 绑定状态 --
        status_frame = ttk.LabelFrame(f, text="绑定状态", padding=15)
        status_frame.pack(fill="x", padx=10, pady=10)

        self.lbl_bound_status = ttk.Label(status_frame, text="⏳ 未绑定", font=("Segoe UI", 11, "bold"),
                                           foreground="gray")
        self.lbl_bound_status.pack(anchor="w")

        self.lbl_bound_detail = ttk.Frame(status_frame)
        self.lbl_bound_detail.pack(fill="x", pady=(8, 0))
        self.lbl_bound_url = ttk.Label(self.lbl_bound_detail, text="", font=("Segoe UI", 9))
        self.lbl_bound_url.pack(anchor="w")
        self.lbl_bound_db = ttk.Label(self.lbl_bound_detail, text="", font=("Segoe UI", 9))
        self.lbl_bound_db.pack(anchor="w")
        self.lbl_bound_sync = ttk.Label(self.lbl_bound_detail, text="", font=("Segoe UI", 9))
        self.lbl_bound_sync.pack(anchor="w")

        info_frame = ttk.LabelFrame(f, text="连接信息", padding=12)
        # Keep connection details directly below the Token area so they are
        # visible without scrolling past the service log.
        info_frame.pack(fill="x", padx=10, pady=(0, 10), before=status_frame)
        self.lbl_connection_server = ttk.Label(info_frame, text="服务器地址: -", font=("Segoe UI", 9))
        self.lbl_connection_server.pack(anchor="w")
        self.lbl_connection_db = ttk.Label(info_frame, text="数据库: -", font=("Segoe UI", 9))
        self.lbl_connection_db.pack(anchor="w", pady=(4, 0))
        self.lbl_connection_token = ttk.Label(info_frame, text="Token: -", font=("Segoe UI", 9))
        self.lbl_connection_token.pack(anchor="w", pady=(4, 0))

        # 初始加载绑定状态
        self._refresh_bound_status()

    def _paste_token_url(self, entry: ttk.Entry):
        try:
            value = self.clipboard_get().strip()
        except tk.TclError:
            value = ""
        if value:
            self.token_url_var.set(value)
            entry.icursor("end")
            entry.focus_set()
        return "break"

    @staticmethod
    def _mask_token(token: str) -> str:
        token = str(token or "")
        if len(token) <= 8:
            return "••••••••" if token else "-"
        return f"{token[:4]}{'•' * max(4, len(token) - 8)}{token[-4:]}"

    def _refresh_bound_status(self) -> None:
        """从配置文件读取并显示当前绑定状态"""
        conn = self.config_store.get_connection()
        self.lbl_connection_server.config(text=f"服务器地址: {conn.get('url', '') or '-'}")
        self.lbl_connection_db.config(text=f"数据库: {conn.get('db_name', '') or '-'}")
        self.lbl_connection_token.config(text=f"Token: {self._mask_token(conn.get('token', ''))}")
        if conn.get("connected") and conn.get("url") and conn.get("iot_channel"):
            self.lbl_bound_status.config(text="✅ 已绑定", foreground="green")
            self.lbl_bound_url.config(text=f"服务器: {conn.get('url', '')}")
            self.lbl_bound_db.config(text=f"数据库: {conn.get('db_name', '')}")
            sync_ok = conn.get("last_sync_ok", False)
            sync_msg = conn.get("last_sync_message", "")
            if sync_ok:
                self.lbl_bound_sync.config(text="同步: ✅ 正常", foreground="green")
            elif sync_msg:
                self.lbl_bound_sync.config(text=f"同步: ❌ {sync_msg}", foreground="red")
            else:
                self.lbl_bound_sync.config(text="同步: 等待同步…", foreground="gray")
            self.token_url_var.set(
                f"{conn.get('url', '')}?token={conn.get('token', '')}"
                f"&db_uuid={conn.get('db_uuid', '')}"
                f"&enterprise_code={conn.get('enterprise_code', '')}"
                f"&db_name={conn.get('db_name', '')}"
            )
        elif conn.get("connected") and conn.get("url"):
            self.lbl_bound_status.config(text="⚠ 本地已保存，Odoo 未登记", foreground="darkorange")
            self.lbl_bound_url.config(text=f"服务器: {conn.get('url', '')}")
            self.lbl_bound_db.config(text=f"数据库: {conn.get('db_name', '')}")
            self.lbl_bound_sync.config(text=f"同步: ❌ {conn.get('last_sync_message', '') or '未返回 Odoo iot_channel'}", foreground="red")
        else:
            self.lbl_bound_status.config(text="⏳ 未绑定", foreground="gray")
            self.lbl_bound_url.config(text="")
            self.lbl_bound_db.config(text="")
            self.lbl_bound_sync.config(text="")

    def _on_connect_server(self) -> None:
        """解析 token URL 并绑定"""
        token_url = self.token_url_var.get().strip()
        if not token_url:
            messagebox.showwarning("提示", "请先粘贴 Odoo Token 连接地址")
            return

        try:
            self.btn_connect.config(state="disabled", text="连接中…")
            configured_url = str(self.config_store.get_local_config().get("local_url") or "").rstrip("/")
            local_urls = [configured_url, "https://127.0.0.1:8398"]
            result = None
            errors = []
            for local_url in dict.fromkeys(url for url in local_urls if url):
                try:
                    request = Request(
                        f"{local_url}/api/connect",
                        data=json.dumps({"token_url": token_url}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    context = ssl._create_unverified_context() if local_url.startswith("https://127.0.0.1") else None
                    with urlopen(request, timeout=8, context=context) as response:
                        result = json.loads(response.read().decode("utf-8"))
                    self._append_log(f"本地 IOTBOX 绑定接口已连接: {local_url}")
                    break
                except HTTPError as exc:
                    try:
                        detail = exc.read().decode("utf-8", errors="replace")[:1000]
                    except Exception:
                        detail = str(exc)
                    errors.append(f"{local_url}: HTTP {exc.code} {detail}")
                except Exception as exc:
                    errors.append(f"{local_url}: {exc}")
            if result is None:
                raise ConnectionError("无法连接本地 IOTBOX 绑定接口\n" + "\n".join(errors))
            # 只有本地 IOTBOX 已确认接收绑定后，才持久化 GUI 的绑定状态。
            self.config_store.connect_from_token_url(token_url)
            if result.get("server_connection"):
                self.config_store.update_connection(**result["server_connection"])
            self._refresh_bound_status()
            self._append_log(f"已绑定到服务器: {self.config_store.get_connection().get('url', '')}")
            messagebox.showinfo("成功", "已成功绑定 Odoo 服务器!")
        except ValueError as e:
            messagebox.showerror("绑定失败", str(e))
        except Exception as e:
            messagebox.showerror("错误", f"绑定失败: {e}")
        finally:
            self.btn_connect.config(state="normal", text="🔗  连接并绑定")

    def _on_disconnect_server(self) -> None:
        """解除绑定：先调用本地 IOTBOX 的 /api/disconnect，让运行中的服务清空配置并断开
        websocket；再清空本机持久化配置。与绑定流程（走 /api/connect）保持对称，
        避免出现"GUI 已断开但服务仍在连接旧服务器、配置又被写回"的残留。"""
        if not messagebox.askyesno("确认", "确定要断开与 Odoo 服务器的绑定吗?"):
            return
        try:
            self.btn_disconnect.config(state="disabled", text="断开中…")
            configured_url = str(self.config_store.get_local_config().get("local_url") or "").rstrip("/")
            local_urls = [configured_url, "https://127.0.0.1:8398"]
            errors: list[str] = []
            service_ok = False
            for local_url in dict.fromkeys(url for url in local_urls if url):
                try:
                    request = Request(
                        f"{local_url}/api/disconnect",
                        data=b"",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    context = ssl._create_unverified_context() if local_url.startswith("https://127.0.0.1") else None
                    with urlopen(request, timeout=8, context=context) as response:
                        json.loads(response.read().decode("utf-8"))
                    self._append_log(f"本地 IOTBOX 已确认断开: {local_url}")
                    service_ok = True
                    break
                except HTTPError as exc:
                    try:
                        detail = exc.read().decode("utf-8", errors="replace")[:1000]
                    except Exception:
                        detail = str(exc)
                    errors.append(f"{local_url}: HTTP {exc.code} {detail}")
                except Exception as exc:
                    errors.append(f"{local_url}: {exc}")
            # 无论服务端接口是否可达，本机配置一律清空，保证 GUI 状态与磁盘一致。
            self.config_store.reset_connection()
            self._refresh_bound_status()
            self.token_url_var.set("")
            if service_ok:
                self._append_log("已解除服务器绑定，连接配置已自动清空")
            else:
                self._append_log("已解除服务器绑定（配置已本地清空），但未能通知本地服务: " + " | ".join(errors))
        except Exception as e:
            messagebox.showerror("错误", f"断开失败: {e}")
        finally:
            self.btn_disconnect.config(state="normal", text="断开")

    # ==================================================================
    # 电子秤配置 Tab
    # ==================================================================

    def _build_scale_tab(self) -> None:
        f = self.tab_scale

        # 预设品牌
        brand_frame = ttk.LabelFrame(f, text="电子秤品牌预设", padding=10)
        brand_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(brand_frame, text="选择品牌快速应用预设参数:", font=("Segoe UI", 10)).pack(anchor="w")

        self.scale_brand_var = StringVar(value=list(SCALE_PRESETS.keys())[0])
        cb = ttk.Combobox(brand_frame, textvariable=self.scale_brand_var,
                           values=list(SCALE_PRESETS.keys()), state="readonly", width=30)
        cb.pack(pady=5)
        cb.bind("<<ComboboxSelected>>", lambda e: self._apply_preset())

        ttk.Button(brand_frame, text="应用预设", command=self._apply_preset).pack(pady=5)

        # 串口设置
        port_frame = ttk.LabelFrame(f, text="串口设置", padding=10)
        port_frame.pack(fill="x", padx=10, pady=10)

        row1 = ttk.Frame(port_frame)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="串口:", width=14).pack(side="left")
        self.scale_port_var = StringVar(value=DEFAULT_SCALE_PORT)
        self.port_combo = ttk.Combobox(row1, textvariable=self.scale_port_var, width=20)
        self.port_combo.pack(side="left")
        ttk.Button(row1, text="刷新串口", command=self._refresh_ports).pack(side="left", padx=5)
        self.lbl_port_status = ttk.Label(row1, text="", foreground="gray")
        self.lbl_port_status.pack(side="left", padx=5)

        row2 = ttk.Frame(port_frame)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="波特率:", width=14).pack(side="left")
        self.scale_baudrate_var = IntVar(value=DEFAULT_SCALE_BAUDRATE)
        ttk.Combobox(row2, textvariable=self.scale_baudrate_var,
                                values=[1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200],
                                width=18).pack(side="left")

        row3 = ttk.Frame(port_frame)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="超时(秒):", width=14).pack(side="left")
        self.scale_timeout_var = StringVar(value="1.2")
        ttk.Entry(row3, textvariable=self.scale_timeout_var, width=20).pack(side="left")

        row4 = ttk.Frame(port_frame)
        row4.pack(fill="x", pady=2)
        ttk.Label(row4, text="指令间隔(秒):", width=14).pack(side="left")
        self.scale_inter_command_delay_var = StringVar(value="0.05")
        ttk.Entry(row4, textvariable=self.scale_inter_command_delay_var, width=20).pack(side="left")
        ttk.Label(row4, text="主动探测之间的最小间隔", foreground="gray").pack(side="left", padx=5)

        # 按钮
        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill="x", padx=10, pady=10)
        ttk.Button(btn_frame, text="💾  保存电子秤配置", command=self._on_save_scale).pack(side="left")

    # ------------------------------------------------------------------

    def _build_vfd_tab(self) -> None:
        frame = self.tab_vfd
        local = self.config_store.get_local_config()
        self.vfd_enabled_var = BooleanVar(value=bool(local.get("vfd_enabled", False)))
        self.vfd_port_var = StringVar(value=str(local.get("vfd_port", "")))
        self.vfd_baudrate_var = IntVar(value=int(local.get("vfd_baudrate", 9600) or 9600))
        self.vfd_protocol_var = StringVar(value=str(local.get("vfd_protocol", "cd5220") or "cd5220"))
        self.vfd_status_var = StringVar(value="VFD COM port is managed by IoTBOX")

        ttk.Checkbutton(frame, text="Enable VFD customer display", variable=self.vfd_enabled_var).pack(
            anchor="w", padx=12, pady=12
        )
        form = ttk.LabelFrame(frame, text="VFD serial settings", padding=10)
        form.pack(fill="x", padx=12, pady=5)
        row = ttk.Frame(form)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="COM port", width=16).pack(side="left")
        self.vfd_port_combo = ttk.Combobox(row, textvariable=self.vfd_port_var, width=20)
        self.vfd_port_combo.pack(side="left")
        ttk.Button(row, text="Refresh", command=self._refresh_ports).pack(side="left", padx=8)
        row = ttk.Frame(form)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Baudrate", width=16).pack(side="left")
        ttk.Combobox(row, textvariable=self.vfd_baudrate_var, values=(9600, 19200, 38400), width=18).pack(side="left")
        row = ttk.Frame(form)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Protocol", width=16).pack(side="left")
        ttk.Combobox(row, textvariable=self.vfd_protocol_var, values=("cd5220", "plain"), state="readonly", width=18).pack(side="left")
        ttk.Button(frame, text="Save VFD configuration", command=self._on_save_vfd).pack(anchor="w", padx=12, pady=10)
        ttk.Label(frame, textvariable=self.vfd_status_var, foreground="blue").pack(anchor="w", padx=12)

    def _on_save_vfd(self) -> None:
        try:
            values = {
                "vfd_enabled": bool(self.vfd_enabled_var.get()),
                "vfd_port": self.vfd_port_var.get().strip(),
                "vfd_baudrate": int(self.vfd_baudrate_var.get() or 9600),
                "vfd_protocol": self.vfd_protocol_var.get().strip().lower() or "cd5220",
            }
            self.config_store.update_local_config(**values)
            local_url = str(self.config_store.get_local_config().get("local_url") or "https://127.0.0.1:8398")
            request = Request(
                local_url.rstrip("/") + "/api/vfd/save_config",
                data=json.dumps(values).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                if response.status >= 400:
                    raise RuntimeError(f"IOTBOX rejected VFD configuration: HTTP {response.status}")
            self.vfd_status_var.set(f"Saved: {values['vfd_port'] or '(no COM port)'}")
            self._append_log(f"VFD configuration saved (port={values['vfd_port']})")
        except Exception as exc:
            self.vfd_status_var.set(f"Save failed: {exc}")
            messagebox.showerror("VFD configuration", f"Save failed: {exc}")

    def _launch_customer_display_screen(self) -> None:
        self._close_customer_display_screen()
        # The customer window is served by this IoT Box.  POS updates arrive
        # through the native display device action, never through a manually
        # configured Odoo customer-display URL.
        proto = self.proto_var.get().strip().lower()
        if proto not in {"http", "https"}:
            proto = "https"
        port = HTTPS_PORT
        url = f"{proto}://127.0.0.1:{port}/customer-display"
        try:
            from ctypes import wintypes

            monitors = []
            callback = ctypes.WINFUNCTYPE(
                wintypes.BOOL,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(wintypes.RECT),
                wintypes.LPARAM,
            )

            def enum_proc(_monitor, _dc, rect, _data):
                bounds = rect.contents
                monitors.append((bounds.left, bounds.top, bounds.right, bounds.bottom))
                return True
            ctypes.windll.user32.EnumDisplayMonitors(None, None, callback(enum_proc), 0)
            if len(monitors) < 2:
                self._append_log("未检测到第二个显示器；客显将在连接后下次启动时自动打开")
                return
            left, top, right, bottom = monitors[1]
            width, height = right - left, bottom - top
            if getattr(sys, "frozen", False):
                helper = Path(sys.executable).resolve().parent / "customer_display_app.exe"
                if not helper.exists():
                    raise FileNotFoundError(f"客户屏程序不存在: {helper}")
                command = [str(helper)]
            else:
                helper = BASE_DIR / "customer_display_app.py"
                command = [sys.executable, str(helper)]
            self.customer_display_proc = subprocess.Popen(
                command + ["--url", url, "--x", str(left), "--y", str(top),
                 "--width", str(width), "--height", str(height)],
                cwd=str(BASE_DIR),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._append_log(f"原生 POS 客显已在第二屏启动: {right-left}x{bottom-top}")
        except Exception as exc:
            self._append_log(f"启动原生 POS 客显失败: {exc}")

    def _close_customer_display_screen(self) -> None:
        self._stop_all_customer_display_processes()
        process = getattr(self, "customer_display_proc", None)
        if process is not None and process.poll() is None:
            process.terminate()
        self.customer_display_proc = None

    def _stop_all_customer_display_processes(self) -> None:
        """清理可能由旧版 GUI 留下的重复客户屏进程。"""
        try:
            subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -eq 'customer_display_app.exe' -or $_.CommandLine -match 'customer_display_app.py' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
                ],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5,
            )
        except Exception:
            pass

    def _refresh_ports(self) -> None:
        """扫描可用串口"""
        ports = self._list_serial_ports()
        self.port_combo["values"] = ports
        if hasattr(self, "vfd_port_combo"):
            self.vfd_port_combo["values"] = ports
        if ports:
            self.lbl_port_status.config(text=f"找到 {len(ports)} 个串口", foreground="green")
        else:
            self.lbl_port_status.config(text="未找到串口", foreground="red")

    @staticmethod
    def _normalize_windows_com_port(value: str) -> str:
        match = re.fullmatch(r"\s*COM\s*(\d+)\s*", str(value), flags=re.IGNORECASE)
        if not match or int(match.group(1)) < 1:
            return ""
        return f"COM{int(match.group(1))}"

    @staticmethod
    def _serial_port_sort_key(port: str) -> tuple[int, str]:
        match = re.search(r"(\d+)$", port)
        return (int(match.group(1)) if match else 9999, port.upper())

    @staticmethod
    def _list_serial_ports() -> list[str]:
        """列出系统可用串口（pyserial 的 comports() 已足够可靠，无需逐个探测）"""
        try:
            import serial.tools.list_ports
            ports = [p.device for p in serial.tools.list_ports.comports()]
            if ports:
                return sorted(set(ports), key=str.upper)
        except ImportError:
            pass
        # 某些 USB 虚拟串口（例如 VfiUniUSBPort）不会被 pyserial 枚举，
        # 但 Windows 注册表仍会登记实际 COM 号。
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
                ports = []
                for index in range(winreg.QueryInfoKey(key)[1]):
                    _, value, _ = winreg.EnumValue(key, index)
                    ports.append(str(value))
                return sorted(set(ports), key=str.upper)
        except (OSError, ImportError):
            return []

    def _apply_preset(self) -> None:
        brand_label = self.scale_brand_var.get()
        preset = SCALE_PRESETS.get(brand_label)
        if not preset:
            return
        self.scale_baudrate_var.set(preset.get("baudrate", 9600))
        self.scale_timeout_var.set(str(preset.get("timeout", 1.2)))
        self.scale_inter_command_delay_var.set(str(preset.get("inter_command_delay", 0.05)))
        self._refresh_ports()
        self._append_log(f"已应用 {brand_label} 预设 (brand={preset.get('brand')})")

    def _on_save_scale(self) -> None:
        """保存电子秤配置到 config_store（直接写入 local_config，runtime 立即生效）"""
        try:
            brand_label = self.scale_brand_var.get()
            preset = SCALE_PRESETS.get(brand_label, {})
            brand_value = preset.get("brand", "zfoc")
            self.config_store.update_local_config(
                scale_port=self.scale_port_var.get().strip(),
                scale_baudrate=int(self.scale_baudrate_var.get()),
                scale_timeout=float(self.scale_timeout_var.get() or "1.2"),
                scale_inter_command_delay=float(self.scale_inter_command_delay_var.get() or "0.05"),
                scale_brand=brand_value,
            )
            local_url = str(self.config_store.get_local_config().get("local_url") or "https://127.0.0.1:8398")
            request = Request(
                local_url.rstrip("/") + "/api/scale/save_config",
                data=json.dumps({
                    "scale_port": self.scale_port_var.get().strip(),
                    "scale_baudrate": int(self.scale_baudrate_var.get()),
                    "scale_timeout": float(self.scale_timeout_var.get() or "1.2"),
                    "scale_inter_command_delay": float(self.scale_inter_command_delay_var.get() or "0.05"),
                    "scale_brand": brand_value,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                if response.status >= 400:
                    raise RuntimeError(f"IOTBOX rejected scale configuration: HTTP {response.status}")
            self._append_log(f"电子秤配置已保存 (port={self.scale_port_var.get()}, brand={brand_value})")
            messagebox.showinfo("成功", "电子秤配置已保存!\n请重启服务使更改生效。")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败: {e}")

    def _on_test_scale(self) -> None:
        port = self.scale_port_var.get()
        baudrate = self.scale_baudrate_var.get()
        self._append_log(f"测试连接 {port} @ {baudrate}...")

        def do_test() -> None:
            try:
                import serial
                ser = serial.Serial(
                    port=port,
                    baudrate=baudrate,
                    timeout=2,
                )
                raw = ser.readline()
                ser.close()
                text = raw.decode("latin-1", errors="replace").strip()
                self.after(0, self._append_log, f"收到: {text}")
                self.after(0, messagebox.showinfo, "测试结果", f"连接成功!\n读取到数据:\n{text}")
            except Exception as e:
                self.after(0, self._append_log, f"测试失败: {e}")
                self.after(0, messagebox.showerror, "测试失败", str(e))

        threading.Thread(target=do_test, daemon=True).start()

    # ==================================================================
    # 关于 & 更新 Tab
    # ==================================================================

    def _build_about_tab(self) -> None:
        f = self.tab_about

        # 版本信息
        ver_frame = ttk.LabelFrame(f, text="版本信息", padding=15)
        ver_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(ver_frame, text="IoT Box Desktop", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(ver_frame, text=f"版本: {APP_VERSION}", font=("Segoe UI", 10)).pack(anchor="w", pady=(5, 0))
        ttk.Label(ver_frame, text=f"Python: {platform.python_version()}", font=("Segoe UI", 10)).pack(anchor="w")
        ttk.Label(ver_frame, text=f"系统: {platform.platform()}", font=("Segoe UI", 10)).pack(anchor="w")

        # 更新设置
        update_frame = ttk.LabelFrame(f, text="在线更新", padding=10)
        update_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(update_frame, text="更新源 URL:", font=("Segoe UI", 10)).pack(anchor="w")
        self.update_url_var = StringVar(value=DEFAULT_UPDATE_MANIFEST_URL)
        ttk.Label(update_frame, text="填写自定义更新清单 URL（JSON）；留空则使用下方 GitHub Releases 模式",
                   foreground="gray").pack(anchor="w")

        # GitHub 模式
        self.gh_owner_var = StringVar(value="mikokeyu1986-arch")
        self.gh_repo_var = StringVar(value="iot_box_comercia")

        self.gh_prerelease_var = BooleanVar(value=False)
        ttk.Checkbutton(update_frame, text="包含预发布版本", variable=self.gh_prerelease_var).pack(anchor="w", pady=(3, 0))
        self.auto_update_var = BooleanVar(value=True)
        ttk.Checkbutton(update_frame, text="自动检测并安装新版本（每10分钟）", variable=self.auto_update_var).pack(anchor="w", pady=(3, 0))

        # 按钮
        btn_frame = ttk.Frame(update_frame)
        btn_frame.pack(fill="x", pady=10)
        self.btn_check_update = ttk.Button(btn_frame, text="🔍  检查更新", command=self._on_check_update)
        self.btn_check_update.pack(side="left", padx=(0, 8))

        self.btn_download = ttk.Button(btn_frame, text="⬇  下载并安装", command=self._on_download_update,
                                        state="disabled")
        self.btn_download.pack(side="left", padx=(0, 8))

        # 进度条
        self.progress_var = IntVar(value=0)
        self.progress_bar = ttk.Progressbar(update_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", pady=(5, 0))

        # 更新日志
        self.lbl_update_status = ttk.Label(update_frame, text="", foreground="blue")
        self.lbl_update_status.pack(anchor="w", pady=(5, 0))

        # 备份管理
        backup_frame = ttk.LabelFrame(f, text="备份管理", padding=10)
        backup_frame.pack(fill="x", padx=10, pady=10)

        btn_row = ttk.Frame(backup_frame)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="📋  查看备份", command=self._on_list_backups).pack(side="left")
        ttk.Button(btn_row, text="🗑  删除最旧备份", command=self._on_delete_oldest_backup).pack(side="left", padx=5)
        self.lbl_backup_list = ttk.Label(backup_frame, text="", font=("Consolas", 9))
        self.lbl_backup_list.pack(anchor="w", pady=(5, 0))

    # ------------------------------------------------------------------

    def _get_update_manager(self) -> UpdateManager:
        """根据当前设置构建 UpdateManager（自定义 URL 优先于 GitHub 模式）"""
        custom_url = self.update_url_var.get().strip()
        gh_owner = self.gh_owner_var.get().strip()
        gh_repo = self.gh_repo_var.get().strip()

        # 自定义 URL 优先
        if custom_url:
            source = UpdateSource(manifest_url=custom_url)
        elif gh_owner and gh_repo:
            source = GitHubUpdateSource(
                owner=gh_owner,
                repo=gh_repo,
                use_prerelease=self.gh_prerelease_var.get(),
            )
        else:
            # 无任何更新源配置，使用默认 URL
            source = UpdateSource(manifest_url=DEFAULT_UPDATE_MANIFEST_URL)

        return UpdateManager(
            current_version=APP_VERSION,
            base_dir=BASE_DIR,
            source=source,
        )

    def _on_check_update(self) -> None:
        self.btn_check_update.config(state="disabled", text="检查中…")
        self.lbl_update_status.config(text="正在连接更新服务器…")
        self._latest_version: Any = None

        def do_check() -> None:
            try:
                mgr = self._get_update_manager()
                result = mgr.check_for_updates()
            except Exception as exc:
                from app.updater import UpdateResult
                result = UpdateResult(False, f"检查更新失败: {exc}", current_version=APP_VERSION)
            self.after(0, self._on_check_done, result)

        threading.Thread(target=do_check, daemon=True).start()

    def _auto_update_check(self) -> None:
        if self.auto_update_var.get() and not getattr(self, "_update_in_progress", False):
            self._auto_update_checking = True
            self._on_check_update()
        self.after(600000, self._auto_update_check)

    def _on_check_done(self, result: Any) -> None:
        self.btn_check_update.config(state="normal", text="🔍  检查更新")
        if not result.success:
            self.lbl_update_status.config(text=f"错误: {result.message}", foreground="red")
            return

        if result.can_update:
            self.lbl_update_status.config(
                text=f"发现新版本: {result.latest_version}\n{result.message}",
                foreground="green",
            )
            self.btn_download.config(state="normal")
            self._latest_version = result.details
            if getattr(self, "_auto_update_checking", False):
                self._auto_update_checking = False
                self._update_in_progress = True
                self._on_download_update()
        else:
            self.lbl_update_status.config(
                text=f"已是最新版本 ({result.current_version})",
                foreground="blue",
            )
            self.btn_download.config(state="disabled")
            self._auto_update_checking = False

    def _on_download_update(self) -> None:
        if not hasattr(self, "_latest_version") or not self._latest_version:
            return

        from app.updater import VersionInfo
        version_info = VersionInfo(self._latest_version)
        version = version_info.version
        self.btn_download.config(state="disabled", text="下载中…")
        self.progress_var.set(0)

        def do_download() -> None:
            mgr = self._get_update_manager()
            try:
                pkg_path = mgr.download_update(
                    version_info,
                    progress_callback=lambda pct: self.after(0, self.progress_var.set, pct),
                )
                self.after(0, self._on_download_done, pkg_path, mgr, version)
            except Exception as e:
                self.after(0, self._on_download_error, str(e))

        threading.Thread(target=do_download, daemon=True).start()

    def _on_download_done(self, pkg_path: Path, mgr: UpdateManager, version: str) -> None:
        """下载完成：在后台线程中停止服务 → 安装 → 通知主线程。"""
        self.lbl_update_status.config(text="下载完成，正在停止服务并安装…", foreground="blue")
        self.progress_var.set(0)

        def do_install() -> None:
            # 安装前停止服务进程（在子线程中操作，避免阻塞 GUI），
            # 否则 Windows 上可能因文件锁导致覆盖失败或运行中的服务加载半更新的模块。
            self._stop_service_process()
            self.after(0, self._update_service_stopped_ui)
            self.after(0, lambda: self._append_log("服务已停止，正在安装更新…"))
            # 等待端口释放
            time.sleep(0.5)
            try:
                result = mgr.install_update(pkg_path, version)
            except Exception as exc:
                from app.updater import UpdateResult
                result = UpdateResult(False, f"安装失败: {exc}")
            self.after(0, self._on_install_done, result)

        threading.Thread(target=do_install, daemon=True).start()

    def _on_install_done(self, result: Any) -> None:
        """安装完成（主线程）：提示用户并选择是否重启整个 GUI。"""
        self.btn_download.config(state="disabled", text="⬇  下载并安装")
        self.progress_var.set(0)
        if result.success:
            self.lbl_update_status.config(text=result.message, foreground="green")
            if getattr(self, "_update_in_progress", False):
                self._update_in_progress = False
                self.after(1000, self._restart_gui)
                return
            if messagebox.askyesno("更新完成", f"{result.message}\n\n需要重启程序使更新生效，是否现在重启？"):
                self._restart_gui()
            else:
                # 用户选择稍后重启：重新启动服务，让现有版本继续可用
                self.after(500, self._on_start)
        else:
            self.lbl_update_status.config(text=result.message, foreground="red")
            # 安装失败：重新启动旧版本服务
            self.after(500, self._on_start)

    @staticmethod
    def _resolve_pythonw(exe: str) -> str:
        """尝试把 python.exe 换成 pythonw.exe（不弹 CMD 黑框），找不到则回退原 exe。"""
        if not exe or not exe.lower().endswith("python.exe"):
            return exe
        candidate = exe[:-len("python.exe")] + "pythonw.exe"
        return candidate if os.path.isfile(candidate) else exe

    def _restart_gui(self) -> None:
        """重启整个 GUI 程序（不仅是服务），让更新的代码生效。"""
        self._stop_service_process()
        time.sleep(1.0)
        # 优先用 pythonw.exe 启动新 GUI，避免再弹一个 CMD 窗口
        gui_py = self._resolve_pythonw(sys.executable)
        subprocess.Popen(
            [gui_py, str(BASE_DIR / "gui_app.py")],
            cwd=str(BASE_DIR),
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
        )
        # 退出当前 GUI（销毁根窗口，终止事件循环）
        try:
            master = self.master
            if master is not None:
                master.destroy()
        except Exception:
            os._exit(0)

    def _on_download_error(self, error: str) -> None:
        self.lbl_update_status.config(text=f"下载失败: {error}", foreground="red")
        self.btn_download.config(state="normal", text="⬇  下载并安装")
        self.progress_var.set(0)

    def _on_list_backups(self) -> None:
        mgr = UpdateManager(current_version=APP_VERSION, base_dir=BASE_DIR)
        backups = mgr.get_backups()
        if not backups:
            self.lbl_backup_list.config(text="(无备份)")
            return
        lines = []
        for b in backups[:10]:
            lines.append(f"  {b['created']}  {b['name']}  ({b['size_mb']} MB)")
        self.lbl_backup_list.config(text="备份列表:\n" + "\n".join(lines))

    def _on_delete_oldest_backup(self) -> None:
        mgr = UpdateManager(current_version=APP_VERSION, base_dir=BASE_DIR)
        backups = mgr.get_backups()
        if not backups:
            messagebox.showinfo("备份", "没有备份可删除")
            return
        oldest = backups[-1]
        path = Path(oldest["path"])
        if path.exists():
            path.unlink()
            self.lbl_backup_list.config(text=f"已删除: {oldest['name']}")
            self._on_list_backups()

    # ==================================================================
    # 配置加载/保存
    # ==================================================================

    def _load_config(self) -> None:
        """从 config_store 加载电子秤设置（与 runtime 读取同一份 local_config）"""
        local = self.config_store.get_local_config()
        saved_protocol = str(local.get("service_protocol") or "https").strip().lower()
        saved_protocol = saved_protocol if saved_protocol in {"http", "https"} else "https"
        self.proto_var.set(saved_protocol)
        self._activate_protocol_config(saved_protocol)
        local = self.config_store.get_local_config()
        self.scale_port_var.set(local.get("scale_port", DEFAULT_SCALE_PORT))
        self.scale_baudrate_var.set(local.get("scale_baudrate", DEFAULT_SCALE_BAUDRATE))
        self.scale_timeout_var.set(str(local.get("scale_timeout", 1.2)))
        self.scale_inter_command_delay_var.set(str(local.get("scale_inter_command_delay", 0.05)))
        self.vfd_enabled_var.set(bool(local.get("vfd_enabled", False)))
        self.vfd_port_var.set(str(local.get("vfd_port", "")))
        self.vfd_baudrate_var.set(int(local.get("vfd_baudrate", 9600) or 9600))
        self.vfd_protocol_var.set(str(local.get("vfd_protocol", "cd5220") or "cd5220"))
        self.auto_start_var.set(bool(local.get("auto_start_service", True)))

        # 根据已保存的 brand 选中对应预设
        saved_brand = str(local.get("scale_brand") or "zfoc").strip().lower()
        matched_label = None
        for label, preset in SCALE_PRESETS.items():
            if preset.get("brand") == saved_brand:
                matched_label = label
                break
        self.scale_brand_var.set(matched_label or list(SCALE_PRESETS.keys())[0])

        self._refresh_ports()


# ============================================================================
# 托盘应用
# ============================================================================


class TrayApplication:
    """系统托盘 + 设置窗口管理器"""

    def __init__(self) -> None:
        self.config_store = ConfigStore(CONFIG_FILE)
        self.settings_window: SettingsWindow | None = None
        self._tray_icon = None
        self.start_minimized = "--minimized" in sys.argv

    # ------------------------------------------------------------------

    def run(self) -> None:
        """启动托盘（尝试 pystray，失败则降级为纯窗口模式）"""
        try:
            import pystray
            self._run_tray(pystray)
        except ImportError:
            _logger.warning("pystray 未安装，使用窗口模式")
            self._run_window_only()
        except Exception:
            _logger.exception("托盘模式启动失败，降级为纯窗口模式")
            self._run_window_only()

    # ------------------------------------------------------------------

    def _run_tray(self, pystray) -> None:
        """带系统托盘的完整模式（托盘在后台线程，GUI 在主线程）"""
        import threading

        # 创建 tk 根窗口（必须在主线程）
        root = Tk()
        root.withdraw()
        self._root = root
        _install_show_settings_listener(self, root)

        def on_quit(icon, item):
            icon.stop()
            root.after(0, self._shutdown, root)

        def on_settings(icon, item):
            root.after(0, self._show_settings, root)

        def on_open_web(icon, item):
            local = self.config_store.get_local_config()
            protocol = str(local.get("service_protocol") or "https").strip().lower()
            protocol = protocol if protocol in {"http", "https"} else "https"
            port = HTTPS_PORT
            webbrowser.open(f"{protocol}://127.0.0.1:{port}")

        # 图标（加载失败时降级为纯窗口模式，避免静默崩溃）
        try:
            icon_image = self._load_icon(pystray)
        except Exception:
            _logger.exception("托盘图标加载失败，降级为纯窗口模式")
            try:
                root.destroy()
            except Exception:
                pass
            self._run_window_only()
            return

        menu = pystray.Menu(
            pystray.MenuItem("打开设置", on_settings, default=True),
            pystray.MenuItem("打开网页界面", on_open_web),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", on_quit),
        )

        self._tray_icon = pystray.Icon(
            "iot_box",
            icon_image,
            APP_NAME,
            menu,
        )

        # 托盘放到后台线程运行。某些精简版 Windows、远程桌面会话或安全
        # 软件会让通知区域后端在启动后立即退出；若此时应用由开机项以
        # --minimized 启动，过去会留下一个既没有托盘图标也没有窗口的进程。
        # 后台后端退出时恢复设置窗口，确保用户始终有入口。
        def run_tray() -> None:
            try:
                self._tray_icon.run()
            except Exception:
                _logger.exception("System tray backend stopped unexpectedly")
            finally:
                try:
                    root.after(0, self._recover_from_tray_failure, root)
                except tk.TclError:
                    pass

        tray_thread = threading.Thread(target=run_tray, daemon=True)
        tray_thread.start()

        # 显示设置窗口
        if self.start_minimized:
            self.settings_window = SettingsWindow(root, self.config_store)
            self.settings_window.enable_minimize_to_tray()
            self.settings_window.withdraw()
        else:
            self._show_settings(master=root)

        # 主线程运行 tkinter 事件循环
        root.mainloop()

    def _shutdown(self, root: Tk) -> None:
        """Stop child processes owned by the GUI before leaving the tray app."""
        self._shutting_down = True
        window = self.settings_window
        try:
            window_exists = window is not None and bool(window.winfo_exists())
        except tk.TclError:
            window_exists = False
        if window_exists:
            window._close_customer_display_screen()
            window._stop_service_process()
        root.destroy()

    def _recover_from_tray_failure(self, root: Tk) -> None:
        """Show the window if the notification-area backend is unavailable."""
        if getattr(self, "_shutting_down", False):
            return
        _logger.warning("System tray is unavailable; restoring the settings window")
        self._tray_icon = None
        self._show_settings(master=root)

    # ------------------------------------------------------------------

    def _run_window_only(self) -> None:
        """降级模式：无托盘，纯窗口"""
        root = Tk()
        root.withdraw()  # 隐藏根窗口
        _install_show_settings_listener(self, root)
        # Without a tray there is no way to restore a minimized window.
        # Always display it, even when Windows launched the app at login.
        self._show_settings(master=root)
        root.mainloop()

    # ------------------------------------------------------------------

    def _show_settings(self, master: Tk | None = None) -> None:
        if self.settings_window is None or not self.settings_window.winfo_exists():
            self.settings_window = SettingsWindow(master, self.config_store)
        if self._tray_icon is not None:
            self.settings_window.enable_minimize_to_tray()
        self.settings_window.deiconify()
        self.settings_window.lift()
        self.settings_window.focus_force()

    # ------------------------------------------------------------------

    def _load_icon(self, pystray):
        """加载托盘图标（PIL 缺失或损坏时抛异常，由调用方降级处理）"""
        ico_path = BASE_DIR / "web" / "favicon.ico"
        try:
            if ico_path.exists():
                from PIL import Image
                img = Image.open(ico_path)
                return img.resize((64, 64), Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC)

            # 创建一个简单的内置图标（蓝色方块）
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(67, 97, 238))
            draw.text((20, 18), "IoT", fill=(255, 255, 255))
            return img
        except Exception:
            _logger.exception("无法加载托盘图标（PIL 可能缺失或损坏）")
            raise


# ============================================================================
# 入口
# ============================================================================


def _ensure_windows_startup_entry() -> None:
    """Start the GUI in the notification area when Windows logs in."""
    if platform.system() != "Windows":
        return
    try:
        import winreg

        # The installed application is a self-contained executable.  Starting
        # it directly avoids a fragile external VBS launcher that is not part
        # of the installer payload.
        if getattr(sys, "frozen", False):
            command = f'"{Path(sys.executable).resolve()}" --minimized'
        else:
            launcher = BASE_DIR / "start_gui_hidden.vbs"
            command = f'wscript.exe "{launcher}" --minimized'
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "IoTBoxDesktop", 0, winreg.REG_SZ, command)
    except Exception:
        _logger.exception("Unable to register Windows startup entry")


def _install_show_settings_listener(app: TrayApplication, root: Tk) -> None:
    """Display the existing tray instance when its shortcut is run again."""
    if platform.system() != "Windows" or not _IOTBOX_SHOW_SETTINGS_EVENT:
        return

    def check_show_request() -> None:
        try:
            if ctypes.windll.kernel32.WaitForSingleObject(_IOTBOX_SHOW_SETTINGS_EVENT, 0) == 0:
                app._show_settings(master=root)
        finally:
            try:
                root.after(250, check_show_request)
            except tk.TclError:
                pass

    root.after(250, check_show_request)


def _setup_logging() -> None:
    """把日志写入安装目录 logs/gui.log，方便现场排错。"""
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        log_dir = INSTALL_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "gui.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=[file_handler],
        )
    except OSError:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )


def _check_runtime_health() -> list[str]:
    """启动前健康检查，返回发现的问题列表（空列表表示正常）。"""
    problems: list[str] = []
    try:
        probe = INSTALL_DIR / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        problems.append(f"安装目录不可写（{exc}）：{INSTALL_DIR}")
    if getattr(sys, "frozen", False):
        internal = INSTALL_DIR / "_internal"
        try:
            if not internal.is_dir() or not any(internal.iterdir()):
                problems.append(f"运行时目录缺失或不完整：{internal}")
        except OSError as exc:
            problems.append(f"无法访问运行时目录（{exc}）：{internal}")
    return problems


def _write_crash_log(exc: BaseException) -> Path | None:
    """把启动异常写入安装目录日志，返回日志文件路径（写入失败返回 None）。"""
    try:
        log_dir = INSTALL_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "gui_crash.log"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 启动失败: {exc!r}\n")
            traceback.print_exc(file=handle)
        return log_path
    except Exception:
        return None


def _show_startup_error(exc: BaseException, log_path: Path | None) -> None:
    """用 Win32 弹窗提示启动失败（不依赖 tkinter，窗口初始化前也能用）。"""
    message = f"IoT Box 启动失败：\n\n{exc}"
    if log_path is not None:
        message += f"\n\n详细信息已写入：\n{log_path}"
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "IoT Box 启动错误", 0x10)
    except Exception:
        pass


def _show_runtime_health_warning(problems: list[str]) -> None:
    """启动健康检查发现问题时给出提示（不阻止启动）。"""
    try:
        message = "检测到以下运行环境问题，可能影响 IOTBOX 正常运行：\n\n" + "\n".join(f"• {p}" for p in problems)
        ctypes.windll.user32.MessageBoxW(None, message, "IoT Box 环境提示", 0x30)
    except Exception:
        pass


def main() -> None:
    try:
        _main_impl()
    except Exception as exc:
        log_path = _write_crash_log(exc)
        _show_startup_error(exc, log_path)
        sys.exit(1)


def _main_impl() -> None:
    """GUI 模式入口（异常由 main() 统一捕获，避免静默退出）。"""
    global _IOTBOX_GUI_MUTEX, _IOTBOX_SHOW_SETTINGS_EVENT
    _setup_logging()
    _IOTBOX_GUI_MUTEX = (
        # The desktop app belongs to the logged-in user.  A Global mutex also
        # spans Remote Desktop / other Windows accounts and can make a normal
        # user believe the UI cannot be opened when another session owns it.
        ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\IOTBOX_GUI_SINGLE_INSTANCE")
        if platform.system() == "Windows" else None
    )
    already_running = platform.system() == "Windows" and ctypes.windll.kernel32.GetLastError() == 183
    _IOTBOX_SHOW_SETTINGS_EVENT = (
        ctypes.windll.kernel32.CreateEventW(None, False, False, "Local\\IOTBOX_GUI_SHOW_SETTINGS")
        if platform.system() == "Windows" else None
    )
    if already_running:
        # Login starts this app minimized. A later shortcut click should show
        # it instead of silently exiting because of the single-instance lock.
        ctypes.windll.kernel32.SetEvent(_IOTBOX_SHOW_SETTINGS_EVENT)
        _logger.info("已有实例在运行，发送显示设置请求")
        return

    health_problems = _check_runtime_health()
    for problem in health_problems:
        _logger.warning("运行环境健康检查: %s", problem)
    if health_problems:
        _show_runtime_health_warning(health_problems)

    _logger.info("IoT Box Desktop 启动 (version=%s, frozen=%s)", APP_VERSION, getattr(sys, "frozen", False))
    _ensure_windows_startup_entry()
    app = TrayApplication()
    app.run()
    _logger.info("IoT Box Desktop 已退出")


if __name__ == "__main__":
    main()
