import base64
import copy
import csv
import hashlib
import io
import json
import sys
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from importguard import InputError, analyze, export_bundle, inspect, money, read_csv, safe_cell

HEADERS = ["Date", "Payee", "Amount", "Currency", "Account", "ID", "Notes"]
CONFIG = {"mapping": dict(zip(["date", "payee", "amount", "currency", "account", "id", "notes"], HEADERS)),
          "date_format": "iso", "decimal_mark": ".", "amount_mode": "signed", "default_account": "Main", "default_currency": "USD"}


def file(rows, headers=HEADERS, delimiter=",", encoding="utf-8"):
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=delimiter)
    writer.writerow(headers)
    writer.writerows(rows)
    return {"name": "sample.csv", "content": base64.b64encode(buf.getvalue().encode(encoding)).decode()}


def tx(day="2026-10-10", payee="Coffee", amount="-5.00", currency="USD", account="Main", id="", notes=""):
    return [day, payee, amount, currency, account, id, notes]


def payload(old, new, trusted=False, window=3):
    return {"existing": file(old), "incoming": file(new), "existing_config": copy.deepcopy(CONFIG),
            "incoming_config": copy.deepcopy(CONFIG), "options": {"day_window": window, "trust_ids": trusted}}


class MatchingTests(unittest.TestCase):
    def test_demo_known_outcomes(self):
        root = Path(__file__).resolve().parents[1] / "examples"
        p = json.loads((root / "config.json").read_text())
        for key, name in [("existing", "existing.csv"), ("incoming", "incoming.csv")]:
            p[key] = {"name": name, "content": base64.b64encode((root / name).read_bytes()).decode()}
        r = analyze(p)
        self.assertEqual(r["meta"]["counts"], {"new": 5, "duplicate": 1, "review": 4, "invalid": 1})
        self.assertEqual(sum(r["meta"]["counts"].values()), 11)

    def test_no_id_same_day_amount_is_review_not_deletion(self):
        r = analyze(payload([tx()], [tx(payee="Different bank description")]))
        self.assertEqual(r["rows"][0]["status"], "review")
        self.assertEqual(r["rows"][0]["default_decision"], "pending")

    def test_ids_not_trusted_by_default(self):
        r = analyze(payload([tx(id="same")], [tx(id="same")]))
        self.assertEqual(r["rows"][0]["status"], "review")

    def test_unique_shared_id_with_name_change(self):
        r = analyze(payload([tx(id="same")], [tx(id="same", payee="Renamed")], True))
        self.assertEqual(r["rows"][0]["status"], "duplicate")

    def test_id_conflicting_date_and_amount(self):
        for new in [tx(id="same",day="2026-10-11"), tx(id="same",amount="-7.00")]:
            r = analyze(payload([tx(id="same")], [new], True))
            self.assertEqual(r["rows"][0]["status"], "review")

    def test_incoming_duplicate_id_quarantines_every_occurrence(self):
        r = analyze(payload([tx(id="same")], [tx(id="same"), tx(id="same")], True))
        self.assertEqual([r["status"] for r in r["rows"]], ["review", "review"])

    def test_historical_duplicate_id_is_ambiguous(self):
        r = analyze(payload([tx(id="same"),tx(id="same")], [tx(id="same")], True))
        self.assertEqual(r["rows"][0]["status"], "review")
        self.assertEqual(r["rows"][0]["candidate_count"], 2)

    def test_true_repeated_purchases_not_collapsed(self):
        r = analyze(payload([tx()], [tx(),tx(),tx()]))
        self.assertEqual(len(r["rows"]), 3)
        self.assertEqual(r["meta"]["counts"]["review"], 3)

    def test_batch_duplicates_without_history_are_retained(self):
        r = analyze(payload([], [tx(),tx()]))
        self.assertEqual(r["meta"]["counts"]["review"], 2)

    def test_account_and_currency_isolation(self):
        r = analyze(payload([tx()], [tx(account="Other"),tx(currency="EUR")]))
        self.assertEqual(r["meta"]["counts"]["new"], 2)

    def test_accounts_normalize_unicode_case_whitespace(self):
        r = analyze(payload([tx(account="Main Account")], [tx(account="  main  ACCOUNT ")]))
        self.assertEqual(r["rows"][0]["status"], "review")

    def test_date_tolerance_boundary(self):
        r = analyze(payload([tx()], [tx(day="2026-10-13"),tx(day="2026-10-14")]))
        self.assertEqual([r["status"] for r in r["rows"]], ["review", "new"])

    def test_zero_day_tolerance(self):
        r = analyze(payload([tx()], [tx(day="2026-10-11")], window=0))
        self.assertEqual(r["rows"][0]["status"], "new")

    def test_extreme_valid_dates_do_not_overflow(self):
        for day in ["0001-01-01", "9999-12-31"]:
            r = analyze(payload([tx(day=day)], [tx(day=day)]))
            self.assertEqual(r["rows"][0]["status"], "review")

    def test_exact_decimal_totals_and_no_float_noise(self):
        r = analyze(payload([], [tx(amount="0.1"),tx(amount="0.2",payee="Other")]))
        self.assertEqual(Decimal(r["meta"]["totals_by_currency"]["USD"]), Decimal("0.3"))

    def test_large_exact_values_not_context_rounded(self):
        value = "123456789012345678901234567890.12345678"
        r = analyze(payload([], [tx(amount=value),tx(amount="0.00000001",payee="Other")]))
        self.assertEqual(r["meta"]["totals_by_currency"]["USD"], "123456789012345678901234567890.12345679")

    def test_old_invalid_rows_block_analysis(self):
        with self.assertRaisesRegex(InputError,"已有账本有1行无效"):
            analyze(payload([tx(day="2026-02-30")], [tx()]))

    def test_new_invalid_rows_preserved(self):
        r = analyze(payload([], [tx(day="2026-02-30"),tx(amount="$5"),tx(currency=""),tx(payee="")]))
        self.assertEqual(r["meta"]["counts"]["invalid"], 4)

    def test_mapping_missing_or_reused_required_field_rejected(self):
        p = payload([], [tx()]);p["incoming_config"]["mapping"]["date"]=""
        with self.assertRaises(InputError):analyze(p)
        p["incoming_config"]["mapping"]["date"]="Payee"
        with self.assertRaises(InputError):analyze(p)

    def test_no_guessed_account_or_currency_when_columns_absent(self):
        for missing in ["account", "currency"]:
            p=payload([], [tx()]);p["incoming_config"]["mapping"][missing]=""
            p["incoming_config"].pop("default_"+missing)
            with self.assertRaises(InputError):analyze(p)

    def test_string_false_cannot_silently_invert_amount(self):
        p=payload([], [tx()]);p["incoming_config"]["invert"]="false"
        with self.assertRaisesRegex(InputError,"布尔值"):analyze(p)

    def test_non_string_column_mapping_rejected(self):
        p=payload([], [tx()]);p["incoming_config"]["mapping"]["date"]=[]
        with self.assertRaises(InputError):analyze(p)

    def test_split_amount_and_dmy_comma_decimal(self):
        headers=["Date","Payee","Debit","Credit"]
        p=payload([],[])
        cfg={"mapping":{"date":"Date","payee":"Payee","debit":"Debit","credit":"Credit"},"date_format":"dmy","amount_mode":"split","decimal_mark":",","default_account":"Card","default_currency":"EUR","delimiter":";"}
        p.update(existing=file([],headers,";"),incoming=file([["11/10/2026","Café","1.234,56",""],["12/10/2026","Salary","","2.000,00"]],headers,";"),existing_config=cfg,incoming_config=cfg)
        r=analyze(p)
        self.assertEqual([r["amount"] for r in r["rows"]],["-1234.56","2000.00"])

    def test_split_invalid_signs_and_two_sides(self):
        p=payload([],[]);cfg=copy.deepcopy(CONFIG);cfg.update(amount_mode="split");cfg["mapping"]={"date":"Date","payee":"Payee","debit":"Amount","credit":"Notes"}
        p.update(existing_config=cfg,incoming_config=cfg,incoming=file([tx(amount="-5",notes="0"),tx(amount="5",notes="6")]))
        self.assertEqual(analyze(p)["meta"]["counts"]["invalid"],2)

    def test_invert_credit_card_sign(self):
        p=payload([], [tx(amount="12.50")]);p["incoming_config"]["invert"]=True
        self.assertEqual(analyze(p)["rows"][0]["amount"],"-12.50")

    def test_candidate_limit_counts_all(self):
        r=analyze(payload([tx() for _ in range(40)], [tx()]))
        self.assertEqual(r["rows"][0]["candidate_count"],40)
        self.assertEqual(len(r["rows"][0]["candidates"]),5)

    def test_invalid_options(self):
        for window in [-1,8,3.2,True,"3"]:
            with self.assertRaises(InputError):analyze(payload([],[],window=window))


