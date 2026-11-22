# aistudio-api

This repository, [Mooece/ais-pg-upgrade](https://github.com/Mooece/ais-pg-upgrade), publishes the complete customized source with OpenAI compatibility, reply caching, analytics, upstream keepalive and anti-truncation. VPS, Termux and Windows deployments track `main`. Existing installations should follow the [Git upgrade guide](GIT_UPGRADE.zh-CN.md) before pulling updates. The original project is [DeconstructedCube/aistudio-api](https://github.com/DeconstructedCube/aistudio-api); its source history and MIT license are preserved.

<p align="center">
  <a href="https://github.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/API-Gemini%20v1beta-4285F4?style=flat-square&logo=google" alt="Gemini API"></a>
  <a href="https://hub.docker.com/r/bedrockblock/aistudio-api"><img src="https://img.shields.io/badge/Docker-bedrockblock%2Faistudio--api-2496ED?style=flat-square&logo=docker" alt="Docker Hub"></a>
  <a href="https://deepwiki.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/DeepWiki-Docs-5B68E4?style=flat-square" alt="DeepWiki"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/Framework-FastAPI-009688?style=flat-square&logo=fastapi" alt="FastAPI"></a>
  <a href="https://vuejs.org/"><img src="https://img.shields.io/badge/Frontend-Vue%203%20%2B%20Vite-4FC08D?style=flat-square&logo=vuedotjs" alt="Vue 3"></a>
  <a href="https://docs.astral.sh/uv/"><img src="https://img.shields.io/badge/Python-3.11%2B%20%7C%20uv-261230?style=flat-square&logo=python" alt="Python uv"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License"></a>
</p>

<p align="center">
  Google AI Studio reverse proxy gateway that exposes native Google Gemini API interfaces.
</p>

<p align="center">
  <a href="./README.md">中文文档</a>
</p>

---

## Table of Contents

- [Features](#features)
- [Architecture & Documentation](#architecture--documentation)
- [Quick Start](#quick-start)
  - [Android (Termux)](#android-termux)
  - [Windows](#windows)
  - [Linux / macOS](#linux--macos)
  - [Docker Deployment](#docker-deployment)
- [Obtaining & Importing Account Credentials (Cookies)](#obtaining--importing-account-credentials-cookies)
- [Third-Party Client Setup Guide (NextChat / Cherry Studio)](#third-party-client-setup-guide-nextchat--cherry-studio--one-api)
- [Configuration & Environment Variables](#configuration--environment-variables)
- [API Usage Examples](#api-usage-examples)
  - [cURL](#curl)
  - [Python SDK (google-genai)](#python-sdk-google-genai)
  - [Function Calling](#function-calling)
- [Web Management Console](#web-management-console)
- [Development & Testing](#development--testing)
- [Credits & Acknowledgments](#credits--acknowledgments)
- [License](#license)

---

## Features

| Feature | Description |
|---|---|
| **Gemini API Compatibility** | Supports `/v1beta/...` endpoints including Thinking, multimodal inputs, function calling, image generation, TTS audio synthesis, and video generation |
| **OpenAI Chat Completions Compatibility** | Provides `/v1/chat/completions` and `/v1/models` with text chat, SSE streaming, function calling, JSON mode, and base64 data URL image input |
| **Dynamic Model Discovery** | Automatically synchronizes and discovers available models from upstream Google AI Studio |
| **Multi-Account Rotation** | Manages multiple accounts with automatic failover, per-model 429 cooldown isolation, Pacific midnight reset, paid-tier preemption, and 7-minute periodic Cookie rotation (`RotateCookies`) |
| **Runtime State Persistence** | Model usage statistics (`stats.json`) and account 429 cooldown & quota status (`rotator_state.json`) are automatically persisted to disk across restarts |
| **Downstream Parameter Fault Tolerance** | `drop_unsupported_params` automatically drops unsupported safety categories (e.g. `HARM_CATEGORY_CIVIC_INTEGRITY`) and tools to prevent 400 errors |
| **Multi-Account Probing** | Extracts and verifies sub-accounts (`u/0`, `u/1`...) automatically when importing a multi-session Cookie |
| **Built-in Tools** | Supports official tools such as Google Search, Google Maps, URL Context, and Code Execution sandbox |
| **Local WAA IPC Signing** | Browserless architecture powered by local Go `waa-signer` (SpiderMonkey 152) over multiplexed stdio NDJSON IPC, aligned with Firefox 152 User-Agent/headers and 5-stage 403 recovery |
| **Web Management Console** | Built-in web dashboard for account management (groups, presets, text/JSON bundle imports), live request metrics, logging/dump controls, and `config.yaml` hot reloading |
> [!NOTE]
> **Memory Usage**: The local Go/SpiderMonkey signer creates WAA workers on demand per account. Memory varies with initialized accounts, concurrent requests, and image/video inputs; the project does not publish a fixed RSS benchmark across these workloads. On a 2 GB VPS, start with a small number of accounts and low concurrency, then monitor memory under real requests.
## Architecture & Documentation

```
┌────────────────────────────────────────────────────────┐
│               Client (cURL / Python SDK)               │
└───────────────────────────┬────────────────────────────┘
                            │ /v1beta/models/...
┌───────────────────────────▼────────────────────────────┐
│              FastAPI HTTP Gateway (Port 8080)          │
├───────────────────────────┬────────────────────────────┤
│    Account Rotator        │    Chat & Wire Codec       │
│  (Sticky & 429 Isolation) │   (Protobuf-JSON / WAA)    │
└───────────────────────────┼────────────────────────────┘
                            │ Proof Mint & HTTP/2 Stream
┌───────────────────────────▼────────────────────────────┐
│       Browserless WAA Engine (waa-signer via stdio IPC)│
└───────────────────────────┬────────────────────────────┘
                            │ HTTPS / RPC
┌───────────────────────────▼────────────────────────────┐
│         Google AI Studio (alkalimakersuite-pa)         │
└────────────────────────────────────────────────────────┘
```

Technical reference documentation:

- [System Architecture Specification (ARCHITECTURE.md)](./docs/ARCHITECTURE.md): Layered architecture, request lifecycle, wire codec, and concurrency model.
- [Wire Protocol Reverse Engineering Specification (WIRE_SPECIFICATION.md)](./docs/WIRE_SPECIFICATION.md): Protobuf-JSON array mappings, field indices, multimodal Part structures, and Schema encoding.
- [BotGuard Verification Chain (BOTGUARD_VERIFICATION_CHAIN.md)](./docs/BOTGUARD_VERIFICATION_CHAIN.md): WAA challenge handshake, content hashing, snapshot signing, and upstream validation.
- [WAA VM Architecture & Signing Specification (WAA_VM_ARCHITECTURE.md)](./docs/WAA_VM_ARCHITECTURE.md): Polymorphic instruction set, 5-stage ARX stream key derivation, DOM probes, and `!proof` TLV schema.
- [Agent & Engineering Conventions (AGENT.md)](./AGENT.md): Strict type specifications, testing standards, and development guidelines.

---

## Quick Start

Except for Docker, all native platforms use a unified one-command installer and launcher that automatically configures Python 3.11+, `uv`, Go, Zig, SpiderMonkey static libraries, and builds `waa-signer`.

Use your own Git repository containing these customizations for Termux, Windows, and VPS deployments. For a fresh deployment, `.env.example` configures an ignored `config.local.yaml` for runtime settings. Existing deployments should migrate their active settings before the first pull. See the [three-platform Git update guide](GIT_UPGRADE.zh-CN.md).

### Android (Termux)

> [!TIP]
> **Download Notice**: Always install Termux from [F-Droid](https://f-droid.org/packages/com.termux/) or [GitHub Releases](https://github.com/termux/termux-app/releases). **Do NOT install from the Google Play Store** (the Play Store build is obsolete and will produce repository update errors).

Open Termux and run:

```bash
pkg update -y && pkg install -y git
REPO_URL='https://github.com/Mooece/ais-pg-upgrade.git'
git clone --branch main "$REPO_URL" aistudio-api
cd aistudio-api
cp .env.example .env
bash install.sh
bash start.sh
```

Once started:
1. Open your phone's browser and go to **`http://localhost:8080`** to access the Web Console;
2. Follow the [Cookie Import Guide](#obtaining--importing-account-credentials-cookies) below to import your credentials;
3. **Background Persistence Tip**: Pull down Android's notification bar, tap `Acquire Wakelock` on Termux's notification, and disable battery optimization for Termux in phone system settings.

---

### Windows

Prerequisite: install [Git for Windows](https://git-scm.com/). The commands below install from this repository's `main` branch.

Double-click **`install.bat`** (or run `.\install.bat`) to set up dependencies and compile `waa-signer.exe` automatically, then double-click **`start.bat`** (or run `.\start.bat`) to launch:

```bat
git clone --branch main https://github.com/Mooece/ais-pg-upgrade.git aistudio-api
cd aistudio-api
copy .env.example .env
.\install.bat
.\start.bat
```

Open **`http://localhost:8080`** in your browser to access the Web Console.

---

### Linux / macOS

```bash
git clone --branch main https://github.com/Mooece/ais-pg-upgrade.git aistudio-api
cd aistudio-api
cp .env.example .env
bash install.sh
bash start.sh
```

> Useful flags: `bash install.sh --start` (launch right after build), `bash install.sh --proxy http://127.0.0.1:7890`, `bash install.sh --rebuild`.

Open **`http://localhost:8080`** in your browser to access the Web Console.

---

### Docker Deployment

> [!NOTE]
> The project's `Dockerfile` uses a multi-stage build that **automatically downloads dependencies and compiles** `waa-signer` inside the image. The container starts with the native local signing daemon enabled out of the box—no manual build steps required.

> [!WARNING]
> The generic commands below are for local/testing use. With the default empty `api_keys`, the API has no authentication, and `-p 8080:8080` listens on all interfaces. Do not expose this setup publicly. For public hosting, follow [the 2 GB VPS deployment guide](DEPLOY_2GB.md) to configure API keys, a reverse proxy, and HTTPS.

```bash
docker run -d \
  --name aistudio-api \
  --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v aistudio-api-data:/app/data \
  ghcr.io/deconstructedcube/aistudio-api:latest
```
Or run with Docker Compose:

```bash
docker compose up -d
```
Open **`http://localhost:8080`** on the host to access the Web Console. The upstream GHCR image includes the OpenAI-compatible routes only after it is built from a source version containing those changes.

---

## Obtaining & Importing Account Credentials (Cookies)

This service does not store your Google account password. It uses standard web session credentials with automated periodic Cookie rotation.

### Step 1: Obtain Google AI Studio Cookies

1. Visit and log in to [Google AI Studio](https://aistudio.google.com/) in your browser, ensuring you can reach the chat workspace;
2. Install the free **Cookie-Editor** browser extension (available on Chrome Web Store, Edge Add-ons, or Firefox Add-ons);
3. On the `aistudio.google.com` tab, click the Cookie-Editor extension icon;
4. Click **Export** in the bottom-right and select **Export as JSON** (or Export as Header); the data is automatically copied to your clipboard.

### Step 2: Import into Web Console

1. Access the Web Console (default `http://localhost:8080`);
2. Click **Accounts** in the left sidebar;
3. Click **"+ Import Credentials"** in the top right;
4. Paste your clipboard data into the input box and click **Confirm Import**;
5. The service will automatically test connectivity and probe any multi-login sub-accounts (`u/0`, `u/1`...) to register them for load balancing.

---

## Third-Party Client Setup Guide (NextChat / Cherry Studio / One API)

The service exposes both native Gemini endpoints and OpenAI Chat Completions-compatible endpoints.

| Setting | Value | Description |
|---|---|---|
| **Provider / Model Type** | **OpenAI** or **OpenAI Compatible** | Select the OpenAI-compatible protocol |
| **API Base URL** | `http://localhost:8080/v1` | For public deployment, use `https://your-domain/v1` |
| **API Key** | A client API key created in the management console | Configure and use a valid API key before public access; do not use the admin password |
| **Model** | `gemini-3.7-flash`, etc. | Query `GET /v1/models` for available model IDs |

The compatibility routes support text chat, streaming, function calling, JSON mode, and base64 data URL images. They currently require `n=1` and do not fetch remote `image_url` URLs. Image, audio, and video generation, plus Gemini-specific extensions, remain available through the native Gemini API.

OpenAI SSE sends a comment heartbeat every 10 idle seconds. Non-streaming requests waiting longer than 10 seconds send whitespace keepalives, followed by one complete JSON response. Once non-streaming keepalive starts, HTTP status remains 200; later failures appear in `error.code` / `error.type`, so clients must inspect the final JSON. If SillyTavern's non-streaming relay waits for complete JSON before responding to the browser, it will not forward whitespace on that leg, whose timeout still needs adjustment.

`request_lifecycle` diagnostics log metadata such as request stages, timing, and connection status at INFO by default, without request or response bodies. A successful ASGI `send()` only means the server accepted the send; it does not prove delivery to the actual client.

### Google Upstream Keepalive and Anti-Truncation Models

While waiting for upstream response data, idle Google HTTP/2 connections send a protocol PING every 25 seconds by default. This runs separately from client heartbeats and does not make additional model requests. PING/ACK frames do not reset the business read timeout. They can help maintain idle connections but cannot extend a fixed server-side request deadline.

Refresh the model list in SillyTavern using the OpenAI-compatible API to select a regular model or its `[防截断]` alias, for example `gemini-3.7-flash[防截断]`. The alias asks the model to place its complete answer in the internal `aistudio_full_reply` tool's `content` argument. The gateway validates and unwraps it into ordinary assistant content. No client tool installation or execution is needed.

This mode buffers and validates the complete tool result before sending answer text, so the first content arrives later than normal streaming; heartbeats continue while waiting. Unsupported tools, incomplete results, token limits, and safety stops produce explicit errors. It does not bypass model limits or automatically continue a response. Use a regular model when client tools or JSON response formatting are required. Native Gemini endpoints do not expose these aliases.

The management-authenticated `GET /rotation` includes `upstream_keepalive` installation status, interval, process-local PING/ACK counters, and the latest error type. `installed` may remain false until the first upstream client is created; counters can remain zero until an idle wait occurs.

## Configuration & Environment Variables

Open `http://localhost:8080` in your browser to access the Web Console.

### Environment Variables
| Variable | Type | Default | Description |
|---|---|---|---|
| `AISTUDIO_PORT` | int | `8080` | HTTP service listening port |
| `AISTUDIO_WEB_PASSWORD` | string | `""` | Web Console access password (also checks `AISTUDIO_ADMIN_PASSWORD`) |
| `AISTUDIO_PROXY` | string | `""` | Outbound proxy URL (`http://`, `https://`, or `socks5://`) |
| `AISTUDIO_UPSTREAM_H2_PING_INTERVAL_SECONDS` | float | `25` | Idle Google HTTP/2 PING interval, from 5 to 240 seconds; restart after changing |
| `AISTUDIO_DATA_DIR` | string | `data/` | Main data directory for persistent files (`/app/data` in Docker) |
| `AISTUDIO_ACCOUNTS_DIR` | string | `data/accounts` | Directory for storing account credentials |
| `AISTUDIO_STATS_FILE` | string | `data/stats.json` | Persistent storage file for model usage metrics |
| `AISTUDIO_ROTATOR_STATE_FILE` | string | `data/rotator_state.json` | Persistent storage file for account 429 cooldowns and dispatch states |
| `AISTUDIO_CONFIG_FILE` | string | `config.yaml` | Set to `config.local.yaml` by `.env.example` for Git deployments. Existing external paths can be preserved |
| `AISTUDIO_PERSIST_STATS` | bool | `true` | Persist request and token usage metrics across service restarts |
| `AISTUDIO_PERSIST_ROTATOR` | bool | `true` | Persist account 429 rate limit dates & cooldowns across restarts |
| `AISTUDIO_DEFAULT_TEXT_MODEL` | string | `gemini-3.7-flash` | Default text model |
| `AISTUDIO_TIMEZONE` | string | Auto | Wire protocol timezone (`America/Los_Angeles` by default) |
| `AISTUDIO_DUMP_REQUESTS` | bool | `false` | Dump API request/response exchanges to console logs (enabled via `DEBUG=1`) |
| `AISTUDIO_DUMP_FILE` | bool | `false` | Dump request/response exchanges to individual disk files |
| `AISTUDIO_DUMP_DIR` | string | `dumps/` | Target directory for dumped exchange files |

---

## API Usage Examples

Supported authentication methods:
- Query param: `?key=YOUR_API_KEY`
- Header: `x-goog-api-key: YOUR_API_KEY`, `x-api-key: YOUR_API_KEY`, or `Authorization: Bearer YOUR_API_KEY`

### OpenAI-compatible Chat Completions

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [
      {"role": "system", "content": "Answer concisely."},
      {"role": "user", "content": "Explain quantum computing in one sentence."}
    ]
  }'
```

For the OpenAI Python SDK, set `base_url` to `http://localhost:8080/v1`; for remote access, use `https://your-domain/v1`. The original `/v1beta/...` routes remain available.

### cURL

<details>
<summary><b>1. List Models</b></summary>

```bash
curl http://localhost:8080/v1beta/models \
  -H "x-goog-api-key: your-api-key"
```
</details>

<details>
<summary><b>2. Generate Content (Non-streaming)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:generateContent \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "Explain quantum computing in one sentence."}]
      }
    ]
  }'
```
</details>

<details>
<summary><b>3. Stream Generate Content (SSE)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:streamGenerateContent?alt=sse \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "Explain how Python asyncio works."}]
      }
    ]
  }'
```
</details>

---

### Python SDK (google-genai)

Using the official `google-genai` Python SDK:

```python
from google import genai

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "http://localhost:8080",
    },
)

# 1. Streaming response
response = client.models.generate_content_stream(
    model="gemini-3.7-flash",
    contents="What is reactive programming?",
)
for chunk in response:
    print(chunk.text, end="", flush=True)

# 2. Non-streaming response
result = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="Write a quicksort implementation in Python.",
)
print(result.text)
```

---

### Function Calling

```python
from google import genai
from google.genai import types

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "http://localhost:8080",
    },
)

# Tool declaration
weather_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="get_current_weather",
            description="Get real-time weather conditions for a given location",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "location": types.Schema(
                        type="STRING",
                        description="City name, e.g. 'San Francisco' or 'Tokyo'",
                    ),
                    "unit": types.Schema(
                        type="STRING",
                        enum=["celsius", "fahrenheit"],
                        description="Temperature unit",
                    ),
                },
                required=["location"],
            ),
        )
    ]
)

response = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="What is the weather in Tokyo right now?",
    config=types.GenerateContentConfig(tools=[weather_tool]),
)

if response.function_calls:
    for call in response.function_calls:
        print(f"Tool called: {call.name}")
        print(f"Arguments: {call.args}")
```

---

## Web Management Console

This workspace adds usage analytics with time/model/result filters, request and token trends,
model/endpoint distributions, average/P95 duration, first meaningful output latency for successful
streams, and paginated request metadata with current-page CSV export. Existing cumulative model
and account statistics remain independent. New request history starts after upgrading and lives
in `data/request_analytics.sqlite3`, bounded to 90 days and 100,000 records. It stores metrics,
not prompts, response text, or API keys. Internal retries do not create additional client request
rows; duration averages include completed requests, and missing token usage is shown as unknown.

For an isolated sample-data UI preview, run `uv run python scripts/preview_dashboard.py --port 8766`
and open `http://127.0.0.1:8766`. The preview binds to loopback and does not modify real accounts
or statistics. Add `--empty` for the empty state; normal deployments keep their usual launcher.

The service includes a built-in static Web Console served at `http://localhost:8080`:

- **Dashboard**: Real-time view of service health, per-model request stats, success rates, and rate-limit cooldown status.
- **Accounts**: Import Cookie credentials (single text or bundle files), auto-probe multiple accounts, organize groups and one-click presets, switch the active account, or reset rate limits.
- **Settings**: Online editor for `config.yaml` with instant hot-reloading, local WAA IPC signer status, API key management, and dynamic log/dump settings.
- **Access Control**: Enable authentication by setting `AISTUDIO_WEB_PASSWORD`.

---

## Development & Testing

Frontend code is located in `web/` (Vue 3 + TypeScript + Vite + Tailwind CSS):

```bash
cd web
bun install
bun run dev          # Start local Vite dev server (localhost:3000 -> 8080)
bun run type-check   # Run TypeScript type check
bun run lint         # Run ESLint check
bun run build        # Build production assets into src/aistudio_api/static
```

Python quality checks and tests:

```bash
# Run code style & lint check
uv run ruff check .
# Run static type checking
uv tool run pyright src tests
# Run Python test suite
uv run pytest
# Run Go WAA signer unit tests
bash waa-signer/build.sh --test
```

---

## Credits & Acknowledgments

During reverse engineering, attestation protocol analysis, and gateway implementation, this project referenced and built upon the following open-source projects and community works:

- **[Mag1cFall/AIStudio2API](https://github.com/Mag1cFall/AIStudio2API)**: Provided the embedded SpiderMonkey 152 runtime and Firefox 152 DOM / WebIDL host environment implementation, along with the uTLS transport stack design. The standalone `waa-signer` module is extracted and adapted from its core logic.
- **[Lianues/aistudio-pg-bridge](https://github.com/Lianues/aistudio-pg-bridge)**: Pioneered the lightweight browserless WAA proof signing service concept and established the `/v1/proof/mint` API interface specification.
- **[spinsnow](https://waa.spinsnow.fun)**: Provided a public, stable Node.js VM WAA attestation relay service.
- **[Camoufox](https://github.com/daijro/camoufox)** & **[go-browserforge](https://github.com/brainplusplus/go-browserforge)**: Provided real-world browser fingerprint datasets and anti-fingerprinting generation algorithms.

---

## License

This project is licensed under the [MIT License](./LICENSE).
