#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refcheck.py — 嵌套文档引用校验工具（纯 Python 标准库，单文件）

引用语法（自定）
----------------
在文档内容中：
  @名称          引用本文档或全局可见的声明 "名称"
  @文档:名称     引用指定文档中的声明（跨文档引用）
  @@             字面量 "@"
名称规则：Unicode 字母/下划线开头，可跟字母、数字、下划线（支持中文名）。

理由："@" 在普通文本中稀少，不易误触发；"文档:名称" 借用常见的
"命名空间:符号" 记法（如 pkg:symbol），读写直观；"@@" 转义与 printf
的 "%%"、模板引擎的 "{{" 一致，零学习成本。

遮蔽规则（自定）
----------------
1. 同一文档内同名声明：后声明遮蔽先声明（流式/追加语义，类似 shell
   变量与多数配置语言：后者覆盖前者，可在不修改历史的情况下覆盖）。
   被遮蔽的声明会在报告中列出，但不视为错误。
2. 本文档的声明遮蔽其他文档的同名全局声明（词法就近原则，如同局部
   变量遮蔽全局变量）。
3. 未限定名 @名称 在本文档无声明、且在多个文档中均可见时，报告
   "引用歧义"。

输入
----
文档流:  [{"name": "甲", "content": "..."}, ...]
声明流:  [{"doc": "甲", "name": "x", "definition": "..."}, ...]
合并流:  {"documents": [...], "declarations": [...]}
声明的 definition 为不透明文本（不再递归解析其中引用）。

用法
----
  python3 refcheck.py 文档.json 声明.json          # 人类可读报告
  python3 refcheck.py 合并.json                    # 合并输入
  python3 refcheck.py 合并.json --json             # JSON 报告
  cat 合并.json | python3 refcheck.py -            # 从 stdin 读

