#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""docrefcheck.py — 嵌套文档引用校验工具（纯 Python 标准库，单文件）。

用法:
    python3 docrefcheck.py 文档流.jsonl 声明流.jsonl [--json]
    python3 docrefcheck.py docs.jsonl decls.jsonl          # 文本报告
    python3 docrefcheck.py docs.jsonl decls.jsonl --json   # JSON 报告
    文件参数可用 "-" 表示标准输入（两路输入只能有一路用 "-"）。

输入格式（JSON Lines，每行一个 JSON 对象）:
    文档流: {"name": "文档名", "content": "正文，可含 @引用"}
    声明流: {"doc": "文档名", "name": "名称", "definition": "定义文本"}

引用语法（自定）:
    @名称            引用当前文档可见的声明
    @文档::名称      限定引用指定文档的声明（跨文档引用）
    @@               转义，表示字面量 "@"
    名称 = Unicode 标识符（字母或下划线开头，可含数字；中文名亦可）
    理由: "@" 在普通文本中出现频率低，单字符即可做线性扫描，无需上下文；
          "::" 限定符读写直观、与常见语言惯例一致，无歧义。

遮蔽规则（自定）:
    1. 声明流有序。同一文档内同名声明，后声明遮蔽先声明（最新生效），
       与流式/增量定义语义一致，结果确定。
    2. 非限定引用 @名称 优先解析到本文档（遮蔽后）的声明；本文档没有时
       在其他文档中查找：唯一命中则解析，多个文档同时可见则报“歧义”
       （跨文档没有自然顺序，静默遮蔽会隐藏错误，故报歧义）。
    3. 限定引用 @文档::名称 只查指定文档，同样按规则 1 遮蔽。

输出:
    每个文档的可见定义（遮蔽后）与逐条引用解析结果，以及错误清单。
    错误类型: 悬空引用 / 引用环 / 引用歧义 / 文档重复定义 /
             内容格式非法 / 输入格式非法 / 声明指向未知文档。
    存在任何错误时进程退出码为 1，否则为 0。
"""

import argparse
import json
import re
import sys
from collections import defaultdict

IDENT = r"(?:_|[^\W\d_])\w*"   # Unicode 标识符：字母/下划线开头，可含数字"
TOKEN_RE = re.compile(r"""
      @@                                            # 转义：字面量 @
    | @(?P<qdoc>%s)::(?P<qname>%s)                  # 限定引用 @文档::名称
    | @(?P<name>%s)                                 # 非限定引用 @名称
    | @(?P<bad>)                                    # 非法 @（后跟字符不合法）
