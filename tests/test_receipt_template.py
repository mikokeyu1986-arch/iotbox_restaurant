import json
import os
import tempfile
import unittest
from pathlib import Path

from app.receipt_builder import build_kitchen_ticket_lines, build_receipt_lines
from app.kitchen_template_store import (
    default_kitchen_template,
    save_kitchen_template,
    validate_kitchen_template,
)
from app.printing.product_parser import ProductParserMixin
from app.printing.image_renderer import ImageRendererMixin
from app.printing.receipt_metadata import ReceiptMetadataMixin
from app.printing.product_options import format_option
from app.printing.section_consumers import ReceiptSectionConsumerMixin
from app.printing.text_layout import TextLayoutMixin
from app.receipt_template_store import _cell_width, default_template, load_template, save_template, validate_template
from app.receipts.structured import StructuredReceiptMixin


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ORDER = {
    "pos_reference": "Order 00042",
    "lang": "es_ES",
    "tracking_number": "42",
    "is_restaurant": True,
    "chino_order_type": "DINE IN",
    "kitchen_title": "NUEVO",
    "table_id": {"table_number": "A08"},
    "date_order": "2026-01-02 20:30:00",
    "config": {"name": "主收银台", "module_pos_restaurant": True, "receipt_language": "es_ES"},
    "lines": [{
        "qty": 2,
        "full_product_name": "Producto plantilla",
        "price_unit": "8,50",
        "price_subtotal_incl": "17,00",
    }],
    "totalDue": "20,00",
    "amountPaid": "20,00",
    "payment_lines": [{"name": "Tarjeta", "amount": "20,00"}],
    "course_groups": [{
        "course_name": "PRINCIPALES",
        "items": [{"qty": 2, "full_product_name": "Producto plantilla"}],
    }],
}


def stock_receipt_template():
    """The canonical receipt layout, free of any installation's tuning.

    ``stock_receipt_template()`` returns the *shipped* layout, which is deliberately
    tuned (font sizes, block order, custom blocks).  A test that asserts column
    geometry or block order must pin its own template, otherwise re-tuning the
    ticket breaks the suite.
    """
    return validate_template({"version": 1, "name": "test", "paper_width": 48, "blocks": []})


def stock_kitchen_template():
    """The canonical kitchen layout, free of any installation's tuning."""
    return validate_kitchen_template({"version": 1, "name": "test", "paper_width": 48, "blocks": []})


