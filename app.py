import gzip
import os
import pickle
import urllib

from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

from urllib import parse
import redis
from redis import Redis
import requests
from flask import Flask, Response, g, request

load_dotenv()

REDIS_URL = os.environ.get("REDIS_URL")
PROXY_URL = os.environ.get("PROXY_URL")
PROXY_EXPIRY = int(os.environ.get("PROXY_EXPIRY", 3600))

app = Flask(__name__)

redis_pool = redis.ConnectionPool.from_url(
    REDIS_URL,
    decode_responses=False,
    socket_connect_timeout=10,
    socket_timeout=10,
    retry_on_timeout=True,
)

executor = ThreadPoolExecutor(max_workers=10)

@app.before_request
def setup_database():
    """Set up the database"""
    g.r = Redis( connection_pool=redis_pool)

def fetch_from_upstream(url):
    """Fetch data from upstream server"""
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
        print(f"UPSTREAM ERROR: Timeout occurred for {url}")
    except requests.exceptions.ConnectionError:
        print(f"UPSTREAM ERROR: Failed to connect to {url}")
    except requests.exceptions.HTTPError as error:
        print(f"UPSTREAM ERROR: HTTP {error.response.status_code} for {url}")
    except requests.exceptions.RequestException as error:
        print(f"UPSTREAM ERROR: A serious error occurred: {error}")
    except Exception as error:
        print(f"CRITICAL UNKNOWN ERROR: {error}")

    return None

def compress_data(data):
    """Compress data for efficient storage"""
    return gzip.compress(pickle.dumps(data))

def decompress_data(compressed_data):
    """Decompress data from storage"""
    return pickle.loads(gzip.decompress(compressed_data))

@app.route("/", defaults={"path": ""}, methods=["GET"])
@app.route("/<path:path>", methods=["GET"])
def caching_proxy(path):
    cache_key = f"cache:{request.full_path}"

    try:
        cached_data = g.r.get(cache_key)
        if cached_data is not None:
            # Cache hit - decompress and return
            response_data = decompress_data(cached_data)
            response = Response(
                response_data["content"],
                response_data["status_code"],
                response_data["headers"],
            )
            response.headers["X-Cache"] = "HIT"

            return response
    except Exception as error:
        print(f"CACHE READ ERROR: {error}.")

    # Cache miss - fetch from upstream
    full_uri = urllib.parse.urljoin(PROXY_URL, request.full_path)
    upstream_response = fetch_from_upstream(full_uri)

    if upstream_response is None:
        return Response("Upstream server error", 502)

    # Prepare response
    response = Response(
        upstream_response.content,
        upstream_response.status_code,
        upstream_response.headers.items(),
    )
    response.headers["X-Cache"] = "MISS"

    # Cache the response asynchronously (fire and forget)
    if upstream_response.status_code == 200:
        response_data = {
            "content": upstream_response.content,
            "status_code": upstream_response.status_code,
            "headers": dict(upstream_response.headers),
        }

        # Store in cache asynchronously
        executor.submit(store_in_cache, g.r, cache_key, response_data, PROXY_EXPIRY)

    return response


def store_in_cache(redis_client, key, data, expiry):
    """Store data in cache asynchronously"""
    try:
        compressed_data = compress_data(data)
        redis_client.set(key, compressed_data, ex=expiry)
    except Exception as error:
        print(f"CACHE WRITE ERROR: {error}.")

@app.route("/health", methods=["GET"])
def health_check():
    try:
        # Ping Redis to make sure the connection is alive
        g.r.ping()
        return {"status": "healthy", "redis": "connected"}, 200
    except Exception as error:
        return {"status": "unhealthy", "reason": str(error)}, 503

@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    """Clear cache endpoint"""
    try:
        g.r.delete(*g.r.keys("cache:*"))
        return {"status": "cache cleared"}
    except Exception as error:
        return {"error": str(error)}, 500

if __name__ == "__main__":
    app.run(port=5000, debug=True)





