"""Live benchmark: realistic bugs the agent must fix with the real model.

Each case ships a buggy mini-project and an issue written the way a user would write it (symptom,
no hint at the fix). A hidden *oracle* test, never shown to the agent, decides whether the final
patch is actually correct. That separates two things the agent itself cannot tell apart:

  claimed  = the agent's own evidence says "verified" (a test failed before the fix, passes after)
  correct  = the hidden oracle also passes on the patched code

`reference` is the known-good fix; the offline test-suite proves every case is solvable and that
the oracle really fails on the buggy code, so a benchmark failure is the model's, not the case's.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FASTAPI_REQ = "fastapi\nhttpx\npytest\n"


@dataclass
class BenchCase:
    id: str
    difficulty: str  # easy | medium | hard
    kind: str  # python | fastapi
    files: dict[str, str]
    issue: str
    oracle: str  # hidden pytest module, written to test_oracle.py
    reference: dict[str, tuple[str, str]] = field(default_factory=dict)  # path -> (old, new)


def _api(body: str) -> str:
    return (
        "from fastapi import FastAPI, HTTPException, Header, Query\nfrom pydantic import BaseModel, Field\n\n"
        + body
    )


BENCH: list[BenchCase] = [
    # ---------------------------------------------------------------- plain Python, easy
    BenchCase(
        "slugify-double-hyphen",
        "easy",
        "python",
        {
            "textutil.py": 'import re\n\n\ndef slugify(text):\n    text = text.lower().strip()\n    return re.sub(r"[^a-z0-9]", "-", text)\n',
            "test_textutil.py": 'from textutil import slugify\n\n\ndef test_basic():\n    assert slugify("Hello") == "hello"\n',
        },
        'slugify("Hello,  World!") gives "hello--world-". I expect "hello-world" (no repeated or trailing hyphens).',
        'from textutil import slugify\n\n\ndef test_o():\n    assert slugify("Hello,  World!") == "hello-world"\n    assert slugify("a_b") == "a-b"\n',
        {
            "textutil.py": (
                'return re.sub(r"[^a-z0-9]", "-", text)',
                'return re.sub(r"[^a-z0-9]+", "-", text).strip("-")',
            )
        },
    ),
    BenchCase(
        "median-even-length",
        "easy",
        "python",
        {
            "stats.py": "def median(values):\n    s = sorted(values)\n    return s[len(s) // 2]\n",
            "test_stats.py": "from stats import median\n\n\ndef test_odd():\n    assert median([3, 1, 2]) == 2\n",
        },
        "median([1, 2, 3, 4]) returns 3 but it should be 2.5.",
        "from stats import median\n\n\ndef test_o():\n    assert median([1, 2, 3, 4]) == 2.5\n    assert median([5]) == 5\n    assert median([9, 1, 5]) == 5\n",
        {
            "stats.py": (
                "    return s[len(s) // 2]\n",
                "    n = len(s)\n    mid = n // 2\n    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2\n",
            )
        },
    ),
    BenchCase(
        "chunk-drops-tail",
        "easy",
        "python",
        {
            "lists.py": "def chunk(items, n):\n    return [items[i:i + n] for i in range(0, len(items) - n, n)]\n",
            "test_lists.py": "from lists import chunk\n\n\ndef test_even():\n    assert chunk([1, 2, 3, 4], 2)[0] == [1, 2]\n",
        },
        "chunk([1,2,3,4,5], 2) loses data: I get [[1, 2], [3, 4]] and the 5 disappears.",
        "from lists import chunk\n\n\ndef test_o():\n    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]\n    assert chunk([1, 2], 2) == [[1, 2]]\n    assert chunk([], 3) == []\n",
        {"lists.py": ("range(0, len(items) - n, n)", "range(0, len(items), n)")},
    ),
    BenchCase(
        "leap-year-centuries",
        "easy",
        "python",
        {
            "calendar_utils.py": "def is_leap_year(year):\n    return year % 4 == 0\n",
            "test_calendar_utils.py": "from calendar_utils import is_leap_year\n\n\ndef test_2024():\n    assert is_leap_year(2024)\n",
        },
        "is_leap_year(1900) says True. 1900 was not a leap year (but 2000 was).",
        "from calendar_utils import is_leap_year\n\n\ndef test_o():\n    assert not is_leap_year(1900)\n    assert is_leap_year(2000)\n    assert is_leap_year(2024)\n    assert not is_leap_year(2023)\n",
        {
            "calendar_utils.py": (
                "return year % 4 == 0",
                "return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)",
            )
        },
    ),
    BenchCase(
        "wordcount-case",
        "easy",
        "python",
        {
            "words.py": "def word_count(text):\n    counts = {}\n    for w in text.split():\n        counts[w] = counts.get(w, 0) + 1\n    return counts\n",
            "test_words.py": "from words import word_count\n\n\ndef test_simple():\n    assert word_count('a b a') == {'a': 2, 'b': 1}\n",
        },
        'word_count("The the THE cat") counts three different words. Case should not matter.',
        'from words import word_count\n\n\ndef test_o():\n    assert word_count("The the THE cat") == {"the": 3, "cat": 1}\n',
        {
            "words.py": (
                "counts[w] = counts.get(w, 0) + 1",
                "w = w.lower()\n        counts[w] = counts.get(w, 0) + 1",
            )
        },
    ),
    BenchCase(
        "money-truncation",
        "easy",
        "python",
        {
            "billing.py": "def with_tax(cents, rate):\n    return int(cents * (1 + rate))\n",
            "test_billing.py": "from billing import with_tax\n\n\ndef test_zero_rate():\n    assert with_tax(100, 0) == 100\n",
        },
        "with_tax(999, 0.07) returns 1068 but should be 1069 (1068.93 rounds to 1069). We lose a cent on many orders.",
        "from billing import with_tax\n\n\ndef test_o():\n    assert with_tax(999, 0.07) == 1069\n    assert with_tax(100, 0.1) == 110\n",
        {"billing.py": ("return int(cents * (1 + rate))", "return round(cents * (1 + rate))")},
    ),
    # ---------------------------------------------------------------- plain Python, medium
    BenchCase(
        "duration-minutes",
        "medium",
        "python",
        {
            "durations.py": 'import re\n\nUNITS = {"h": 3600, "m": 1, "s": 1}\n\n\ndef parse_duration(text):\n    total = 0\n    for amount, unit in re.findall(r"(\\d+)([hms])", text):\n        total += int(amount) * UNITS[unit]\n    return total\n',
            "test_durations.py": 'from durations import parse_duration\n\n\ndef test_hours():\n    assert parse_duration("2h") == 7200\n\n\ndef test_secs():\n    assert parse_duration("45s") == 45\n',
        },
        'parse_duration("1h30m") returns 3630 but should be 5400 seconds.',
        'from durations import parse_duration\n\n\ndef test_o():\n    assert parse_duration("1h30m") == 5400\n    assert parse_duration("2m10s") == 130\n',
        {"durations.py": ('"m": 1,', '"m": 60,')},
    ),
    BenchCase(
        "intervals-touching",
        "medium",
        "python",
        {
            "intervals.py": "def merge(intervals):\n    out = []\n    for start, end in intervals:\n        if out and start < out[-1][1]:\n            out[-1][1] = max(out[-1][1], end)\n        else:\n            out.append([start, end])\n    return out\n",
            "test_intervals.py": "from intervals import merge\n\n\ndef test_overlap():\n    assert merge([[1, 3], [2, 4]]) == [[1, 4]]\n",
        },
        "merge([[5, 6], [1, 2], [2, 3]]) returns [[5, 6], [1, 2], [2, 3]]. Expected [[1, 3], [5, 6]]: input is not always sorted and intervals that touch should merge.",
        "from intervals import merge\n\n\ndef test_o():\n    assert merge([[5, 6], [1, 2], [2, 3]]) == [[1, 3], [5, 6]]\n    assert merge([[1, 3], [2, 4]]) == [[1, 4]]\n",
        {
            "intervals.py": (
                "    for start, end in intervals:\n        if out and start < out[-1][1]:",
                "    for start, end in sorted(intervals):\n        if out and start <= out[-1][1]:",
            )
        },
    ),
    BenchCase(
        "ratelimit-off-by-one",
        "medium",
        "python",
        {
            "ratelimit.py": "class RateLimiter:\n    def __init__(self, limit, window):\n        self.limit, self.window, self.hits = limit, window, []\n\n    def allow(self, now):\n        self.hits = [t for t in self.hits if now - t < self.window]\n        if len(self.hits) > self.limit:\n            return False\n        self.hits.append(now)\n        return True\n",
            "test_ratelimit.py": "from ratelimit import RateLimiter\n\n\ndef test_first_allowed():\n    assert RateLimiter(2, 10).allow(0)\n",
        },
        "RateLimiter(limit=2, window=10) lets 3 requests through inside the same window. The third call at t=2 should be rejected.",
        "from ratelimit import RateLimiter\n\n\ndef test_o():\n    r = RateLimiter(2, 10)\n    assert r.allow(0) and r.allow(1)\n    assert not r.allow(2)\n    assert r.allow(11)\n",
        {"ratelimit.py": ("len(self.hits) > self.limit", "len(self.hits) >= self.limit")},
    ),
    BenchCase(
        "lru-get-recency",
        "medium",
        "python",
        {
            "cache.py": "from collections import OrderedDict\n\n\nclass LRU:\n    def __init__(self, cap):\n        self.cap, self.d = cap, OrderedDict()\n\n    def get(self, k):\n        return self.d.get(k)\n\n    def put(self, k, v):\n        self.d[k] = v\n        if len(self.d) > self.cap:\n            self.d.popitem(last=False)\n",
            "test_cache.py": "from cache import LRU\n\n\ndef test_put_get():\n    c = LRU(2)\n    c.put('a', 1)\n    assert c.get('a') == 1\n",
        },
        "Reading a key does not protect it from eviction. With capacity 2: put a, put b, get a, put c -> 'a' is evicted but I just used it, 'b' should go.",
        "from cache import LRU\n\n\ndef test_o():\n    c = LRU(2)\n    c.put('a', 1)\n    c.put('b', 2)\n    c.get('a')\n    c.put('c', 3)\n    assert c.get('a') == 1 and c.get('b') is None and c.get('c') == 3\n    c.put('c', 4)\n    c.put('d', 5)\n    assert c.get('c') == 4\n",
        {
            "cache.py": (
                "        return self.d.get(k)\n",
                "        if k in self.d:\n            self.d.move_to_end(k)\n        return self.d.get(k)\n",
            ),
        },
    ),
    BenchCase(
        "env-bool-parsing",
        "medium",
        "python",
        {
            "config.py": "import os\n\n\ndef flag(name, default=False):\n    raw = os.environ.get(name)\n    if raw is None:\n        return default\n    return bool(raw)\n",
            "test_config.py": 'import os\n\nfrom config import flag\n\n\ndef test_default():\n    os.environ.pop("X_FLAG", None)\n    assert flag("X_FLAG") is False\n\n\ndef test_true():\n    os.environ["X_FLAG"] = "true"\n    assert flag("X_FLAG") is True\n',
        },
        'Setting DEBUG=false in the environment still turns debug mode on (flag("DEBUG") is True). "0", "no" and "off" have the same problem.',
        'import os\n\nfrom config import flag\n\n\ndef test_o(monkeypatch):\n    for v in ("false", "0", "no", "off", "False", ""):\n        monkeypatch.setenv("F", v)\n        assert flag("F") is False, v\n    for v in ("true", "1", "yes", "ON"):\n        monkeypatch.setenv("F", v)\n        assert flag("F") is True, v\n',
        {
            "config.py": (
                "return bool(raw)",
                'return raw.strip().lower() in {"1", "true", "yes", "on"}',
            )
        },
    ),
    BenchCase(
        "days-between",
        "medium",
        "python",
        {
            "dates.py": "from datetime import datetime\n\n\ndef days_between(a, b):\n    return (b - a).seconds // 86400\n",
            "test_dates.py": "from datetime import datetime\n\nfrom dates import days_between\n\n\ndef test_zero():\n    d = datetime(2024, 1, 1)\n    assert days_between(d, d) == 0\n",
        },
        "days_between(datetime(2024,1,1), datetime(2024,1,11)) returns 0, expected 10. Always 0 for anything over a day.",
        "from datetime import datetime\n\nfrom dates import days_between\n\n\ndef test_o():\n    assert days_between(datetime(2024, 1, 1), datetime(2024, 1, 11)) == 10\n    assert days_between(datetime(2024, 1, 1, 12), datetime(2024, 1, 2, 11)) == 0\n",
        {"dates.py": ("(b - a).seconds // 86400", "(b - a).days")},
    ),
    # ---------------------------------------------------------------- FastAPI, medium
    BenchCase(
        "api-pagination-pages",
        "medium",
        "fastapi",
        {
            "requirements.txt": FASTAPI_REQ,
            "app.py": _api(
                'ITEMS = list(range(1, 26))\napp = FastAPI()\n\n\n@app.get("/items")\ndef items(page: int = 1, size: int = 10):\n    start = (page - 1) * size\n    return {"items": ITEMS[start:start + size], "pages": len(ITEMS) // size}\n'
            ),
            "test_app.py": 'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_first():\n    assert c.get("/items?page=1&size=10").json()["items"][0] == 1\n',
        },
        "GET /items?size=10 reports pages=2 for 25 items, but there are 3 pages (the last page has 5 items).",
        'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_o():\n    assert c.get("/items?size=10").json()["pages"] == 3\n    assert c.get("/items?size=5").json()["pages"] == 5\n    assert c.get("/items?size=25").json()["pages"] == 1\n',
        {"app.py": ("len(ITEMS) // size", "-(-len(ITEMS) // size)")},
    ),
    BenchCase(
        "api-missing-returns-200",
        "medium",
        "fastapi",
        {
            "requirements.txt": FASTAPI_REQ,
            "app.py": _api(
                'USERS = {1: "ann", 2: "bob"}\napp = FastAPI()\n\n\n@app.get("/users/{uid}")\ndef get_user(uid: int):\n    return {"id": uid, "name": USERS.get(uid)}\n'
            ),
            "test_app.py": 'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_found():\n    assert c.get("/users/1").json() == {"id": 1, "name": "ann"}\n',
        },
        'GET /users/99 returns 200 {"id":99,"name":null}. It should be a 404.',
        'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_o():\n    assert c.get("/users/99").status_code == 404\n    assert c.get("/users/2").json()["name"] == "bob"\n',
        {
            "app.py": (
                '    return {"id": uid, "name": USERS.get(uid)}',
                '    if uid not in USERS:\n        raise HTTPException(404, "not found")\n    return {"id": uid, "name": USERS[uid]}',
            )
        },
    ),
    BenchCase(
        "api-create-status-duplicate",
        "medium",
        "fastapi",
        {
            "requirements.txt": FASTAPI_REQ,
            "app.py": _api(
                'class Tag(BaseModel):\n    name: str\n\n\nTAGS = []\napp = FastAPI()\n\n\n@app.post("/tags")\ndef create(tag: Tag):\n    TAGS.append(tag.name)\n    return {"name": tag.name}\n\n\n@app.get("/tags")\ndef tags():\n    return TAGS\n'
            ),
            "test_app.py": 'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_list_is_list():\n    assert isinstance(c.get("/tags").json(), list)\n',
        },
        "POST /tags answers 200 when it creates a tag (should be 201), and creating the same tag twice silently stores a duplicate (should be 409 Conflict).",
        'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_o():\n    assert c.post("/tags", json={"name": "x"}).status_code == 201\n    assert c.post("/tags", json={"name": "x"}).status_code == 409\n    assert c.get("/tags").json().count("x") == 1\n',
        {
            "app.py": (
                '@app.post("/tags")\ndef create(tag: Tag):\n    TAGS.append',
                '@app.post("/tags", status_code=201)\ndef create(tag: Tag):\n    if tag.name in TAGS:\n        raise HTTPException(409, "exists")\n    TAGS.append',
            )
        },
    ),
    BenchCase(
        "api-filter-inclusive",
        "medium",
        "fastapi",
        {
            "requirements.txt": FASTAPI_REQ,
            "app.py": _api(
                'PRODUCTS = [{"n": "a", "price": 5}, {"n": "b", "price": 10}, {"n": "c", "price": 20}]\napp = FastAPI()\n\n\n@app.get("/products")\ndef products(min_price: float = 0, max_price: float = 1e9):\n    return [p for p in PRODUCTS if p["price"] > min_price and p["price"] < max_price]\n'
            ),
            "test_app.py": 'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_all():\n    assert len(c.get("/products").json()) == 3\n',
        },
        "GET /products?min_price=10&max_price=20 returns an empty list. Both bounds should be inclusive, I expect products b and c.",
        'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_o():\n    got = [p["n"] for p in c.get("/products?min_price=10&max_price=20").json()]\n    assert got == ["b", "c"]\n',
        {
            "app.py": (
                'p["price"] > min_price and p["price"] < max_price',
                'min_price <= p["price"] <= max_price',
            )
        },
    ),
    BenchCase(
        "api-delete-noop",
        "medium",
        "fastapi",
        {
            "requirements.txt": FASTAPI_REQ,
            "app.py": _api(
                'NOTES = [{"id": 1}, {"id": 2}, {"id": 3}]\napp = FastAPI()\n\n\n@app.delete("/notes/{nid}", status_code=204)\ndef delete(nid: int):\n    notes = [n for n in NOTES if n["id"] != nid]\n    return None\n\n\n@app.get("/notes")\ndef notes():\n    return NOTES\n'
            ),
            "test_app.py": 'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_list():\n    assert len(c.get("/notes").json()) >= 0\n',
        },
        "DELETE /notes/2 says 204 but the note is still there on the next GET /notes.",
        'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_o():\n    assert c.delete("/notes/2").status_code == 204\n    assert [n["id"] for n in c.get("/notes").json()] == [1, 3]\n',
        {
            "app.py": (
                '    notes = [n for n in NOTES if n["id"] != nid]\n',
                '    NOTES[:] = [n for n in NOTES if n["id"] != nid]\n',
            )
        },
    ),
    BenchCase(
        "api-negative-quantity",
        "medium",
        "fastapi",
        {
            "requirements.txt": FASTAPI_REQ,
            "app.py": _api(
                'class Line(BaseModel):\n    sku: str\n    qty: int\n\n\napp = FastAPI()\n\n\n@app.post("/cart")\ndef add(line: Line):\n    return {"sku": line.sku, "qty": line.qty}\n'
            ),
            "test_app.py": 'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_ok():\n    assert c.post("/cart", json={"sku": "a", "qty": 2}).status_code == 200\n',
        },
        "POST /cart accepts qty=-5 and qty=0. Quantity must be a positive integer and the API should answer 422.",
        'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_o():\n    assert c.post("/cart", json={"sku": "a", "qty": -5}).status_code == 422\n    assert c.post("/cart", json={"sku": "a", "qty": 0}).status_code == 422\n    assert c.post("/cart", json={"sku": "a", "qty": 1}).status_code == 200\n',
        {
            "app.py": ("    qty: int\n", "    qty: int = Field(gt=0)\n"),
        },
    ),
    BenchCase(
        "api-token-prefix-auth",
        "medium",
        "fastapi",
        {
            "requirements.txt": FASTAPI_REQ,
            "app.py": _api(
                'TOKEN = "s3cret-token"\napp = FastAPI()\n\n\n@app.get("/admin")\ndef admin(x_token: str = Header("")):\n    if x_token not in TOKEN:\n        raise HTTPException(401, "bad token")\n    return {"ok": True}\n'
            ),
            "test_app.py": 'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_good():\n    assert c.get("/admin", headers={"x-token": "s3cret-token"}).status_code == 200\n\n\ndef test_bad():\n    assert c.get("/admin", headers={"x-token": "nope"}).status_code == 401\n',
        },
        "Security: GET /admin with the header X-Token: s3cret (just part of the token) or even X-Token: t is accepted, and an empty token too. Only the exact token may pass.",
        'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_o():\n    for t in ("s3cret", "t", "", "s3cret-token-x"):\n        assert c.get("/admin", headers={"x-token": t}).status_code == 401, t\n    assert c.get("/admin", headers={"x-token": "s3cret-token"}).status_code == 200\n    assert c.get("/admin").status_code == 401\n',
        {"app.py": ("if x_token not in TOKEN:", "if x_token != TOKEN:")},
    ),
    BenchCase(
        "api-sort-descending",
        "medium",
        "fastapi",
        {
            "requirements.txt": FASTAPI_REQ,
            "app.py": _api(
                'BOOKS = [{"t": "a", "p": 3}, {"t": "b", "p": 1}, {"t": "c", "p": 2}]\napp = FastAPI()\n\n\n@app.get("/books")\ndef books(sort: str = ""):\n    field = sort.lstrip("-")\n    if field:\n        return sorted(BOOKS, key=lambda b: b[field])\n    return BOOKS\n'
            ),
            "test_app.py": 'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_asc():\n    assert [b["p"] for b in c.get("/books?sort=p").json()] == [1, 2, 3]\n',
        },
        "GET /books?sort=-p is documented as descending by price but returns ascending order, same as sort=p.",
        'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_o():\n    assert [b["p"] for b in c.get("/books?sort=-p").json()] == [3, 2, 1]\n    assert [b["p"] for b in c.get("/books?sort=p").json()] == [1, 2, 3]\n',
        {
            "app.py": (
                "        return sorted(BOOKS, key=lambda b: b[field])",
                '        return sorted(BOOKS, key=lambda b: b[field], reverse=sort.startswith("-"))',
            )
        },
    ),
    BenchCase(
        "api-mutable-default",
        "hard",
        "fastapi",
        {
            "requirements.txt": FASTAPI_REQ,
            "app.py": _api(
                'app = FastAPI()\n\n\ndef collect(item, bucket=[]):\n    bucket.append(item)\n    return bucket\n\n\n@app.post("/events/{name}")\ndef add(name: str):\n    return {"events": collect(name)}\n'
            ),
            "test_app.py": 'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_ok():\n    assert c.post("/events/x").status_code == 200\n',
        },
        'Each POST /events/{name} should return only that one event, but responses keep growing: the second call returns ["a", "b"], the third has three entries and so on. Looks like state leaking between requests.',
        'from fastapi.testclient import TestClient\n\nfrom app import app\n\nc = TestClient(app)\n\n\ndef test_o():\n    assert c.post("/events/a").json() == {"events": ["a"]}\n    assert c.post("/events/b").json() == {"events": ["b"]}\n',
        {
            "app.py": (
                "def collect(item, bucket=[]):\n    bucket.append(item)",
                "def collect(item, bucket=None):\n    bucket = [] if bucket is None else bucket\n    bucket.append(item)",
            )
        },
    ),
    # ---------------------------------------------------------------- multi-file / misleading, hard
    BenchCase(
        "inventory-not-persisted",
        "hard",
        "python",
        {
            "repo.py": "class Repo:\n    def __init__(self):\n        self.stock = {'apple': 5}\n\n    def get(self, sku):\n        return dict(sku=sku, qty=self.stock[sku])\n\n    def save(self, item):\n        self.stock[item['sku']] = item['qty']\n",
            "service.py": "from repo import Repo\n\nrepo = Repo()\n\n\ndef reserve(sku, n):\n    item = repo.get(sku)\n    if item['qty'] < n:\n        raise ValueError('insufficient stock')\n    item['qty'] -= n\n    return item['qty']\n",
            "test_service.py": "import pytest\n\nfrom service import reserve\n\n\ndef test_insufficient():\n    with pytest.raises(ValueError):\n        reserve('apple', 99)\n",
        },
        "reserve('apple', 3) returns 2, which is right, but calling it again also returns 2 and I can reserve the same apples forever. Stock never goes down.",
        "import pytest\n\nimport service\n\n\ndef test_o():\n    assert service.reserve('apple', 3) == 2\n    assert service.reserve('apple', 2) == 0\n    with pytest.raises(ValueError):\n        service.reserve('apple', 1)\n",
        {"service.py": ("    item['qty'] -= n\n", "    item['qty'] -= n\n    repo.save(item)\n")},
    ),
    BenchCase(
        "cart-total-rounding-misleading",
        "hard",
        "python",
        {
            "money.py": "def to_cents(amount):\n    return int(amount * 100)\n\n\ndef from_cents(cents):\n    return cents / 100\n",
            "cart.py": "from money import from_cents, to_cents\n\n\ndef total(prices):\n    return from_cents(sum(to_cents(p) for p in prices))\n",
            "test_cart.py": "from cart import total\n\n\ndef test_simple():\n    assert total([1.0, 2.0]) == 3.0\n",
        },
        "Our checkout total is wrong: total([0.29, 0.57, 19.99]) gives 20.84 instead of 20.85. Customers are being undercharged by a cent here and there. I suspect cart.total.",
        "from cart import total\n\n\ndef test_o():\n    assert total([0.29, 0.57, 19.99]) == 20.85\n    assert total([0.1, 0.2]) == 0.3\n    assert total([1.0, 2.0]) == 3.0\n",
        {"money.py": ("return int(amount * 100)", "return round(amount * 100)")},
    ),
    BenchCase(
        "csv-quoted-comma",
        "hard",
        "python",
        {
            "csvparse.py": "def parse_line(line):\n    return [f.strip() for f in line.split(',')]\n",
            "test_csvparse.py": "from csvparse import parse_line\n\n\ndef test_plain():\n    assert parse_line('a, b,c') == ['a', 'b', 'c']\n",
        },
        "parse_line('1,\"Smith, John\",42') returns 4 fields; the quoted name should stay one field: ['1', 'Smith, John', '42'].",
        "from csvparse import parse_line\n\n\ndef test_o():\n    assert parse_line('1,\"Smith, John\",42') == ['1', 'Smith, John', '42']\n    assert parse_line('a, b,c') == ['a', 'b', 'c']\n    assert parse_line('x,\"say \"\"hi\"\"\",y') == ['x', 'say \"hi\"', 'y']\n",
        {
            "csvparse.py": (
                "    return [f.strip() for f in line.split(',')]\n",
                "    import csv\n\n    return [f.strip() for f in next(csv.reader([line], skipinitialspace=True))]\n",
            )
        },
    ),
    BenchCase(
        "dedupe-keeps-order",
        "easy",
        "python",
        {
            "uniq.py": "def dedupe(items):\n    return list(set(items))\n",
            "test_uniq.py": "from uniq import dedupe\n\n\ndef test_len():\n    assert len(dedupe([1, 1, 2])) == 2\n",
        },
        "dedupe([3, 1, 3, 2, 1]) returns the values in random order. It should keep first-seen order: [3, 1, 2].",
        "from uniq import dedupe\n\n\ndef test_o():\n    assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]\n    assert dedupe(['b', 'a', 'b']) == ['b', 'a']\n",
        {"uniq.py": ("return list(set(items))", "return list(dict.fromkeys(items))")},
    ),
]
