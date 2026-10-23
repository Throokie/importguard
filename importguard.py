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

VERSION = "0.3.0"
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


from localization import InputError, format_message, normalize_language, error_payload


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
        raise InputError('file.required')
    try:
        raw = base64.b64decode(file["content"], validate=True)
    except (ValueError, TypeError) as exc:
        raise InputError('file.invalid_base64') from exc
    if len(raw) > MAX_BYTES:
        raise InputError('file.too_large')
    return raw


def read_csv(file, config=None):
    config = config or {}
    if not isinstance(config, dict):
        raise InputError('config.object')
    raw = unpack_file(file)
    encoding = config.get("encoding", "utf-8-sig")
    if encoding not in ("utf-8-sig", "cp1252"):
        raise InputError('encoding.unsupported')
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise InputError('encoding.decode') from exc
    if "\x00" in text:
        raise InputError('csv.null_byte')
    delimiter = config.get("delimiter", "auto")
    if delimiter == "auto":
        try:
            delimiter = csv.Sniffer().sniff(text[:65536], delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
    if delimiter not in (",", ";", "\t"):
        raise InputError('csv.delimiter')
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        headers = next(reader)
        if not headers or any(not h.strip() for h in headers):
            raise InputError('csv.empty_header')
        if len(headers) > 100 or len(set(folded(h) for h in headers)) != len(headers):
            raise InputError('csv.headers')
        rows = []
        for values in reader:
            if not values or not any(v.strip() for v in values):
                continue
            if len(values) != len(headers):
                raise InputError('csv.columns', line=reader.line_num)
            rows.append({"line": reader.line_num, "raw": dict(zip(headers, values))})
            if len(rows) > MAX_ROWS:
                raise InputError("csv.too_many_rows")
    except StopIteration as exc:
        raise InputError('csv.empty') from exc
    except csv.Error as exc:
        raise InputError('csv.malformed', detail=str(exc)) from exc
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
        raise InputError('money.empty')
    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1].strip()
        if value.startswith(("+", "-")):
            raise InputError('money.double_sign')
    group = "," if decimal_mark == "." else "."
    if decimal_mark not in (".", ","):
        raise InputError('money.decimal_mark')
    pattern = rf"[+-]?(?:\d+|\d{{1,3}}(?:{re.escape(group)}\d{{3}})+)(?:{re.escape(decimal_mark)}\d{{1,8}})?"
    if not re.fullmatch(pattern, value, flags=re.ASCII):
        raise InputError('money.format')
    if len(value) > 40:
        raise InputError('money.too_long')
    try:
        result = Decimal(value.replace(group, "").replace(decimal_mark, "."))
    except InvalidOperation as exc:
        raise InputError('money.invalid') from exc
    return -result if negative else result


def decimal_text(value):
    return format(value, "f") if value else "0"


@precise
def normalize(parsed, config):
    if not isinstance(config, dict) or not isinstance(config.get("mapping"), dict):
        raise InputError('mapping.required')
    mapping = config["mapping"]
    for key, header in mapping.items():
        if key not in FIELDS or not isinstance(header, str) or (header and header not in parsed["headers"]):
            raise InputError('mapping.invalid', field=key)
    if "invert" in config and type(config["invert"]) is not bool:
        raise InputError('config.invert')
    mode = config.get("amount_mode", "signed")
    required = ["date", "payee"] + (["amount"] if mode == "signed" else ["debit", "credit"])
    if mode not in ("signed", "split") or any(not mapping.get(k) for k in required):
        raise InputError('mapping.missing')
    if len({mapping[k] for k in required}) != len(required):
        raise InputError('mapping.duplicate')
    date_format = DATE_FORMATS.get(config.get("date_format", "iso"))
    if not date_format:
        raise InputError('date.format')
    default_account = str(config.get("default_account", "")).strip()
    default_currency = str(config.get("default_currency", "")).strip().upper()
    if not mapping.get("account") and not default_account:
        raise InputError('account.default')
    if not mapping.get("currency") and not re.fullmatch(r"[A-Z]{3}", default_currency, flags=re.ASCII):
        raise InputError('currency.default')
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
                raise InputError('date.pattern')
            try:
                day = datetime.strptime(date_value, date_format).date()
            except ValueError as exc:
                raise InputError('date.invalid') from exc
            if mode == "signed":
                amount = money(cell("amount"), config.get("decimal_mark", "."))
                if config.get("invert", False):
                    amount = -amount
            else:
                debit = money(cell("debit") or "0", config.get("decimal_mark", "."))
                credit = money(cell("credit") or "0", config.get("decimal_mark", "."))
                if debit < 0 or credit < 0 or (debit != 0 and credit != 0):
                    raise InputError('money.split')
                amount = credit - debit
            account = cell("account") if mapping.get("account") else default_account
            currency = (cell("currency") if mapping.get("currency") else default_currency).upper()
            if not account:
                raise InputError('account.empty')
            if not re.fullmatch(r"[A-Z]{3}", currency, flags=re.ASCII):
                raise InputError('currency.invalid')
            payee = cell("payee")
            if not payee:
                raise InputError('payee.empty')
            row.update(date=day.isoformat(), amount=decimal_text(amount), payee=payee, account=account,
                       currency=currency, id=cell("id"), notes=cell("notes"), error="")
        except InputError as exc:
            row["error"] = str(exc)
            row["error_code"] = exc.code
            row["error_params"] = exc.params
        result.append(row)
    return result