class FormatTests(unittest.TestCase):
    def test_money_formats(self):
        for value,mark,expected in [("(1,234.56)",".","-1234.56"),("+1.234,56",",","1234.56"),("-0.00000001",".","-0.00000001")]:
            self.assertEqual(money(value,mark),Decimal(expected))
        for invalid in ["1e3","NaN","Infinity","$5","1,23.45","(-5)","1.123456789","","1_000"]:
            with self.assertRaises(InputError):money(invalid)

    def test_utf8_bom_and_cp1252(self):
        for encoding in ["utf-8-sig","cp1252"]:
            p=file([tx(payee="Café")],encoding=encoding)
            r=read_csv(p,{"encoding":encoding});self.assertEqual(r["rows"][0]["raw"]["Payee"],"Café")

    def test_wrong_encoding_is_actionable(self):
        with self.assertRaisesRegex(InputError,"编码"):
            inspect(file([tx(payee="Café")],encoding="cp1252"))

    def test_delimiters(self):
        for delim in [",",";","\t"]:
            self.assertEqual(inspect(file([tx()],delimiter=delim))["count"],1)

    def test_duplicate_empty_headers_and_shape_errors(self):
        for p in [file([],headers=["A","A"]),file([],headers=["A",""]),file([["a"]],headers=["A","B"])]:
            with self.assertRaises(InputError):read_csv(p)

    def test_multiline_row_physical_line_is_traceable(self):
        p=file([tx(notes="first\nsecond"),tx(day="2026-10-11")])
        rows=read_csv(p)["rows"]
        self.assertEqual([r["line"] for r in rows],[3,4])
        self.assertEqual(rows[0]["raw"]["Notes"],"first\nsecond")

    def test_empty_file_empty_rows_and_headers_only(self):
        with self.assertRaises(InputError):read_csv({"content":""})
        self.assertEqual(read_csv(file([]))["rows"],[])

    def test_bad_base64_and_nul(self):
        with self.assertRaises(InputError):read_csv({"content":"!!"})
        with self.assertRaises(InputError):read_csv({"content":base64.b64encode(b"a\x00b").decode()})

    def test_size_cap(self):
        with self.assertRaises(InputError):read_csv({"content":base64.b64encode(b"x"*(5*1024*1024+1)).decode()})

    def test_formula_cells_escaped_signed_numbers_preserved(self):
        for value in ["=1+1","  @SUM(A1:A2)","\t=1+1","-1+CMD()","+HYPERLINK(1)","\n=2"]:
            self.assertTrue(safe_cell(value).startswith("'"))
        for value in ["-5","-1,234.56","-1.234,56","+10.25","Normal"]:
            self.assertEqual(safe_cell(value),value)


