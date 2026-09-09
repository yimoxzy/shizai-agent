"""Loopback-only browser fixture for independent native RPA evaluations."""

from __future__ import annotations

import argparse
import html
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ROWS = [
    ("HZ-001", "杭州", 12), ("SH-001", "上海", 30), ("BJ-001", "北京", 41),
    ("HZ-002", "杭州", 24), ("HZ-003", "杭州", 36), ("SH-002", "上海", 60),
    ("HZ-004", "杭州", 48), ("BJ-002", "北京", 82), ("HZ-005", "杭州", 60),
    ("SH-003", "上海", 90), ("HZ-006", "杭州", 72), ("HZ-007", "杭州", 84),
]


def render(city: str, page: int) -> str:
    selected = [row for row in ROWS if not city or row[1] == city]
    pages = max(1, (len(selected) + 2) // 3)
    page = max(1, min(page, pages))
    rows = selected[(page - 1) * 3:page * 3]
    cells = "".join("<tr>" + "".join(f'<td data-column="{key}">{html.escape(str(value))}</td>'
                                     for key, value in zip(("order", "city", "amount"), row)) + "</tr>" for row in rows)
    def navigation(label: str, target: int, enabled: bool, node_id: str) -> str:
        url = "/?" + urlencode({"city": city, "page": target})
        return f'<a id="{node_id}" href="{html.escape(url)}">{label}</a>' if enabled else f'<span id="{node_id}" aria-disabled="true">{label}</span>'
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><title>RPA 测评订单查询</title>
<style>body{{font:18px sans-serif;margin:40px;max-width:900px}}input,button{{font:inherit;padding:8px}}
table{{border-collapse:collapse;width:100%;margin:24px 0}}th,td{{border:1px solid #bbb;padding:14px;text-align:left}}
nav{{display:flex;gap:28px}}[aria-disabled=true]{{color:#999}}</style></head><body>
<h1>订单查询</h1><p>输入城市查看订单，每页最多显示 3 条。</p>
<form method="get" action="/"><label for="city">城市</label>
<input id="city" name="city" value="{html.escape(city)}" placeholder="例如：上海" autocomplete="off">
<button id="search" type="submit">查询</button></form>
<p id="result-count">共 {len(selected)} 条订单，第 {page} / {pages} 页</p>
<table id="orders"><thead><tr><th>订单号</th><th>城市</th><th>金额</th></tr></thead><tbody>{cells}</tbody></table>
<nav>{navigation('上一页', page - 1, page > 1, 'previous')}{navigation('下一页', page + 1, page < pages, 'next')}</nav>
</body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--audit", type=Path, default=Path("_tmp/luna-eval/expanded/browser-requests.jsonl"))
    args = parser.parse_args()
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path != "/":
                self.send_error(404)
                return
            query = parse_qs(parsed.query)
            city = query.get("city", [""])[0]
            try:
                page = int(query.get("page", ["1"])[0])
            except ValueError:
                self.send_error(400)
                return
            with lock, args.audit.open("a", encoding="utf8") as audit:
                audit.write(json.dumps({"time": time.time(), "city": city, "page": page,
                                        "user_agent": self.headers.get("User-Agent")}, ensure_ascii=False) + "\n")
            body = render(city, page).encode("utf8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"RPA fixture: http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
