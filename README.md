# CaptureWebsite

CaptureWebsite records the HTTP resources received by a real browser during a configured visit, stores the capture as WARC/WACZ, extracts the observed content, and produces a verified ZIP. Version 2 runs without Docker, WSL, or virtualization.

A complete capture means everything the controlled browser observed during that visit. It does not include server-side code, databases, server secrets, inaccessible resources, or content the browser never received. The extracted folders are intended for inspection and are not guaranteed to form a runnable offline copy of the site.

## Windows application

1. Run `dist\CaptureWebsite.exe` as a normal user. Do not run it as administrator.
2. Enter a public URL beginning with `https://` or `http://`.
3. If a site blocks automated browser sessions, keep **Compatibility: open in visible browser** enabled.
4. Select **START CAPTURE**. If the site presents a verification step, complete it in the temporary browser window.
5. When the capture finishes, select **Open folder**.

CaptureWebsite uses the installed stable Microsoft Edge browser when available and falls back to Google Chrome. The **Isolated** session mode never opens the user's normal browser profile.

**Persistent Chrome** and **Persistent Edge** use profiles owned by CaptureWebsite under:

```text
%LOCALAPPDATA%\CaptureWebsite\browser-profiles\
```

Use **Open session** to open one of those profiles in a visible browser, sign in when needed, and close the browser afterward. Cookies and local storage in that CaptureWebsite-owned profile remain available to later captures that use the same profile.

The default output directory is:

```text
%LOCALAPPDATA%\CaptureWebsite\output/
└── example/
    ├── example-complete/
    └── example-complete.zip
```

## Security model

CaptureWebsite applies the following restrictions by default:

- Isolated captures use a new temporary, non-persistent browser context.
- Persistent sessions use CaptureWebsite-owned profiles and never point to the user's normal Edge or Chrome profile.
- The application is intended to run without administrator privileges.
- The native Edge/Chrome sandbox remains enabled.
- Only HTTP and HTTPS URLs are accepted.
- `localhost`, private/reserved IP addresses, and DNS results that resolve to private addresses are blocked.
- Ports other than 80 and 443 are blocked in safe mode.
- An internal proxy resolves, validates, and pins public IP addresses to prevent DNS rebinding.
- Connections and redirect targets are checked again before they are allowed.
- Service workers, WebSockets, downloads, pop-ups, notifications, and extensions are blocked.
- WebRTC is restricted to reduce exposure of local network addresses.
- Dialogs are dismissed, and forms or buttons are not clicked automatically.
- Authentication headers, cookies, request bodies, and secret-looking URL parameters are excluded or redacted from the WARC and derived metadata.
- Each profile is limited to 128 MiB per response, 2 GiB of decoded bodies, 10,000 routed requests, and 10,000 recorded responses.
- Browser profiles and processes are closed when a capture finishes or is cancelled.
- Existing valid results are replaced only after the new ZIP has been created and verified.

These controls reduce risk, but they do not provide the additional isolation of a virtual machine. A captured site still executes JavaScript inside Edge or Chrome, so an unknown browser vulnerability remains a residual risk. Keep Windows and the browser updated, capture only sites you trust, and do not run the application as administrator. Response bodies can contain sensitive information even when cookies and authentication data are excluded.

The CLI option `--allow-private` disables the private-network and port restrictions for controlled infrastructure testing. It is not available in the GUI and should not be used with third-party URLs.

## Capture output

A capture directory can contain:

```text
example-complete/
├── archive/
│   └── example.wacz
├── html/ css/ js/ fonts/ images/ svg/
├── videos/ audio/ json/ wasm/ other/
├── screenshots/
├── network/
│   ├── requests.json
│   └── responses.json
├── logs/
│   ├── errors.json
│   └── native-browser-*.log
├── manifest.json
└── README.txt
```

The WACZ 1.2 archive contains a compressed WARC 1.1 file, a CDXJ index, integrity hashes, observed request/response pairs, and screenshots stored as `resource` records.

