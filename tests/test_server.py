import concurrent.futures
import copy
import http.client
import io
import json
import sys
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Handler, MAX_REQUEST, demo


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def post(self, path, payload, headers=None):
        return self.request("POST", path, json.dumps(payload).encode(), {"Content-Type": "application/json", **(headers or {})})

    def test_static_page_and_security_headers(self):
        status, headers, content = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"ImportGuard", content)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])

    def test_health_and_synthetic_demo(self):
        for path in ["/api/health", "/api/demo"]:
            status, _, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertIsInstance(json.loads(body), dict)

    def test_analysis_and_zip_roundtrip(self):
        data=demo()
        status, _, body=self.post("/api/analyze",data)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)["meta"]["counts"]["review"],4)
        data["decisions"]={"3":"exclude","6":"include","7":"include"}
        status,headers,body=self.post("/api/export",data)
        self.assertEqual(status,200);self.assertEqual(headers["Content-Type"],"application/zip")
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(json.loads(z.read("audit.json"))["meta"]["decisions"],{"include":7,"exclude":2,"pending":2})

    def test_origin_rejected_and_same_origin_allowed(self):
        data=demo()
        status,_,_=self.post("/api/analyze",data,{"Origin":"https://attacker.invalid"})
        self.assertEqual(status,403)
        status,_,_=self.post("/api/analyze",data,{"Origin":f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status,200)

    def test_unexpected_host_rejected(self):
        status,_,_=self.request("GET","/",headers={"Host":f"attacker.invalid:{self.port}"})
        self.assertEqual(status,403)

    def test_source_files_and_path_traversal_not_served(self):
        for path in ["/server.py","/examples/existing.csv","/../importguard.py","/%2e%2e/server.py"]:
            self.assertEqual(self.request("GET",path)[0],404)

    def test_invalid_json_content_type_length_and_route(self):
        self.assertEqual(self.request("POST","/api/analyze",b"{broken",{"Content-Type":"application/json"})[0],400)
        self.assertEqual(self.request("POST","/api/analyze",b"{}",{"Content-Type":"text/plain"})[0],415)
        self.assertEqual(self.request("POST","/api/analyze",b"",{"Content-Type":"application/json","Content-Length":str(MAX_REQUEST+1)})[0],413)
        self.assertEqual(self.post("/does-not-exist",{})[0],404)

    def test_inspect_errors_and_recovery(self):
        status,_,body=self.post("/api/inspect",{"file":{"content":"bad!"}})
        self.assertEqual(status,400);self.assertIn("error",json.loads(body))
        self.assertEqual(self.post("/api/inspect",{"file":demo()["existing"]})[0],200)

    def test_shared_config_has_no_mutation_and_concurrent_requests_isolate_decisions(self):
        data=demo();original=copy.deepcopy(data)
        def run(index):
            p=copy.deepcopy(data);p["decisions"]={"3":"include" if index%2 else "exclude"}
            status,_,body=self.post("/api/export",p)
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                return status,json.loads(z.read("audit.json"))["meta"]["decisions"]["include"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(run,range(8)))
        self.assertEqual([r[1] for r in results],[5,6,5,6,5,6,5,6])
        self.assertTrue(all(r[0]==200 for r in results));self.assertEqual(data,original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