退出码：0 无错误；1 有校验错误；2 输入/用法错误。
"""

import argparse
import bisect
import json
import re
import sys

NAME_RE = re.compile(r"(?!\d)\w+")  # 非数字开头的 Unicode 词字符序列


# ---------------------------------------------------------------- 内容解析

def find_references(content):
    """扫描内容，返回 (refs, invalids)。

    refs:     [(offset, end, qualifier|None, name), ...]
    invalids: [(offset, reason), ...]
    """
    refs, invalids = [], []
    pos = 0
    while True:
        at = content.find("@", pos)
        if at < 0:
            return refs, invalids
        if content.startswith("@@", at):
            pos = at + 2
            continue
        m = NAME_RE.match(content, at + 1)
        if not m:
            bad = content[at + 1:at + 2] or "（内容结束）"
            invalids.append((at, '"@" 后应跟名称或另一个 "@"，却遇到 {!r}'.format(bad)))
            pos = at + 1
            continue
        first = m.group(0)
        end = m.end()
        if content.startswith(":", end):
            m2 = NAME_RE.match(content, end + 1)
            if not m2:
                invalids.append((at, '限定引用 "@{}:" 缺少名称部分'.format(first)))
                pos = end + 1
                continue
            refs.append((at, m2.end(), first, m2.group(0)))
            pos = m2.end()
        else:
            refs.append((at, end, None, first))
            pos = end


def line_starts(text):
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def locate(starts, offset):
    line = bisect.bisect_right(starts, offset)
    col = offset - starts[line - 1] + 1
    return line, col


# ---------------------------------------------------------------- 环检测

def tarjan_scc(nodes, edges):
    """迭代版 Tarjan，返回强连通分量列表。"""
    index, low, stack, on_stack, sccs = {}, {}, [], set(), []
    counter = 0
    for start in nodes:
        if start in index:
            continue
        todo = [(start, iter(edges.get(start, ())))]
        while todo:
            node, it = todo[-1]
            if node not in index:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            descended = False
            for nxt in it:
                if nxt not in index:
                    todo.append((nxt, iter(edges.get(nxt, ()))))
                    descended = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if descended:
                continue
            todo.pop()
            if todo:
                parent = todo[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                sccs.append(comp)
    return sccs


# ---------------------------------------------------------------- 核心校验

def check(documents, declarations):
    """执行全部校验，返回报告 dict（可直接 JSON 序列化）。"""
    errors = []

    # 1) 文档流：重复定义报告，保留首次定义
    doc_order, doc_content = [], {}
    for i, entry in enumerate(documents, 1):
        name = entry["name"]
        if name in doc_content:
            errors.append({
                "type": "duplicate_document",
                "document": name,
                "stream_index": i,
                "message": '文档 "{}" 重复定义（流中第 {} 个，保留首次定义）'.format(name, i),
            })
            continue
        doc_content[name] = entry["content"]
        doc_order.append(name)

    # 2) 声明流：同文档内后者遮蔽前者；指向不存在文档的声明报错
    effective = {d: {} for d in doc_order}      # doc -> {name: (definition, 声明序号)}
    shadowed = {d: [] for d in doc_order}
    for i, decl in enumerate(declarations, 1):
        d, n, defn = decl["doc"], decl["name"], decl["definition"]
        if d not in doc_content:
            errors.append({
                "type": "unknown_document",
                "document": d,
                "stream_index": i,
                "message": '声明 #{} 指向不存在的文档 "{}"'.format(i, d),
            })
            continue
        prev = effective[d].get(n)
        if prev is not None:
            shadowed[d].append({"name": n, "shadowed_declaration": prev[1],
                                "by_declaration": i})
        effective[d][n] = (defn, i)

    # 全局可见性：名称 -> 提供它的文档集合（基于遮蔽后的有效声明）
    global_providers = {}
    for d in doc_order:
        for n in effective[d]:
            global_providers.setdefault(n, set()).add(d)

    # 3) 解析各文档内容中的引用
    doc_refs = {}
    graph = {d: set() for d in doc_order}       # 文档级引用图（仅跨文档边）
    for d in doc_order:
        content = doc_content[d]
        starts = line_starts(content)
        refs, invalids = find_references(content)

        for off, reason in invalids:
            line, col = locate(starts, off)
            errors.append({
                "type": "invalid_content",
                "document": d,
                "position": {"line": line, "column": col, "offset": off},
                "message": '文档 "{}" 第 {} 行第 {} 列：{}'.format(d, line, col, reason),
            })

        records = []
        for off, end, qualifier, name in refs:
            line, col = locate(starts, off)
            pos = {"line": line, "column": col, "offset": off}
            rec = {"position": pos, "text": content[off:end], "status": None}

            if qualifier is not None:  # @文档:名称 —— 跨文档/限定引用
                if qualifier not in doc_content:
                    rec["status"] = "dangling"
                    errors.append({
                        "type": "dangling_reference", "document": d, "position": pos,
                        "name": name,
                        "message": '文档 "{}" 第 {} 行第 {} 列：引用了不存在的文档 '
                                   '"{}" 的 "{}"'.format(d, line, col, qualifier, name),
                    })
                elif name not in effective[qualifier]:
                    rec["status"] = "dangling"
                    errors.append({
                        "type": "dangling_reference", "document": d, "position": pos,
                        "name": name,
                        "message": '文档 "{}" 第 {} 行第 {} 列：文档 "{}" 中未声明 '
                                   '"{}"'.format(d, line, col, qualifier, name),
                    })
                else:
                    defn, idx = effective[qualifier][name]
                    rec["status"] = "resolved"
                    rec["target"] = {"doc": qualifier, "name": name,
                                     "definition": defn, "declaration": idx}
                    if qualifier != d:
                        graph[d].add(qualifier)
            else:  # @名称 —— 先查本文档（局部遮蔽全局），再查全局
                if name in effective[d]:
                    defn, idx = effective[d][name]
                    rec["status"] = "resolved"
                    rec["target"] = {"doc": d, "name": name,
                                     "definition": defn, "declaration": idx}
                else:
                    providers = sorted(global_providers.get(name, ()))
                    if not providers:
                        rec["status"] = "dangling"
                        errors.append({
                            "type": "dangling_reference", "document": d,
                            "position": pos, "name": name,
                            "message": '文档 "{}" 第 {} 行第 {} 列："{}" 未在任何 '
                                       '文档中声明（悬空引用）'.format(d, line, col, name),
                        })
                    elif len(providers) == 1:
                        target_doc = providers[0]
                        defn, idx = effective[target_doc][name]
                        rec["status"] = "resolved"
                        rec["target"] = {"doc": target_doc, "name": name,
                                         "definition": defn, "declaration": idx}
                        graph[d].add(target_doc)
                    else:
                        rec["status"] = "ambiguous"
                        rec["candidates"] = providers
                        graph[d].update(providers)
                        errors.append({
                            "type": "ambiguous_reference", "document": d,
                            "position": pos, "name": name, "candidates": providers,
                            "message": '文档 "{}" 第 {} 行第 {} 列："{}" 在多个文档中'
                                       '可见（{}），引用歧义'.format(
                                           d, line, col, name, "、".join(providers)),
                        })
            records.append(rec)
        doc_refs[d] = records

    # 4) 环检测：文档级引用图上的强连通分量（大小 > 1 即环）
    for comp in tarjan_scc(doc_order, graph):
        if len(comp) > 1:
            cyc = sorted(comp)
            errors.append({
                "type": "reference_cycle",
                "documents": cyc,
                "message": "引用形成环：" + " -> ".join(cyc + [cyc[0]]),
            })

    report_docs = {}
    for d in doc_order:
        report_docs[d] = {
            "visible_definitions": {
                n: {"definition": defn, "declaration": idx}
                for n, (defn, idx) in sorted(effective[d].items())
            },
            "shadowed": shadowed[d],
            "references": doc_refs[d],
        }
    return {"documents": report_docs, "errors": errors}


# ---------------------------------------------------------------- 输出

def short(text, limit=50):
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "…"


def print_human(report):
    for d, info in report["documents"].items():
        print("文档 {}".format(d))
        vis = info["visible_definitions"]
        print("  可见定义 ({}):".format(len(vis)))
        for n, v in vis.items():
            print('    {} = "{}"   [声明 #{}]'.format(n, short(v["definition"]),
                                                       v["declaration"]))
        for s in info["shadowed"]:
            print('    （"{}" 的声明 #{} 已被 #{} 遮蔽）'.format(
                s["name"], s["shadowed_declaration"], s["by_declaration"]))
        refs = info["references"]
        print("  引用解析 ({}):".format(len(refs)))
        for r in refs:
            p = r["position"]
            loc = "L{}:C{}".format(p["line"], p["column"])
            if r["status"] == "resolved":
                t = r["target"]
                print('    {:<9} {:<14} -> {}/{} = "{}"'.format(
                    loc, r["text"], t["doc"], t["name"], short(t["definition"])))
            elif r["status"] == "ambiguous":
                print('    {:<9} {:<14} !! 歧义，候选：{}'.format(
                    loc, r["text"], "、".join(r["candidates"])))
            else:
                print('    {:<9} {:<14} !! 悬空引用'.format(loc, r["text"]))
        print()
    errs = report["errors"]
    print("错误汇总 ({}):".format(len(errs)))
    for e in errs:
        print("  [{}] {}".format(e["type"], e["message"]))


# ---------------------------------------------------------------- 输入

def load_json(path):
    text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    return json.loads(text)


def validate(documents, declarations):
    if not isinstance(documents, list):
        return "文档流必须是数组"
    for i, e in enumerate(documents, 1):
        if not (isinstance(e, dict) and isinstance(e.get("name"), str)
                and isinstance(e.get("content"), str)):
            return '文档流第 {} 项须为 {{"name": str, "content": str}}'.format(i)
    if not isinstance(declarations, list):
        return "声明流必须是数组"
    for i, e in enumerate(declarations, 1):
        if not (isinstance(e, dict) and isinstance(e.get("doc"), str)
                and isinstance(e.get("name"), str)
                and isinstance(e.get("definition"), str)):
            return ('声明流第 {} 项须为 {{"doc": str, "name": str, '
                    '"definition": str}}'.format(i))
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="嵌套文档引用校验工具（详见文件头文档字符串）")
    ap.add_argument("inputs", nargs="+",
                    help="1 个合并 JSON 文件，或 2 个文件（文档流 声明流）；'-' 表示 stdin")
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = ap.parse_args(argv)

    try:
        if len(args.inputs) == 1:
            data = load_json(args.inputs[0])
            documents = data.get("documents") if isinstance(data, dict) else None
            declarations = data.get("declarations") if isinstance(data, dict) else None
        elif len(args.inputs) == 2:
            documents = load_json(args.inputs[0])
            declarations = load_json(args.inputs[1])
            if isinstance(documents, dict):
                documents = documents.get("documents")
            if isinstance(declarations, dict):
                declarations = declarations.get("declarations")
        else:
            ap.error("需要 1 个（合并）或 2 个（文档流 + 声明流）输入文件")
    except (OSError, json.JSONDecodeError) as exc:
        print("输入读取/解析失败：{}".format(exc), file=sys.stderr)
        return 2

    problem = validate(documents, declarations)
    if problem:
        print("输入格式错误：{}".format(problem), file=sys.stderr)
        return 2

    report = check(documents, declarations)
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print_human(report)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