Playwright exposes response bodies after browser decoding. For those bodies, CaptureWebsite removes `Content-Encoding` and `Transfer-Encoding`, recalculates `Content-Length`, and adds:

```text
X-SiteCapture-Body-Representation: decoded-by-browser
```

This prevents decoded bytes from being labeled as gzip or Brotli data.

Responses are classified mainly by `Content-Type`. SHA-256 hashes are calculated from extracted bytes. Identical payloads share one physical file while each URL keeps its own entry in `manifest.json`. Before a capture is reported as complete, the output ZIP is reopened, checked for unsafe paths, and verified with a CRC test.

## Command line

Create a development environment:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Do not run `playwright install`. CaptureWebsite uses the stable Edge or Chrome installation already present on the system.

Examples:

```powershell
.\.venv\Scripts\capturewebsite.exe https://example.com
.\.venv\Scripts\capturewebsite.exe https://example.com --full
.\.venv\Scripts\capturewebsite.exe https://example.com --desktop --mobile
.\.venv\Scripts\capturewebsite.exe https://example.com --timeout 180 --no-scroll
.\.venv\Scripts\capturewebsite.exe https://example.com --browser edge
.\.venv\Scripts\capturewebsite.exe https://example.com --visible-browser
.\.venv\Scripts\capturewebsite.exe https://example.com --session persistent-edge
```

Main options:

- `--full` increases settling and scrolling time while keeping the capture limited to one page.
- `--desktop` and `--mobile` run separate capture profiles. Desktop is the default.
- `--timeout` sets a timeout from 10 to 3600 seconds per profile.
- `--scroll` / `--no-scroll` enables or disables controlled scrolling.
- `--screenshots` / `--no-screenshots` controls screenshot capture.
- `--visible-browser` keeps the selected browser session visible during capture.
- `--session isolated|persistent-chrome|persistent-edge` selects browser state handling.
- `--browser auto|edge|chrome` selects the browser.
- `--output DIRECTORY` changes the output root.
- `--allow-private` disables the private-network restrictions for controlled infrastructure testing.

`--full` does not remove capture limits, crawl the domain, or perform destructive interactions. The page limit remains one.

## Tests and build

Run the test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Build the Windows executable:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build.ps1
.\dist\CaptureWebsite.exe --smoke-test
```

The PyInstaller build bundles the Playwright Python package and driver, but it does not bundle a browser or Docker. CaptureWebsite uses the stable Edge or Chrome installation on the system.

For a real capture integration test:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\integration-test.ps1
```

## Project structure

- `src/sitecapture/capture/native.py` handles browser sessions, network policy, Playwright events, and WARC/WACZ generation.
- `src/sitecapture/extractor/wacz.py` handles streaming WACZ reads, defensive decoding, and safe extraction.
- `src/sitecapture/processor/` contains MIME classification, deterministic naming, deduplication, and manifest generation.
- `src/sitecapture/packager/` builds and verifies the final ZIP.
- `src/sitecapture/core.py` contains the pipeline shared by the CLI and GUI.
- `src/sitecapture/gui/` contains the native Win32 interface. `win32.py` contains the lower-level Windows interop used by the folder picker and read-only log control.
- `tests/` covers network policy, malformed archives, path traversal, deduplication, GUI smoke behavior, and the capture pipeline.

## Limitations

- CaptureWebsite does not bypass CAPTCHA, DRM, authentication, paywalls, or access controls.
- Sites with anti-automation protections can return HTTP 403. Compatibility mode can open a visible browser for legitimate verification, but the site can still refuse the capture.
- Service workers are blocked so requests cannot bypass the network policy. Sites that depend on them may be incomplete.
- Browser downloads are cancelled. Normal HTTP responses observed by the browser can still be archived.
- WebSockets are blocked because their traffic cannot be archived and inspected with the same guarantees as HTTP responses.
- Streaming media and response bodies that are unavailable through the browser API may not be extracted.
- Enterprise Edge or Chrome policies can prevent automation.
- The operator is responsible for having permission to archive the target content.

Browser integration uses Playwright for Python: https://playwright.dev/python/docs/browsers
