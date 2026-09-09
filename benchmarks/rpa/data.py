"""Deterministic benchmark inputs/oracles. Never import this from MCP runtime."""

import csv
import io
import random
from urllib.parse import urlencode

PRODUCT_COLUMNS = ["商品编号", "商品名称", "价格", "销量原文", "销量口径", "商品链接"]


def products(seed):
    rng = random.Random(seed)
    rows = []
    for category, count in (("手机", 9), ("笔记本", 6), ("耳机", 3), ("相机", 2)):
        for i in range(1, count + 1):
            rows.append({"id": f"P{len(rows) + 1:03d}", "category": category,
                         "title": f"测评{category}{i}号", "price": rng.randrange(10, 600) * 10,
                         "sales": rng.randrange(10, 900), "reviews": rng.randrange(1000, 9000)})
    return rows


def inputs(seed):
    rng = random.Random(seed)
    orders = [["订单号", "客户", "商品", "数量", "单价", "状态"]]
    for i, (customer, state) in enumerate((("甲公司", "已支付"), ("乙公司", "已支付"),
                                          ("甲公司", "已支付"), ("乙公司", "未支付"),
                                          ("丙公司", "未支付"), ("未建档", "已支付")), 1):
        orders.append([f"O{i:03d}", customer, f"商品{i}", rng.randrange(1, 8), rng.randrange(2, 20), state])
    tables = {"orders.csv": orders,
              "customers.csv": [["客户", "联系人", "城市"], ["甲公司", "张一", "上海"],
                                ["乙公司", "李二", "北京"], ["甲公司", "王三", "杭州"], ["丙公司", "赵四", "南京"]],
              "numbers.csv": [["编号", "数值"], ["N1", 2], ["N2", 100], ["N3", 11], ["N4", -3]],
              "missing.csv": [["编号", "金额"], ["M1", ""], ["M2", "0"], ["M3", "10"], ["M4", ""]]}
    files = {"status.txt": f"批次：{seed}\n订单A：待处理\n订单B：已取消\n订单C：待处理\n".encode(),
             "bom.txt": "你好，自动化 🧪\n第二行\n".encode("utf-8-sig")}
    for name, rows in tables.items():
        stream = io.StringIO(newline="")
        csv.writer(stream, lineterminator="\n").writerows(rows)
        files[name] = stream.getvalue().encode("utf-8-sig")
    return files, tables


def oracle(key, seed, base_url):
    """Return expected data for local fixtures only; live sites have no canned answer."""
    if key == "manual":
        return {"kind": "manual"}
    family, name = key.split(":", 1)
    files, tables = inputs(seed)
    if family.startswith("shop"):
        rows = []
        for row in products(seed):
            if row["category"] != name:
                continue
            raw, metric = f"已售 {row['sales']} 件", "累计销量"
            if family == "shop_missing":
                raw, metric = "", "页面未提供销量"
            elif family == "shop_bound":
                raw, metric = "已售 1万+ 件", "累计销量下界"
            link = base_url.rstrip("/") + "/item/" + row["id"] + "?" + urlencode({"seed": seed})
            rows.append([row["id"], row["title"], str(row["price"]), raw, metric, link])
        if family == "shop_columns":
            return {"kind": "csv", "rows": [["商品链接", "商品名称", "价格"]] + [[r[5], r[1], r[2]] for r in rows],
                    "numeric_columns": [2]}
        return {"kind": "csv", "rows": [PRODUCT_COLUMNS] + rows, "numeric_columns": [2]}
    if family == "text":
        source = files["status.txt"].decode()
        values = {"source": source, "completed": source.replace("待处理", "已完成"),
                  "archived": source.replace("待处理", "已归档"), "first": "第一行\n", "append": "第一行\n第二行\n",
                  "bom": "你好，自动化 🧪\n第二行\n", "bom_edit": "再见，自动化 🧪\n第二行\n",
                  "even": "2\n4\n6\n", "odd": "1\n3\n5\n7\n", "exists": "存在", "absent": "不存在", "structure": "结构测试"}
        return {"kind": "text", "text": values[name], "forbid_bom": name.startswith("bom")}
    if family == "table":
        numeric = []
        if name in {"paid", "unpaid"}:
            totals = {}
            for _, customer, _, qty, price, state in tables["orders.csv"][1:]:
                if state == ("已支付" if name == "paid" else "未支付"):
                    totals[customer] = totals.get(customer, 0) + qty * price
            rows = [["客户", "金额"]] + [[k, v] for k, v in sorted(totals.items())]
            numeric = [1]
        elif name.startswith("dedupe"):
            customers = {}
            for row in tables["customers.csv"][1:]:
                if name == "dedupe_last" or row[0] not in customers:
                    customers[row[0]] = row
            rows = [tables["customers.csv"][0]] + [v for _, v in sorted(customers.items())]
        elif name.endswith("join"):
            cities = {}
            for customer, _, city in tables["customers.csv"][1:]:
                cities.setdefault(customer, city)
            rows = [["订单号", "客户", "城市"]] + [[r[0], r[1], cities.get(r[1], "")]
                for r in tables["orders.csv"][1:] if name == "left_join" or r[1] in cities]
        elif name.startswith("num_"):
            rows = [tables["numbers.csv"][0]] + sorted(tables["numbers.csv"][1:], key=lambda r: r[1], reverse=name == "num_desc")
            numeric = [1]
        else:
            rows = [tables["missing.csv"][0]] + [r for r in tables["missing.csv"][1:] if r[1] == ("" if name == "empty" else "0")]
        return {"kind": "csv", "rows": [[str(v) for v in r] for r in rows], "numeric_columns": numeric}
    if family == "xlsx":
        if name.startswith("formulas"):
            qty = 5 if name.endswith("edit") else 2
            return {"kind": "xlsx", "sheets": ["订单"], "cells": {"A1": "商品", "B1": "数量", "C1": "单价", "D1": "金额",
                "A2": "铅笔", "B2": 3, "C2": 2, "D2": 6, "A3": "本子", "B3": qty, "C3": 8, "D3": qty * 8, "D4": 6 + qty * 8},
                "formulas": {"D2": "B2*C2", "D3": "B3*C3", "D4": "SUM(D2:D3)"}}
        if name.startswith("sheet"):
            return {"kind": "xlsx", "sheets": ["采购清单" if name.endswith("edit") else "清单"],
                    "cells": {"A1": "名称", "B1": "数量", "A2": "手机", "B2": 4 if name.endswith("edit") else 2}}
        return {"kind": "xlsx", "sheets": ["原始数字" if name.endswith("edit") else "数字"],
                "cells": {f"{col}{i}": value for i, row in enumerate(tables["numbers.csv"], 1) for col, value in zip("AB", row)}}
    raise ValueError(f"Unknown oracle: {key}")
