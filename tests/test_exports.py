"""Fixed-before-observation account handoff checks; not a real ledger integration."""
import base64
import copy
import csv
import hashlib
import importlib.util
import io
import json
import re
import unittest
import zipfile
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/handoff"
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

current = load("export_current", ROOT / "importguard.py")

P = json.loads((FIXTURES / "new-input.json").read_text())
EXPECTED = json.loads((FIXTURES / "expected-before.json").read_text())


def rows(archive, name):
    return list(csv.DictReader(io.StringIO(archive.read(name).decode("utf-8-sig"))))


def opened(payload=None):
    return zipfile.ZipFile(io.BytesIO(current.export_bundle(copy.deepcopy(payload or P))))


def one_file(data, headers=("Date", "Payee", "Amount", "Currency", "Account", "ID", "Notes")):
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(data)
    return {"name": "fresh.csv", "content": base64.b64encode(buf.getvalue().encode()).decode()}


def simple_payload():
    p=copy.deepcopy(P);p["decisions"]={}
    p["incoming"]=one_file([["2026-10-20","Fresh","-12.30","USD","Only account","id-a","original"],["2026-10-21","Income","5.10","USD","Only account","id-b","note"]])
    return p


class AccountHandoffTests(unittest.TestCase):
    def test_frozen_fixture_did_not_change(self):
        hashes=json.loads((FIXTURES/"frozen-hashes.json").read_text())
        for name,sha in hashes.items():
            self.assertEqual(hashlib.sha256((FIXTURES/name).read_bytes()).hexdigest(),sha,name)

    def test_multiscope_has_no_misleading_root_ready_file(self):
        with opened() as z:
            self.assertNotIn("ready_rows.csv",z.namelist())
            self.assertNotIn("normalized_rows.csv",z.namelist())
            manifest=json.loads(z.read("import-manifest.json"))
            self.assertEqual(manifest["mode"],"partitioned")
            self.assertEqual(manifest["group_count"],5)
            self.assertEqual(len(rows(z,"all_selected_rows_REVIEW_ONLY.csv")),9)

    def test_each_manifest_ready_file_has_one_expected_account_currency(self):
        with opened() as z:
            manifest=json.loads(z.read("import-manifest.json"))
            for group in manifest["groups"]:
                data=rows(z,group["ready_file"])
                identities={(" ".join(r["Account"].casefold().split()),r["Currency"]) for r in data}
                self.assertEqual(identities,{(group["account_key"],group["currency"])})
                expected=EXPECTED["scopes"][group["account_key"]+"|"+group["currency"]]
                self.assertEqual(len(data),expected["count"])
                self.assertEqual(Decimal(group["amount_total"]),Decimal(expected["amount"]))

    def test_no_selected_source_line_lost_or_duplicated_across_partitions(self):
        with opened() as z:
            groups=json.loads(z.read("import-manifest.json"))["groups"]
            lines=[line for g in groups for line in g["source_lines"]]
            self.assertEqual(Counter(lines),Counter([2,3,4,5,6,7,8,11,12]))
            self.assertEqual(sum(len(rows(z,g["ready_file"])) for g in groups),9)

    def test_currency_totals_and_repeated_real_purchases_survive(self):
        with opened() as z:
            groups=json.loads(z.read("import-manifest.json"))["groups"]
            totals=defaultdict(Decimal);coffee=0
            for g in groups:
                for row in rows(z,g["normalized_file"]):
                    totals[row["Currency"]]+=Decimal(row["Amount"])
                    coffee+=row["Payee"]=="Two coffees"
            self.assertEqual(dict(totals),{k:Decimal(v) for k,v in EXPECTED["totals"].items()})
            self.assertEqual(coffee,2)

    def test_unselected_and_invalid_stay_only_in_review_queue(self):
        with opened() as z:
            groups=json.loads(z.read("import-manifest.json"))["groups"]
            ids=[r["ID"] for g in groups for r in rows(z,g["ready_file"])]
            self.assertNotIn("bad",ids);self.assertNotIn("omit",ids)
            queue=rows(z,"review.csv")
            self.assertEqual({r["source_line"] for r in queue},{"9","10"})
            audit=json.loads(z.read("audit.json"))
            self.assertEqual(audit["meta"]["decisions"],{"include":9,"exclude":1,"pending":1})

    def test_account_strings_never_control_export_paths(self):
        with opened() as z:
            groups=json.loads(z.read("import-manifest.json"))["groups"]
            for group in groups:
                self.assertRegex(group["ready_file"],r"^accounts/g\d{3}_[a-f0-9]{10}_[A-Z]{3}/ready_rows\.csv$")
            for name in z.namelist():
                self.assertFalse(name.startswith("/"));self.assertNotIn("..",Path(name).parts)
                self.assertNotIn("危险",name);self.assertNotIn("主营",name)

    def test_single_account_csv_bytes_remain_compatible_with_baseline(self):
        p=simple_payload()
        with opened(p) as new:
            self.assertEqual(new.read("ready_rows.csv"),(FIXTURES/"single-ready-v0.1.csv").read_bytes())
            self.assertEqual(new.read("normalized_rows.csv"),(FIXTURES/"single-normalized-v0.1.csv").read_bytes())
            manifest=json.loads(new.read("import-manifest.json"))
            self.assertEqual(manifest["group_count"],1)
            self.assertEqual(manifest["groups"][0]["ready_file"],"ready_rows.csv")
            self.assertFalse(any(n.startswith("accounts/") for n in new.namelist()))

    def test_missing_account_currency_columns_use_explicit_context(self):
        p=simple_payload();headers=["Date","Payee","Amount"]
        p["existing"]=one_file([],headers);p["incoming"]=one_file([["2026-10-20","Fresh","-12.30"]],headers)
        cfg={"mapping":{"date":"Date","payee":"Payee","amount":"Amount"},"amount_mode":"signed","date_format":"iso","decimal_mark":".","default_account":"Explicit account","default_currency":"EUR"}
        p["existing_config"]=cfg;p["incoming_config"]=cfg
        with opened(p) as z:
            group=json.loads(z.read("import-manifest.json"))["groups"][0]
            self.assertEqual((group["account_key"],group["currency"]),("explicit account","EUR"))
            self.assertEqual(rows(z,group["ready_file"])[0]["Amount"],"-12.30")

    def test_excluding_entire_group_removes_its_import_file(self):
        p=copy.deepcopy(P);p["decisions"]["3"]="exclude"
        with opened(p) as z:
            manifest=json.loads(z.read("import-manifest.json"))
            self.assertEqual(manifest["group_count"],4)
            self.assertNotIn("savings",[g["account_key"] for g in manifest["groups"]])

    def test_zero_selected_yields_headers_no_account_files(self):
        p=copy.deepcopy(P);p["decisions"]={str(n):"exclude" for n in range(2,13)}
        with opened(p) as z:
            manifest=json.loads(z.read("import-manifest.json"))
            self.assertEqual(manifest["selected_count"],0)
            self.assertEqual(manifest["group_count"],0)
            self.assertEqual(rows(z,"ready_rows.csv"),[])
            self.assertFalse(any(n.startswith("accounts/") for n in z.namelist()))

    def test_case_alias_is_one_group_but_punctuation_names_are_not_collapsed(self):
        p=simple_payload();p["incoming"]=one_file([["2026-10-20","A","-1","USD","Main","a",""],["2026-10-21","B","-2","USD"," main ","b",""],["2026-10-22","C","-3","USD","X/Y","c",""],["2026-10-23","D","-4","USD","X:Y","d",""]])
        with opened(p) as z:
            groups=json.loads(z.read("import-manifest.json"))["groups"]
            self.assertEqual(len(groups),3)
            main=next(g for g in groups if g["account_key"]=="main")
            self.assertEqual(main["row_count"],2)
            self.assertEqual(set(main["account_aliases"]),{"Main","main"})
            self.assertEqual(len({g["ready_file"] for g in groups}),3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
