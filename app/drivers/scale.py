from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Any, Callable

from ..serial_utils import SharedSerialPort

_logger = logging.getLogger(__name__)

ZFOC_ENQ = 0x05
ZFOC_ACK = 0x06
ZFOC_DC1 = 0x11
EOT = 0x04
STX = 0x02
ETX = 0x03
CR = 0x0D
WAKE_BYTE = ord("W")

KNOWN_UNITS = {"TJ", "TL", "SJ", "LB", "KG", "G"}

# 预编译正则：流式解析在热路径上每帧都会调用，避免 re 模块内部缓存查找开销。
_RE_EPELSA_FRAME = re.compile(r"(-?\d+\.\d+)\r")
_RE_ZFOC_STX_ETX = re.compile(r"\x02([ -~]{1,32})\r\x03")
_RE_ZFOC_DIGITS = re.compile(r"\x02([0-9 ]{4,8})\r")
_RE_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def _parse_zfoc_weight_packet(payload: bytes) -> float | None:
    if not payload or len(payload) < 9:
        return None
    if payload[0] != 0x01 or payload[1] != 0x02:
        return None
    if payload[-2] != 0x03 or payload[-1] != 0x04:
        return None

    bcc_index = len(payload) - 3
    data_bytes = payload[2:bcc_index]
    if not data_bytes:
        return None

    bcc = payload[bcc_index]
    computed = 0
    for b in data_bytes:
        computed ^= b
    if computed != bcc:
        return None

    status = chr(data_bytes[0])
    sign = chr(data_bytes[1])
    body = data_bytes[2:]

    unit = ""
    weight_ascii = ""
    for candidate_len in (2, 1):
        if len(body) <= candidate_len:
            continue
        candidate_unit = body[-candidate_len:].decode("latin-1", errors="ignore").strip().upper()
        candidate_weight = body[:-candidate_len].decode("latin-1", errors="ignore").strip()
        if candidate_unit in KNOWN_UNITS:
            unit = candidate_unit
            weight_ascii = candidate_weight
            break

    if not unit or not weight_ascii:
        return None

    cleaned = weight_ascii.replace(" ", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None

    if sign == "-":
        value = -value
    if status == "F":
        return None

    if unit == "G":
        value = value / 1000.0
    elif unit == "LB":
        value = round(value * 0.45359237, 6)

    return value


def _parse_epelsa_56ppi_weight(frame: bytes) -> float | None:
    if not frame:
        return None
    try:
        text = frame.decode("latin-1").strip()
    except Exception:
        return None
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _extract_stream_weight(buffer: bytes, brand: str = "zfoc") -> float | None:
    if not buffer:
        return None
    try:
        text = buffer.decode("latin-1")
    except Exception:
        return None

    matches: list[float] = []

    if brand.lower() == "epelsa":
        for m in _RE_EPELSA_FRAME.finditer(text):
            parsed = _parse_epelsa_56ppi_weight(m.group(1).encode("latin-1"))
            if parsed is not None:
                matches.append(parsed)
        return matches[-1] if matches else None

    for m in _RE_ZFOC_STX_ETX.finditer(text):
        parsed = _parse_stream_ascii_weight(m.group(1).encode("latin-1"), None)
        if parsed is not None:
            matches.append(parsed)

    for m in _RE_ZFOC_DIGITS.finditer(text):
        parsed = _parse_stream_ascii_weight(m.group(1).encode("latin-1"), 3)
        if parsed is not None:
            matches.append(parsed)

    return matches[-1] if matches else None


def _parse_stream_ascii_weight(frame: bytes, implicit_decimals: int | None) -> float | None:
    if not frame:
        return None
    try:
        text = frame.decode("latin-1").strip()
    except Exception:
        return None
    if not text:
        return None

    if text[0] == "I":
        return None

    match = _RE_NUMBER.search(text)
    if not match:
        return None
    number = match.group(0).replace(",", ".")

    if implicit_decimals is not None and "." not in number:
        sign = ""
        digits = number
        if digits[0] in "+-":
            sign = digits[0]
            digits = digits[1:]
        if not digits.isdigit():
            return None
        if len(digits) <= implicit_decimals:
            digits = digits.zfill(implicit_decimals + 1)
        number = f"{sign}{digits[:-implicit_decimals]}.{digits[-implicit_decimals:]}"

    try:
        # 保留更多精度：串口原始读数可能是 3 位小数（如 0.155），
        # 之前 round(...,2) 会把 0.155 舍入成 0.15，导致微小变化被吞掉、
        # Odoo POS 显示看起来"不实时"。保留 4 位以完整传递秤的读数。
        return round(float(number), 4)
    except ValueError:
        return None


class ScaleConfig:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        self.port: str = str(data.get("scale_port") or "").strip()
        self.baudrate: int = int(data.get("scale_baudrate") or 9600)
        self.timeout: float = float(data.get("scale_timeout") or 1.2)
        self.inter_command_delay: float = float(data.get("scale_inter_command_delay") or 0.05)
        self.brand: str = str(data.get("scale_brand") or "zfoc").strip().lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale_port": self.port,
            "scale_baudrate": self.baudrate,
            "scale_timeout": self.timeout,
            "scale_inter_command_delay": self.inter_command_delay,
            "scale_brand": self.brand,
        }

    @property
    def is_configured(self) -> bool:
        return bool(self.port)

    def signature(self) -> tuple:
        return (self.port, self.baudrate, self.timeout, self.inter_command_delay, self.brand)


