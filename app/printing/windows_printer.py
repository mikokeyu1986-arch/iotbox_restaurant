from __future__ import annotations

import logging
import os
from pathlib import Path
from time import time

from PIL import Image
from ..dev_logger import dev_log
from ..models import Device
from ..printing.common import perf_log as _perf_log

try:
    import win32print  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    win32print = None

try:
    import win32con  # type: ignore[import-not-found]
    import win32ui  # type: ignore[import-not-found]
    from PIL import ImageWin
except ImportError:  # pragma: no cover
    win32con = None
    win32ui = None
    ImageWin = None

_logger = logging.getLogger(__name__)

class WindowsPrinterMixin:
    def _send_native_image_to_windows_printer(self, device: Device, image: Image.Image) -> bool:
        if os.name != "nt" or win32ui is None or win32con is None or ImageWin is None:
            _logger.error(
                "Windows native image printing unavailable os_name=%s has_win32ui=%s has_imagewin=%s",
                os.name,
                win32ui is not None,
                ImageWin is not None,
            )
            return False

        printer_name = self._printer_name(device)
        resolved_printer = self._resolve_windows_printer(printer_name)
        if not resolved_printer:
            _logger.error("Native image print no Windows printer resolved requested=%s", printer_name)
            return False

        dc = None
        try:
            dc = win32ui.CreateDC()
            dc.CreatePrinterDC(resolved_printer)
            printable_width = int(dc.GetDeviceCaps(win32con.HORZRES))
            printable_height = int(dc.GetDeviceCaps(win32con.VERTRES))
            configured_width = int(os.getenv("IOT_NATIVE_IMAGE_TARGET_WIDTH", "0") or "0")
            target_width = configured_width if configured_width > 0 else image.width
            target_width = max(1, min(target_width, printable_width))
            default_left = max(0, int((printable_width - target_width) / 2))
            left_margin = max(0, int(os.getenv("IOT_NATIVE_IMAGE_MARGIN_LEFT", str(default_left)) or str(default_left)))
            top_margin = max(0, int(os.getenv("IOT_NATIVE_IMAGE_MARGIN_TOP", "0") or "0"))
            scale = target_width / max(1, image.width)
            target_height = max(1, int(round(image.height * scale)))
            if printable_height > 0:
                # Thermal receipt drivers often expose a long virtual page. If
                # they expose a short fixed page, keep the same width and let the
                # driver spool the page instead of creating ESC/POS raster data.
                target_height = min(target_height, max(1, printable_height - top_margin))

            bitmap = ImageWin.Dib(image)
            dc.StartDoc("Odoo Native IoT Receipt Image")
            try:
                dc.StartPage()
                bitmap.draw(dc.GetHandleOutput(), (left_margin, top_margin, left_margin + target_width, top_margin + target_height))
                dc.EndPage()
            finally:
                dc.EndDoc()
            _logger.info(
                "Native image printed through Windows driver printer=%s source=%sx%s target=%sx%s printable=%sx%s",
                resolved_printer,
                image.width,
                image.height,
                target_width,
                target_height,
                printable_width,
                printable_height,
            )
            return True
        except Exception as exc:
            _logger.exception(
                "Native image Windows driver print exception printer=%s error_type=%s error=%s",
                resolved_printer,
                type(exc).__name__,
                str(exc),
            )
            return False
        finally:
            if dc is not None:
                try:
                    dc.DeleteDC()
                except Exception:
                    pass
    def _is_windows_native_printing_available(self) -> bool:
        return os.name == "nt" and win32print is not None
    def _send_raw_to_windows_printer(self, printer_name: str | None, target_file: Path) -> bool:
        if not self._is_windows_native_printing_available():
            _logger.error(
                "Windows native printing unavailable os_name=%s has_win32print=%s",
                os.name,
                win32print is not None,
            )
            dev_log(
                "windows_raw_print_unavailable",
                requested_printer=printer_name,
                target_file=str(target_file),
                os_name=os.name,
                has_win32print=win32print is not None,
            )
            return False
        resolve_started_at = time()
        requested_printer = str(printer_name or "").strip()
        if requested_printer:
            resolved_printer = requested_printer
        else:
            resolved_printer = self._resolve_windows_printer(None)
        resolve_duration_ms = (time() - resolve_started_at) * 1000
        if not resolved_printer:
            available_queues = self._windows_printer_queues()
            _logger.error(
                "No Windows printer resolved requested=%s available=%s resolve_ms=%.1f",
                printer_name or "<none>",
                available_queues,
                resolve_duration_ms,
            )
            dev_log(
                "windows_raw_print_no_printer",
                requested_printer=printer_name,
                target_file=str(target_file),
                available_queues=available_queues,
                resolve_ms=round(resolve_duration_ms, 1),
            )
            return False
        _logger.debug(
            "Windows printer resolved requested=%s resolved=%s queues=%s resolve_ms=%.1f",
            requested_printer or "<none>",
            resolved_printer,
            self._windows_printer_queues(),
            resolve_duration_ms,
        )
        try:
            read_started_at = time()
            payload = target_file.read_bytes()
            read_duration_ms = (time() - read_started_at) * 1000
            open_started_at = time()
            handle = win32print.OpenPrinter(resolved_printer)
            open_duration_ms = (time() - open_started_at) * 1000
            _logger.debug(
                "Windows printer opened printer=%s open_ms=%.1f",
                resolved_printer,
                open_duration_ms,
            )
            try:
                job_name = f"Custom IoT Raw {target_file.name}"
                doc_started_at = time()
                win32print.StartDocPrinter(handle, 1, (job_name, None, "RAW"))
                try:
                    win32print.StartPagePrinter(handle)
                    write_started_at = time()
                    win32print.WritePrinter(handle, payload)
                    write_duration_ms = (time() - write_started_at) * 1000
                    _logger.debug(
                        "Windows printer write completed printer=%s bytes=%s write_ms=%.1f",
                        resolved_printer,
                        len(payload),
                        write_duration_ms,
                    )
                    win32print.EndPagePrinter(handle)
                finally:
                    win32print.EndDocPrinter(handle)
            finally:
                win32print.ClosePrinter(handle)
        except Exception as exc:
            try:
                payload_bytes = len(payload)
            except NameError:
                payload_bytes = -1
            _logger.error(
                "Windows raw print exception printer=%s target=%s error_type=%s error=%s bytes=%s",
                resolved_printer,
                target_file,
                type(exc).__name__,
                str(exc),
                payload_bytes,
            )
            dev_log(
                "windows_raw_print_exception",
                requested_printer=printer_name,
                resolved_printer=resolved_printer,
                target_file=str(target_file),
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            return False
        dev_log(
            "windows_raw_print_success",
            requested_printer=printer_name,
            resolved_printer=resolved_printer,
            target_file=str(target_file),
            bytes=len(payload),
            resolve_ms=round(resolve_duration_ms, 1),
            read_ms=round(read_duration_ms, 1),
            open_ms=round(open_duration_ms, 1),
            write_ms=round(write_duration_ms, 1),
        )
        _perf_log(
            "Windows raw print "
            f"printer={resolved_printer} "
            f"file={target_file.name} "
            f"bytes={len(payload)} "
            f"resolve_ms={resolve_duration_ms:.1f} "
            f"read_ms={read_duration_ms:.1f} "
            f"open_ms={open_duration_ms:.1f} "
            f"write_ms={write_duration_ms:.1f}"
        )
        return True

    def _resolve_windows_printer(self, printer_name: str | None) -> str | None:
        queues = self._windows_printer_queues()
        _logger.debug(
            "Resolve Windows printer requested=%s available_queues=%s",
            printer_name or "<none>",
            queues,
        )
        if printer_name and printer_name in queues:
            _logger.debug("Windows printer resolved by exact match printer=%s", printer_name)
            return printer_name
        if printer_name:
            _logger.warning(
                "Strict printer binding rejected missing Windows queue=%s available=%s",
                printer_name,
                queues,
            )
            return None
        default_queue = self._windows_default_queue(queues)
        fallback_queue = queues[0] if queues else None
        resolved = default_queue or fallback_queue
        _logger.debug(
            "Windows printer resolved by fallback default=%s fallback=%s resolved=%s",
            default_queue,
            fallback_queue,
            resolved,
        )
        return resolved
