import unittest
import uuid
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server
from testarossa_intelligence.registry import Registry


def sample_plan():
    return {
        "schema_version": 1,
        "plan_id": str(uuid.uuid4()),
        "project_slug": "testarossa_server_test",
        "steps": [
            {"step_id": "s1", "operation": "create_collection",
             "args": {"name": "Testarossa"}},
            {"step_id": "s2", "operation": "save_project", "args": {}},
        ],
    }


class FakeManager:
    def __init__(self):
        self.received = None
        self.approved = []
        self.rejected = []

    def approve(self, proposal_id):
        self.approved.append(proposal_id)
        return {"ok": True, "project_path": "verified-path"}

    def reject(self, proposal_id):
        self.rejected.append(proposal_id)
        return {"ok": True, "rejected": True}

    def prepare(self, plan):
        self.received = plan
        return {
            "type": "blender", "proposal_id": str(uuid.uuid4()),
            "project_slug": plan["project_slug"], "plan_digest": "a" * 64,
            "step_count": len(plan["steps"]), "operations": [],
            "execution_model": "typed_allowlist_only", "expires_in": 300,
            "status": "awaiting your approval",
        }


class BlenderServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.original = server._BLENDER_APPROVAL_MANAGER
        self.fake = FakeManager()
        server._BLENDER_APPROVAL_MANAGER = self.fake

    def tearDown(self):
        server._BLENDER_APPROVAL_MANAGER = self.original

    def test_tool_is_registered(self):
        names = {
            item["function"]["name"] for item in server.TESTAROSSA_TOOLS
        }
        self.assertIn("testarossa_propose_blender_plan", names)

    def test_registry_marks_blender_proposal_as_approval_required_mutation(self):
        registry = Registry()
        item = registry.get("testarossa_propose_blender_plan")
        self.assertEqual(item.capability, "blender.write")
        self.assertEqual(item.risk, "mutation")
        self.assertTrue(item.approval_required)

    def test_dispatch_prepares_only_a_proposal(self):
        plan = sample_plan()
        result = server.testarossa_dispatch(
            "testarossa_propose_blender_plan", {"plan": plan}
        )
        received = self.fake.received
        self.assertEqual(received["project_slug"], plan["project_slug"])
        self.assertEqual(received["steps"], plan["steps"])
        self.assertNotEqual(received["plan_id"], plan["plan_id"])
        self.assertEqual(str(uuid.UUID(received["plan_id"])), received["plan_id"])
        self.assertEqual(result["type"], "blender")
        self.assertNotIn("authorization_token", result)
        self.assertNotIn("plan", result)

    def test_dispatch_rejects_extra_arguments(self):
        with self.assertRaises(ValueError):
            server.testarossa_dispatch(
                "testarossa_propose_blender_plan",
                {"plan": sample_plan(), "execute_now": True},
            )

    def test_chat_returns_blender_proposal_for_approval_card(self):
        plan = sample_plan()
        original_ask = server.ask
        calls = {"count": 0}
        def fake_ask(path, payload):
            calls["count"] += 1
            if calls["count"] == 1:
                return {"message": {"tool_calls": [{
                    "function": {
                        "name": "testarossa_propose_blender_plan",
                        "arguments": {"plan": plan},
                    }
                }]}}
            return {"message": {"content": "The Blender plan is awaiting your approval."}}
        server.ask = fake_ask
        try:
            answer, proposals = server.chat_with_development_tools([
                {"role": "user", "content": "Prepare a test scene."}
            ])
        finally:
            server.ask = original_ask
        self.assertIn("awaiting", answer)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["type"], "blender")



class BlenderApprovalRouteTests(unittest.TestCase):
    def setUp(self):
        self.original = server._BLENDER_APPROVAL_MANAGER
        self.manager = FakeManager()
        server._BLENDER_APPROVAL_MANAGER = self.manager
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.App)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.token = "test-browser-session-token"
        with server.STATE_LOCK:
            server.SESSIONS[self.token] = time.time() + 600

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        with server.STATE_LOCK:
            server.SESSIONS.pop(self.token, None)
        server._BLENDER_APPROVAL_MANAGER = self.original

    def post(self, route, origin=None, cookie=True):
        body = json.dumps({"proposal_id": str(uuid.uuid4())}).encode()
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        if cookie:
            headers["Cookie"] = "baby_approval_session=" + self.token
        request = urllib.request.Request(
            self.url + route, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_approval_requires_browser_session(self):
        status, body = self.post(
            "/api/blender/approve",
            origin="http://172.30.50.10:8765",
            cookie=False,
        )
        self.assertEqual(status, 403)
        self.assertFalse(self.manager.approved)

    def test_approval_requires_exact_origin(self):
        status, body = self.post(
            "/api/blender/approve",
            origin="http://evil.example",
        )
        self.assertEqual(status, 403)
        self.assertFalse(self.manager.approved)

    def test_approval_route_dispatches_only_after_browser_approval(self):
        status, body = self.post(
            "/api/blender/approve",
            origin="http://172.30.50.10:8765",
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(self.manager.approved), 1)

    def test_reject_route_consumes_proposal(self):
        status, body = self.post(
            "/api/blender/reject",
            origin="http://172.30.50.10:8765",
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["rejected"])
        self.assertEqual(len(self.manager.rejected), 1)

    def test_malformed_approval_body_is_rejected(self):
        request = urllib.request.Request(
            self.url + "/api/blender/approve",
            data=b"[]",
            headers={
                "Content-Type": "application/json",
                "Origin": "http://172.30.50.10:8765",
                "Cookie": "baby_approval_session=" + self.token,
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3)
            self.fail("malformed body should not be accepted")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
