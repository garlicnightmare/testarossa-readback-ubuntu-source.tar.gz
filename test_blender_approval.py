import hashlib
import hmac
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from testarossa_intelligence.blender_approval import (
    BlenderApprovalManager, ProposalUnavailable, BridgeExecutionError,
    canonical_plan_bytes, PROPOSAL_TTL,
)


def sample_plan():
    return {
        "schema_version": 1,
        "plan_id": str(uuid.uuid4()),
        "project_slug": "testarossa_approval_test",
        "steps": [
            {"step_id": "s1", "operation": "create_collection",
             "args": {"name": "Testarossa"}},
            {"step_id": "s2", "operation": "add_mesh",
             "args": {"name": "Torso", "primitive": "UV_SPHERE",
                      "collection": "Testarossa", "location": [0, 0, 1],
                      "rotation": [0, 0, 0], "scale": [0.5, 0.4, 0.7]}},
            {"step_id": "s3", "operation": "save_project", "args": {}},
        ],
    }


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.payload[:limit]


class BlenderApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key_path = Path(self.temp.name) / "key"
        self.key = bytes(range(32))
        self.key_path.write_text(self.key.hex(), encoding="ascii")
        os.chmod(self.key_path, 0o640)
        self.now = 1000000.0
        self.last_request = None
        self.manager = BlenderApprovalManager(
            key_path=self.key_path,
            clock=lambda: self.now,
            opener=self.fake_opener,
        )

    def tearDown(self):
        self.temp.cleanup()

    def fake_opener(self, request, timeout=0):
        self.last_request = request
        body = request.data
        ts = request.get_header("X-jarvis-timestamp")
        nonce = request.get_header("X-jarvis-nonce")
        signature = request.get_header("X-jarvis-signature")
        expected = hmac.new(
            self.key,
            ts.encode() + b"\n" + nonce.encode() + b"\n" + body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise AssertionError("request HMAC did not cover exact body")
        plan = json.loads(body)["plan"]
        steps = [
            {"step_id": s["step_id"], "operation": s["operation"]}
            for s in plan["steps"]
        ]
        slug = plan["project_slug"]
        result = {
            "ok": True,
            "operation": "execute_plan",
            "project_slug": slug,
            "project_path": (
                r"C:\AI\JARVIS\Testarossa\Projects" + "\\" + slug + "\\" + slug + ".blend"
            ),
            "preview": "{}",
            "result": {
                "ok": True, "plan_id": plan["plan_id"], "project_slug": slug,
                "executed_steps": steps, "object_count": 1,
                "collection_count": 2, "material_count": 0,
                "blender_version": "5.3.0",
            },
        }
        return FakeResponse(result)

    def test_prepare_returns_digest_and_no_secret_or_plan(self):
        proposal = self.manager.prepare(sample_plan())
        self.assertEqual(len(proposal["plan_digest"]), 64)
        self.assertNotIn("authorization_token", proposal)
        self.assertNotIn("plan", proposal)
        self.assertEqual(proposal["expires_in"], PROPOSAL_TTL)

    def test_digest_is_canonical(self):
        plan = sample_plan()
        expected = hashlib.sha256(canonical_plan_bytes(plan)).hexdigest()
        proposal = self.manager.prepare(plan)
        self.assertEqual(proposal["plan_digest"], expected)

    def test_reject_consumes_once(self):
        proposal = self.manager.prepare(sample_plan())
        self.assertTrue(self.manager.reject(proposal["proposal_id"])["rejected"])
        with self.assertRaises(ProposalUnavailable):
            self.manager.reject(proposal["proposal_id"])

    def test_expired_proposal_rejected(self):
        proposal = self.manager.prepare(sample_plan())
        self.now += PROPOSAL_TTL + 1
        with self.assertRaises(ProposalUnavailable):
            self.manager.approve(proposal["proposal_id"])

    def test_successful_approval_executes_and_verifies(self):
        proposal = self.manager.prepare(sample_plan())
        result = self.manager.approve(proposal["proposal_id"])
        self.assertTrue(result["verification"]["executed_steps_match"])
        self.assertEqual(result["plan_digest"], proposal["plan_digest"])
        self.assertIn("x-jarvis-signature", [k.lower() for k, _ in self.last_request.header_items()])

    def test_approval_is_one_time(self):
        proposal = self.manager.prepare(sample_plan())
        self.manager.approve(proposal["proposal_id"])
        with self.assertRaises(ProposalUnavailable):
            self.manager.approve(proposal["proposal_id"])

    def test_tampered_stored_plan_rejected(self):
        proposal = self.manager.prepare(sample_plan())
        self.manager._pending[proposal["proposal_id"]]["plan_bytes"] += b" "
        with self.assertRaises(BridgeExecutionError):
            self.manager.approve(proposal["proposal_id"])

    def test_bad_key_rejected_and_consumed(self):
        proposal = self.manager.prepare(sample_plan())
        self.key_path.write_text("not-a-key", encoding="ascii")
        os.chmod(self.key_path, 0o640)
        with self.assertRaises(BridgeExecutionError):
            self.manager.approve(proposal["proposal_id"])
        with self.assertRaises(ProposalUnavailable):
            self.manager.approve(proposal["proposal_id"])

    def test_world_readable_key_rejected(self):
        proposal = self.manager.prepare(sample_plan())
        os.chmod(self.key_path, 0o644)
        with self.assertRaises(BridgeExecutionError):
            self.manager.approve(proposal["proposal_id"])

    def test_symlink_key_rejected(self):
        proposal = self.manager.prepare(sample_plan())
        link = Path(self.temp.name) / "key-link"
        link.symlink_to(self.key_path)
        self.manager.key_path = link
        with self.assertRaises(BridgeExecutionError):
            self.manager.approve(proposal["proposal_id"])

    def test_noncanonical_proposal_id_rejected(self):
        with self.assertRaises(ProposalUnavailable):
            self.manager.approve("A" * 36)

    def test_bridge_error_is_not_retried(self):
        proposal = self.manager.prepare(sample_plan())
        self.manager.opener = lambda *a, **k: FakeResponse(
            {"ok": False, "error": "execution_disabled_until_approval_workflow_is_installed"},
            status=400,
        )
        with self.assertRaises(BridgeExecutionError):
            self.manager.approve(proposal["proposal_id"])
        with self.assertRaises(ProposalUnavailable):
            self.manager.approve(proposal["proposal_id"])

    def test_wrong_project_path_rejected(self):
        proposal = self.manager.prepare(sample_plan())
        original = self.manager.opener
        def wrong_path(request, timeout=0):
            response = original(request, timeout)
            obj = json.loads(response.payload)
            obj["project_path"] = r"C:\outside\evil.blend"
            return FakeResponse(obj)
        self.manager.opener = wrong_path
        with self.assertRaises(BridgeExecutionError):
            self.manager.approve(proposal["proposal_id"])

    def test_duplicate_plan_step_ids_rejected(self):
        plan = sample_plan()
        plan["steps"][1]["step_id"] = "s1"
        with self.assertRaises(ValueError):
            self.manager.prepare(plan)

    def test_pending_capacity_is_bounded(self):
        for _ in range(32):
            self.manager.prepare(sample_plan())
        with self.assertRaises(ValueError):
            self.manager.prepare(sample_plan())


if __name__ == "__main__":
    unittest.main()
