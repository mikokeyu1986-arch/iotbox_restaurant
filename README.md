# Restaurant Native Print IoT Box Runtime

This is a local copy of your IoT Box runtime for the restaurant instance.

This project is now script-start only.
Do not use or regenerate packaged `.exe` builds from this copy.

What is changed:
- The web UI in `web/` is refactored.
- Local runtime configuration is reset and isolated.
- The runtime title is renamed for easier identification.
- The runtime is trimmed for cloud ESC/POS printing only.

Core routes kept:
- `/iot_drivers/action`
- `/iot_drivers/event`
- cloud websocket bridge
- ESC/POS printer queue and raw printing

Run example:

```powershell
python -m pip install -e .
python run_https.py
```

Only HTTPS is shipped. The plain-HTTP runtime was removed: starting it rewrote the shared
`runtime_config.json` to `plain_http`, which invalidated the certificates and the CA that
clients pin.

Detailed developer diagnostics are written to `logs/dev_debug.log`.

Runtime pairing data, generated certificates and spool output are deliberately excluded from Git.
Use `runtime_config.example.json` as the configuration shape; each IoT Box generates its own
`runtime_config*.json` and `certs/` files. The `/api/` administration endpoints are local-only by
default. For deliberate remote administration, set `IOT_ADMIN_TOKEN` and send it in the
`X-IoT-Admin-Token` header.
The PKCS#12 password is generated per installation and stored locally in `certs/.p12_password`.

If you want this copy to use a different config path, set `IOT_CONFIG_PATH` before starting.

## Visual receipt editor

After pairing the runtime, open its local control page and use **Receipt Studio / 小票可视化编辑器**. It supports:

- drag-and-drop block ordering;
- per-block visibility, alignment, bold style, and spacing;
- editable company, invoice, order-information, and footer text;
- custom text, separator, and spacer blocks;
- a fixed 80 mm / 48-character live preview matching the configured printer;
- separate **顾客小票 / 厨房单** templates with independent saving;
- undo, reset, and persistent save.

The validated template is stored in `receipt_template.json` beside the runtime files. Set
`IOT_RECEIPT_TEMPLATE_PATH` to use a different location. The editor only accepts known receipt
blocks and safe style values; it does not execute template code.

The kitchen layout is stored separately in `kitchen_template.json`. Set
`IOT_KITCHEN_TEMPLATE_PATH` to use a different location. It controls the order type, kitchen
notification (`NUEVO`, `CANCELA`, etc.), order/table reference, course-grouped products and notes,
separators, location, and order time. Kitchen products use quantity × name only; receipt columns
such as `Uds.`, `Producto`, and `Importe` are not used.