class ReceiptTemplateTests(unittest.TestCase):
    def test_default_product_columns_fill_48_characters(self):
        template = validate_template(stock_receipt_template())
        header = next(block for block in template["blocks"] if block["id"] == "product_header")

        widths = (
            header["qty_columns"],
            header["product_columns"],
            header["gutter_columns"],
            header["amount_columns"],
        )
        self.assertEqual(widths, (6, 30, 2, 10))
        self.assertEqual(sum(widths), 48)

    def test_default_template_has_complete_independent_loyalty_suite(self):
        template = validate_template(stock_receipt_template())
        block_ids = [block["id"] for block in template["blocks"]]

        for block_id in ("promotions", "coupons", "vouchers", "loyalty"):
            self.assertIn(block_id, block_ids)
        self.assertIn("redsys", block_ids)
        self.assertLess(block_ids.index("products"), block_ids.index("promotions"))
        self.assertLess(block_ids.index("promotions"), block_ids.index("totals"))
        self.assertLess(block_ids.index("payments"), block_ids.index("redsys"))
        self.assertLess(block_ids.index("payments"), block_ids.index("redsys"))
        # Restaurant-specific visual templates may arrange footer, QR and
        # loyalty sections differently; all must remain independently present.

    def test_default_escpos_encoding_prints_real_euro_glyph(self):
        class EncodingRenderer(TextLayoutMixin):
            def _normalize_print_text(self, text):
                return str(text)

        renderer = EncodingRenderer()
        encoding, codepage = renderer._escpos_encoding_config()

        self.assertEqual((encoding, codepage), ("cp858", 19))
        self.assertEqual("€".encode(encoding), b"\xd5")
        self.assertEqual(renderer._escpos_safe_text("8.50 €", encoding), "8.50 €")

    def test_escpos_uses_chinese_codepage_when_payload_omits_language(self):
        renderer = TextLayoutMixin()

        encoding, codepage = renderer._escpos_encoding_config(
            lines=[
                {"text": "堂食"},
                {"type": "product_line", "name": "宫保鸡丁"},
            ]
        )

        self.assertEqual((encoding, codepage), ("gb18030", 255))
        self.assertEqual("宫保鸡丁".encode(encoding), b"\xb9\xac\xb1\xa3\xbc\xa6\xb6\xa1")

    def test_escpos_preserves_preformatted_product_column_spaces(self):
        renderer = TextLayoutMixin()
        header = "Uds.  Producto                           Importe"
        product = "6     Producto plantilla                 13.90 €"

        self.assertEqual(
            renderer._render_escpos_lines(
                {"text": header, "align": "left", "classes": ["receipt-product-header"]},
                48,
            ),
            [header],
        )
        self.assertEqual(
            renderer._render_escpos_lines(
                {"text": product, "align": "left", "classes": ["receipt-product-row"]},
                48,
            ),
            [product],
        )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_path = os.environ.get("IOT_RECEIPT_TEMPLATE_PATH")
        self.previous_kitchen_path = os.environ.get("IOT_KITCHEN_TEMPLATE_PATH")
        os.environ["IOT_RECEIPT_TEMPLATE_PATH"] = str(Path(self.temp_dir.name) / "receipt_template.json")
        os.environ["IOT_KITCHEN_TEMPLATE_PATH"] = str(Path(self.temp_dir.name) / "kitchen_template.json")

    def tearDown(self):
        if self.previous_path is None:
            os.environ.pop("IOT_RECEIPT_TEMPLATE_PATH", None)
        else:
            os.environ["IOT_RECEIPT_TEMPLATE_PATH"] = self.previous_path
        if self.previous_kitchen_path is None:
            os.environ.pop("IOT_KITCHEN_TEMPLATE_PATH", None)
        else:
            os.environ["IOT_KITCHEN_TEMPLATE_PATH"] = self.previous_kitchen_path
        self.temp_dir.cleanup()

    def test_default_template_preserves_all_blocks(self):
        lines = build_receipt_lines(SAMPLE_ORDER)
        self.assertTrue(lines)
        self.assertFalse(any("_template_block" in line for line in lines))
        self.assertFalse(any(line.get("text") == "示例餐厅" for line in lines))
        header = next(line for line in lines if "receipt-product-header" in line.get("classes", []))
        self.assertIn("Uds.", header["text"])
        self.assertIn("Producto", header["text"])
        self.assertIn("Importe", header["text"])
        self.assertEqual(len(header["text"]), 48)

    def test_bundled_default_is_the_saved_visual_layout(self):
        template = stock_receipt_template()
        block_ids = [block["id"] for block in template["blocks"]]
        custom_blocks = [block for block in template["blocks"] if block["kind"] != "builtin"]

        self.assertEqual(len(block_ids), len(set(block_ids)))
        self.assertTrue(all(block.get("enabled") in (True, False) for block in template["blocks"]))
        self.assertTrue(all(block["kind"] in {"builtin", "text", "separator", "spacer"} for block in custom_blocks))

    def test_saved_template_hides_and_reorders_blocks(self):
        template = stock_receipt_template()
        next(block for block in template["blocks"] if block["id"] == "company")["enabled"] = False
        products = next(block for block in template["blocks"] if block["id"] == "products")
        template["blocks"].remove(products)
        template["blocks"].insert(0, products)

        saved = save_template(template)
        lines = build_receipt_lines(SAMPLE_ORDER)

        self.assertEqual(saved, load_template())
        self.assertIn("receipt-product-row", lines[0].get("classes", []))
        self.assertEqual(_cell_width(lines[0]["text"]), 48)
        self.assertTrue(lines[0]["text"].endswith("17,00 €"))
        self.assertFalse(any(line.get("text") == "示例餐厅" for line in lines))

    def test_unknown_blocks_are_rejected(self):
        template = stock_receipt_template()
        template["blocks"][0]["id"] = "arbitrary_python"
        with self.assertRaises(ValueError):
            validate_template(template)

    def test_old_template_is_migrated_to_fixed_width_with_product_header(self):
        template = stock_receipt_template()
        template["paper_width"] = 32
        template["blocks"] = [block for block in template["blocks"] if block["id"] != "product_header"]

        migrated = validate_template(template)
        ids = [block["id"] for block in migrated["blocks"]]

        self.assertEqual(migrated["paper_width"], 48)
        self.assertLess(ids.index("product_header"), ids.index("products"))

    def test_product_header_column_widths_are_editable(self):
        template = stock_receipt_template()
        header_block = next(block for block in template["blocks"] if block["id"] == "product_header")
        header_block.update({"qty_columns": 8, "amount_columns": 12})

        lines = build_receipt_lines(SAMPLE_ORDER, template=template)
        header = next(line for line in lines if "receipt-product-header" in line.get("classes", []))

        self.assertEqual(len(header["text"]), 48)
        self.assertEqual(header["text"].index("Producto"), 8)
        self.assertGreaterEqual(header["text"].index("Importe"), 40)

    def test_product_detail_font_size_two_scales_height_only(self):
        """A larger size must not cost characters per line.

        The row keeps all 48 columns and grows taller; multiplying the width
        would reflow it to 24 columns and wrap product names much sooner.
        """
        template = stock_receipt_template()
        products = next(block for block in template["blocks"] if block["id"] == "products")
        products["font_size"] = 2

        lines = build_receipt_lines(SAMPLE_ORDER, template=template)
        product_rows = [
            line for line in lines
            if "receipt-product-row" in line.get("classes", [])
        ]

        self.assertTrue(product_rows)
        self.assertTrue(all(_cell_width(line["text"]) == 48 for line in product_rows))
        self.assertTrue(all(line["width_multiplier"] == 1 for line in product_rows))
        self.assertTrue(all(line["height_multiplier"] == 2 for line in product_rows))
        self.assertTrue(all(line["double_width"] is False for line in product_rows))
        self.assertTrue(any("17,00 €" in line["text"] for line in product_rows))

    def test_unused_columns_create_a_safe_gutter_before_amount(self):
        template = stock_receipt_template()
        header_block = next(block for block in template["blocks"] if block["id"] == "product_header")
        header_block.update({"qty_columns": 6, "product_columns": 20, "amount_columns": 10})
        header_block.pop("gutter_columns", None)

        validated = validate_template(template)
        saved_header = next(block for block in validated["blocks"] if block["id"] == "product_header")
        lines = build_receipt_lines(SAMPLE_ORDER, template=validated)
        header = next(line for line in lines if "receipt-product-header" in line.get("classes", []))

        self.assertEqual(saved_header["gutter_columns"], 12)
        self.assertEqual(_cell_width(header["text"]), 48)
        self.assertTrue(header["text"].endswith("Importe"))

    def test_explicit_gutter_leaves_unused_columns_after_amount(self):
        template = validate_template(stock_receipt_template())
        header_block = next(block for block in template["blocks"] if block["id"] == "product_header")
        header_block.update({
            "qty_columns": 6,
            "product_columns": 26,
            "gutter_columns": 2,
            "amount_columns": 10,
        })

        validated = validate_template(template)
        saved_header = next(block for block in validated["blocks"] if block["id"] == "product_header")
        lines = build_receipt_lines(SAMPLE_ORDER, template=validated)
        header = next(line for line in lines if "receipt-product-header" in line.get("classes", []))

        self.assertEqual(saved_header["gutter_columns"], 2)
        self.assertEqual(_cell_width(header["text"]), 48)
        self.assertEqual(header["text"][37:44], "Importe")
        self.assertEqual(header["text"][44:], " " * 4)

    def test_long_product_names_wrap_without_splitting_words(self):
        template = validate_template(stock_receipt_template())
        header_block = next(block for block in template["blocks"] if block["id"] == "product_header")
        header_block.update({
            "qty_columns": 6,
            "product_columns": 30,
            "gutter_columns": 2,
            "amount_columns": 10,
        })
        order = {
            **SAMPLE_ORDER,
            "lines": [{
                "qty": "12,00",
                "full_product_name": "Producto plantilla grande familiar especial",
                "price_subtotal_incl": "100,00",
            }],
        }

        lines = build_receipt_lines(order, template=template)
        product_rows = [
            line["text"] for line in lines
            if "receipt-product-row" in line.get("classes", [])
        ]

        self.assertEqual(product_rows[0][6:36].rstrip(), "Producto plantilla grande")
        self.assertEqual(product_rows[1][6:36].rstrip(), "familiar especial")
        self.assertTrue(all(_cell_width(row) == 48 for row in product_rows))

    def test_native_odoo_discount_line_format(self):
        order = {
            **SAMPLE_ORDER,
            "lines": [{
                "qty": 1,
                "full_product_name": "Producto",
                "price_subtotal_incl": 6.95,
                "price_unit": 13.90,
                "discount": 50,
            }],
            "totalDue": 6.95,
            "amountPaid": 6.95,
        }

        lines = build_receipt_lines(order, template=stock_receipt_template())
        discount_rows = [
            line["text"].strip()
            for line in lines
            if "receipt-product-discount-row" in line.get("classes", [])
        ]

        self.assertEqual(discount_rows, ["50% de descuento en 13,90 €"])

    def test_product_hides_unit_price_and_total_uses_euro_symbol(self):
        lines = build_receipt_lines(SAMPLE_ORDER, template=stock_receipt_template())
        unit_rows = [
            line["text"].strip()
            for line in lines
            if "receipt-product-unit-price-row" in line.get("classes", [])
        ]
        product_rows = [
            line["text"]
            for line in lines
            if "receipt-product-row" in line.get("classes", [])
        ]
        total_line = next(
            line["text"] for line in lines
            if str(line.get("text") or "").startswith("TOTAL ")
        )

        self.assertEqual(unit_rows, [])
        self.assertTrue(product_rows[0].rstrip().endswith("17,00 €"))
        self.assertEqual(total_line, "TOTAL 20,00 €")

    def test_multi_attribute_product_keeps_product_fields_and_prefixes_attributes(self):
        order = {
            **SAMPLE_ORDER,
            "lines": [{
                "qty": 1,
                "full_product_name": "Hamburguesa (Sin cebolla, Extra queso x2)",
                "price_unit": "8,50",
                "price_subtotal_incl": "8,50",
            }],
        }

        lines = build_receipt_lines(order, template=stock_receipt_template())
        product_row = next(
            line["text"] for line in lines
            if "receipt-product-row" in line.get("classes", [])
        )
        attribute_rows = [
            line["text"].strip() for line in lines
            if "receipt-product-option-row" in line.get("classes", [])
        ]
        unit_rows = [
            line["text"].strip() for line in lines
            if "receipt-product-unit-price-row" in line.get("classes", [])
        ]

        self.assertTrue(product_row.startswith("1 "))
        self.assertTrue(product_row.rstrip().endswith("8,50 €"))
        self.assertEqual(attribute_rows, ["+ Sin cebolla", "+ 2 X Extra queso"])
        self.assertEqual(unit_rows, [])

    def test_multi_attribute_uses_each_attributes_own_quantity_and_price(self):
        self.assertEqual(
            format_option({"name": "Sin cebolla", "qty": 1}, fallback_qty="1"),
            "+ Sin cebolla",
        )
        self.assertEqual(
            format_option({"name": "23432", "qty": 3, "unit_price": "120 €"}, fallback_qty="1"),
            "+ 3 X 23432 (+120 €)",
        )
        self.assertEqual(
            format_option({"name": "12321", "quantity": 2, "price_extra": "12 €"}, fallback_qty="1"),
            "+ 2 X 12321 (+12 €)",
        )

        class StructuredRenderer(StructuredReceiptMixin, ImageRendererMixin, ReceiptMetadataMixin):
            @staticmethod
            def _escpos_line_width():
                return 48

        lines = StructuredRenderer()._build_structured_receipt_lines({
            "items": [{
                "qty": "1",
                "name": "Coca-Cola (23432, 12321)",
                "total": "162,38 €",
                "unit_price": "162,38 €",
                "attribute_details": [
                    {"name": "23432", "qty": 3, "unit_price": "120 €"},
                    {"name": "12321", "qty": 2, "unit_price": "12 €"},
                ],
            }],
        }, template=stock_receipt_template())
        product = next(line for line in lines if "receipt-product-row" in line.get("classes", []))
        options = [
            line["text"].strip() for line in lines
            if "receipt-product-option-row" in line.get("classes", [])
        ]

        self.assertIn("Coca-Cola", product["text"])
        self.assertNotIn("23432, 12321", product["text"])
        self.assertEqual(options, ["+ 3 X 23432 (+120 €)", "+ 2 X 12321 (+12 €)"])

    def test_structured_receipt_places_invoice_before_pickup_and_order_number_below_barcode(self):
        class StructuredRenderer(StructuredReceiptMixin, ImageRendererMixin, ReceiptMetadataMixin):
            @staticmethod
            def _escpos_line_width():
                return 48

        lines = StructuredRenderer()._build_structured_receipt_lines({
            "is_restaurant": True,
            "tracking_number": "42",
            "pos_reference": "Order 00042",
            "factura_simplificada_number": "262-1-000042",
            "header_lines": ["MESA 4"],
            "barcode": {"src": "https://example.test/barcode.png"},
            "items": [],
        }, template=stock_receipt_template())

        invoice_index = next(i for i, line in enumerate(lines) if "simplified-invoice-title" in line.get("classes", []))
        tracking_index = next(i for i, line in enumerate(lines) if "tracking-info" in line.get("classes", []))
        barcode_index = next(i for i, line in enumerate(lines) if "order-barcode-img" in line.get("classes", []))
        barcode_number = lines[barcode_index + 1]

        self.assertLess(invoice_index, tracking_index)
        self.assertEqual(barcode_number["text"], "Order 00042")
        self.assertIn("order-barcode-reference", barcode_number["classes"])
        self.assertIn("no-barcode-separator", lines[barcode_index]["classes"])

    def test_raw_order_voucher_prints_code_barcode_and_euro_balance(self):
        order = {
            **SAMPLE_ORDER,
            "loyalty_cards": [{
                "name": "Tarjeta regalo",
                "code": "VALE-1234",
                "point": "25,00",
                "qrSrc": "data:image/png;base64,ignored",
            }],
        }
        lines = build_receipt_lines(order, template=stock_receipt_template())
        voucher_lines = [
            line for line in lines
            if any(str(cls).startswith("gift-card-") for cls in line.get("classes", []))
        ]

        self.assertTrue(any(line.get("text") == "Tarjeta regalo" for line in voucher_lines))
        self.assertTrue(any(line.get("text") == "VALE-1234" for line in voucher_lines))
        barcode = next(line for line in voucher_lines if "gift-card-barcode" in line.get("classes", []))
        self.assertEqual(barcode.get("barcode_value"), "VALE-1234")
        self.assertIn("barcode_type=Code128", barcode.get("src", ""))
        self.assertTrue(any(line.get("text") == "25,00 €" for line in voucher_lines))

    def test_structured_voucher_and_portal_qr_use_separate_blocks(self):
        class StructuredRenderer(StructuredReceiptMixin):
            @staticmethod
            def _escpos_line_width():
                return 48

        template = validate_template(stock_receipt_template())
        next(block for block in template["blocks"] if block["id"] == "vouchers")["enabled"] = False
        next(block for block in template["blocks"] if block["id"] == "qr")["enabled"] = True
        save_template(template)
        source_lines = [
            {"text": "VALE-1234", "classes": ["gift-card-code"]},
            {"type": "image", "image_kind": "qr", "classes": ["portal-qr"]},
        ]

        lines = StructuredRenderer()._apply_visual_template_to_structured_lines(source_lines)

        self.assertFalse(any("gift-card-code" in line.get("classes", []) for line in lines))
        self.assertTrue(any("portal-qr" in line.get("classes", []) for line in lines))

    def test_structured_total_drops_native_adjacent_separators(self):
        class StructuredRenderer(StructuredReceiptMixin):
            @staticmethod
            def _escpos_line_width():
                return 48

        source_lines = [
            {"text": "-" * 48, "classes": ["receipt-separator"]},
            {"text": "TOTAL 20,00 €", "classes": ["receipt-total"]},
            {"text": "-" * 48, "classes": ["receipt-separator"]},
        ]

        lines = StructuredRenderer()._apply_visual_template_to_structured_lines(source_lines)

        self.assertTrue(any(line.get("text") == "TOTAL 20,00 €" for line in lines))
        self.assertFalse(any("receipt-separator" in line.get("classes", []) for line in lines))

    def test_redsys_terminal_record_is_an_independent_block(self):
        order = {
            **SAMPLE_ORDER,
            "payment_terminal_receipts": [{
                "lines": [
                    "Tarjeta: VISA",
                    "Auth Code: 123456",
                    "Terminal: 00998877",
                    "OPERACION CONTACTLESS. FIRMA NO NECESARIA.",
                ],
            }],
        }

        lines = build_receipt_lines(order, template=stock_receipt_template())
        redsys_lines = [line for line in lines if "redsys-receipt-line" in line.get("classes", [])]
        nfc_logo = next(line for line in lines if "redsys-nfc-logo" in line.get("classes", []))

        self.assertEqual(nfc_logo.get("src"), "/assets/nfc_override.png")
        self.assertEqual(nfc_logo.get("width"), 80)
        self.assertLess(lines.index(nfc_logo), lines.index(redsys_lines[0]))
        self.assertEqual(
            [line.get("text") for line in redsys_lines],
            [
                "Tarjeta: VISA", "Auth Code: 123456", "Terminal: 00998877",
                "OPERACION CONTACTLESS. FIRMA NO NECESARIA.",
            ],
        )

    def test_redsys_metadata_fallback_is_printed(self):
        order = {
            **SAMPLE_ORDER,
            "payment_lines": [{
                "name": "Tarjeta",
                "amount": 20,
                "card_type": "VISA",
                "card_number": "************1234",
                "authorization_code": "654321",
                "transaction_id": "TX-99",
                "terminal_id": "TERM-1",
            }],
        }

        texts = [line.get("text") for line in build_receipt_lines(order, template=stock_receipt_template())]

        for expected in (
            "Tarjeta: VISA", "Tarjeta nº: ************1234", "Autorización: 654321",
            "Terminal: TERM-1", "Transacción: TX-99",
        ):
            self.assertIn(expected, texts)

    def test_structured_redsys_can_be_hidden_without_hiding_payment_amount(self):
        class StructuredRenderer(StructuredReceiptMixin):
            @staticmethod
            def _escpos_line_width():
                return 48

        template = stock_receipt_template()
        next(block for block in template["blocks"] if block["id"] == "redsys")["enabled"] = False
        save_template(template)
        source_lines = [
            {"text": "Tarjeta 20,00 €", "classes": ["paymentlines"]},
            {"type": "image", "src": "/assets/nfc_override.png", "classes": ["payment-terminal-nfc-icon", "redsys-nfc-logo"]},
            {"text": "Auth Code: 123456", "classes": ["payment-terminal-line"]},
        ]

        lines = StructuredRenderer()._apply_visual_template_to_structured_lines(source_lines)

        self.assertTrue(any("paymentlines" in line.get("classes", []) for line in lines))
        self.assertFalse(any("redsys-nfc-logo" in line.get("classes", []) for line in lines))
        self.assertFalse(any("payment-terminal-line" in line.get("classes", []) for line in lines))

    def test_structured_voucher_points_use_euro_currency(self):
        class VoucherRenderer(ReceiptSectionConsumerMixin, ProductParserMixin, TextLayoutMixin):
            pass

        field_name, amount = VoucherRenderer()._gift_card_display_amount({"point": "25,00"})

        self.assertEqual(field_name, "point")
        self.assertEqual(amount, "25,00 €")

    def test_member_loyalty_points_are_not_currency(self):
        order = {
            **SAMPLE_ORDER,
            "loyalty_points": [{
                "couponId": 42,
                "program": {"portal_visible": True},
                "points": {
                    "name": "Puntos Club",
                    "won": 12.5,
                    "spent": 3,
                    "balance": 84,
                    "total": 93.5,
                },
            }],
        }

        lines = build_receipt_lines(order, template=stock_receipt_template())
        loyalty_lines = [line for line in lines if "loyalty-points" in line.get("classes", [])]

        self.assertEqual(
            [(line["left_text"], line["right_text"]) for line in loyalty_lines],
            [
                ("Puntos Club Ganados", "12,5"),
                ("Puntos Club Utilizados", "3"),
                ("Saldo Puntos Club", "84"),
            ],
        )
        self.assertFalse(any("€" in line["right_text"] for line in loyalty_lines))

    def test_complete_customer_information_is_rendered(self):
        order = {
            **SAMPLE_ORDER,
            "partner_id": {
                "parent_name": "Empresa Matriz",
                "name": "Ana García",
                "vat": "ES12345678A",
                "street": "Calle Mayor 10",
                "street2": "Local 2",
                "zip": "35001",
                "city": "Las Palmas",
                "state_id": {"name": "Las Palmas"},
                "country_id": {"name": "España"},
                "phone": "928000001",
                "mobile": "600000001",
                "email": "ana@example.com",
            },
        }

        lines = build_receipt_lines(order, template=stock_receipt_template())
        customer_texts = [
            line.get("text") for line in lines
            if "customer-info" in line.get("classes", [])
        ]

        for expected in (
            "Empresa Matriz, Ana García", "ES12345678A", "Calle Mayor 10", "Local 2",
            "35001 Las Palmas Las Palmas", "España", "928000001", "600000001", "ana@example.com",
        ):
            self.assertIn(expected, customer_texts)

    def test_customer_alias_is_supported_when_partner_id_is_absent(self):
        order = {**SAMPLE_ORDER, "partner_id": None, "customer": {"name": "Cliente mostrador", "phone": "123"}}

        lines = build_receipt_lines(order, template=stock_receipt_template())
        texts = [line.get("text") for line in lines]

        self.assertIn("Cliente mostrador", texts)
        self.assertIn("123", texts)

    def test_new_coupon_info_is_rendered_in_independent_coupon_block(self):
        order = {
            **SAMPLE_ORDER,
            "new_coupon_info": [{
                "program_name": "Cupón próxima visita",
                "code": "CUPON-5678",
                "expiration_date": "2026-12-31",
            }],
        }

        lines = build_receipt_lines(order, template=stock_receipt_template())
        texts = [line.get("text") for line in lines]

        self.assertIn("Cupón próxima visita", texts)
        self.assertIn("CUPON-5678", texts)
        self.assertIn("Hasta: 2026-12-31", texts)
        coupon_lines = [
            line for line in lines
            if any(str(value).startswith("coupon-") for value in line.get("classes", []))
        ]
        self.assertTrue(any("coupon-code" in line.get("classes", []) for line in coupon_lines))
        self.assertFalse(any("gift-card-code" in line.get("classes", []) for line in coupon_lines))

    def test_reward_line_populates_promotions_block(self):
        order = {
            **SAMPLE_ORDER,
            "lines": [{
                "qty": 1,
                "full_product_name": "Descuento club",
                "price_subtotal_incl": -5,
                "is_reward_line": True,
                "points_cost": 20,
                "reward_identifier_code": "PROMO-20",
                "reward_id": {
                    "name": "20% socios",
                    "reward_type": "discount",
                    "program_id": {"name": "Club Restaurante"},
                },
            }],
        }

        lines = build_receipt_lines(order, template=stock_receipt_template())
        promotion_lines = [
            line for line in lines
            if any(str(value).startswith("promotion-") for value in line.get("classes", []))
        ]

        self.assertTrue(any(line.get("text") == "20% socios" for line in promotion_lines))
        self.assertTrue(any(line.get("text") == "Club Restaurante" for line in promotion_lines))
        self.assertTrue(any("20 pts" in line.get("text", "") for line in promotion_lines))
        self.assertTrue(any(line.get("text") == "PROMO-20" for line in promotion_lines))

    def test_four_loyalty_modules_can_be_hidden_independently(self):
        order = {
            **SAMPLE_ORDER,
            "promotions": [{"name": "Promo visible", "reward_type": "discount", "amount": -2}],
            "new_coupon_info": [{"program_name": "Cupón visible", "code": "C-1"}],
            "loyalty_cards": [{"name": "Tarjeta visible", "code": "G-1", "balance": 15}],
            "loyalty_points": [{"points": {"name": "Puntos", "won": 5, "balance": 5}}],
        }
        template = stock_receipt_template()
        next(block for block in template["blocks"] if block["id"] == "coupons")["enabled"] = False

        lines = build_receipt_lines(order, template=template)
        texts = [line.get("text") for line in lines]

        self.assertIn("Promo visible", texts)
        self.assertNotIn("Cupón visible", texts)
        self.assertIn("Tarjeta visible", texts)
        self.assertTrue(any("loyalty-points" in line.get("classes", []) for line in lines))

    def test_odoo_program_types_are_routed_to_the_correct_modules(self):
        order = {
            **SAMPLE_ORDER,
            "loyalty_cards": [
                {"name": "Gift", "code": "G-1", "program_type": "gift_card"},
                {"name": "Not a gift", "code": "P-1", "program_type": "promo_code"},
            ],
            "coupons": [
                {"name": "Discount code", "code": "C-1", "program_type": "promo_code"},
                {"name": "Not a coupon", "code": "G-2", "program_type": "gift_card"},
            ],
            "promotions": [
                {"name": "Buy X Get Y", "program_type": "buy_x_get_y"},
                {"name": "Not a promotion", "program_type": "ewallet"},
            ],
            "loyalty_points": [
                {"program": {"program_type": "loyalty"}, "points": {"name": "Club", "won": 2}},
                {"program": {"program_type": "gift_card"}, "points": {"name": "Money", "won": 9}},
            ],
        }

        rendered = build_receipt_lines(order, template=stock_receipt_template())
        texts = [line.get("text") for line in rendered]

        for expected in ("Gift", "Discount code", "Buy X Get Y"):
            self.assertIn(expected, texts)
        for excluded in ("Not a gift", "Not a coupon", "Not a promotion"):
            self.assertNotIn(excluded, texts)
        loyalty_labels = [line.get("left_text") for line in rendered if "loyalty-points" in line.get("classes", [])]
        self.assertIn("Club Ganados", loyalty_labels)
        self.assertNotIn("Money Ganados", loyalty_labels)

    def test_comma_decimal_quantity_and_total_are_preserved(self):
        order = {
            **SAMPLE_ORDER,
            "lines": [{
                "qty": "12,00",
                "full_product_name": "Producto prueba especial largo",
                "unit_price": "8,50",
                "price_subtotal_incl": "100,00",
            }],
            "totalDue": "100,00",
        }
        lines = build_receipt_lines(order, template=stock_receipt_template())
        product = next(
            line["text"] for line in lines
            if "receipt-product-row" in line.get("classes", [])
        )
        total = next(
            line["text"] for line in lines
            if str(line.get("text") or "").startswith("TOTAL ")
        )

        self.assertTrue(product.startswith("12,00 "))
        self.assertTrue(product.rstrip().endswith("100,00 €"))
        self.assertEqual(total, "TOTAL 100,00 €")

    def test_editor_preview_uses_field_names_and_total_has_no_fixed_separators(self):
        template = stock_receipt_template()
        next(block for block in template["blocks"] if block["id"] == "company")["enabled"] = True
        lines = build_receipt_lines(SAMPLE_ORDER, template=template, preview_fields=True)
        texts = [line.get("text", "") for line in lines]
        total_index = texts.index("TOTAL {{ totalDue }}")

        self.assertIn("{{ company.name }}", texts)
        self.assertIn("{{ config.receipt_footer }}", texts)
        self.assertIn("{{ partner_id.vat }}", texts)
        self.assertIn("{{ partner_id.phone }} / {{ partner_id.mobile }}", texts)
        self.assertIn("{{ partner_id.email }}", texts)
        self.assertIn("{{ loyalty_cards[].name }}", texts)
        self.assertIn("{{ loyalty_cards[].code }}", texts)
        self.assertIn("{{ loyalty_cards[].balance }}", texts)
        self.assertIn("{{ loyalty_cards[].point }}", texts)
        self.assertIn("{{ loyalty_cards[].qrSrc }}", texts)
        self.assertIn("{{ loyalty_cards[].program_type }}", texts)
        self.assertIn("{{ new_coupon_info[].program_name }}", texts)
        self.assertIn("{{ new_coupon_info[].code }}", texts)
        self.assertIn("{{ new_coupon_info[].expiration_date }}", texts)
        self.assertIn("{{ coupons[].program_type }}", texts)
        self.assertIn("{{ lines[].reward_id.name }}", texts)
        self.assertIn("{{ lines[].reward_id.program_id.name }}", texts)
        self.assertIn("{{ payment_terminal_receipts[].lines[] }}", texts)
        self.assertIn("{{ payment_lines[].authorization_code }} / {{ payment_lines[].transaction_id }}", texts)
        self.assertTrue(any("redsys-nfc-logo" in line.get("classes", []) for line in lines))
        self.assertIn("{{ loyalty_points[].points.total }}", texts)
        loyalty_preview = [line for line in lines if "loyalty-points" in line.get("classes", [])]
        self.assertTrue(any("points.won" in line.get("right_text", "") for line in loyalty_preview))
        self.assertTrue(any("points.spent" in line.get("right_text", "") for line in loyalty_preview))
        self.assertTrue(any("points.balance" in line.get("right_text", "") for line in loyalty_preview))
        order_label_index = texts.index("PEDIDO {{ pos_reference }}")
        barcode = lines[order_label_index + 1]
        self.assertEqual(barcode.get("image_kind"), "barcode")
        self.assertEqual(barcode.get("barcode_value"), "{{ pos_reference }}")
        for neighbor in (lines[total_index - 1], lines[total_index + 1]):
            if neighbor.get("text") == "-" * 48:
                self.assertIn("template-custom-separator", neighbor.get("classes", []))
        product_row = next(line for line in lines if "receipt-product-row" in line.get("classes", []))
        self.assertIn("qty", product_row["text"])
        self.assertIn("full_product_name", product_row["text"])
        product_rows = [line["text"] for line in lines if "receipt-product-row" in line.get("classes", [])]
        self.assertIn("price_subt", product_rows[0])
        self.assertTrue(any("otal_incl" in row for row in product_rows))
        self.assertTrue(any("discount% de descuento en" in row for row in texts))

    def test_custom_text_and_footer_override_are_rendered(self):
        template = stock_receipt_template()
        footer = next(block for block in template["blocks"] if block["id"] == "footer")
        footer["content"] = "自定义页脚\n再次感谢"
        template["blocks"].insert(0, {
            "id": "custom_test_text",
            "kind": "text",
            "label": "欢迎文字",
            "text": "欢迎光临",
            "enabled": True,
            "align": "center",
            "bold": True,
            "horizontal_offset": 3,
            "double_size": True,
            "spacing_after": 1,
        })

        lines = build_receipt_lines(SAMPLE_ORDER, template=template)

        self.assertEqual(lines[0].get("text").lstrip(), "欢迎光临")
        self.assertTrue(lines[0].get("text").startswith(" "))
        self.assertEqual(lines[0].get("align"), "left")
        self.assertTrue(lines[0].get("double_width"))
        self.assertTrue(any(line.get("text") == "自定义页脚" for line in lines))
        self.assertFalse(any(line.get("text") == "谢谢惠顾" for line in lines))

    def test_kitchen_template_preview_uses_odoo_field_names(self):
        lines = build_kitchen_ticket_lines(
            SAMPLE_ORDER, template=stock_kitchen_template(), preview_fields=True,
        )
        texts = [str(line.get("text") or "") for line in lines]
        product = next(line for line in lines if line.get("type") == "product_line")

        self.assertEqual(texts[0], "# {{ tracking_number }}")
        self.assertEqual(texts[1], "{{ chino_order_type || 'DINE IN' }}")
        self.assertEqual(texts[2], "{{ kitchen_title || changes.title || 'NUEVO' }}")
        self.assertEqual(texts[3], "{{ floor_name }} - T {{ table_id.table_number }}")
        self.assertFalse(any("MESA" in text for text in texts))
        self.assertEqual(product["qty"], "{{ course_groups[].items[].qty }}")
        self.assertEqual(product["name"], "{{ course_groups[].items[].full_product_name }}")
        self.assertTrue(any("course_groups[].course_name" in text for text in texts))
        footer_meta = next(
            line for line in lines
            if "kitchen-location-time" in line.get("classes", [])
        )
        self.assertEqual(footer_meta["left_text"], "{{ config.name }}")
        self.assertEqual(footer_meta["right_text"], "{{ date_order.time }}")

    def test_kitchen_tracking_is_top_and_table_has_no_prompt_word(self):
        lines = build_kitchen_ticket_lines(SAMPLE_ORDER, template=stock_kitchen_template())

        self.assertEqual(lines[0].get("text"), "# 42")
        self.assertIn("kitchen-tracking-number", lines[0].get("classes", []))
        table_line = next(line for line in lines if "kitchen-table-number" in line.get("classes", []))
        self.assertEqual(table_line.get("text"), "A08")
        self.assertNotIn("MESA", table_line.get("text", ""))

    def test_kitchen_table_is_prefixed_with_floor(self):
        order = {
            **SAMPLE_ORDER,
            "table_id": {
                "table_number": "109",
                "floor_id": {"name": "Patio"},
            },
        }

        lines = build_kitchen_ticket_lines(order, template=stock_kitchen_template())
        table_line = next(line for line in lines if "kitchen-table-number" in line.get("classes", []))

        self.assertEqual(table_line.get("text"), "Patio - T 109")

    def test_delivery_kitchen_ticket_keeps_table_value_without_mesa_prompt(self):
        order = {
            **SAMPLE_ORDER,
            "chino_order_type": "DELIVERY",
            "table_id": {"table_number": "A08"},
        }

        lines = build_kitchen_ticket_lines(order, template=stock_kitchen_template())
        texts = [str(line.get("text") or "") for line in lines]

        self.assertIn("DELIVERY", texts)
        self.assertIn("A08", texts)
        self.assertFalse(any("MESA" in text for text in texts))

    def test_saved_kitchen_template_hides_and_reorders_blocks(self):
        template = stock_kitchen_template()
        next(block for block in template["blocks"] if block["id"] == "status")["enabled"] = False
        location_block = next(block for block in template["blocks"] if block["id"] == "location")
        template["blocks"].remove(location_block)
        template["blocks"].insert(0, location_block)
        save_kitchen_template(template)

        lines = build_kitchen_ticket_lines(SAMPLE_ORDER)
        self.assertEqual(lines[0].get("type"), "header_meta_line")
        self.assertEqual(lines[0].get("left_text"), "主收银台")
        self.assertEqual(lines[0].get("right_text"), "20:30")
        self.assertFalse(any(line.get("text") == "NUEVO" for line in lines))

    def test_kitchen_notification_and_course_sequence_have_no_receipt_columns(self):
        order = {
            **SAMPLE_ORDER,
            "kitchen_title": "CANCELA",
            "course_groups": [
                {
                    "course_name": "1º ENTRANTES",
                    "items": [{
                        "qty": 1,
                        "full_product_name": "Ensalada",
                        "customer_note": "Sin cebolla",
                        "_cancelled": True,
                    }],
                },
                {
                    "course_name": "2º PRINCIPALES",
                    "items": [{"qty": 2, "full_product_name": "Paella"}],
                },
            ],
        }

        lines = build_kitchen_ticket_lines(order, template=stock_kitchen_template())
        texts = [str(line.get("text") or "") for line in lines]
        products = [line for line in lines if line.get("type") == "product_line"]

        self.assertIn("CANCELA", texts)
        self.assertIn("** 1º ENTRANTES **", texts)
        self.assertIn("** 2º PRINCIPALES **", texts)
        self.assertEqual([line["name"] for line in products], ["Ensalada", "Paella"])
        self.assertEqual(products[0]["kitchen_notification"], "CANCELA")
        self.assertFalse(any("Uds." in text or "Producto" in text or "Importe" in text for text in texts))

    def test_kitchen_group_merges_items_and_cancelled_without_duplicates(self):
        order = {
            **SAMPLE_ORDER,
            "course_groups": [],
            "changes": {
                "title": "CANCELA",
                "groupedData": [{
                    "course_name": "1º ENTRANTES",
                    "items": [{"uuid": "line-1", "qty": 1, "full_product_name": "Ensalada"}],
                    "new": [{"uuid": "line-2", "qty": 1, "full_product_name": "Pan"}],
                    "cancelled": [{"uuid": "line-1", "qty": 1, "full_product_name": "Ensalada"}],
                }],
            },
        }

        lines = build_kitchen_ticket_lines(order, template=stock_kitchen_template())
        products = [line for line in lines if line.get("type") == "product_line"]

        self.assertEqual([line["name"] for line in products], ["Ensalada", "Pan"])
        self.assertEqual(products[0]["kitchen_notification"], "CANCELA")


if __name__ == "__main__":
    unittest.main()
