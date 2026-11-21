"""Server-side one-time approval and authenticated dispatch for Blender plans.

No browser receives the execution key, authorization token, or full plan payload.
A proposal is immutable after preparation and expires after five minutes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .blender_protocol import validate_plan, summarize_plan

KEY_PATH = Path("/etc/jarvis-baby/windows-blender-execution.key")
BRIDGE_URL = "http://172.30.50.1:8768/v1/execute"
PROPOSAL_TTL = 300
MAX_PENDING = 32
MAX_RESPONSE = 1024 * 1024


class ProposalUnavailable(Exception):
    pass


class BridgeExecutionError(Exception):
    pass


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def canonical_plan_bytes(plan: dict) -> bytes:
    valid = validate_plan(plan)
    return json.dumps(
        valid, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


class BlenderApprovalManager:
    """Prepare immutable proposals; dispatch only after one browser approval."""

    def __init__(
        self,
        key_path=KEY_PATH,
        bridge_url=BRIDGE_URL,
        clock=time.time,
        opener=urllib.request.urlopen,
    ):
        self.key_path = Path(key_path)
        self.bridge_url = bridge_url
        self.clock = clock
        self.opener = opener
        self._lock = threading.RLock()
        self._pending = {}

    def _prune_locked(self, now):
        expired = [
            proposal_id for proposal_id, item in self._pending.items()
            if item["expires_at"] <= now
        ]
        for proposal_id in expired:
            self._pending.pop(proposal_id, None)

    def prepare(self, plan):
        raw = canonical_plan_bytes(plan)
        valid = json.loads(raw.decode("utf-8"))
        preview = summarize_plan(valid)
        now = self.clock()
        proposal_id = str(uuid.uuid4())
        authorization_token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw).hexdigest()

        with self._lock:
            self._prune_locked(now)
            if len(self._pending) >= MAX_PENDING:
                raise ValueError("Too many pending Blender proposals")
            self._pending[proposal_id] = {
                "plan_bytes": raw,
                "digest": digest,
                "expires_at": now + PROPOSAL_TTL,
                "authorization_token": authorization_token,
                "project_slug": valid["project_slug"],
                "plan_id": valid["plan_id"],
                "preview": preview,
            }

        return {
            "type": "blender",
            "proposal_id": proposal_id,
            "project_slug": valid["project_slug"],
            "plan_digest": digest,
            "step_count": preview["step_count"],
            "operations": preview["operations"],
            "execution_model": preview["execution_model"],
            "expires_in": PROPOSAL_TTL,
            "status": "awaiting your approval",
        }

    def _consume(self, proposal_id):
        if type(proposal_id) is not str or len(proposal_id) != 36:
            raise ProposalUnavailable("proposal_expired_or_unavailable")
        try:
            if str(uuid.UUID(proposal_id)) != proposal_id:
                raise ValueError("noncanonical UUID")
        except (ValueError, AttributeError):
            raise ProposalUnavailable("proposal_expired_or_unavailable") from None
        now = self.clock()
        with self._lock:
            self._prune_locked(now)
            item = self._pending.pop(proposal_id, None)
        if item is None:
            raise ProposalUnavailable("proposal_expired_or_already_used")
        token = item.get("authorization_token")
        if type(token) is not str or len(token) < 40:
            raise ProposalUnavailable("authorization_token_unavailable")
        # The capability is consumed atomically with the proposal and is never
        # exposed to the browser or reused as a bridge authentication secret.
        item["authorization_token"] = None
        return item

    def reject(self, proposal_id):
        self._consume(proposal_id)
        return {"ok": True, "rejected": True, "proposal_id": proposal_id}

    def _load_key(self):
        fd = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(self.key_path), flags)
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise BridgeExecutionError("execution_key_not_regular_file")
            permissions = stat.S_IMODE(metadata.st_mode)
            # The installed key is root-owned and group-readable by jarvis-baby
            # (0640). Never accept group-writable or any world permissions.
            if permissions & 0o027:
                raise BridgeExecutionError("execution_key_permissions_invalid")
            with os.fdopen(fd, "r", encoding="ascii") as stream:
                fd = None
                raw = stream.read(1024).strip()
        except BridgeExecutionError:
            raise
        except OSError as exc:
            raise BridgeExecutionError("execution_key_unavailable") from exc
        finally:
            if fd is not None:
                os.close(fd)
        if len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
            raise BridgeExecutionError("execution_key_invalid")
        return bytes.fromhex(raw)

    def _dispatch(self, item):
        raw = item["plan_bytes"]
        digest = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(digest, item["digest"]):
            raise BridgeExecutionError("proposal_digest_mismatch")

        try:
            plan = json.loads(raw.decode("utf-8"))
            valid = validate_plan(plan)
            if valid["plan_id"] != item["plan_id"] or valid["project_slug"] != item["project_slug"]:
                raise ValueError("proposal_identity_mismatch")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise BridgeExecutionError("stored_plan_invalid") from exc

        key = self._load_key()
        timestamp = str(int(self.clock()))
        nonce = secrets.token_hex(32)
        body = json.dumps(
            {"plan": valid}, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        signature = hmac.new(
            key, timestamp.encode("ascii") + b"\n" +
            nonce.encode("ascii") + b"\n" + body, hashlib.sha256
        ).hexdigest()
        request = urllib.request.Request(
            self.bridge_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Jarvis-Timestamp": timestamp,
                "X-Jarvis-Nonce": nonce,
                "X-Jarvis-Signature": signature,
            },
        )
        try:
            with self.opener(request, timeout=200) as response:
                status = response.status
                body = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read(MAX_RESPONSE + 1)
        except Exception as exc:
            raise BridgeExecutionError("bridge_unreachable_or_timeout") from exc

        if len(body) > MAX_RESPONSE:
            raise BridgeExecutionError("bridge_response_too_large")
        try:
            result = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except Exception as exc:
            raise BridgeExecutionError("bridge_response_invalid") from exc

        if status != 200 or type(result) is not dict or result.get("ok") is not True:
            code = result.get("error", "bridge_rejected") if type(result) is dict else "bridge_rejected"
            # Never reflect arbitrary bridge response bodies to the browser.
            if type(code) is not str or len(code) > 120:
                code = "bridge_rejected"
            raise BridgeExecutionError(code)

        if result.get("operation") != "execute_plan" or result.get("project_slug") != item["project_slug"]:
            raise BridgeExecutionError("bridge_result_identity_mismatch")
        execution = result.get("result")
        expected_steps = [
            {"step_id": step["step_id"], "operation": step["operation"]}
            for step in valid["steps"]
        ]
        if (
            type(execution) is not dict
            or execution.get("ok") is not True
            or execution.get("plan_id") != item["plan_id"]
            or execution.get("project_slug") != item["project_slug"]
            or execution.get("executed_steps") != expected_steps
            or type(execution.get("object_count")) is not int
            or not 0 <= execution.get("object_count", -1) <= 100000
            or type(execution.get("collection_count")) is not int
            or not 0 <= execution.get("collection_count", -1) <= 100000
            or type(execution.get("material_count")) is not int
            or not 0 <= execution.get("material_count", -1) <= 100000
        ):
            raise BridgeExecutionError("execution_result_verification_failed")

        expected_path = (
            r"C:\AI\JARVIS\Testarossa\Projects" +
            "\\" + item["project_slug"] + "\\" + item["project_slug"] + ".blend"
        )
        if result.get("project_path") != expected_path:
            raise BridgeExecutionError("execution_project_path_mismatch")

        return {
            "ok": True,
            "proposal_id": None,
            "plan_digest": item["digest"],
            "project_slug": item["project_slug"],
            "project_path": expected_path,
            "execution": execution,
            "verification": {
                "plan_id_matches": True,
                "project_slug_matches": True,
                "executed_steps_match": True,
                "project_path_matches": True,
            },
        }

    def approve(self, proposal_id):
        # Consumed before network I/O: a timeout or error cannot cause a replay.
        item = self._consume(proposal_id)
        result = self._dispatch(item)
        result["proposal_id"] = proposal_id
        return result

    def pending_count(self):
        with self._lock:
            self._prune_locked(self.clock())
            return len(self._pending)
