# caching-proxy

A Flask-based HTTP reverse proxy with Redis-backed response caching, API key authentication, per-minute rate limiting, and admin endpoints for key management and cache invalidation.

---

## How It Works

Incoming requests are authenticated via an API key passed in the `X-API-Key` header. If a cached response exists in Redis for the requested path, it is returned immediately with `X-Cache: HIT`. On a cache miss, the request is forwarded to the configured upstream server, the response is returned to the client with `X-Cache: MISS`, and the response body is compressed (gzip) and stored in Redis asynchronously via a `ThreadPoolExecutor` — keeping the response time unaffected by the write.

---

## Features

- **Redis response caching** — compressed with gzip before storage
- **API key authentication** — keys stored in Redis, passed via `X-API-Key` header
- **Sliding-window rate limiting** — per API key, per minute, enforced with Redis pipelines
- **Async cache writes** — background thread pool keeps latency low
- **`X-Cache` headers** — `HIT` or `MISS` on every proxied response
- **Admin endpoints** — create/revoke keys, clear cache
- **Health check** — `/health` verifies Redis connectivity

---

## Requirements

- Python 3.8+
- Redis instance
- Dependencies: `flask`, `redis`, `requests`, `python-dotenv`

---

## Setup

**1. Clone the repo and install dependencies:**

```bash
pip install flask redis requests python-dotenv
```

**2. Create a `.env` file:**

```env
REDIS_URL=redis://localhost:6379
PROXY_URL=https://your-upstream-server.com
PROXY_EXPIRY=3600
ADMIN_KEY=your-secret-admin-key
RATE_LIMIT=100
```

| Variable | Description | Default |
|---|---|---|
| `REDIS_URL` | Redis connection URL | required |
| `PROXY_URL` | Upstream server to proxy to | required |
| `PROXY_EXPIRY` | Cache TTL in seconds | `3600` |
| `ADMIN_KEY` | Key for admin endpoints | required |
| `RATE_LIMIT` | Max requests per minute per API key | `100` |

**3. Run the server:**

```bash
python app.py
```

---

## Authentication

All routes (except `/health`) require authentication.

**Proxy routes** require an API key:

```
X-API-Key: <your-api-key>
```

**Admin routes** require the admin key:

```
X-Admin-Key: <your-admin-key>
```

---

## API Reference

### Proxy

```
GET /<path>
```

Proxies the request to the upstream server. Returns cached response if available.

**Response headers:**

| Header | Value |
|---|---|
| `X-Cache` | `HIT` (served from cache) or `MISS` (fetched from upstream) |

---

### Admin

#### Create an API key

```
POST /keys/create
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "label": "my-client" }
```

```json
{ "api_key": "...", "label": "my-client" }
```

#### Revoke an API key

```
POST /keys/revoke
X-Admin-Key: <admin-key>
Content-Type: application/json

{ "api_key": "..." }
```

#### Clear the cache

```
POST /cache/clear
X-Admin-Key: <admin-key>
```

```json
{ "status": "cache cleared", "keys_deleted": 42 }
```

#### Health check

```
GET /health
```

```json
{ "status": "healthy", "redis": "connected" }
```

---

## Rate Limiting

Each API key is limited to `RATE_LIMIT` requests per minute (default: 100). The limit is tracked using a Redis key scoped to the current 60-second window. Exceeding it returns:

```json
{
  "error": "Rate limit exceeded.",
  "limit": 100,
  "requests_this_minute": 101
}
```

with a `429` status code.

---

## License

MIT