class ExportTests(unittest.TestCase):
    def test_decisions_and_input_hashes_roundtrip(self):
        p=payload([tx()], [tx(),tx(day="2026-10-20",payee="New"),tx(day="bad")])
        original=copy.deepcopy(p)
        p["decisions"]={"2":"include","3":"exclude"}
        with zipfile.ZipFile(io.BytesIO(export_bundle(p))) as z:
            audit=json.loads(z.read("audit.json"))
            self.assertEqual(audit["meta"]["decisions"],{"include":1,"exclude":1,"pending":1})
            imported=list(csv.DictReader(io.StringIO(z.read("ready_rows.csv").decode("utf-8-sig"))))
            self.assertEqual(len(imported),1)
            self.assertEqual(imported[0]["Payee"],"Coffee")
            self.assertEqual(audit["meta"]["incoming"]["sha256"],hashlib.sha256(base64.b64decode(p["incoming"]["content"])).hexdigest())
        self.assertEqual(original["incoming"],p["incoming"])

    def test_pending_not_imported_invalid_cannot_include(self):
        p=payload([tx()], [tx(),tx(day="bad")])
        with zipfile.ZipFile(io.BytesIO(export_bundle(p))) as z:
            self.assertEqual(len(list(csv.reader(io.StringIO(z.read("ready_rows.csv").decode("utf-8-sig"))))),1)
        p["decisions"]={"3":"include"}
        with self.assertRaises(InputError):export_bundle(p)

    def test_unknown_decision_and_row_rejected(self):
        p=payload([], [tx()])
        for decisions in [{"999":"include"},{"2":"nonsense"}]:
            p["decisions"]=decisions
            with self.assertRaises(InputError):export_bundle(p)

    def test_export_formula_text_escaped_and_audit_retains_original(self):
        p=payload([], [tx(payee="=HYPERLINK(1)")])
        with zipfile.ZipFile(io.BytesIO(export_bundle(p))) as z:
            audit=json.loads(z.read("audit.json"));self.assertEqual(audit["meta"]["escaped_text_cells"],1)
            self.assertEqual(audit["rows"][0]["raw"]["Payee"],"=HYPERLINK(1)")
            self.assertIn("'=HYPERLINK(1)",z.read("ready_rows.csv").decode("utf-8-sig"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
