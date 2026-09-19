"""Apify adapter for scrapeai/yahoo-news-scraper. Standard library only."""
import json
import math
import os
import re
import time
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import NewsBatch, NewsProviderError

ACTOR = "scrapeai~yahoo-news-scraper"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _HTTP:
    def __init__(self, token):
        self._token = token
        self._opener = build_opener(_NoRedirect())

    def request(self, method, path, body=None, *, timeout=15):
        request = Request(
            "https://api.apify.com/v2/" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise NewsProviderError("Apify response exceeds the size limit.")
            return json.loads(data)
        except HTTPError as exc:
            exc.close()
            raise NewsProviderError(f"Apify HTTP {exc.code}; check credentials, quota or service status.") from None
        except (URLError, TimeoutError, OSError):
            raise NewsProviderError("Apify network request failed or timed out.") from None
        except (ValueError, UnicodeError):
            raise NewsProviderError("Apify returned invalid JSON.") from None


class ApifyNewsProvider:
    name = "apify/scrapeai/yahoo-news-scraper"
    is_fixture = False

    def __init__(self, token=None, *, max_charge_usd=0.05, transport=None):
        if not math.isfinite(max_charge_usd) or not 0 < max_charge_usd <= 1:
            raise ValueError("max_charge_usd must be in (0, 1] per query.")
        self.max_charge_usd = max_charge_usd
        if transport is None:
            token = token or os.environ.get("APIFY_TOKEN", "")
            if not token or any(c.isspace() for c in token):
                raise NewsProviderError("Set APIFY_TOKEN in the terminal environment.")
            transport = _HTTP(token)
        self._http = transport

    def search(self, query: str, *, limit=10, lookback_days=7, deadline=None) -> NewsBatch:
        if not isinstance(query, str) or not query.strip() or len(query) > 240:
            raise ValueError("A search query must contain 1–240 characters.")
        if not 1 <= limit <= 20 or not 1 <= lookback_days <= 30:
            raise ValueError("Use 1–20 results and a 1–30 day window.")
        time_range = "1d" if lookback_days == 1 else "7d" if lookback_days <= 7 else "30d"
        deadline = min(deadline or float("inf"), time.monotonic() + 28)

        def request(method, path, body=None):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NewsProviderError("News search reached its time limit.")
            if isinstance(self._http, _HTTP):
                return self._http.request(method, path, body, timeout=min(15, remaining))
            return self._http.request(method, path, body)

        params = urlencode({
            "timeout": max(1, min(28, int(deadline - time.monotonic()))), "maxItems": limit,
            "maxTotalChargeUsd": self.max_charge_usd, "waitForFinish": 0,
        })
        # Never retry this POST: a lost response could otherwise create paid duplicates.
        response = request("POST", f"actors/{ACTOR}/runs?{params}", {
            "query": query, "maxItems": limit, "timeRange": time_range,
            "sortBy": "date", "country": "lang_en", "language": "en",
        })
        run = self._run(response)
        run_id = self._id(run.get("id"))
        try:
            for _ in range(12):
                if run.get("status") in {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}:
                    break
                if run.get("status") not in {"READY", "RUNNING", "TIMING-OUT", "ABORTING"}:
                    raise NewsProviderError("Apify returned an unknown run status.")
                run = self._run(request("GET", f"actor-runs/{run_id}?waitForFinish=5"))
        except NewsProviderError:
            self._abort(run_id)
            raise
        if run.get("status") != "SUCCEEDED":
            if run.get("status") not in {"FAILED", "ABORTED", "TIMED-OUT"}:
                self._abort(run_id)
            raise NewsProviderError(f"Apify run {run_id} did not succeed; no automatic rerun.")
        dataset_id = self._id(run.get("defaultDatasetId"))
        items = request(
            "GET", f"datasets/{dataset_id}/items?format=json&clean=true&limit={limit}",
        )
        if not isinstance(items, list):
            raise NewsProviderError("Apify dataset must be a list.")
        return NewsBatch(items=items[:limit], retrieved_at=datetime.now(UTC), run_id=run_id)

    def _abort(self, run_id):
        """Best effort only; the remote actor also has its own runtime limit."""
        try:
            if isinstance(self._http, _HTTP):
                self._http.request("POST", f"actor-runs/{run_id}/abort", timeout=2)
            else:
                self._http.request("POST", f"actor-runs/{run_id}/abort")
        except NewsProviderError:
            pass

    @staticmethod
    def _run(response):
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            raise NewsProviderError("Apify returned an invalid run response.")
        return response["data"]

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]+", value):
            raise NewsProviderError("Apify returned an invalid resource ID.")
        return value
