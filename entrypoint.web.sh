#!/bin/bash
exec uvicorn web.server:app --host 0.0.0.0 --port "${WEBUI_PORT:-8000}"