def scope(row):
    return folded(row["account"]), row["currency"]


def signature(row):
    return scope(row) + (row["date"], Decimal(row["amount"]), folded(row["payee"]))


@precise
def analyze(payload):
    if not isinstance(payload, dict):
        raise InputError('request.invalid')
    language = normalize_language(payload.get("language"), default="en" if "language" in payload else "zh-CN")
    options = payload.get("options", {})
    if not isinstance(options, dict):
        raise InputError('options.invalid')
    window = options.get("day_window", 3)
    if type(window) is not int or not 0 <= window <= 7:
        raise InputError('options.day_window')
    old = read_csv(payload.get("existing"), payload.get("existing_config"))
    new = read_csv(payload.get("incoming"), payload.get("incoming_config"))
    old_rows = normalize(old, payload.get("existing_config"))
    new_rows = normalize(new, payload.get("incoming_config"))
    old_errors = [r for r in old_rows if r["error"]]
    if old_errors:
        details = [{"code": "error.row_detail", "params": {"line": r["line"],
                    "detail": {"code": r["error_code"], "params": r["error_params"]}}} for r in old_errors[:5]]
        raise InputError("error.existing_invalid", count=len(old_errors), details=details)
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
            row["error"] = format_message(row["error_code"], row["error_params"], language)
            row.update(status="invalid", reason=row["error"], reason_code=row["error_code"],
                       reason_params=row["error_params"], default_decision="pending")
            output.append(row)
            continue
        row["export_scope"] = {"account_key": scope(row)[0], "currency": row["currency"],
            "key": json.dumps(scope(row), ensure_ascii=False, separators=(",", ":"))}
        key = scope(row)
        id_matches = ids[key + (row["id"],)] if trusted and row["id"] else []
        batch_id_conflict = bool(trusted and row["id"] and new_ids[key + (row["id"],)] > 1)
        if batch_id_conflict:
            row.update(status="review", reason_code="reason.batch_id_conflict", reason_params={}, default_decision="pending")
            candidates = id_matches
        elif id_matches:
            exact = len(id_matches) == 1 and all(row[k] == id_matches[0][k] for k in ("date", "currency")) and Decimal(row["amount"]) == Decimal(id_matches[0]["amount"])
            if exact:
                row.update(status="duplicate", reason_code="reason.trusted_id_duplicate", reason_params={}, default_decision="exclude")
            else:
                row.update(status="review", reason_code="reason.id_conflict", reason_params={}, default_decision="pending")
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
                reason_code = "reason.candidates_same_day" if exact_day else "reason.candidates_date_window"
                reason_params = {"window": window, "id_warning": ""}
                if trusted and row["id"] and any(r["id"] and r["id"] != row["id"] for r in candidates):
                    reason_params["id_warning"] = {"code": "reason.id_differs", "params": {}}
                row.update(status="review", reason_code=reason_code, reason_params=reason_params, default_decision="pending")
            elif batch_repeat:
                row.update(status="review", reason_code="reason.batch_repeat", reason_params={}, default_decision="pending")
            else:
                row.update(status="new", reason_code="reason.no_candidates", reason_params={}, default_decision="include")
        row["reason"] = format_message(row["reason_code"], row["reason_params"], language)
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
    meta = {"version": VERSION, "language": language, "existing": {k: old[k] for k in ("name", "sha256", "bytes", "headers")},
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
    language = result["meta"]["language"]
    decisions = payload.get("decisions", {})
    if not isinstance(decisions, dict):
        raise InputError('decisions.object')
    known = {str(r["line"]) for r in result["rows"]}
    if any(k not in known for k in decisions):
        raise InputError('decisions.unknown_line')
    selected, audit_rows, queue = [], [], []
    counts = Counter()
    escaped = 0
    headers = result["meta"]["incoming"]["headers"]
    for row in result["rows"]:
        decision = decisions.get(str(row["line"]), row["default_decision"])
        if decision not in ("include", "exclude", "pending"):
            raise InputError('decisions.invalid')
        if row["status"] == "invalid" and decision == "include":
            raise InputError('decisions.invalid_include', line=row['line'])
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
    manifest = {"schema_version": 2, "version": VERSION, "language": language, "scope": "one canonical account and currency per import file",
        "mode": "partitioned" if multiple else "single", "group_count": len(groups), "selected_count": len(selected),
        "groups": groups, "review_only_files": [raw_name, normalized_name] if multiple else [],
        "warning": format_message("export.target_warning", language=language)}
    meta = result["meta"] | {"exported_at_utc": datetime.now().astimezone().astimezone(__import__("datetime").timezone.utc).isoformat(),
        "decisions": {k: counts[k] for k in ("include", "exclude", "pending")}, "escaped_text_cells": escaped,
        "export_layout": {"schema_version": 2, "mode": manifest["mode"], "group_count": len(groups)},
        "unresolved_warning": format_message("export.unresolved_warning", language=language),
        "scope_warning": format_message("export.scope_warning", language=language)}
    files = {
        raw_name: csv_bytes(headers, [[r["raw"][h] for h in headers] for r in selected]),
        normalized_name: csv_bytes(["Date", "Payee", "Amount", "Currency", "Account", "Import ID", "Notes"],
            [[r[k] for k in ("date", "payee", "amount", "currency", "account", "id", "notes")] for r in selected]),
        "review.csv": csv_bytes(["source_line", "status", "decision", "reason", "candidate_lines", "original_row_json"],
            [[r["line"], r["status"], r["decision"], r["reason"], ";".join(str(c["line"]) for c in r["candidates"]), json.dumps(r["raw"], ensure_ascii=False)] for r in queue]),
        "audit.json": json.dumps({"meta": meta, "rows": audit_rows}, ensure_ascii=False, indent=2).encode(),
        "import-manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
        "README.txt": format_message("export.readme", {
            "version": VERSION, "include": counts["include"], "exclude": counts["exclude"], "pending": counts["pending"],
            "layout": {"code": "export.layout_multiple" if multiple else "export.layout_single", "params": {"count": len(groups)}}
        }, language).encode("utf-8"),
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
        raise InputError('file.too_large')
    return {"name": path.name, "content": base64.b64encode(path.read_bytes()).decode()}


def main():
    parser = argparse.ArgumentParser(description="Local CSV overlap preflight; source files are never modified.")
    parser.add_argument("--existing", required=True, type=Path)
    parser.add_argument("--incoming", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path, help="JSON with existing_config/incoming_config/options/decisions")
    parser.add_argument("--output", required=True, type=Path, help="New ZIP path; existing files are never overwritten")
    parser.add_argument("--language", choices=("en", "zh-CN"), default="en", help="Language of messages and exported review text")
    args = parser.parse_args()
    try:
        payload = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise InputError("request.invalid")
        payload.update(existing=file_object(args.existing), incoming=file_object(args.incoming), language=args.language)
        bundle = export_bundle(payload)
        with args.output.open("xb") as target:
            target.write(bundle)
        print(format_message("cli.exported", {"path": str(args.output)}, args.language))
    except (InputError, OSError, ValueError) as exc:
        detail = error_payload(exc, args.language)["error"] if isinstance(exc, InputError) else str(exc)
        parser.exit(2, format_message("cli.failed", {"detail": detail}, args.language) + "\n")


if __name__ == "__main__":
    main()