class ScaleService:
    CACHE_MAX_AGE = 1.0
    MONITOR_WARMUP = 0.9
    DIRECT_READ_MIN_TIMEOUT = 0.8
    STREAM_READ_TIMEOUT = 0.6
    # 主动探测间隔（非流式回退场景）：从 50ms 降到 30ms，提升 command/response 秤的采样率。
    IDLE_COMMAND_SECONDS = 0.03
    # 监控循环空闲休眠：缓冲区空时不再睡 10ms，改为 2ms，
    # 把被动读取的最坏延迟从 10ms 降到 2ms，CPU 唤醒开销可忽略。
    MONITOR_IDLE_SLEEP = 0.002
    # 流式静默阈值：电子秤持续推送时（Gram ZFOC / Epelsa 均为流式），
    # 只要最近收到过数据就继续被动等待，避免发送 ENQ/ACK/DC1 干扰流并增加往返延迟。
    # 仅当超过此阈值仍无数据时才回退到主动探测。
    STREAM_SILENCE_BEFORE_PROBE = 0.15
    READ_CHUNK_SIZE = 128
    BUFFER_TRIM_THRESHOLD = 1024
    BUFFER_TRIM_KEEP = 256
    # 重量变化阈值 (kg)：超过此值才向 Odoo EventBus 推送，避免微小抖动刷屏。
    # 设为极小值，让任何变化都立即推送
    WEIGHT_CHANGE_THRESHOLD = 0.00001
    # 零值确认时长：从非零读到 0 时，先暂存不推送，持续此秒数仍为 0
    # 才认定为「真实归零」（取走物品）并推送/缓存；期间出现非 0 则视为毛刺丢弃。
    # 这样既过滤了不完整帧解析出的瞬态 0，又不会挡住真实的归零读数。
    ZERO_CONFIRM_SECONDS = 0.2
    # 无变化时每隔多少秒强制推送一次保活心跳（0 表示禁用）
    # 0.2 秒保活，确保 POS 持续收到更新
    KEEPALIVE_INTERVAL_SECONDS = 0.2
    # 按需读取模式：ScaleMonitor 不在后台预读，只在 POS 打开称重界面（调用 read_once）时启动。
    # 超过此秒数没有新的 read_once action，自动停止 ScaleMonitor。
    # 设为 300 秒（5分钟），避免称重过程中 ScaleMonitor 停止导致重量不更新
    ON_DEMAND_TIMEOUT_SECONDS = 300.0

    def __init__(self, config_provider: Callable[[], dict[str, Any]], event_bus: Any = None) -> None:
        self._config_provider = config_provider
        self._event_bus = event_bus
        self._cached_weight: float | None = None
        self._cached_at: float = 0.0
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._monitor: ScaleMonitor | None = None
        self._monitor_signature: tuple | None = None
        self._last_pushed_weight: float | None = None
        self._last_pushed_at: float = 0.0
        # 零值确认：首次读到候选 0 的时刻（monotonic）。0.0 表示当前不在确认窗口。
        self._zero_since: float = 0.0
        self._main_loop: asyncio.AbstractEventLoop | None = None
        # Odoo 19 POS owner：当 POS 调用 read_once action 时设置，
        # 后续 ScaleMonitor 发布事件时使用此 owner，POS 才能匹配到事件。
        # 对应 Odoo 19 driver.py 中 self.data["owner"] = session_id
        self._current_owner: str = ""
        # 按需读取：最后收到 read_once action 的时间
        self._last_action_at: float = 0.0

    def set_owner(self, owner: str) -> None:
        """设置当前 POS 会话的 owner（对应 Odoo 19 driver.py 的 self.data["owner"] = session_id）。

        当 POS 调用 /iot_drivers/action 发送 read_once 时，controller 把 session_id 加到 data 中，
        Driver.action 设置 self.data["owner"] = session_id。
        后续 _take_measure 发布事件时，事件中 owner = session_id，
        POS 的 onMessage 检查 message.owner === requestId 才会处理事件。
        """
        self._current_owner = str(owner or "")
        _logger.info("Scale owner set: %s", self._current_owner)

    def touch_action(self) -> None:
        """按需读取模式：POS 打开称重界面时调用，启动 ScaleMonitor 并刷新活跃时间。

        ScaleMonitor 不在服务启动时自动运行，避免后台预读旧重量。
        只有当 POS 调用 read_once action 时才启动，实时读取并推送重量变化。
        超过 ON_DEMAND_TIMEOUT_SECONDS 没有新的 action，自动停止。
        """
        self._last_action_at = time.monotonic()
        if not self.is_monitor_running:
            _logger.info("Scale on-demand monitor starting (POS opened scale screen)")
            self.prime_monitor()
        else:
            _logger.debug("Scale on-demand monitor already running, refreshed timeout")

    def publish_weight_event(self, weight: float) -> None:
        """立即发布一次重量事件（带当前 owner）。

        当 POS 调用 read_once action 时，除了设置 owner 外，还立即发布一次事件，
        让 POS 能快速收到响应，而不需要等待 ScaleMonitor 的下一次读取周期。
        """
        self._publish_to_event_bus(weight)

    @property
    def device_identifier(self) -> str:
        """电子秤在 device_manager 中的标识符，默认 'scale_main'。

        必须与 device_manager._discover_extra_devices 中注册的 identifier 一致，
        这样 POS 通过 /iot_drivers/event 监听 scale_main 时才能匹配到事件。
        """
        try:
            did = str(self._config_provider().get("scale_device_identifier") or "").strip()
            return did or "scale_main"
        except Exception:
            return "scale_main"

    def bind_main_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定主事件循环，供 ScaleMonitor 子线程安全地推送重量事件。"""
        self._main_loop = loop
        _logger.info("ScaleService bound to main event loop")

    def get_config(self) -> ScaleConfig:
        return ScaleConfig(self._config_provider())

    def get_cached_weight(self, max_age: float = 2.0) -> float | None:
        with self._cache_lock:
            if self._cached_weight is None:
                return None
            if (time.monotonic() - self._cached_at) > max_age:
                return None
            return self._cached_weight

    def _set_cached_weight(self, weight: float) -> None:
        now = time.monotonic()
        with self._cache_lock:
            previous = self._last_pushed_weight
            # 零值确认：区分「真实归零」（取走物品，秤盘为空）和「毛刺 0」
            # （数据帧不完整解析出 0.000，下一帧又恢复真实重量）。
            # 仅当从非零过渡到 0 时启用：先暂存不推送/不缓存，
            # 持续 ZERO_CONFIRM_SECONDS 仍为 0 才认定为真实归零并推送；
            # 期间出现非 0 读数则撤销（毛刺），重置确认窗口。
            # previous is None（首次读数）时不走确认，直接放行 0。
            is_candidate_zero = (
                weight == 0.0
                and previous is not None
                and previous > 0.01
            )
            if is_candidate_zero:
                if self._zero_since == 0.0:
                    self._zero_since = now
                if (now - self._zero_since) < self.ZERO_CONFIRM_SECONDS:
                    # 暂未确认，按毛刺处理：不推送、不更新缓存（保持上一个真实值）
                    return
                # 已持续为 0 足够久 → 认定为真实归零，放行
                self._zero_since = 0.0
            else:
                # 非 0 读数（或首次读数），撤销任何零值确认窗口
                self._zero_since = 0.0

            # 变化检测：从未推送过 / 变化超过阈值 / 超过保活间隔
            should_publish = (
                previous is None
                or abs(weight - previous) >= self.WEIGHT_CHANGE_THRESHOLD
                or (now - self._last_pushed_at) >= self.KEEPALIVE_INTERVAL_SECONDS
            )
            if should_publish:
                self._last_pushed_weight = weight
                self._last_pushed_at = now
            # 缓存始终更新（走到这里说明不是未确认的毛刺 0），保证 scale_read 拿到最新值
            self._cached_weight = weight
            self._cached_at = now
        if should_publish:
            # 1. 始终通过 EventBus 发布给 Odoo POS（Event 模式）
            self._publish_to_event_bus(weight)

    def _publish_to_event_bus(self, weight: float) -> None:
        """通过 EventBus 向 Odoo POS 发布重量事件（子线程安全调用）。

        Odoo 19 POS 通过 /iot_drivers/event 长轮询监听 scale 设备事件。
        事件格式遵循 Odoo 19 driver.py + serial_scale_driver.py 协议：
        - result: 重量数值（float），来自 device.data['result']
        - value: 重量数值（float），来自 device.data['value']（TODO: deprecate，但 POS 前端仍可能读取）
        - status: 'success'（字符串，来自 response）
        - device_identifier: 与 device_manager 注册的标识符一致
        - owner: POS 调用 read_once action 时设置的 session_id（即 action_unique_id），
          POS 的 onMessage 检查 message.owner === requestId 才会处理事件
        """
        if self._event_bus is None or self._main_loop is None or not self._main_loop.is_running():
            return
        from ..models import IoTEvent

        rounded = round(weight, 3)
        event = IoTEvent(
            device_identifier=self.device_identifier,
            owner=self._current_owner,  # Odoo 19: session_id/messageId，POS 检查 owner === requestId
            status="success",
            result=rounded,
            extra={
                "value": rounded,  # Odoo 19 Driver.data['value']，POS 前端可能读这个字段
            },
        )
        try:
            asyncio.run_coroutine_threadsafe(
                self._event_bus.publish(event),
                self._main_loop,
            )
        except RuntimeError:
            _logger.debug("EventBus publish failed: event loop closed")

    def ensure_monitor(self) -> None:
        config = self.get_config()
        sig = config.signature()

        with self._lock:
            current_sig = self._monitor_signature
            if not config.is_configured:
                self._stop_monitor()
                return
            if self._monitor is not None and self._monitor.is_running and current_sig == sig:
                return
            self._stop_monitor()
            self._monitor = ScaleMonitor(config, self._set_cached_weight, self)
            self._monitor_signature = sig
            self._monitor.start()

    def _stop_monitor(self) -> None:
        monitor = self._monitor
        self._monitor = None
        self._monitor_signature = None
        self._cached_weight = None
        # 重置推送状态：监控重启后第一次读到重量立即推送
        self._last_pushed_weight = None
        self._last_pushed_at = 0.0
        # 重置零值确认窗口：监控重启后归零判定从头开始
        self._zero_since = 0.0
        if monitor is not None:
            monitor.stop()

    @property
    def is_monitor_running(self) -> bool:
        return self._monitor is not None and self._monitor.is_running

    def prime_monitor(self) -> None:
        config = self.get_config()
        if not config.is_configured:
            self._stop_monitor()
            return
        self.ensure_monitor()
        # 不再调用 read_weight()：ScaleMonitor 已经打开 COM8 持续读取，
        # _direct_read 会尝试打开第二个串口实例 → Windows 上 Access Denied。
        # 重量数据由 ScaleMonitor 流式解析后通过 _set_cached_weight 缓存
        # 并推送到 EventBus，调用方只需等待缓存就绪。

    def read_weight(self, timeout_override: float | None = None) -> float | None:
        self.ensure_monitor()

        warmup = timeout_override if timeout_override is not None else self.MONITOR_WARMUP
        warmup = max(0.15, min(self.MONITOR_WARMUP, warmup))
        deadline = time.monotonic() + warmup
        while time.monotonic() < deadline:
            cached = self.get_cached_weight(self.CACHE_MAX_AGE)
            if cached is not None:
                return cached
            time.sleep(0.05)

        # 如果 ScaleMonitor 已在运行，COM8 被其占用，_direct_read 会因串口冲突
        # (Windows: Access Denied) 而失败，返回已有缓存或 None 即可。
        if self.is_monitor_running:
            return self.get_cached_weight(max_age=999.0)

        return self._direct_read(timeout_override)

    def _direct_read(self, timeout_override: float | None = None) -> float | None:
        config = self.get_config()
        if not config.is_configured:
            return None

        timeout = max(config.timeout, self.DIRECT_READ_MIN_TIMEOUT)
        if timeout_override is not None:
            timeout = max(0.05, min(config.timeout, timeout_override))

        port = SharedSerialPort(config.port, config.baudrate, timeout=timeout)
        try:
            port.open()
            port.flush_input()
            port.flush_output()

            if config.brand == "epelsa":
                weight = self._read_epelsa_streaming(port, timeout)
                if weight is not None:
                    self._set_cached_weight(weight)
                return weight

            port.write(bytes([ZFOC_ENQ]))
            port.flush_output()
            ack = self._read_bytes(port, 1)
            if ack and ack[0] == ZFOC_ACK:
                time.sleep(max(config.inter_command_delay, 0.02))
                port.write(bytes([ZFOC_DC1]))
                port.flush_output()
                payload = self._read_until_eot(port, timeout)
                weight = _parse_zfoc_weight_packet(payload)
                if weight is not None:
                    self._set_cached_weight(weight)
                    return weight

            return self._read_streaming(port, timeout, config)
        except Exception as exc:
            _logger.debug("Scale direct read failed port=%s: %s", config.port, exc)
            return None
        finally:
            port.close()

    def _read_epelsa_streaming(self, port: SharedSerialPort, timeout: float) -> float | None:
        # Epelsa 持续推送模式：flush 后等待一帧数据抵达再开始读。
        time.sleep(0.15)
        deadline = time.monotonic() + max(timeout, 0.8)
        buffer = b""
        while time.monotonic() < deadline:
            chunk = self._read_chunk(port)
            if not chunk:
                time.sleep(0.02)
                continue
            buffer += chunk
            weight = _extract_stream_weight(buffer, "epelsa")
            if weight is not None:
                return weight
        return None

    def _read_streaming(self, port: SharedSerialPort, timeout: float, config: ScaleConfig) -> float | None:
        deadline = time.monotonic() + max(timeout, 0.6)
        buffer = b""
        last_command_at = 0.0

        while time.monotonic() < deadline:
            chunk = self._read_chunk(port)
            if chunk:
                buffer += chunk
                weight = _extract_stream_weight(buffer, config.brand)
                if weight is not None:
                    return weight
                continue

            now = time.monotonic()
            if (now - last_command_at) >= self.IDLE_COMMAND_SECONDS:
                if config.brand != "epelsa":
                    try:
                        port.write(bytes([ZFOC_ENQ]))
                        port.flush_output()
                        ack = self._read_bytes(port, 1)
                        if ack and ack[0] == ZFOC_ACK:
                            time.sleep(max(config.inter_command_delay, 0.02))
                            port.write(bytes([ZFOC_DC1]))
                            port.flush_output()
                            payload = self._read_until_eot(port, timeout)
                            weight = _parse_zfoc_weight_packet(payload)
                            if weight is not None:
                                return weight
                    except Exception:
                        pass
                last_command_at = now

        return None

    def _read_chunk(self, port: SharedSerialPort) -> bytes:
        try:
            waiting = port._serial.in_waiting if port._serial else 0
            if waiting <= 0:
                return b""
            to_read = min(self.READ_CHUNK_SIZE, waiting)
            return port.read(to_read)
        except Exception:
            return b""

    def _read_bytes(self, port: SharedSerialPort, count: int) -> bytes | None:
        try:
            return port.read(count)
        except Exception:
            return None

    def _read_until_eot(self, port: SharedSerialPort, timeout: float) -> bytes:
        deadline = time.monotonic() + max(timeout, 0.2)
        payload = bytearray()
        while time.monotonic() < deadline:
            one = self._read_bytes(port, 1)
            if not one:
                continue
            payload.append(one[0])
            if one[0] == EOT:
                break
        return bytes(payload)

    def read_hw_proxy_scale(self) -> float | None:
        # 如果 ScaleMonitor 在运行，直接返回缓存值（避免串口冲突）
        if self.is_monitor_running:
            return self.get_cached_weight(max_age=999.0)
        # ScaleMonitor 没运行，先读缓存（1 秒内），再直接读串口
        cached = self.get_cached_weight(self.CACHE_MAX_AGE)
        if cached is not None:
            return cached
        return self._direct_read(self.STREAM_READ_TIMEOUT)


class ScaleMonitor:
    def __init__(self, config: ScaleConfig, on_weight: Callable[[float], None], scale_service: Any = None) -> None:
        self._config = config
        self._on_weight = on_weight
        self._scale_service = scale_service
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, name="custom-iot-scale-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._stop_event.wait(timeout=1.0)
            self._thread = None

    def _monitor_loop(self) -> None:
        retry_deadline = 0.0
        port: SharedSerialPort | None = None

        while not self._stop_event.is_set():
            now = time.monotonic()
            if now < retry_deadline:
                self._stop_event.wait(0.1)
                continue

            try:
                port = SharedSerialPort(self._config.port, self._config.baudrate, timeout=self._config.timeout)
                port.open()
                port.flush_input()
                port.flush_output()

                initial = self._probe_weight(port)
                if initial is not None:
                    _logger.info("Scale monitor initial weight port=%s weight=%.3f", self._config.port, initial)
                    self._on_weight(initial)

                self._monitor_stream(port)
            except Exception as exc:
                _logger.debug("Scale monitor loop error port=%s: %s", self._config.port, exc)
                retry_deadline = time.monotonic() + 1.0
            finally:
                if port is not None:
                    try:
                        port.close()
                    except Exception:
                        pass
                    port = None

    def _monitor_stream(self, port: SharedSerialPort) -> None:
        buffer = b""
        last_command_at = 0.0
        last_data_at = time.monotonic()

        while not self._stop_event.is_set():
            # 按需读取模式：超过 ON_DEMAND_TIMEOUT_SECONDS 没有新的 read_once action，
            # 自动停止监控（POS 已关闭称重界面）
            # 优先被动读取：电子秤（如 Gram ZFOC / Epelsa）持续发送实时重量数据。
            # 不 flush_input，避免丢弃电子秤发送的实时数据。
            chunk = self._read_chunk(port)
            if chunk:
                buffer += chunk
                last_data_at = time.monotonic()
                parsed = _extract_stream_weight(buffer, self._config.brand)
                if parsed is not None:
                    # 热路径：流式数据每帧都会走到这里，用 debug 避免 INFO 日志
                    # 的字符串格式化 + handler I/O 拖慢推送（这是之前主要的软件延迟来源）。
                    _logger.debug("Scale monitor parsed weight=%.3f", parsed)
                    self._on_weight(parsed)
                    buffer = b""
                if len(buffer) > ScaleService.BUFFER_TRIM_THRESHOLD:
                    buffer = buffer[-ScaleService.BUFFER_TRIM_KEEP:]
                continue

            # 被动读取无数据：判断电子秤是否仍在流式推送。
            # 若最近 STREAM_SILENCE_BEFORE_PROBE 内收到过数据，说明仍在流式推送，
            # 短暂休眠继续等待即可，不要发 ENQ/ACK/DC1 干扰流（那会增加往返延迟）。
            now = time.monotonic()
            if (now - last_data_at) < ScaleService.STREAM_SILENCE_BEFORE_PROBE:
                time.sleep(ScaleService.MONITOR_IDLE_SLEEP)
                continue

            # 流式静默超过阈值，回退到主动探测（command/response 模式的秤走这里）
            if (now - last_command_at) >= ScaleService.IDLE_COMMAND_SECONDS:
                if self._config.brand == "epelsa":
                    # Epelsa 不需要主动请求，继续等待数据
                    pass
                else:
                    try:
                        probed = self._probe_weight(port)
                        if probed is not None:
                            _logger.debug("Scale monitor probed weight=%.3f", probed)
                            self._on_weight(probed)
                            buffer = b""
                        else:
                            # 电子秤不响应 ENQ/ACK，发送 WAKE 唤醒
                            port.write(bytes([WAKE_BYTE]))
                            port.flush_output()
                            time.sleep(max(self._config.inter_command_delay, 0.01))
                    except Exception:
                        return
                last_command_at = now
            else:
                # 缓冲区空且未到下次 probe 时间，短暂休眠避免 CPU 空转
                time.sleep(ScaleService.MONITOR_IDLE_SLEEP)

    def _probe_weight(self, port: SharedSerialPort) -> float | None:
        if self._config.brand == "epelsa":
            return self._probe_epelsa(port)

        try:
            port.write(bytes([ZFOC_ENQ]))
            port.flush_output()
            ack = self._read_bytes(port, 1)
            if ack and ack[0] == ZFOC_ACK:
                time.sleep(max(self._config.inter_command_delay, 0.02))
                port.write(bytes([ZFOC_DC1]))
                port.flush_output()
                payload = self._read_until_eot(port)
                return _parse_zfoc_weight_packet(payload)
        except Exception:
            return None
        return None

    def _probe_epelsa(self, port: SharedSerialPort) -> float | None:
        try:
            # Epelsa 56 PPI 持续推送约每 100ms 一帧（9600 baud）。
            # flush_input 后 OS 缓冲区为空，立即 _read_chunk 几乎总是命中 in_waiting==0
            # → break → 返回 None。等一帧数据抵达后再开始读取。
            time.sleep(0.15)
            deadline = time.monotonic() + max(self._config.timeout, 0.8)
            buffer = b""
            while time.monotonic() < deadline:
                chunk = self._read_chunk(port)
                if not chunk:
                    time.sleep(0.02)  # 等待下一帧，不 break
                    continue
                buffer += chunk
                weight = _extract_stream_weight(buffer, "epelsa")
                if weight is not None:
                    return weight
        except Exception:
            return None
        return None

    def _read_chunk(self, port: SharedSerialPort) -> bytes:
        try:
            waiting = port._serial.in_waiting if port._serial else 0
            if waiting <= 0:
                return b""
            to_read = min(ScaleService.READ_CHUNK_SIZE, waiting)
            return port.read(to_read)
        except Exception:
            return b""

    def _read_bytes(self, port: SharedSerialPort, count: int) -> bytes | None:
        try:
            return port.read(count)
        except Exception:
            return None

    def _read_until_eot(self, port: SharedSerialPort) -> bytes:
        deadline = time.monotonic() + max(self._config.timeout, 0.2)
        payload = bytearray()
        while time.monotonic() < deadline:
            one = self._read_bytes(port, 1)
            if not one:
                continue
            payload.append(one[0])
            if one[0] == EOT:
                break
        return bytes(payload)
