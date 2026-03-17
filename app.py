import base64
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin
from urllib.request import Request, urlopen

MAX_IMAGE_SIZE = 11 * 1024
REQUEST_TIMEOUT = 12
PROCESS_TIMEOUT = 180
HOST = "0.0.0.0"
PORT = 8000
BASE_DIR = Path(__file__).parent

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

RESULT_STORE: Dict[str, Dict] = {}
LOCK = threading.Lock()


@dataclass
class LogoCandidate:
    id: str
    source: str
    image_url: str
    confidence: float
    reason: str
    size_bytes: int
    width: int
    height: int
    data_b64: str


class ImageExtractor(HTMLParser):
    def __init__(self, page_url: str):
        super().__init__()
        self.page_url = page_url
        self.links: List[Tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        if tag == "img":
            src = attr_map.get("src") or attr_map.get("data-src") or attr_map.get("data-lazy-src")
            if src:
                descriptor = " ".join(
                    [attr_map.get("alt", ""), attr_map.get("class", ""), attr_map.get("id", ""), src]
                ).strip().lower()
                self.links.append((urljoin(self.page_url, src), descriptor))
        if tag == "meta":
            prop = (attr_map.get("property") or attr_map.get("name") or "").lower()
            if prop in {"og:image", "twitter:image", "twitter:image:src"}:
                content = attr_map.get("content")
                if content:
                    self.links.append((urljoin(self.page_url, content), f"meta:{prop}"))


def clean_url(url: str) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = f"https://{url}"
    return url


def fetch_url(url: str) -> Tuple[bytes, str]:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def fetch_html(url: str) -> Optional[str]:
    try:
        data, ctype = fetch_url(url)
        if "html" in ctype.lower() or b"<html" in data[:500].lower():
            return data.decode("utf-8", errors="ignore")
    except Exception:
        return None
    return None


def extract_image_links(page_url: str, html: str) -> List[Tuple[str, str]]:
    parser = ImageExtractor(page_url)
    parser.feed(html)
    seen = set()
    out = []
    for link, desc in parser.links:
        if link in seen:
            continue
        seen.add(link)
        out.append((link, desc))
    return out


def parse_png_dimensions(data: bytes) -> Optional[Tuple[int, int]]:
    sig = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(sig) or len(data) < 24:
        return None
    if data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def download_png(url: str) -> Optional[Tuple[bytes, int, int]]:
    try:
        data, _ = fetch_url(url)
    except Exception:
        return None
    if len(data) == 0 or len(data) > MAX_IMAGE_SIZE:
        return None
    dims = parse_png_dimensions(data)
    if not dims:
        return None
    width, height = dims
    if width < 20 or height < 20:
        return None
    return data, width, height


def score_candidate(url: str, descriptor: str, fi_name: str, source_hint: str) -> float:
    score = 0.25
    u = url.lower()
    d = descriptor.lower()
    fi = fi_name.lower()
    if "logo" in u or "logo" in d:
        score += 0.3
    if any(k in u for k in ["header", "brand", "navbar"]):
        score += 0.1
    if fi and fi.replace(" ", "") in re.sub(r"[^a-z0-9]", "", u):
        score += 0.1
    if fi and any(part in d for part in fi.split() if len(part) > 3):
        score += 0.1
    if source_hint == "direct_input_url":
        score += 0.12
    if u.endswith(".png"):
        score += 0.08
    return min(score, 0.98)


def query_duckduckgo(fi_name: str) -> List[str]:
    q = quote_plus(f"{fi_name} official website")
    url = f"https://duckduckgo.com/html/?q={q}"
    html = fetch_html(url)
    if not html:
        return []
    return re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', html)[:4]


def scan_targets(targets: List[Tuple[str, str]], fi_name: str, start_time: float) -> List[LogoCandidate]:
    candidates: List[LogoCandidate] = []
    for page_url, source_hint in targets:
        if time.time() - start_time > PROCESS_TIMEOUT:
            break
        html = fetch_html(page_url)
        if not html:
            continue
        for image_url, descriptor in extract_image_links(page_url, html):
            if time.time() - start_time > PROCESS_TIMEOUT:
                break
            rec = download_png(image_url)
            if not rec:
                continue
            data, w, h = rec
            candidates.append(
                LogoCandidate(
                    id=str(uuid.uuid4()),
                    source=f"{source_hint} ({page_url})",
                    image_url=image_url,
                    confidence=round(score_candidate(image_url, descriptor, fi_name, source_hint), 2),
                    reason=descriptor[:140] or "image reference",
                    size_bytes=len(data),
                    width=w,
                    height=h,
                    data_b64=base64.b64encode(data).decode("utf-8"),
                )
            )
    return candidates


def find_logos(fi_name: str, home_url: Optional[str], login_url: Optional[str]) -> Dict:
    start = time.time()
    direct_targets: List[Tuple[str, str]] = []
    if home_url:
        direct_targets.append((home_url, "direct_input_url"))
    if login_url:
        direct_targets.append((login_url, "direct_input_url"))

    all_candidates = scan_targets(direct_targets, fi_name, start)

    if len(all_candidates) < 1:
        search_targets = [(u, "web_search") for u in query_duckduckgo(fi_name)]
        all_candidates.extend(scan_targets(search_targets, fi_name, start))

    all_candidates.sort(key=lambda x: x.confidence, reverse=True)

    deduped: List[LogoCandidate] = []
    seen = set()
    for c in all_candidates:
        if c.image_url in seen:
            continue
        seen.add(c.image_url)
        deduped.append(c)
        if len(deduped) >= 3:
            break

    if not deduped:
        return {
            "status": "needs_more_details",
            "message": "I could not confirm the exact logo. Please provide more FI details (legal name/state/official page).",
            "results": [],
        }

    with LOCK:
        for c in deduped:
            RESULT_STORE[c.id] = asdict(c)

    message = (
        "Found a high-confidence logo candidate."
        if deduped[0].confidence >= 0.85
        else "I am not 100% sure. Here are up to 3 options with confidence and source."
    )

    return {"status": "ok", "message": message, "results": [asdict(c) for c in deduped]}


class AppHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: Dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str):
        if not path.exists():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            return self._send_file(BASE_DIR / "templates" / "index.html", "text/html; charset=utf-8")
        if self.path == "/static/styles.css":
            return self._send_file(BASE_DIR / "static" / "styles.css", "text/css; charset=utf-8")
        if self.path == "/static/app.js":
            return self._send_file(BASE_DIR / "static" / "app.js", "application/javascript; charset=utf-8")
        if self.path.startswith("/api/download/"):
            item_id = self.path.split("/api/download/")[-1]
            item = RESULT_STORE.get(item_id)
            if not item:
                return self._send_json(404, {"error": "Unknown image id."})
            data = base64.b64decode(item["data_b64"])
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Disposition", f'attachment; filename="{item_id}.png"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path != "/api/find-logo":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._send_json(400, {"error": "Invalid JSON."})

        fi_name = (body.get("fi_name") or "").strip()
        home_url = clean_url(body.get("home_url") or "")
        login_url = clean_url(body.get("login_url") or "")
        if not fi_name:
            return self._send_json(400, {"error": "FI name is required."})

        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(find_logos, fi_name, home_url, login_url)
            try:
                result = task.result(timeout=PROCESS_TIMEOUT)
                return self._send_json(200, result)
            except FuturesTimeout:
                return self._send_json(408, {"status": "timeout", "message": "Search exceeded 3 minutes."})


def run():
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"Running on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    run()
