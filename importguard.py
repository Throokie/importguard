"""ImportGuard: conservative, local CSV overlap preflight. Standard library only."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, date
from decimal import Decimal, InvalidOperation, localcontext
from functools import wraps
from pathlib import Path

VERSION = "0.2.0"
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20000
MAX_CANDIDATES = 5
FIELDS = ("date", "payee", "amount", "debit", "credit", "currency", "account", "id", "notes")
DATE_FORMATS = {"iso": "%Y-%m-%d", "dmy": "%d/%m/%Y", "mdy": "%m/%d/%Y", "dmy_dot": "%d.%m.%Y"}
ALIASES = {
    "date": ["date", "transaction date", "posted date", "日期", "交易日期"],
    "payee": ["payee", "description", "name", "merchant", "收款人", "摘要", "交易说明"],
    "amount": ["amount", "signed amount", "金额", "交易金额"],
    "debit": ["debit", "outflow", "withdrawal", "支出"],
    "credit": ["credit", "inflow", "deposit", "收入"],
    "currency": ["currency", "currency code", "币种"],
    "account": ["account", "account name", "账户"],
    "id": ["id", "transaction id", "imported id", "import id", "external id", "交易id"],
    "notes": ["notes", "note", "memo", "备注"],
}


class InputError(ValueError):
    """A user-correctable input problem."""


def precise(function):
    @wraps(function)
    def call(*args, **kwargs):
        with localcontext() as context:
            context.prec = 64  # 40-character inputs + at most 20,000 additions
            return function(*args, **kwargs)
    return call


def folded(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def unpack_file(file):
    if not isinstance(file, dict) or not isinstance(file.get("content"), str):
        raise InputError("请选择CSV文件。")
    try:
        raw = base64.b64decode(file["content"], validate=True)
    except (ValueError, TypeError) as exc:
        raise InputError("文件内容无效，请重新选择。") from exc
    if len(raw) > MAX_BYTES:
        raise InputError("单个文件最多5 MiB，请先分批导出。")
    return raw


def read_csv(file, config=None):
    config = config or {}
    if not isinstance(config, dict):
        raise InputError("字段设置必须是对象。")
    raw = unpack_file(file)
    encoding = config.get("encoding", "utf-8-sig")
    if encoding not in ("utf-8-sig", "cp1252"):
        raise InputError("不支持该编码；请选择UTF-8或Windows-1252。")
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise InputError("文件不是所选编码，请转换为UTF-8或在设置中选择Windows-1252。") from exc
    if "\x00" in text:
        raise InputError("文件含空字节，可能不是CSV；请从原应用重新导出。")
    delimiter = config.get("delimiter", "auto")
    if delimiter == "auto":
        try:
            delimiter = csv.Sniffer().sniff(text[:65536], delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
    if delimiter not in (",", ";", "\t"):
        raise InputError("分隔符只支持逗号、分号和Tab。")
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        headers = next(reader)
        if not headers or any(not h.strip() for h in headers):
            raise InputError("表头不能为空。")
        if len(headers) > 100 or len(set(folded(h) for h in headers)) != len(headers):
            raise InputError("表头重复或超过100列，请先修正。")
        rows = []
        for values in reader:
            if not values or not any(v.strip() for v in values):
                continue
            if len(values) != len(headers):
                raise InputError(f"CSV第{reader.line_num}物理行列数与表头不一致；请检查引号和分隔符。")
            rows.append({"line": reader.line_num, "raw": dict(zip(headers, values))})
            if len(rows) > MAX_ROWS:
                raise InputError("单份CSV最多20,000行，请分批。")
    except StopIteration as exc:
        raise InputError("CSV为空，请选择含表头的文件。") from exc
    except csv.Error as exc:
        raise InputError(f"CSV格式错误：{exc}") from exc
    return {"headers": headers, "rows": rows, "sha256": hashlib.sha256(raw).hexdigest(),
            "name": Path(str(file.get("name", "data.csv"))).name, "delimiter": delimiter,
            "bytes": len(raw), "encoding": encoding}


def guess_mapping(headers):
    lookup = {folded(h): h for h in headers}
    return {key: next((lookup[a] for a in names if a in lookup), "") for key, names in ALIASES.items()}


def inspect(file, config=None):
    parsed = read_csv(file, config)
    return {k: parsed[k] for k in ("headers", "sha256", "name", "delimiter", "bytes", "encoding")} | {
        "count": len(parsed["rows"]), "preview": parsed["rows"][:4], "mapping": guess_mapping(parsed["headers"])}


@precise
def money(value, decimal_mark="."):
    value = unicodedata.normalize("NFKC", str(value)).strip().replace("\u00a0", " ")
    if not value:
        raise InputError("金额为空")
    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1].strip()
        if value.startswith(("+", "-")):
            raise InputError("括号负数不能再带符号")
    group = "," if decimal_mark == "." else "."
    if decimal_mark not in (".", ","):
        raise InputError("小数分隔符无效")
    pattern = rf"[+-]?(?:\d+|\d{{1,3}}(?:{re.escape(group)}\d{{3}})+)(?:{re.escape(decimal_mark)}\d{{1,8}})?"
    if not re.fullmatch(pattern, value, flags=re.ASCII):
        raise InputError("金额格式无效（不接货币符号、科学计数或不规则分组；最多8位小数）")
    if len(value) > 40:
        raise InputError("金额过长")
    try:
        result = Decimal(value.replace(group, "").replace(decimal_mark, "."))
    except InvalidOperation as exc:
        raise InputError("金额格式无效") from exc
    return -result if negative else result


def decimal_text(value):
    return format(value, "f") if value else "0"


@precise
def normalize(parsed, config):
    if not isinstance(config, dict) or not isinstance(config.get("mapping"), dict):
        raise InputError("请先确认字段映射。")
    mapping = config["mapping"]
    for key, header in mapping.items():
        if key not in FIELDS or not isinstance(header, str) or (header and header not in parsed["headers"]):
            raise InputError(f"字段映射无效：{key}")
    if "invert" in config and type(config["invert"]) is not bool:
        raise InputError("金额反转设置须为布尔值true或false，不能使用字符串。")
    mode = config.get("amount_mode", "signed")
    required = ["date", "payee"] + (["amount"] if mode == "signed" else ["debit", "credit"])
    if mode not in ("signed", "split") or any(not mapping.get(k) for k in required):
        raise InputError("请映射日期、名称和金额；收支分列时需同时映射支出及收入列。")
    if len({mapping[k] for k in required}) != len(required):
        raise InputError("必填字段不能映射到同一列。")
    date_format = DATE_FORMATS.get(config.get("date_format", "iso"))
    if not date_format:
        raise InputError("日期格式无效。")
    default_account = str(config.get("default_account", "")).strip()
    default_currency = str(config.get("default_currency", "")).strip().upper()
    if not mapping.get("account") and not default_account:
        raise InputError("未映射账户列时，请填写明确的默认账户。")
    if not mapping.get("currency") and not re.fullmatch(r"[A-Z]{3}", default_currency, flags=re.ASCII):
        raise InputError("未映射币种列时，请填写三位字母默认币种，例如USD、EUR或CNY。")
    result = []
    for source in parsed["rows"]:
        raw = source["raw"]
        def cell(key):
            return raw.get(mapping.get(key, ""), "").strip()
        row = {"line": source["line"], "raw": raw}
        try:
            date_value = cell("date")
            pattern = {"%Y-%m-%d": r"\d{4}-\d{2}-\d{2}", "%d/%m/%Y": r"\d{2}/\d{2}/\d{4}",
                       "%m/%d/%Y": r"\d{2}/\d{2}/\d{4}", "%d.%m.%Y": r"\d{2}\.\d{2}\.\d{4}"}[date_format]
            if not re.fullmatch(pattern, date_value, flags=re.ASCII):
                raise InputError("日期不符合所选格式，必须包含完整年/月/日及两位月日")
            try:
                day = datetime.strptime(date_value, date_format).date()
            except ValueError as exc:
                raise InputError("日期不存在或格式不正确") from exc
            if mode == "signed":
                amount = money(cell("amount"), config.get("decimal_mark", "."))
                if config.get("invert", False):
                    amount = -amount
            else:
                debit = money(cell("debit") or "0", config.get("decimal_mark", "."))
                credit = money(cell("credit") or "0", config.get("decimal_mark", "."))
                if debit < 0 or credit < 0 or (debit != 0 and credit != 0):
                    raise InputError("收支两列须为非负，且不能同时非零")
                amount = credit - debit
            account = cell("account") if mapping.get("account") else default_account
            currency = (cell("currency") if mapping.get("currency") else default_currency).upper()
            if not account:
                raise InputError("账户为空")
            if not re.fullmatch(r"[A-Z]{3}", currency, flags=re.ASCII):
                raise InputError("币种须为三位字母；请补全币种")
            payee = cell("payee")
            if not payee:
                raise InputError("名称为空；请在原CSV补充说明后重跑")
            row.update(date=day.isoformat(), amount=decimal_text(amount), payee=payee, account=account,
                       currency=currency, id=cell("id"), notes=cell("notes"), error="")
        except InputError as exc:
            row["error"] = str(exc)
        result.append(row)
    return result


def scope(row):
    return folded(row["account"]), row["currency"]


def signature(row):
    return scope(row) + (row["date"], Decimal(row["amount"]), folded(row["payee"]))


@precise
def analyze(payload):
    if not isinstance(payload, dict):
        raise InputError("请求格式无效。")
    options = payload.get("options", {})
    if not isinstance(options, dict):
        raise InputError("检查设置无效。")
    window = options.get("day_window", 3)
    if type(window) is not int or not 0 <= window <= 7:
        raise InputError("日期容差须为0至7的整数。")
    old = read_csv(payload.get("existing"), payload.get("existing_config"))
    new = read_csv(payload.get("incoming"), payload.get("incoming_config"))
    old_rows = normalize(old, payload.get("existing_config"))
    new_rows = normalize(new, payload.get("incoming_config"))
    old_errors = [r for r in old_rows if r["error"]]
    if old_errors:
        sample = "；".join(f"第{r['line']}行：{r['error']}" for r in old_errors[:5])
        raise InputError(f"已有账本有{len(old_errors)}行无效，不能可靠检查重叠。请先修复：{sample}")
    ids = defaultdict(list)
    by_amount = defaultdict(lambda: defaultdict(list))
    for row in old_rows:
        if row["id"]:
            ids[scope(row) + (row["id"],)].append(row)
        by_amount[scope(row) + (Decimal(row["amount"]),)][row["date"]].append(row)
    new_ids = Counter(scope(r) + (r["id"],) for r in new_rows if not r["error"] and r["id"])
    new_sigs = Counter(signature(r) for r in new_rows if not r["error"])
    trusted = options.get("trust_ids") is True
    output = []
    for row in new_rows:
        row = row.copy()
        row.update(candidates=[], candidate_count=0)
        if row["error"]:
            row.update(status="invalid", reason=row["error"], default_decision="pending")
            output.append(row)
            continue
        row["export_scope"] = {"account_key": scope(row)[0], "currency": row["currency"],
            "key": json.dumps(scope(row), ensure_ascii=False, separators=(",", ":"))}
        key = scope(row)
        id_matches = ids[key + (row["id"],)] if trusted and row["id"] else []
        batch_id_conflict = bool(trusted and row["id"] and new_ids[key + (row["id"],)] > 1)
        if batch_id_conflict:
            row.update(status="review", reason="待导入文件同一账户/币种内交易ID重复；保留每笔，请核原导出。", default_decision="pending")
            candidates = id_matches
        elif id_matches:
            exact = len(id_matches) == 1 and all(row[k] == id_matches[0][k] for k in ("date", "currency")) and Decimal(row["amount"]) == Decimal(id_matches[0]["amount"])
            if exact:
                row.update(status="duplicate", reason="已确认同一ID体系；唯一ID及日期/金额/账户/币种一致。", default_decision="exclude")
            else:
                row.update(status="review", reason="相同ID存在多个已有记录，或日期/金额与历史不一致。", default_decision="pending")
            candidates = id_matches
        else:
            day = date.fromisoformat(row["date"])
            candidates = []
            candidate_count = 0
            from datetime import timedelta
            amount_bucket = by_amount[key + (Decimal(row["amount"]),)]
            for offset in sorted(range(-window, window + 1), key=lambda n: (abs(n), n)):
                ordinal = day.toordinal() + offset
                if not 1 <= ordinal <= date.max.toordinal():
                    continue
                bucket = amount_bucket.get(date.fromordinal(ordinal).isoformat(), [])
                candidate_count += len(bucket)
                candidates.extend(bucket[:max(0, MAX_CANDIDATES - len(candidates))])
            batch_repeat = new_sigs[signature(row)] > 1
            if candidates:
                exact_day = any(r["date"] == row["date"] for r in candidates)
                reason = "同账户/币种有同日同额记录；可能重叠，也可能是真实多笔消费。" if exact_day else f"同账户/币种有同额记录，日期相差不超过{window}天。"
                if trusted and row["id"] and any(r["id"] and r["id"] != row["id"] for r in candidates):
                    reason += " ID不同；不要仅凭金额排除。"
                row.update(status="review", reason=reason, default_decision="pending")
            elif batch_repeat:
                row.update(status="review", reason="待导入文件存在同日同额同名多笔；逐笔保留，请确认是否真实消费。", default_decision="pending")
            else:
                row.update(status="new", reason="已提供的历史范围内没有发现候选；仍须核导出覆盖范围。", default_decision="include")
        row["candidate_count"] = candidate_count if not (batch_id_conflict or id_matches) else len(candidates)
        row["candidates"] = [{k: r[k] for k in ("line", "date", "amount", "payee", "account", "currency", "id")} for r in candidates[:MAX_CANDIDATES]]
        output.append(row)
    counts = dict(Counter(r["status"] for r in output))
    for status in ("new", "duplicate", "review", "invalid"):
        counts.setdefault(status, 0)
    totals = defaultdict(Decimal)
    for row in output:
        if not row["error"]:
            totals[row["currency"]] += Decimal(row["amount"])
    meta = {"version": VERSION, "existing": {k: old[k] for k in ("name", "sha256", "bytes", "headers")},
            "incoming": {k: new[k] for k in ("name", "sha256", "bytes", "headers")},
            "existing_rows": len(old_rows), "incoming_rows": len(output), "counts": counts,
            "totals_by_currency": {k: decimal_text(v) for k, v in totals.items()},
            "options": {"day_window": window, "trust_ids": trusted},
            "configs": {"existing": payload["existing_config"], "incoming": payload["incoming_config"]}}
    return {"meta": meta, "rows": output}


def safe_cell(value):
    value = str(value)
    probe = value.lstrip(" \r\n\t\ufeff")
    numeric = False
    if probe.startswith(("+", "-")):
        for decimal_mark in (".", ","):
            try:
                money(probe, decimal_mark)
                numeric = True
            except InputError:
                pass
    if probe and (probe[0] in "=@" or (probe[0] in "+-" and not numeric) or value.startswith(("\t", "\r", "\n"))):
        return "'" + value
    return value


def csv_bytes(headers, rows):
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([safe_cell(h) for h in headers])
    for values in rows:
        writer.writerow([safe_cell(v) for v in values])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def partition_exports(selected, headers):
    """One import context per file. Names never contain user account path text."""
    grouped = defaultdict(list)
    for row in selected:
        grouped[scope(row)].append(row)
    groups, files = [], {}
    standard_headers = ["Date", "Payee", "Amount", "Currency", "Account", "Import ID", "Notes"]
    for index, (key, rows) in enumerate(sorted(grouped.items()), 1):
        account_key, currency = key
        token = hashlib.sha256(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:10]
        folder = f"accounts/g{index:03d}_{token}_{currency}"
        original = f"{folder}/ready_rows.csv"
        normalized = f"{folder}/normalized_rows.csv"
        files[original] = csv_bytes(headers, [[r["raw"][h] for h in headers] for r in rows])
        files[normalized] = csv_bytes(standard_headers,
            [[r[k] for k in ("date", "payee", "amount", "currency", "account", "id", "notes")] for r in rows])
        with localcontext() as context:
            context.prec = 64
            total = sum((Decimal(r["amount"]) for r in rows), Decimal(0))
        groups.append({"group_id": f"g{index:03d}", "account_key": account_key, "account": rows[0]["account"],
            "account_aliases": sorted({r["account"] for r in rows}), "currency": currency,
            "row_count": len(rows), "amount_total": decimal_text(total), "source_lines": [r["line"] for r in rows],
            "ready_file": original, "normalized_file": normalized})
    if len(groups) == 1:
        groups[0].update(ready_file="ready_rows.csv", normalized_file="normalized_rows.csv")
        files = {}  # Preserve single-context layout without duplicate import copies.
    return groups, files


def export_bundle(payload):
    result = analyze(payload)
    decisions = payload.get("decisions", {})
    if not isinstance(decisions, dict):
        raise InputError("复核决策格式无效。")
    known = {str(r["line"]) for r in result["rows"]}
    if any(k not in known for k in decisions):
        raise InputError("复核决策含不存在的行号，请重新检查。")
    selected, audit_rows, queue = [], [], []
    counts = Counter()
    escaped = 0
    headers = result["meta"]["incoming"]["headers"]
    for row in result["rows"]:
        decision = decisions.get(str(row["line"]), row["default_decision"])
        if decision not in ("include", "exclude", "pending"):
            raise InputError("复核决策只能为加入、排除或待定。")
        if row["status"] == "invalid" and decision == "include":
            raise InputError(f"第{row['line']}行无效，须修复原文件后重新检查，不能加入。")
        counts[decision] += 1
        record = row | {"decision": decision, "manual_decision": str(row["line"]) in decisions}
        audit_rows.append(record)
        if decision == "include":
            selected.append(row)
            escaped += sum(safe_cell(row["raw"][h]) != row["raw"][h] for h in headers)
        else:
            queue.append(record)
    groups, import_files = partition_exports(selected, headers)
    multiple = len(groups) > 1
    raw_name = "all_selected_rows_REVIEW_ONLY.csv" if multiple else "ready_rows.csv"
    normalized_name = "all_normalized_rows_REVIEW_ONLY.csv" if multiple else "normalized_rows.csv"
    manifest = {"schema_version": 2, "version": VERSION, "scope": "one canonical account and currency per import file",
        "mode": "partitioned" if multiple else "single", "group_count": len(groups), "selected_count": len(selected),
        "groups": groups, "review_only_files": [raw_name, normalized_name] if multiple else [],
        "warning": "选择对应账户和币种逐文件预览。导入目标仍会运行自己的规则/去重；本包不是目标账本导入成功证明。"}
    meta = result["meta"] | {"exported_at_utc": datetime.now().astimezone().astimezone(__import__("datetime").timezone.utc).isoformat(),
        "decisions": {k: counts[k] for k in ("include", "exclude", "pending")}, "escaped_text_cells": escaped,
        "export_layout": {"schema_version": 2, "mode": manifest["mode"], "group_count": len(groups)},
        "unresolved_warning": "待定与无效行未进入ready_rows.csv；请逐行核review.csv。",
        "scope_warning": "只与已提供历史对比，不证明完整无重叠；不保证目标应用直接兼容。"}
    files = {
        raw_name: csv_bytes(headers, [[r["raw"][h] for h in headers] for r in selected]),
        normalized_name: csv_bytes(["Date", "Payee", "Amount", "Currency", "Account", "Import ID", "Notes"],
            [[r[k] for k in ("date", "payee", "amount", "currency", "account", "id", "notes")] for r in selected]),
        "review.csv": csv_bytes(["source_line", "status", "decision", "reason", "candidate_lines", "original_row_json"],
            [[r["line"], r["status"], r["decision"], r["reason"], ";".join(str(c["line"]) for c in r["candidates"]), json.dumps(r["raw"], ensure_ascii=False)] for r in queue]),
        "audit.json": json.dumps({"meta": meta, "rows": audit_rows}, ensure_ascii=False, indent=2).encode(),
        "import-manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
        "README.txt": (f"ImportGuard {VERSION}\n已选新增：{counts['include']}；排除：{counts['exclude']}；待定：{counts['pending']}。\n"
            + (f"本包有{len(groups)}个账户/币种组合：请按import-manifest.json选择accounts/下对应文件，逐个导入相同账户和币种。\n"
               "根目录all_*_REVIEW_ONLY.csv只供汇总复核，不要导入单个账户。多账户包不再提供混合ready_rows.csv。\n" if multiple else
               "单账户包保留根目录ready_rows.csv与normalized_rows.csv；它们是两种格式，选择一种导入，勿重复。\n")
            +
            "ready_rows.csv保留输入字段顺序（UTF-8 BOM，逗号分隔），危险文本单元格加单引号防公式执行。\n"
            "normalized_rows.csv为ISO日期、有符号点小数金额，负数支出；请在目标应用预览确认映射与金额方向。\n"
            "review.csv保留未加入行，audit.json保留全部行、依据、输入哈希及决策。\n"
            "保留原CSV及账本备份，在目标应用小批预览；本工具不回写账本。\n"
            "只检查用户提供的历史范围，不能发现未导出/描述缺失/拆分或跨币种转换的重叠。\n").encode(),
    }
    files.update(import_files)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return out.getvalue()


def file_object(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise InputError("单个文件最多5 MiB。")
    return {"name": path.name, "content": base64.b64encode(path.read_bytes()).decode()}


def main():
    parser = argparse.ArgumentParser(description="本地CSV导入前重叠检查；不修改源文件。")
    parser.add_argument("--existing", required=True, type=Path)
    parser.add_argument("--incoming", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path, help="含existing_config/incoming_config/options/decisions的JSON")
    parser.add_argument("--output", required=True, type=Path, help="新ZIP路径；不覆盖已有文件")
    args = parser.parse_args()
    try:
        payload = json.loads(args.config.read_text(encoding="utf-8"))
        payload.update(existing=file_object(args.existing), incoming=file_object(args.incoming))
        bundle = export_bundle(payload)
        with args.output.open("xb") as target:
            target.write(bundle)
        print(f"已导出 {args.output}；请先核ZIP内README、review.csv及audit.json。")
    except (InputError, OSError, ValueError) as exc:
        parser.exit(2, f"未导出：{exc}\n")


if __name__ == "__main__":
    main()
