from __future__ import annotations

from io import BytesIO
import logging
import os

from PIL import Image
from ..printing.common import CODE128_PATTERNS as _CODE128_PATTERNS

_logger = logging.getLogger(__name__)

class BarcodeMixin:
    def _generate_local_code128_png(self, data: str, query: dict[str, list[str]]) -> bytes:
        try:
            from PIL import Image
        except ImportError:
            return b""

        width = self._query_int(query, "width", 400)
        height = self._query_int(query, "height", 70)
        try:
            image = self._render_local_code128_image(data, width, height)
            if image is None:
                return b""
            resized = BytesIO()
            image.save(resized, format="PNG")
            _logger.info("Generated local Code128 receipt image size=%s data_len=%s", image.size, len(data))
            return resized.getvalue()
        except Exception:
            _logger.exception("Failed to generate local Code128 receipt image data=%s", data[:80])
            return b""
        return b""

    def _render_local_code128_image(self, data: str, width: int, height: int):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return None
        code_values = self._encode_code128_values(data)
        if not code_values:
            return None
        pattern = "".join(_CODE128_PATTERNS[value] for value in code_values)
        quiet_modules = 10
        total_modules = quiet_modules + sum(int(module) for module in pattern) + quiet_modules
        configured_module_width = os.getenv("IOT_CODE128_MODULE_WIDTH", "").strip()
        if configured_module_width.isdigit():
            module_width = max(1, int(configured_module_width))
        else:
            module_width = 2
        min_barcode_width = total_modules * module_width
        target_width = max(32, width, min_barcode_width)
        target_height = max(24, height)
        image = Image.new("RGB", (target_width, target_height), "white")
        draw = ImageDraw.Draw(image)
        top_padding = max(6, target_height // 8)
        bottom_padding = max(4, target_height // 10)
        available_height = max(16, target_height - top_padding - bottom_padding)
        barcode_width = total_modules * module_width
        left_padding = max(0, (target_width - barcode_width) // 2)
        x = left_padding + (quiet_modules * module_width)
        is_bar = True
        for token in pattern:
            bar_width = int(token) * module_width
            if is_bar:
                draw.rectangle(
                    (x, top_padding, x + bar_width - 1, top_padding + available_height - 1),
                    fill="black",
                )
            x += bar_width
            is_bar = not is_bar
        return image

    def _encode_code128_values(self, data: str) -> list[int]:
        normalized = str(data or "")
        if not normalized:
            return []
        if normalized.isdigit() and len(normalized) % 2 == 0:
            values = [105]
            for index in range(0, len(normalized), 2):
                values.append(int(normalized[index:index + 2]))
        else:
            values = [104]
            for char in normalized:
                code_point = ord(char)
                if code_point < 32 or code_point > 126:
                    return []
                values.append(code_point - 32)
        checksum = values[0]
        for index, value in enumerate(values[1:], start=1):
            checksum += value * index
        values.append(checksum % 103)
        values.append(106)
        return values
