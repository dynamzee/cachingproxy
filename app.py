import gzip
import json
import os
import secrets
import urllib
import requests
from time import time

from dotenv import load_dotenv
from redis import Redis
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, Response, request, jsonify

load_dotenv()

REDIS_URL = os.environ.get("REDIS_URL")
PROXY_URL = os.environ.get("PROXY_URL")
PROXY_EXPIRY = int(os.environ.get("PROXY_EXPIRY", 3600))
ADMIN_KEY = os.environ.get("ADMIN_KEY")
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", 100))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization",
    "proxy-authenticate",
}

redis_client = Redis.from_url(
    REDIS_URL,
    decode_responses=False,
    socket_connect_timeout=10,
    retry_on_timeout=True,
)

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=10)

OPEN_PATHS = {"/health"}
ADMIN_PATHS = {"/cache/clear", "/keys/create", "/keys/revoke"}


def compress_data(data):
    return gzip.compress(json.dumps(data).encode("utf-8"))


def decompress_data(compressed_data):
    return json.loads(gzip.decompress(compressed_data).decode("utf-8"))


def is_valid_api_key(api_key):
    if not api_key:
        return False
    return bool(redis_client.exists(f"apikey:{api_key}"))


def check_rate_limit(api_key):
    window = int(time() // 60)
    rate_key = f"ratelimit:{api_key}:{window}"
    pipe = redis_client.pipeline()
    pipe.incr(rate_key)
    pipe.expire(rate_key, 120)
    results = pipe.execute()
    count = results[0]
    return count <= RATE_LIMIT, count


def fetch_from_upstream(url):
    try:
        response = requests.get(
            url,
            timeout=(3, 10),
            stream=True,
            headers={
                "User-Agent": "CachingProxy/1.0",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
            },
        )
        return response
    except requests.exceptions.Timeout:
        print(f"UPSTREAM ERROR: Timeout for {url}")
    except requests.exceptions.ConnectionError:
        print(f"UPSTREAM ERROR: Failed to connect to {url}")
    except requests.exceptions.HTTPError as error:
        print(f"UPSTREAM ERROR: HTTP {error.response.status_code} for {url}")
    except requests.exceptions.RequestException as error:
        print(f"UPSTREAM ERROR: {error}")
    except Exception as error:
        print(f"CRITICAL ERROR: {error}")
    return None


def store_in_cache(key, data, expiry):
    try:
        compressed = compress_data(data)
        redis_client.set(key, compressed, ex=expiry)
    except Exception as error:
        print(f"CACHE WRITE ERROR: {error}")


def filter_headers(headers):
    return [
        (k, v) for k, v in headers
        if k.lower() not in HOP_BY_HOP_HEADERS
    ]


def build_cache_key():
    path = request.path
    query = request.query_string.decode("utf-8")
    return f"cache:{path}?{query}" if query else f"cache:{path}"


# AUTHENTICATION MIDDLEWARE/GENERATING KEYS:

@app.before_request
def authenticate():
    if request.path in OPEN_PATHS:
        return

    if request.path in ADMIN_PATHS:
        admin_key = request.headers.get("X-Admin-Key")
        if not ADMIN_KEY or admin_key != ADMIN_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return

    api_key = request.headers.get("X-API-Key")
    if not is_valid_api_key(api_key):
        return jsonify({"error": "Invalid or missing API key. Pass it as X-API-Key header."}), 401

    allowed, count = check_rate_limit(api_key)
    if not allowed:
        return jsonify({
            "error": "Rate limit exceeded.",
            "limit": RATE_LIMIT,
            "requests_this_minute": count,
        }), 429


# PROXY ROUTE:

@app.route("/", defaults={"path": ""}, methods=["GET"])
@app.route("/<path:path>", methods=["GET"])
def caching_proxy(path):
    cache_key = build_cache_key()

    try:
        cached_data = redis_client.get(cache_key)
        if cached_data is not None:
            response_data = decompress_data(cached_data)
            response = Response(
                response_data["content"].encode("latin-1"),
                response_data["status_code"],
                filter_headers(response_data["headers"].items()),
            )
            response.headers["X-Cache"] = "HIT"
            return response
    except Exception as error:
        print(f"CACHE READ ERROR: {error}")

    full_uri = urllib.parse.urljoin(PROXY_URL, request.full_path)
    upstream_response = fetch_from_upstream(full_uri)

    if upstream_response is None:
        return Response("Upstream server error", 502)

    safe_headers = filter_headers(upstream_response.headers.items())
    response = Response(
        upstream_response.content,
        upstream_response.status_code,
        safe_headers,
    )
    response.headers["X-Cache"] = "MISS"

    if upstream_response.status_code == 200:
        response_data = {
            "content": upstream_response.content.decode("latin-1"),
            "status_code": upstream_response.status_code,
            "headers": dict(safe_headers),
        }
        executor.submit(store_in_cache, cache_key, response_data, PROXY_EXPIRY)

    return response


# ADMIN ROUTES:

@app.route("/health", methods=["GET"])
def health_check():
    try:
        redis_client.ping()
        return jsonify({"status": "healthy", "redis": "connected"}), 200
    except Exception as error:
        return jsonify({"status": "unhealthy", "reason": str(error)}), 503


@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    try:
        deleted = 0
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(cursor, match="cache:*", count=100)
            if keys:
                redis_client.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        return jsonify({"status": "cache cleared", "keys_deleted": deleted}), 200
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/keys/create", methods=["POST"])
def create_api_key():
    label = request.json.get("label", "unnamed") if request.json else "unnamed"
    api_key = secrets.token_urlsafe(32)
    redis_client.set(f"apikey:{api_key}", label.encode())
    return jsonify({"api_key": api_key, "label": label}), 201


@app.route("/keys/revoke", methods=["POST"])
def revoke_api_key():
    data = request.json or {}
    api_key = data.get("api_key")
    if not api_key:
        return jsonify({"error": "api_key is required"}), 400
    deleted = redis_client.delete(f"apikey:{api_key}")
    if deleted:
        return jsonify({"status": "revoked"}), 200
    return jsonify({"error": "API key not found"}), 404


if __name__ == "__main__":
    app.run(port=5000, debug=DEBUG)