""" % (IDENT, IDENT, IDENT), re.VERBOSE)


# ---------------------------------------------------------------- 工具

def line_col(text, offset):
    """把字符偏移换算成 1 起始的 (行, 列)。"""
    line = text.count("\n", 0, offset) + 1
    col = offset - text.rfind("\n", 0, offset)
    return line, col


def make_error(etype, message, doc=None, name=None, pos=None, detail=None):
    err = {"type": etype, "message": message}
    if doc is not None:
        err["doc"] = doc
    if name is not None:
        err["name"] = name
    if pos is not None:
        err["position"] = pos
    if detail is not None:
        err["detail"] = detail
    return err


# ---------------------------------------------------------------- 输入

def load_jsonl(path, required_fields, kind, errors):
    """读取 JSONL 流，逐行校验字段；坏行记入 errors 并跳过。"""
    if path == "-":
        lines = sys.stdin.read().splitlines()
        source = "<stdin>"
    else:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        source = path
    records = []
    for lineno, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(make_error(
                "输入格式非法",
                "%s 第 %d 行不是合法 JSON: %s" % (kind, lineno, exc),
                pos={"file": source, "line": lineno}))
            continue
        missing = [f for f in required_fields if f not in obj]
        if missing:
            errors.append(make_error(
                "输入格式非法",
                "%s 第 %d 行缺少字段: %s" % (kind, lineno, ", ".join(missing)),
                pos={"file": source, "line": lineno}))
            continue
        records.append(obj)
    return records


# ---------------------------------------------------------------- 引用扫描

def scan_references(doc_name, content, errors):
    """线性扫描正文，返回引用列表 [{name, qual, offset, line, column, text}]。

    非法 "@" 记入 errors（内容格式非法）并跳过，不中断扫描。
    """
    refs = []
    for m in TOKEN_RE.finditer(content):
        if m.group(0) == "@@":
            continue
        line, col = line_col(content, m.start())
        pos = {"line": line, "column": col, "offset": m.start()}
        if m.group("bad") is not None:
            nxt = content[m.start() + 1:m.start() + 2]
            errors.append(make_error(
                "内容格式非法",
                "文档 «%s» 行 %d 列 %d: '@' 后不是合法名称"
                "（遇到 %r）；字面量请写 '@@'" % (doc_name, line, col, nxt or "结尾"),
                doc=doc_name, pos=pos))
            continue
        refs.append({
            "name": m.group("qname") or m.group("name"),
            "qual": m.group("qdoc"),          # None 表示非限定引用
            "text": m.group(0),
            **pos,
        })
    return refs


# ---------------------------------------------------------------- 解析

class Resolver:
    def __init__(self, declarations):
        # (doc, name) -> [(order, definition), ...] 按声明流顺序
        self.by_doc_name = defaultdict(list)
        for order, d in enumerate(declarations):
            self.by_doc_name[(d["doc"], d["name"])].append(
                (order, d["definition"]))

    def latest(self, doc, name):
        """取 (doc, name) 遮蔽后的声明，返回 (definition, 被遮蔽数) 或 None。"""
        entries = self.by_doc_name.get((doc, name))
        if not entries:
            return None
        return entries[-1][1], len(entries) - 1

    def docs_declaring(self, name, exclude=None):
        return sorted({doc for (doc, n) in self.by_doc_name
                       if n == name and doc != exclude})

    def resolve(self, from_doc, ref):
        """解析一条引用。

        返回 ("ok", target_doc, definition, shadowed) 或
             ("dangling", None, None, None) 或
             ("ambiguous", candidate_docs, None, None)。
        """
        name, qual = ref["name"], ref["qual"]
        if qual is not None:                      # 限定引用：只查指定文档
            hit = self.latest(qual, name)
            if hit is None:
                return ("dangling", None, None, None)
            return ("ok", qual, hit[0], hit[1])
        hit = self.latest(from_doc, name)         # 非限定：本文档优先
        if hit is not None:
            return ("ok", from_doc, hit[0], hit[1])
        candidates = self.docs_declaring(name, exclude=from_doc)
        if not candidates:
            return ("dangling", None, None, None)
        if len(candidates) > 1:
            return ("ambiguous", candidates, None, None)
        definition, shadowed = self.latest(candidates[0], name)
        return ("ok", candidates[0], definition, shadowed)


# ---------------------------------------------------------------- 环检测

def find_cycles(nodes, edges):
    """Tarjan 强连通分量；返回所有环（大小>1 的分量或自环）。"""
    sys.setrecursionlimit(max(10000, len(nodes) * 4))
    index, low, on_stack, stack, sccs = {}, {}, set(), [], []
    counter = [0]

    def strongconnect(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in edges.get(v, ()):
            if w not in index:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            sccs.append(scc)

    for v in nodes:
        if v not in index:
            strongconnect(v)
    return [sorted(s) for s in sccs
            if len(s) > 1 or (len(s) == 1 and s[0] in edges.get(s[0], ()))]


# ---------------------------------------------------------------- 主流程

def run(doc_records, decl_records):
    errors = []

    # 文档重复定义：保留先出现的，报告后出现的
    documents, seen = {}, set()
    for i, rec in enumerate(doc_records):
        name = rec["name"]
        if name in seen:
            errors.append(make_error(
                "文档重复定义",
                "文档 «%s» 第 %d 次定义被忽略（保留首次定义）" % (name, i + 1),
                doc=name, pos={"entry": i + 1}))
            continue
        seen.add(name)
        documents[name] = rec["content"]

    # 声明指向未知文档
    for d in decl_records:
        if d["doc"] not in documents:
            errors.append(make_error(
                "声明指向未知文档",
                "声明 «%s::%s» 指向未定义的文档 «%s»"
                % (d["doc"], d["name"], d["doc"]),
                doc=d["doc"], name=d["name"]))

    resolver = Resolver(decl_records)

    # 逐文档扫描并解析引用
    results = {}
    edges = defaultdict(set)          # 文档引用图（仅已解析的引用参与）
    for doc_name, content in documents.items():
        refs = scan_references(doc_name, content, errors)
        resolved = []
        for ref in refs:
            status, target, definition, shadowed = resolver.resolve(doc_name, ref)
            entry = {"ref": ref["text"],
                     "position": {"line": ref["line"], "column": ref["column"],
                                  "offset": ref["offset"]}}
            pos_str = "行 %d 列 %d" % (ref["line"], ref["column"])
            if status == "ok":
                entry["target"] = "%s::%s" % (target, ref["name"])
                entry["definition"] = definition
                if shadowed:
                    entry["shadowed"] = shadowed   # 被遮蔽的旧声明条数
                if target != doc_name:      # 环检测只看跨文档依赖
                    edges[doc_name].add(target)
            elif status == "dangling":
                entry["error"] = "悬空引用"
                errors.append(make_error(
                    "悬空引用",
                    "文档 «%s» %s: 引用 %s 未声明"
                    % (doc_name, pos_str, ref["text"]),
                    doc=doc_name, name=ref["name"], pos=entry["position"]))
            else:  # ambiguous
                entry["error"] = "引用歧义"
                entry["candidates"] = ["%s::%s" % (d, ref["name"]) for d in target]
                errors.append(make_error(
                    "引用歧义",
                    "文档 «%s» %s: 引用 %s 在多个文档可见: %s"
                    % (doc_name, pos_str, ref["text"], "、".join(target)),
                    doc=doc_name, name=ref["name"], pos=entry["position"],
                    detail=entry["candidates"]))
            resolved.append(entry)

        visible = {}
        for (d, n) in sorted(resolver.by_doc_name):
            if d == doc_name:
                definition, shadowed = resolver.latest(d, n)
                visible[n] = {"definition": definition, "shadowed": shadowed}
        results[doc_name] = {"visible_definitions": visible,
                             "references": resolved}

    # 引用环（跨文档依赖环；文档内部自引用属正常解析，不参与）
    for cycle in find_cycles(sorted(documents), edges):
        errors.append(make_error(
            "引用环",
            "检测到引用环: %s" % " → ".join(cycle + [cycle[0]]),
            detail=cycle))

    return {"documents": results, "errors": errors}


# ---------------------------------------------------------------- 输出

def print_text(report):
    for doc, res in report["documents"].items():
        print("文档 «%s»" % doc)
        vis = res["visible_definitions"]
        print("  可见定义 (%d):" % len(vis))
        for name, info in vis.items():
            note = "（遮蔽 %d 条旧声明）" % info["shadowed"] if info["shadowed"] else ""
            print("    %s = %s%s" % (name, info["definition"], note))
        refs = res["references"]
        print("  引用解析 (%d):" % len(refs))
        for r in refs:
            pos = "行{line}:列{column}".format(**r["position"])
            if "target" in r:
                note = "（遮蔽 %d 条旧声明）" % r["shadowed"] if r.get("shadowed") else ""
                print("    [%s] %s → %s = %s%s"
                      % (pos, r["ref"], r["target"], r["definition"], note))
            elif r.get("error") == "引用歧义":
                print("    [%s] %s → ✗ 歧义: %s" % (pos, r["ref"], "、".join(r["candidates"])))
            else:
                print("    [%s] %s → ✗ %s" % (pos, r["ref"], r["error"]))
        print()
    errs = report["errors"]
    print("错误清单 (%d):" % len(errs))
    for e in errs:
        loc = ""
        if "position" in e:
            p = e["position"]
            if "line" in p and "column" in p:
                loc = " [行{line}:列{column}]".format(**p)
            elif "entry" in p:
                loc = " [第%d条]" % p["entry"]
            elif "file" in p:
                loc = " [%s:行%d]" % (p["file"], p["line"])
        scope = " 文档=«%s»" % e["doc"] if "doc" in e else ""
        print("  [%s]%s%s %s" % (e["type"], scope, loc, e["message"]))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="嵌套文档引用校验工具（详见文件头文档字符串）")
    ap.add_argument("documents", help="文档定义流（JSONL），'-' 表示标准输入")
    ap.add_argument("declarations", help="引用声明流（JSONL）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = ap.parse_args(argv)

    if args.documents == "-" and args.declarations == "-":
        ap.error("两路输入不能同时使用标准输入")

    errors = []
    try:
        doc_records = load_jsonl(args.documents, ("name", "content"), "文档流", errors)
        decl_records = load_jsonl(args.declarations, ("doc", "name", "definition"),
                                  "声明流", errors)
    except OSError as exc:
        print("读取输入失败: %s" % exc, file=sys.stderr)
        return 2

    report = run(doc_records, decl_records)
    report["errors"] = errors + report["errors"]   # 输入格式错误排在前面

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text(report)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
