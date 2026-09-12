# Restaurant Native Print IoT Box Runtime

This directory is a Windows deployment overlay, not a standalone application.
Copy it over a complete checkout that contains the `app/` package.

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
uvicorn app.main:app --host 0.0.0.0 --port 8398
```

Before publishing the overlay, refresh its canonical web and launcher files:

```powershell
python scripts/sync_script_bundle.py
```

If you want this copy to use a different config path, set `IOT_CONFIG_PATH` before starting.
