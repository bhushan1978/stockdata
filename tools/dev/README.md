# Developer tools (not needed for normal use)

Normal operation needs only `start_stockdata.bat` and `stop_stockdata.bat` in the repository root. The prebuilt interface is in
`webui/dist`, so Node.js is not required.

| Tool | Purpose |
|---|---|
| `build_frontend.bat` | rebuild `webui/dist` after editing `webui/src` (Node.js 20+) |
| `frontend_dev.bat` | backend + Vite hot-reload server on http://localhost:5173 (Node.js 20+; run `start_stockdata.bat` once first) |

Other development commands (run from the repository root, inside `.venv`):

```
.venv\Scripts\python.exe -m stockdata.web serve            :: the server in this console (add --dev for the Vite origin)
.venv\Scripts\python.exe -m stockdata.web status           :: server and download workers, even when the server is down
.venv\Scripts\python.exe -m stockdata.web.bootstrap check  :: dependency and interface check
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
cd webui && npm run typecheck
```
