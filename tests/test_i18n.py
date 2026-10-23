"""Bilingual boundaries must not change money, identity or users' own text."""
import concurrent.futures
import copy
import http.client
import io
import json
import re
import string
import subprocess
import sys
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from importguard import InputError, analyze, export_bundle
from localization import CATALOG, error_payload, format_message, normalize_language, request_language
from server import Handler, demo
from test_importguard import file, payload, tx

HAN = re.compile(r'[\u3400-\u9fff]')
ROOT = Path(__file__).resolve().parents[1]


class BilingualTests(unittest.TestCase):
    def test_catalog_parity_and_format_parameters(self):
        self.assertEqual(set(CATALOG['en']), set(CATALOG['zh-CN']))
        formatter = string.Formatter()
        for code, english in CATALOG['en'].items():
            chinese = CATALOG['zh-CN'][code]
            self.assertFalse(HAN.search(english), code)
            fields = lambda text: {field for _, field, _, _ in formatter.parse(text) if field is not None}
            self.assertEqual(fields(english), fields(chinese), code)

    def test_ui_catalog_covers_markup_and_accessible_labels(self):
        catalog = json.loads((ROOT / 'static/i18n.json').read_text(encoding='utf-8'))
        self.assertEqual(set(catalog['en']), set(catalog['zh-CN']))
        markup = (ROOT / 'static/index.html').read_text(encoding='utf-8')
        keys = set(re.findall(r'data-i18n(?:-aria|-placeholder)?="([^"]+)"', markup))
        keys.update('field_' + field for field in ('date', 'payee', 'amount', 'debit', 'credit', 'id', 'account', 'currency', 'notes'))
        self.assertTrue(keys <= set(catalog['en']))
        formatter = string.Formatter()
        for code, english in catalog['en'].items():
            self.assertFalse(HAN.search(english), code)
            fields = lambda text: {field for _, field, _, _ in formatter.parse(text) if field is not None}
            self.assertEqual(fields(english), fields(catalog['zh-CN'][code]), code)

    def test_default_and_explicit_unknown_language(self):
        p = demo()
        self.assertEqual(analyze(p)['meta']['language'], 'zh-CN')
        self.assertEqual(analyze(p | {'language': 'not-supported'})['meta']['language'], 'en')
        self.assertEqual(analyze(p | {'language': None})['meta']['language'], 'en')
        self.assertEqual(request_language(None, 'en-US;q=0.4,zh-CN;q=0.9'), 'zh-CN')
        self.assertEqual(request_language({'language': 'en'}, 'zh-CN'), 'en')
        self.assertEqual(normalize_language('en-US'), 'en')

    def test_both_languages_preserve_decisions_numbers_and_raw_values(self):
        p = payload([tx(payee='咖啡店', account='储蓄账户', notes='原始备注')],
                    [tx(payee='咖啡店', account='储蓄账户', notes='不要翻译'), tx(day='bad')])
        original = copy.deepcopy(p)
        zh, en = [analyze(p | {'language': lang}) for lang in ('zh-CN', 'en')]
        self.assertEqual(zh['meta']['counts'], en['meta']['counts'])
        self.assertEqual(zh['meta']['totals_by_currency'], en['meta']['totals_by_currency'])
        for left, right in zip(zh['rows'], en['rows']):
            for key in set(left) - {'reason', 'error'}:
                self.assertEqual(left[key], right[key], key)
            self.assertFalse(HAN.search(right['reason']))
            self.assertFalse(HAN.search(right['error']))
        self.assertEqual(en['rows'][0]['payee'], '咖啡店')
        self.assertEqual(en['rows'][0]['notes'], '不要翻译')
        self.assertEqual(en['rows'][0]['raw']['Account'], '储蓄账户')
        self.assertEqual(p, original)

    def test_date_window_and_id_warning_remain_retranslatable(self):
        p = payload([tx(id='old-id')], [tx(day='2026-10-12', id='new-id')], trusted=True, window=2)
        row = analyze(p | {'language': 'en'})['rows'][0]
        self.assertEqual(row['reason_code'], 'reason.candidates_date_window')
        self.assertEqual(row['reason_params']['window'], 2)
        self.assertEqual(row['reason_params']['id_warning']['code'], 'reason.id_differs')
        self.assertIn('within 2 days', row['reason'])
        self.assertIn('IDs differ', row['reason'])
        switched = format_message(row['reason_code'], row['reason_params'], 'zh-CN')
        self.assertIn('不超过2天', switched)
        self.assertIn('ID不同', switched)

    def test_invalid_existing_rows_use_nested_error_descriptors(self):
        p = payload([tx(day='bad'), tx(amount='oops')], [])
        with self.assertRaises(InputError) as failure:
            analyze(p | {'language': 'en'})
        exc = failure.exception
        self.assertIn('已有账本有2行无效', str(exc))
        self.assertEqual(exc.code, 'error.existing_invalid')
        self.assertEqual(len(exc.params['details']), 2)
        en = error_payload(exc, 'en')
        self.assertFalse(HAN.search(en['error']))
        self.assertIn('Line 2:', en['error'])
        self.assertIn('Line 3:', en['error'])
        self.assertIn('金额', error_payload(exc, 'zh-CN')['error'])
        self.assertEqual(en['error_params']['details'][0]['params']['detail']['code'], 'date.pattern')

    def test_bilingual_exports_keep_import_csv_bytes_identical(self):
        p = demo() | {'decisions': {'3': 'exclude', '6': 'include', '7': 'include'}}
        archives = [zipfile.ZipFile(io.BytesIO(export_bundle(p | {'language': lang}))) for lang in ('zh-CN', 'en')]
        try:
            zh, en = archives
            self.assertEqual(set(zh.namelist()), set(en.namelist()))
            for name in zh.namelist():
                if name.endswith('.csv') and name != 'review.csv':
                    self.assertEqual(zh.read(name), en.read(name), name)
            self.assertFalse(HAN.search(en.read('README.txt').decode()))
            self.assertIn('待定', zh.read('README.txt').decode())
            self.assertNotEqual(zh.read('review.csv'), en.read('review.csv'))
            for name in ['audit.json', 'import-manifest.json']:
                data = json.loads(en.read(name))
                if name == 'audit.json':
                    self.assertEqual(data['meta']['language'], 'en')
                    self.assertFalse(HAN.search(data['meta']['scope_warning']))
                    self.assertTrue(all(not HAN.search(r['reason']) for r in data['rows']))
                else:
                    self.assertEqual(data['language'], 'en')
                    self.assertFalse(HAN.search(data['warning']))
        finally:
            for archive in archives:
                archive.close()

    def test_cli_help_default_is_english_and_lists_language_option(self):
        for filename in ('importguard.py', 'server.py'):
            result = subprocess.run([sys.executable, str(ROOT / filename), '--help'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn('--language', result.stdout)
            self.assertFalse(HAN.search(result.stdout))


class BilingualHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def request(self, path, payload=None, language=None, method='POST', raw=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        headers = {'Content-Type': 'application/json'}
        if language:
            headers['Accept-Language'] = language
        body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        status, body = response.status, response.read()
        conn.close()
        return status, body

    def test_messages_route_and_header_language_for_inspect_errors(self):
        status, body = self.request('/messages.json', method='GET')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), CATALOG)
        for language in ('en-US', 'zh-CN'):
            status, body = self.request('/api/inspect', {'file': {'content': 'bad!'}}, language)
            result = json.loads(body)
            self.assertEqual(status, 400)
            self.assertEqual(result['error_code'], 'file.invalid_base64')
            self.assertEqual(bool(HAN.search(result['error'])), language == 'zh-CN')

    def test_concurrent_requests_do_not_share_language_or_decisions(self):
        def run(index):
            language = 'en' if index % 2 else 'zh-CN'
            p = demo() | {'language': language, 'decisions': {'3': 'include' if index % 2 else 'exclude'}}
            status, body = self.request('/api/export', p, 'zh-CN' if language == 'en' else 'en')
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                audit = json.loads(archive.read('audit.json'))
                return status, audit['meta']['language'], audit['meta']['decisions']['include'], archive.read('README.txt').decode()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(12)))
        for index, (status, language, included, readme) in enumerate(results):
            self.assertEqual(status, 200)
            self.assertEqual(language, 'en' if index % 2 else 'zh-CN')
            self.assertEqual(included, 6 if index % 2 else 5)
            self.assertEqual(bool(HAN.search(readme)), not bool(index % 2))

    def test_dynamic_existing_error_and_malformed_http_input_use_english(self):
        p = payload([tx(day='invalid'), tx(amount='invalid')], []) | {'language': 'en'}
        status, body = self.request('/api/analyze', p)
        self.assertEqual(status, 400)
        result = json.loads(body)
        self.assertEqual(result['error_code'], 'error.existing_invalid')
        self.assertFalse(HAN.search(result['error']))
        status, body = self.request('/api/analyze', language='en', raw=b'{bad json')
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)['error_code'], 'http.invalid')
        self.assertFalse(HAN.search(json.loads(body)['error']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
