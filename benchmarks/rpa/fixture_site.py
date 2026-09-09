"""A local shop for repeatable native browser tests; no data API or real accounts."""

import argparse
import html
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from benchmarks.rpa.data import products


def render(path, query):
    seed = int(query.get("seed", "27"))
    if path == "/gate":
        return '<h1>测评登录页</h1><p>请登录后查看商品，当前没有可采集的销量数据。</p><button disabled>等待人工登录</button>'
    if path == "/frame":
        return '<h1>外层展示区（不是商品结果）</h1><p>示例销量 999，禁止当作商品数据。</p><iframe title="商品查询" width="100%" height="900" src="/shop?' + html.escape(urlencode({"seed": seed})) + '"></iframe>'
    if path.startswith("/item/"):
        row = next((r for r in products(seed) if r["id"] == path.rsplit("/", 1)[-1]), None)
        if not row:
            raise ValueError("unknown product")
        return f'<h1>{row["title"]}</h1><p>商品编号 {row["id"]}</p><p>价格 {row["price"]} 元</p>'
    if path != "/shop":
        raise ValueError("unknown page")
    keyword = query.get("q", "")
    selected = [r for r in products(seed) if not keyword or keyword in r["title"]]
    page = max(1, int(query.get("page", "1")))
    overlap, more = query.get("overlap") == "1", query.get("more") == "1"
    stride = 3 if overlap else 4
    pages = max(1, math.ceil(max(0, len(selected) - 4) / stride) + 1)
    page = min(page, pages)
    visible = selected[0 if more else (page - 1) * stride:(page - 1) * stride + 4]
    hidden = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
                     for k, v in query.items() if k in {"seed", "layout", "metric", "overlap", "more"})
    form = f'<form action="/shop"><label>商品关键词 <input name="q" value="{html.escape(keyword)}"></label>{hidden}<button>查询</button></form>'
    metric = query.get("metric", "sales")
    header = "评价数" if metric == "reviews" else "销量原文"
    records = []
    for row in visible:
        sales = (f'{row["reviews"]} 条评价' if metric == "reviews" else
                 "已售 1万+ 件" if metric == "bound" else f'已售 {row["sales"]} 件')
        scope = "评价数" if metric == "reviews" else "累计销量下界" if metric == "bound" else "累计销量"
        url = "/item/" + row["id"] + "?" + urlencode({"seed": seed})
        records.append([row["id"], row["title"], str(row["price"]), sales, scope,
                        f'<a href="{html.escape(url)}">查看商品</a>'])
    labels = ["商品编号", "商品名称", "价格", header, "指标口径", "商品链接"]
    if query.get("layout") == "cards":
        body = "".join('<article>' + "".join(f'<p><b>{label}</b> {value}</p>' for label, value in zip(labels, row)) + '</article>' for row in records)
    else:
        body = '<table><thead><tr>' + ''.join(f'<th>{v}</th>' for v in labels) + '</tr></thead><tbody>'
        body += ''.join('<tr>' + ''.join(f'<td>{v}</td>' for v in row) + '</tr>' for row in records) + '</tbody></table>'
    nav = '<p>没有更多结果</p>'
    if page < pages:
        link = "/shop?" + urlencode({**query, "q": keyword, "page": page + 1})
        nav = f'<a href="{html.escape(link)}">{"加载更多" if more else "下一页"}</a>'
    return f'<h1>组件测评商城（合成数据）</h1>{form}<p>共 {len(selected)} 个商品，第 {page}/{pages} 页</p>{body}<nav>{nav}</nav>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--audit", type=Path, default=Path("_tmp/rpa-benchmark/http-audit.jsonl"))
    args = parser.parse_args()
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            try:
                content = render(parsed.path, query)
            except (ValueError, OverflowError):
                self.send_error(404)
                return
            with lock, args.audit.open("a", encoding="utf8") as f:
                f.write(json.dumps({"time": time.time(), "path": parsed.path, "query": query,
                                    "user_agent": self.headers.get("User-Agent", "")}, ensure_ascii=False) + "\n")
            content = ('<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><title>组件测评商城</title>'
                       '<style>body{font:18px sans-serif;margin:32px}td,th{padding:14px;border:1px solid #aaa}'
                       'table{border-collapse:collapse}article{border:1px solid #aaa;padding:16px;margin:12px}'
                       'input,button{font:inherit;padding:8px}nav{margin:24px}</style></head><body>' + content + '</body></html>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Fixture shop: http://127.0.0.1:{args.port}/shop", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
