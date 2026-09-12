from __future__ import annotations

from base64 import b64decode
import hashlib
import logging
from io import BytesIO
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, unquote_to_bytes, urljoin, urlparse, urlencode
from urllib.request import urlopen
from xml.etree import ElementTree as ET
from PIL import Image, ImageDraw, ImageOps
_logger = logging.getLogger(__name__)

class ImageRendererMixin:
    def _build_escpos_image(self, line: dict[str, Any]) -> bytes:
        try:
            from PIL import Image
        except ImportError:
            print("[IOT ESCPOS] PIL not available for image rendering")
            return b""

        image_kind = str(line.get("image_kind") or "image")
        src = str(line.get("src") or "").strip()
        if not src:
            print(f"[IOT ESCPOS] empty src for image_kind={image_kind}")
            return b""
        image_data = self._fetch_receipt_image(src)
        if not image_data:
            print(f"[IOT ESCPOS] failed to fetch image_kind={image_kind} src={src[:200]}")
            return b""
        image_data = self._maybe_convert_svg_to_png(src, image_data)
        if not image_data:
            print(f"[IOT ESCPOS] failed svg conversion image_kind={image_kind} src={src[:200]}")
            return b""

        target_width = self._escpos_image_width(line)
        requested_size = self._escpos_requested_image_size(line, target_width)
        cache_key = hashlib.sha1(
            f"{image_kind}|{target_width}|{requested_size}|".encode("utf-8") + image_data
        ).hexdigest()
        cached_raster = self._escpos_raster_cache.get(cache_key)
        if cached_raster is not None:
            return cached_raster

        try:
            with Image.open(BytesIO(image_data)) as img:
                print(
                    f"[IOT ESCPOS] rendering image_kind={image_kind} "
                    f"format={img.format} size={img.size} mode={img.mode}"
                )
                img = self._prepare_receipt_image(img, image_kind, line)
                img = self._resize_escpos_image(img, line, target_width)
                if image_kind == "logo":
                    img = self._finalize_escpos_logo_image(img)
                classes = (
                    {str(cls) for cls in line.get("classes") or []}
                    if isinstance(line.get("classes"), list)
                    else set()
                )
                if "cn-dish-line" in classes:
                    raster = self._image_to_escpos_bit_image(img)
                else:
                    raster = self._image_to_escpos_raster(img)
                self._escpos_raster_cache[cache_key] = raster
                if len(self._escpos_raster_cache) > 64:
                    oldest_key = next(iter(self._escpos_raster_cache))
                    self._escpos_raster_cache.pop(oldest_key, None)
                return raster
        except Exception as exc:
            print(f"[IOT ESCPOS] failed to build raster image_kind={image_kind}: {exc}")
            return b""

    def _prepare_receipt_image(self, img, kind: str, line: dict[str, Any] | None = None):
        try:
            from PIL import Image
        except ImportError:
            return img.convert("1")

        kind = kind or "image"
        classes = (
            {str(cls) for cls in (line or {}).get("classes") or []}
            if isinstance((line or {}).get("classes"), list)
            else set()
        )
        if "A" in img.getbands():
            alpha = img.getchannel("A")
            alpha_bbox = alpha.getbbox()
            if alpha_bbox and "payment-terminal-nfc-icon" in classes:
                pad = 4
                left = max(0, alpha_bbox[0] - pad)
                top = max(0, alpha_bbox[1] - pad)
                right = min(img.width, alpha_bbox[2] + pad)
                bottom = min(img.height, alpha_bbox[3] + pad)
                img = img.crop((left, top, right, bottom))
            rgba = img.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            img = Image.alpha_composite(background, rgba)
        if kind == "qr":
            grayscale = img.convert("L")
            inverted_bbox = grayscale.point(lambda px: 255 - px, mode="L").getbbox()
            if inverted_bbox:
                cropped = grayscale.crop(inverted_bbox)
                quiet_zone = max(8, int(max(cropped.width, cropped.height) * 0.12))
                canvas = Image.new(
                    "L",
                    (cropped.width + (quiet_zone * 2), cropped.height + (quiet_zone * 2)),
                    255,
                )
                canvas.paste(cropped, (quiet_zone, quiet_zone))
                grayscale = canvas
            return grayscale.point(lambda px: 0 if px < 180 else 255, mode="1")
        if kind == "barcode":
            grayscale = img.convert("L")
            inverted_bbox = grayscale.point(lambda px: 255 - px, mode="L").getbbox()
            if inverted_bbox:
                cropped = grayscale.crop(inverted_bbox)
                quiet_zone = max(16, int(cropped.width * 0.08))
                canvas = Image.new(
                    "L",
                    (cropped.width + (quiet_zone * 2), cropped.height),
                    255,
                )
                canvas.paste(cropped, (quiet_zone, 0))
                grayscale = canvas
            threshold = int(os.getenv("IOT_ESCPOS_BARCODE_THRESHOLD", "200") or "200")
            threshold = max(1, min(254, threshold))
            return grayscale.point(lambda px: 0 if px < threshold else 255, mode="1")
        if kind == "logo":
            # Keep logos in grayscale until after resizing; thresholding first
            # softens edges and makes small marks look muddy on thermal printers.
            return ImageOps.autocontrast(img.convert("L"), cutoff=1)

        if kind == "receipt":
            img = self._trim_receipt_image_vertical_whitespace(img)

        grayscale = img.convert("L")
        return grayscale.point(lambda px: 0 if px < 180 else 255, mode="1")

    def _trim_receipt_image_vertical_whitespace(self, img):
        grayscale = img.convert("L")
        threshold = int(os.getenv("IOT_RECEIPT_IMAGE_TRIM_THRESHOLD", "245") or "245")
        threshold = max(1, min(254, threshold))

        min_dark_pixels = int(os.getenv("IOT_RECEIPT_IMAGE_MIN_DARK_PIXELS", "0") or "0")
        if min_dark_pixels <= 0:
            min_dark_pixels = max(4, int(img.width * 0.015))

        pixels = grayscale.load()
        content_rows = []
        for y in range(img.height):
            dark_pixels = 0
            for x in range(img.width):
                if pixels[x, y] < threshold:
                    dark_pixels += 1
                    if dark_pixels >= min_dark_pixels:
                        content_rows.append(y)
                        break

        if not content_rows:
            return img

        top = content_rows[0]
        bottom = content_rows[-1] + 1
        vertical_pad = int(os.getenv("IOT_RECEIPT_IMAGE_VERTICAL_PAD", "15") or "15")
        vertical_pad = max(0, min(80, vertical_pad))
        crop_top = max(0, top - vertical_pad)
        crop_bottom = min(img.height, bottom + vertical_pad)
        if crop_top == 0 and crop_bottom == img.height:
            return img

        _logger.info(
            "Trimmed receipt image vertical whitespace original=%sx%s crop_top=%s crop_bottom=%s threshold=%s min_dark_pixels=%s",
            img.width,
            img.height,
            crop_top,
            crop_bottom,
            threshold,
            min_dark_pixels,
        )
        return img.crop((0, crop_top, img.width, crop_bottom))

    def _finalize_escpos_logo_image(self, img):
        grayscale = ImageOps.autocontrast(img.convert("L"), cutoff=1)
        threshold = int(os.getenv("IOT_ESCPOS_LOGO_THRESHOLD", "196") or "196")
        threshold = max(1, min(254, threshold))
        return grayscale.point(lambda px: 0 if px < threshold else 255, mode="1")
    def _escpos_image_width(self, line: dict[str, Any]) -> int:
        configured = os.getenv("IOT_ESCPOS_DOTS_WIDTH", "").strip()
        if configured.isdigit():
            return max(128, int(configured))
        kind = str(line.get("image_kind") or "image")
        classes = (
            {str(cls) for cls in line.get("classes") or []}
            if isinstance(line.get("classes"), list)
            else set()
        )
        paper = os.getenv("IOT_ESCPOS_PAPER_WIDTH", "80").strip()
        full_width = 576 if paper == "80" else 384
        if kind == "logo":
            # Preserve the source logo's natural dimensions whenever they fit
            # the paper.  Only constrain it at the printer's printable edge.
            return full_width
        if kind == "qr":
            return min(full_width, 320 if paper == "80" else 240)
        if kind == "barcode":
            return min(full_width, 420 if paper == "80" else 300)
        if "payment-terminal-nfc-icon" in classes:
            return min(full_width, 80 if paper == "80" else 64)
        if "payment-terminal-logo" in classes:
            return min(full_width, 72 if paper == "80" else 56)
        return full_width

    def _escpos_requested_image_size(
        self,
        line: dict[str, Any],
        max_width: int,
    ) -> tuple[int | None, int | None]:
        width = self._parse_receipt_image_dimension(line.get("width"))
        height = self._parse_receipt_image_dimension(line.get("height"))
        if width is not None:
            width = min(width, max_width)
        return width, height

    def _resize_escpos_image(self, img, line: dict[str, Any], target_width: int):
        requested_width, requested_height = self._escpos_requested_image_size(line, target_width)
        image_kind = str(line.get("image_kind") or "image")
        if image_kind == "barcode":
            return self._resize_escpos_barcode_image(
                img,
                requested_width=requested_width,
                requested_height=requested_height,
                target_width=target_width,
            )
        width = img.width
        height = img.height

        if requested_width is not None and requested_height is not None:
            width = requested_width
            height = requested_height
        elif requested_width is not None:
            ratio = requested_width / float(max(1, img.width))
            width = requested_width
            height = max(1, int(img.height * ratio))
        elif requested_height is not None:
            ratio = requested_height / float(max(1, img.height))
            height = requested_height
            width = max(1, int(img.width * ratio))
        elif img.width > target_width:
            ratio = target_width / float(img.width)
            width = target_width
            height = max(1, int(img.height * ratio))

        if image_kind == "qr":
            side = max(1, min(width, height, target_width))
            width = side
            height = side

        if width == img.width and height == img.height:
            return img
        resample = Image.Resampling.LANCZOS
        if image_kind == "qr" or image_kind == "barcode":
            resample = Image.Resampling.NEAREST
        return img.resize((max(1, width), max(1, height)), resample=resample)

    def _resize_escpos_barcode_image(
        self,
        img,
        requested_width: int | None,
        requested_height: int | None,
        target_width: int,
    ):
        try:
            from PIL import Image
        except ImportError:
            return img

        width = img.width
        height = img.height

        # Keep the original barcode raster unless we can enlarge it without
        # changing bar proportions. Shrinking to the receipt CSS size tends to
        # make 1D barcodes unreadable on thermal printers.
        desired_width = width
        if requested_width is not None and requested_width > width:
            desired_width = min(requested_width, target_width)

        if desired_width > width:
            scale = desired_width // max(1, width)
            if scale > 1:
                desired_width = width * scale
                if requested_height is not None and requested_height >= height * scale:
                    desired_height = height * scale
                else:
                    desired_height = height * scale
                return img.resize((desired_width, desired_height), Image.Resampling.NEAREST)

        return img

    def _parse_receipt_image_dimension(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return max(1, int(value))
        match = re.search(r"\d+", str(value))
        if not match:
            return None
        return max(1, int(match.group(0)))

    def _fetch_receipt_image(self, src: str) -> bytes:
        cached = self._image_fetch_cache.get(src)
        if cached is not None:
            return cached

        generated = self._build_local_receipt_image(src)
        if generated:
            self._image_fetch_cache[src] = generated
            if len(self._image_fetch_cache) > 64:
                oldest_key = next(iter(self._image_fetch_cache))
                self._image_fetch_cache.pop(oldest_key, None)
            return generated

        if src.startswith("data:image") and "," in src:
            try:
                header, payload_text = src.split(",", 1)
                if ";base64" in header.lower():
                    payload = b64decode(payload_text)
                else:
                    payload = unquote_to_bytes(payload_text)
                self._image_fetch_cache[src] = payload
                return payload
            except Exception:
                _logger.exception("Failed to decode inline receipt image src=%s", src[:200])
                return b""

        if src.startswith(("http://", "https://")):
            url = src
        else:
            base_url = os.getenv("IOT_RECEIPT_BASE_URL", "http://127.0.0.1:8069").rstrip("/") + "/"
            url = urljoin(base_url, src.lstrip("/"))

        try:
            with urlopen(url, timeout=10) as resp:
                payload = resp.read()
                self._image_fetch_cache[src] = payload
                if len(self._image_fetch_cache) > 64:
                    oldest_key = next(iter(self._image_fetch_cache))
                    self._image_fetch_cache.pop(oldest_key, None)
                return payload
        except Exception:
            _logger.exception("Failed to fetch receipt image src=%s resolved_url=%s", src[:200], url)
            return b""

    def _clear_runtime_image_caches(self) -> None:
        self._image_fetch_cache.clear()
        self._escpos_raster_cache.clear()

    def _with_logo_cache_buster(self, src: str) -> str:
        normalized = str(src or "").strip()
        if not normalized or "/web/image" not in normalized.lower():
            return normalized
        parsed = urlparse(normalized)
        query = parse_qs(parsed.query)
        query["_iot_logo_v"] = [self._runtime_logo_cache_buster]
        encoded_query = urlencode(query, doseq=True)
        return parsed._replace(query=encoded_query).geturl()

    def _build_local_receipt_image(self, src: str) -> bytes:
        parsed = urlparse(src)
        route = parsed.path or ""
        query = parse_qs(parsed.query)
        if self._is_payment_terminal_nfc_src(route):
            override_paths = (
                self.resource_dir / "web" / "nfc_override.png",
                Path(__file__).resolve().parents[2] / "web" / "nfc_override.png",
            )
            try:
                override_path = next((path for path in override_paths if path.is_file()), None)
                if override_path is not None:
                    image_data = override_path.read_bytes()
                    print(
                        f"[IOT IMAGE TRACE] NFC src={src} -> override={override_path} "
                        f"bytes={len(image_data)}"
                    )
                    return image_data
            except OSError:
                pass
            return self._generate_local_nfc_icon_png()
        if "/report/barcode/QR/" in route:
            payload = route.split("/report/barcode/QR/", 1)[1]
            if not payload:
                return b""
            qr_data = unquote(payload)
            return self._generate_local_qr_png(qr_data, query)
        if "/report/barcode/Code128/" in route:
            payload = route.split("/report/barcode/Code128/", 1)[1]
            if not payload:
                return b""
            barcode_value = unquote(payload)
            return self._generate_local_code128_png(barcode_value, query)
        if route.rstrip("/") == "/report/barcode":
            barcode_type = str((query.get("barcode_type") or query.get("type") or [""])[0]).strip().upper()
            value = str((query.get("value") or query.get("data") or [""])[0]).strip()
            if not barcode_type or not value:
                return b""
            if barcode_type == "QR":
                return self._generate_local_qr_png(unquote(value), query)
            if barcode_type == "CODE128":
                return self._generate_local_code128_png(unquote(value), query)
        return b""

    def _is_qr_receipt_image_src(self, src: str, image_payload: dict[str, Any] | None = None) -> bool:
        payload = image_payload if isinstance(image_payload, dict) else {}
        explicit_kind = str(
            payload.get("image_kind")
            or payload.get("kind")
            or payload.get("barcode_type")
            or payload.get("type")
            or ""
        ).strip().upper()
        if explicit_kind == "QR":
            return True

        parsed = urlparse(str(src or ""))
        route = parsed.path or ""
        query = parse_qs(parsed.query)
        if "/report/barcode/QR/" in route:
            return True
        query_type = str((query.get("barcode_type") or query.get("type") or [""])[0]).strip().upper()
        return route.rstrip("/") == "/report/barcode" and query_type == "QR"

    def _generate_local_nfc_icon_png(self) -> bytes:
        icon = Image.new("RGBA", (220, 90), (255, 255, 255, 255))
        draw = ImageDraw.Draw(icon)
        accent = (0, 0, 0, 255)
        secondary = (255, 255, 255, 255)

        draw.rounded_rectangle((6, 6, 214, 84), radius=20, outline=accent, width=4, fill=secondary)
        draw.rounded_rectangle((22, 22, 74, 62), radius=8, outline=accent, width=4)
        draw.arc((62, 14, 118, 70), start=-60, end=60, fill=accent, width=5)
        draw.arc((80, 8, 146, 76), start=-60, end=60, fill=accent, width=5)
        draw.arc((98, 2, 174, 82), start=-60, end=60, fill=accent, width=5)
        draw.text((154, 31), "NFC", fill=accent)

        output = BytesIO()
        icon.save(output, format="PNG")
        return output.getvalue()

    def _generate_local_qr_png(self, data: str, query: dict[str, list[str]]) -> bytes:
        qrcode = self._import_optional_module("qrcode")
        if qrcode is None:
            _logger.warning("QR receipt image generation skipped because qrcode module is unavailable")
            return b""
        try:
            from PIL import Image
        except ImportError:
            _logger.warning("QR receipt image generation skipped because Pillow is unavailable")
            return b""

        width = self._query_int(query, "width", 200) or self._query_int(query, "img_width", 200)
        height = self._query_int(query, "height", 200) or self._query_int(query, "img_height", 200)
        size = max(64, min(width, height))
        try:
            qr = qrcode.QRCode(border=2, box_size=8)
            qr.add_data(data)
            qr.make(fit=True)
            image = qr.make_image(fill_color="black", back_color="white")
            if hasattr(image, "convert"):
                image = image.convert("RGB")
            if image.size != (size, size):
                image = image.resize((size, size), Image.Resampling.NEAREST)
            output = BytesIO()
            image.save(output, format="PNG")
            _logger.info("Generated local QR receipt image size=%s data_len=%s", image.size, len(data))
            return output.getvalue()
        except Exception:
            _logger.exception("Failed to generate local QR receipt image data_len=%s", len(data))
            return b""
    def _query_int(self, query: dict[str, list[str]], key: str, default: int) -> int:
        raw_values = query.get(key) or []
        raw_value = str(raw_values[0]).strip() if raw_values else ""
        return int(raw_value) if raw_value.isdigit() else default

    def _maybe_convert_svg_to_png(self, src: str, image_data: bytes) -> bytes:
        if "image/svg+xml" not in src and not image_data.lstrip().startswith(b"<svg"):
            return image_data
        try:
            import cairosvg
        except (ImportError, OSError):
            return self._render_simple_svg_to_png(image_data)
        try:
            return cairosvg.svg2png(bytestring=image_data)
        except Exception:
            return self._render_simple_svg_to_png(image_data)

    def _render_simple_svg_to_png(self, image_data: bytes) -> bytes:
        try:
            from PIL import ImageDraw
        except ImportError:
            return b""
        try:
            root = ET.fromstring(image_data.decode("utf-8"))
        except Exception:
            return b""

        view_box = str(root.attrib.get("viewBox") or "").strip().split()
        vb_width = self._parse_svg_number(view_box[2]) if len(view_box) == 4 else None
        vb_height = self._parse_svg_number(view_box[3]) if len(view_box) == 4 else None
        width = self._parse_svg_number(str(root.attrib.get("width") or "")) or vb_width or 128
        height = self._parse_svg_number(str(root.attrib.get("height") or "")) or vb_height or 128
        if width <= 0 or height <= 0:
            return b""

        vb_x = self._parse_svg_number(view_box[0]) if len(view_box) == 4 else 0.0
        vb_y = self._parse_svg_number(view_box[1]) if len(view_box) == 4 else 0.0
        vb_width = vb_width or width
        vb_height = vb_height or height
        scale_x = width / float(vb_width) if vb_width else 1.0
        scale_y = height / float(vb_height) if vb_height else 1.0

        def scale_rect(x: float, y: float, rect_width: float, rect_height: float) -> tuple[float, float, float, float]:
            scaled_x = (x - (vb_x or 0.0)) * scale_x
            scaled_y = (y - (vb_y or 0.0)) * scale_y
            scaled_width = rect_width * scale_x
            scaled_height = rect_height * scale_y
            return scaled_x, scaled_y, scaled_width, scaled_height

        image = Image.new("RGB", (int(round(width)), int(round(height))), "white")
        draw = ImageDraw.Draw(image)

        template_rects: dict[str, tuple[float, float, str]] = {}
        all_elements = list(root.iter())
        href_keys = (
            "{http://www.w3.org/1999/xlink}href",
            "href",
        )
        for element in all_elements:
            tag = element.tag.rsplit("}", 1)[-1]
            if tag != "rect":
                continue
            rect_width = self._parse_svg_number(str(element.attrib.get("width") or "")) or 0
            rect_height = self._parse_svg_number(str(element.attrib.get("height") or "")) or 0
            fill = str(element.attrib.get("fill") or "#000000").strip() or "#000000"
            rect_id = str(element.attrib.get("id") or "").strip()
            x = self._parse_svg_number(str(element.attrib.get("x") or "0")) or 0
            y = self._parse_svg_number(str(element.attrib.get("y") or "0")) or 0
            if rect_id:
                template_rects[rect_id] = (rect_width, rect_height, fill)
                continue
            if rect_width > 0 and rect_height > 0 and fill.lower() not in {"#ffffff", "white", "none"}:
                scaled_x, scaled_y, scaled_width, scaled_height = scale_rect(x, y, rect_width, rect_height)
                draw.rectangle(
                    [
                        scaled_x,
                        scaled_y,
                        scaled_x + scaled_width - 1,
                        scaled_y + scaled_height - 1,
                    ],
                    fill=fill,
                )

        for element in all_elements:
            tag = element.tag.rsplit("}", 1)[-1]
            if tag != "use":
                continue
            href = ""
            for key in href_keys:
                href = str(element.attrib.get(key) or "").strip()
                if href:
                    break
            if not href.startswith("#"):
                continue
            template = template_rects.get(href[1:])
            if not template:
                continue
            rect_width, rect_height, fill = template
            if rect_width <= 0 or rect_height <= 0 or fill.lower() in {"#ffffff", "white", "none"}:
                continue
            x = self._parse_svg_number(str(element.attrib.get("x") or "0")) or 0
            y = self._parse_svg_number(str(element.attrib.get("y") or "0")) or 0
            scaled_x, scaled_y, scaled_width, scaled_height = scale_rect(x, y, rect_width, rect_height)
            draw.rectangle(
                [
                    scaled_x,
                    scaled_y,
                    scaled_x + scaled_width - 1,
                    scaled_y + scaled_height - 1,
                ],
                fill=fill,
            )

        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def _parse_svg_number(self, raw: str) -> float | None:
        text = str(raw or "").strip()
        if not text:
            return None
        match = re.match(r"^-?\d+(?:\.\d+)?", text)
        if not match:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None

    def _image_to_escpos_raster(self, image) -> bytes:
        width, height = image.size
        max_band_height = int(os.getenv("IOT_ESCPOS_RASTER_BAND_HEIGHT", "240") or "240")
        max_band_height = max(24, min(512, max_band_height))
        if height > max_band_height:
            return b"".join(
                self._image_to_escpos_raster_band(image.crop((0, band_top, width, min(height, band_top + max_band_height))))
                for band_top in range(0, height, max_band_height)
            )

        return self._image_to_escpos_raster_band(image)

    def _image_to_escpos_raster_band(self, image) -> bytes:
        width, height = image.size
        width_bytes = (width + 7) // 8
        data = bytearray()
        pixels = image.load()
        for y in range(height):
            row = 0
            bit_count = 0
            for x in range(width):
                bit = 0 if pixels[x, y] else 1
                row = (row << 1) | bit
                bit_count += 1
                if bit_count == 8:
                    data.append(row)
                    row = 0
                    bit_count = 0
            if bit_count:
                row <<= 8 - bit_count
                data.append(row)
        xL = width_bytes % 256
        xH = width_bytes // 256
        yL = height % 256
        yH = height // 256
        return b"\x1dv0\x00" + bytes([xL, xH, yL, yH]) + bytes(data)

    def _image_to_escpos_bit_image(self, image) -> bytes:
        width, height = image.size
        width_bytes = (width + 7) // 8
        pixels = image.load()
        chunks = bytearray()
        for band_top in range(0, height, 24):
            band_height = min(24, height - band_top)
            chunks.extend(b"\x1b\x33\x18")
            chunks.extend(b"\x1b*\x21" + bytes([width_bytes % 256, width_bytes // 256]))
            for x_byte in range(width_bytes):
                for stripe in range(3):
                    value = 0
                    for bit in range(8):
                        y = band_top + (stripe * 8) + bit
                        x_base = x_byte * 8
                        byte_value = 0
                        for x_offset in range(8):
                            x = x_base + x_offset
                            pixel_on = y < height and x < width and not pixels[x, y]
                            byte_value = (byte_value << 1) | (1 if pixel_on else 0)
                        chunks.append(byte_value)
            chunks.extend(b"\n")
            if band_height < 24:
                chunks.extend(b"\n")
        chunks.extend(b"\x1b\x32")
        return bytes(chunks)
