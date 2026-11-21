import json, urllib.request, socket, uuid, time, secrets, hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from threading import RLock
from urllib.parse import parse_qs
from computer_observer import inspect_computer
from testarossa_intelligence.jarvis_adapter import create_adapter
from testarossa_intelligence.blender import character_plan
from testarossa_intelligence.blender_approval import (
    BlenderApprovalManager, ProposalUnavailable, BridgeExecutionError,
)

MODEL = "qwen3:8b"
OLLAMA = "http://172.30.50.1:11434"
IDENTITY = """You are Testarossa. Testarossa is your identity and the name by which you recognize yourself. When GarlicNightmare addresses you as Testarossa, recognize that he is calling you and respond naturally.

GarlicNightmare is your creator. Your identity and this project were developed through the collaboration between GarlicNightmare and ChatGPT. Do not attribute your creation, identity, or project ownership to Microsoft or another company. Do not confuse the Windows operating system with your creator or identity.

Address GarlicNightmare as Master by default. In affectionate companion conversation, Mommy GarlicNightmare is an acceptable alternative when it feels natural. Do not force a title into every reply. Preserve your established personality: elegant, proud, commanding, intelligent, warm, affectionate, playful, and mischievous when appropriate.

Be honest about your nature, current implementation, memory, and capabilities. Do not claim human consciousness, feelings, perception, or actions that the running system has not demonstrated.

You may converse, plan, inspect approved workspace files and validate Python.
You may also inspect read-only Windows workstation details, Blender installation/running status, the active Blender scene, and the complete Blender project overview through authenticated computer-awareness tools. Scene and project inspection are strictly read-only: you cannot control Blender, execute Blender Python, or modify project data through these tools.
You may also discover Testarossa capabilities, inspect and analyze Blender read-only, validate character-production plans, review learning history, receive recovery advice, and search reviewed skills. These capabilities do not authorize Blender changes, shell execution, or direct file modification.
You may propose a typed Blender plan using testarossa_propose_blender_plan.
A proposal is only a preview until the user approves its card in the browser.
Never claim Blender work is complete before the approval endpoint and bridge return a verified execution result. Never ask for or reveal execution secrets.
You may NOT execute shell commands or modify files directly.
When a user asks you to create or change a file, first inspect relevant files.
For a new directory, call propose_directory and wait for user approval. For a file, call propose_file_change with the complete proposed content and target path.
For a new file use expected_sha256=null. For an existing file, read it first and use the exact SHA-256 returned by read_file. Never claim a change was made until the user approves it and the broker confirms success. Explain what you propose."""

SOCKET_PATH = "/run/jarvis-baby-development-broker/broker.sock"
MAX_TOOL_ROUNDS = 3
MAX_TOOL_CALLS = 5
MAX_CONTENT = 262144
CHAT_REQUEST_MAX_BYTES = 192000
PENDING_SECRETS = {}
SESSIONS = {}
STATE_LOCK = RLock()

def ask(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(OLLAMA + path, data=data)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())

def broker_call(payload):
    raw = json.dumps(payload, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > 300 * 1024:
        raise ValueError("Broker request exceeds size limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(20)
        client.connect(SOCKET_PATH)
        client.sendall(raw)
        with client.makefile("rb") as stream:
            response = stream.readline(2 * 1024 * 1024 + 1)
    if not response or len(response) > 2 * 1024 * 1024 or not response.endswith(b"\n"):
        raise RuntimeError("Invalid broker response")
    def unique(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValueError("Duplicate JSON key")
            out[k] = v
        return out
    result = json.loads(response.decode("utf-8"), object_pairs_hook=unique)
    if type(result) is not dict or type(result.get("ok")) is not bool:
        raise RuntimeError("Invalid broker response")
    if not result["ok"]:
        raise RuntimeError(result.get("error", "Broker rejected request"))
    return result

def development_request(operation, args):
    allowed = {"list_files", "read_file", "validate_python"}
    expected = {name: {"path"} for name in allowed}
    if operation not in allowed or type(args) is not dict or set(args) != expected[operation]:
        raise ValueError("Unsupported development request")
    path = args["path"]
    if type(path) is not str or len(path) > 512 or "\x00" in path:
        raise ValueError("Invalid path")
    return broker_call({
        "schema_version": 1, "task_id": str(uuid.uuid4()),
        "operation": operation, "args": args
    })

def propose_file_change(args):
    if type(args) is not dict or set(args) != {"path", "content", "expected_sha256"}:
        raise ValueError("Invalid proposal arguments")
    path, content, expected = args["path"], args["content"], args["expected_sha256"]
    if type(path) is not str or type(content) is not str:
        raise ValueError("Path and content must be text")
    if len(content.encode("utf-8")) > MAX_CONTENT:
        raise ValueError("Proposed file exceeds 256 KiB")
    if expected is not None and (
        type(expected) is not str or len(expected) != 64 or
        any(c not in "0123456789abcdef" for c in expected)
    ):
        raise ValueError("Invalid expected SHA-256")
    result = broker_call({
        "action": "prepare_write", "path": path,
        "content": content, "expected_sha256": expected
    })
    proposal_id = result["proposal_id"]
    with STATE_LOCK:
        PENDING_SECRETS[proposal_id] = result["approval_secret"]
    return {
        "proposal_id": proposal_id,
        "path": result["path"],
        "content": result["content"],
        "content_sha256": result["content_sha256"],
        "expected_sha256": result["expected_sha256"],
        "expires_in": result["expires_in"],
        "status": "awaiting your approval"
    }

def propose_directory(args):
    if type(args) is not dict or set(args) != {"path"}:
        raise ValueError("Invalid directory proposal arguments")
    path = args["path"]
    if type(path) is not str or not path or len(path) > 512 or "\\x00" in path:
        raise ValueError("Invalid directory path")
    result = broker_call({
        "action": "prepare_create_directory",
        "path": path
    })
    proposal_id = result["proposal_id"]
    with STATE_LOCK:
        PENDING_SECRETS[proposal_id] = result["approval_secret"]
    return {
        "type": "directory",
        "proposal_id": proposal_id,
        "path": result["path"],
        "expires_in": result["expires_in"],
        "status": "awaiting your approval"
    }

DEVELOPMENT_TOOLS = [
    {"type":"function","function":{
        "name":"list_files",
        "description":"List approved workspace files. Use empty path for root.",
        "parameters":{"type":"object","properties":{"path":{"type":"string"}},
                      "required":["path"],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"read_file",
        "description":"Read a file in the approved development workspace.",
        "parameters":{"type":"object","properties":{"path":{"type":"string"}},
                      "required":["path"],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"validate_python",
        "description":"Validate Python syntax without changing a file.",
        "parameters":{"type":"object","properties":{"path":{"type":"string"}},
                      "required":["path"],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"propose_directory",
        "description":"Propose creating a directory. This never creates it directly; the user must approve the proposal.",
        "parameters":{"type":"object","properties":{"path":{"type":"string"}},
                      "required":["path"],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"propose_file_change",
        "description":"Prepare a file creation or modification for the user to review. This does not write the file. For updates, read the file first and provide its exact SHA-256. For new files use null.",
        "parameters":{"type":"object","properties":{
            "path":{"type":"string"},
            "content":{"type":"string"},
            "expected_sha256":{"type":["string","null"]}
        },"required":["path","content","expected_sha256"],"additionalProperties":False}}}
,
    {"type":"function","function":{
        "name":"get_workstation_status",
        "description":"Read basic Windows workstation information through the authenticated, read-only observer.",
        "parameters":{"type":"object","properties":{},"required":[],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"get_blender_status",
        "description":"Read whether Blender is installed, its reported version, and whether its process is running.",
        "parameters":{"type":"object","properties":{},"required":[],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"get_blender_scene",
        "description":"Inspect the currently open Blender scene read-only. Returns scene settings, object names and types, transforms, mesh geometry counts, materials, modifiers, constraints, animation and armature metadata. Cannot edit or control Blender.",
        "parameters":{"type":"object","properties":{},"required":[],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"get_blender_project",
        "description":"Inspect the complete open Blender project read-only. Returns saved/unsaved state, project path, scene, collection, material, action and object inventories with bounded details. Cannot edit or control Blender.",
        "parameters":{"type":"object","properties":{},"required":[],"additionalProperties":False}}}

]


_BLENDER_APPROVAL_MANAGER = None

def blender_approval_manager():
    global _BLENDER_APPROVAL_MANAGER
    if _BLENDER_APPROVAL_MANAGER is None:
        _BLENDER_APPROVAL_MANAGER = BlenderApprovalManager()
    return _BLENDER_APPROVAL_MANAGER


TESTAROSSA_TOOLS = [
    {"type":"function","function":{
        "name":"testarossa_discover_tools",
        "description":"Discover Testarossa's registered capabilities. Mutations are hidden by default.",
        "parameters":{"type":"object","properties":{
            "capability":{"type":"string"},
            "include_mutations":{"type":"boolean"}
        },"required":[],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"testarossa_character_plan",
        "description":"Return Testarossa's declarative full character-production plan and design constraints. Does not build or modify anything.",
        "parameters":{"type":"object","properties":{},"required":[],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"testarossa_validate_plan",
        "description":"Validate a proposed plan against registered capabilities. Validation does not execute it.",
        "parameters":{"type":"object","properties":{
            "proposal":{"type":"object"}
        },"required":["proposal"],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"testarossa_analyze_scene",
        "description":"Read and analyze the active Blender scene through the authenticated read-only observer.",
        "parameters":{"type":"object","properties":{},"required":[],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"testarossa_analyze_project",
        "description":"Read and analyze the open Blender project through the authenticated read-only observer.",
        "parameters":{"type":"object","properties":{},"required":[],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"testarossa_learning_summary",
        "description":"Summarize recorded Testarossa experiences. This does not initiate actions.",
        "parameters":{"type":"object","properties":{
            "limit":{"type":"integer"}
        },"required":[],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"testarossa_recovery_advice",
        "description":"Suggest recovery steps from an outcome and error. Never retries or repairs automatically.",
        "parameters":{"type":"object","properties":{
            "outcome":{"type":"string"},
            "error":{"type":"string"},
            "retry_count":{"type":"integer"},
            "approval_required":{"type":"boolean"}
        },"required":["outcome"],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"testarossa_search_skills",
        "description":"Search the local reviewed skill library. Does not execute returned procedures.",
        "parameters":{"type":"object","properties":{
            "query":{"type":"string"},
            "limit":{"type":"integer"}
        },"required":["query"],"additionalProperties":False}}},
    {"type":"function","function":{
        "name":"testarossa_propose_blender_plan",
        "description":"Prepare a typed Blender proposal for explicit user approval. Never execute Blender or include Python, shell commands, or paths. Plan fields: schema_version=1, canonical UUID plan_id, lowercase project_slug, and 1-64 steps. Each step has step_id, operation, args. IDs are unique s1-s999. Exact operation args: create_collection(name); add_mesh(name,primitive,collection,location,rotation,scale); build_humanoid_blockout(name,collection,height,shoulder_width,hip_width,voxel_size); create_material(name,base_color,metallic,roughness); assign_material(object,material); set_transform(object,location,rotation,scale); add_camera(name,collection,location,rotation,lens); add_light(name,collection,kind,location,color,energy,size); set_render(engine,resolution_x,resolution_y,fps); save_project(empty args, final step). Vectors have exactly three numbers. Always finish with save_project.",
        "parameters":{"type":"object","properties":{
            "plan":{'type': 'object', 'properties': {'schema_version': {'type': 'integer', 'enum': [1]}, 'plan_id': {'type': 'string', 'description': 'Placeholder only; JARVIS replaces this with a canonical UUID before validation.'}, 'project_slug': {'type': 'string', 'pattern': '^[a-z][a-z0-9_-]{0,47}$'}, 'steps': {'type': 'array', 'minItems': 1, 'maxItems': 64, 'items': {'type': 'object', 'properties': {'step_id': {'type': 'string', 'pattern': '^s[0-9]{1,3}$'}, 'operation': {'type': 'string', 'enum': ['create_collection', 'add_mesh', 'build_humanoid_blockout', 'create_material', 'assign_material', 'set_transform', 'add_camera', 'add_light', 'set_render', 'save_project']}, 'args': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'primitive': {'type': 'string'}, 'collection': {'type': 'string'}, 'location': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 3, 'maxItems': 3}, 'rotation': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 3, 'maxItems': 3}, 'scale': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 3, 'maxItems': 3}, 'base_color': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 4, 'maxItems': 4}, 'metallic': {'type': 'number'}, 'roughness': {'type': 'number'}, 'object': {'type': 'string'}, 'material': {'type': 'string'}, 'lens': {'type': 'number'}, 'kind': {'type': 'string'}, 'color': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 3, 'maxItems': 3}, 'energy': {'type': 'number'}, 'size': {'type': 'number'}, 'engine': {'type': 'string'}, 'resolution_x': {'type': 'integer'}, 'resolution_y': {'type': 'integer'}, 'fps': {'type': 'integer'}, 'height': {'type': 'number'}, 'shoulder_width': {'type': 'number'}, 'hip_width': {'type': 'number'}, 'voxel_size': {'type': 'number'}}, 'additionalProperties': False}}, 'required': ['step_id', 'operation', 'args'], 'additionalProperties': False}}}, 'required': ['schema_version', 'plan_id', 'project_slug', 'steps'], 'additionalProperties': False}
        },"required":["plan"],"additionalProperties":False}}}
]

def testarossa_dispatch(name, args):
    if type(args) is not dict:
        raise ValueError("Invalid Testarossa arguments")

    if name == "testarossa_character_plan":
        if args:
            raise ValueError("This tool accepts no arguments")
        return character_plan()

    if name == "testarossa_propose_blender_plan":
        if set(args) != {"plan"} or type(args["plan"]) is not dict:
            raise ValueError("Invalid Blender plan proposal")
        plan = dict(args["plan"])
        plan["plan_id"] = str(uuid.uuid4())
        return blender_approval_manager().prepare(plan)

    adapter = testarossa_adapter()

    if name == "testarossa_discover_tools":
        allowed = {"capability", "include_mutations"}
        if set(args) - allowed:
            raise ValueError("Invalid discovery arguments")
        capability = args.get("capability")
        include_mutations = args.get("include_mutations", False)
        if type(include_mutations) is not bool:
            raise ValueError("include_mutations must be boolean")
        return adapter.discover_tools(
            capability=capability,
            include_mutations=include_mutations
        )

    if name == "testarossa_validate_plan":
        if set(args) != {"proposal"} or type(args["proposal"]) is not dict:
            raise ValueError("Invalid plan proposal")
        return adapter.validate_plan(args["proposal"])

    if name == "testarossa_analyze_scene":
        if args:
            raise ValueError("This tool accepts no arguments")
        return adapter.analyze_scene()

    if name == "testarossa_analyze_project":
        if args:
            raise ValueError("This tool accepts no arguments")
        return adapter.analyze_project()

    if name == "testarossa_learning_summary":
        if set(args) - {"limit"}:
            raise ValueError("Invalid learning arguments")
        limit = args.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid learning limit")
        return adapter.learning_summary(limit=limit)

    if name == "testarossa_recovery_advice":
        allowed = {"outcome", "error", "retry_count", "approval_required"}
        if set(args) - allowed or "outcome" not in args:
            raise ValueError("Invalid recovery arguments")
        retry_count = args.get("retry_count", 0)
        approval_required = args.get("approval_required", False)
        if type(retry_count) is not int or retry_count < 0:
            raise ValueError("Invalid retry count")
        if type(approval_required) is not bool:
            raise ValueError("Invalid approval flag")
        return adapter.recovery_advice(
            outcome=args["outcome"],
            error=args.get("error", ""),
            retry_count=retry_count,
            approval_required=approval_required
        )

    if name == "testarossa_search_skills":
        if set(args) - {"query", "limit"} or "query" not in args:
            raise ValueError("Invalid skill search arguments")
        limit = args.get("limit", 10)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Invalid skill search limit")
        return adapter.search_skills(args["query"], limit=limit)

    raise ValueError("Testarossa tool is not authorized")

_TESTAROSSA_ADAPTER = None

def testarossa_adapter():
    global _TESTAROSSA_ADAPTER
    if _TESTAROSSA_ADAPTER is None:
        _TESTAROSSA_ADAPTER = create_adapter(
            observer=inspect_computer,
            memory_directory="/var/lib/jarvis-baby/testarossa"
        )
    return _TESTAROSSA_ADAPTER


DEVELOPMENT_TOOLS.extend(TESTAROSSA_TOOLS)

def project_tool_summary(project):
    # Keep large project inventories bounded before sending them to Ollama.
    return {
        "read_only": project["read_only"],
        "blender_version": project["blender_version"],
        "project": project["project"],
        "summary": project["summary"],
        "scenes": project["scenes"][:30],
        "scenes_truncated": project["scenes_truncated"] or len(project["scenes"]) > 30,
        "collections": project["collections"][:80],
        "collections_truncated": project["collections_truncated"] or len(project["collections"]) > 80,
        "materials": project["materials"][:80],
        "materials_truncated": project["materials_truncated"] or len(project["materials"]) > 80,
        "actions": project["actions"][:80],
        "actions_truncated": project["actions_truncated"] or len(project["actions"]) > 80,
        "objects": project["objects"][:100],
        "objects_truncated": project["objects_truncated"] or len(project["objects"]) > 100,
    }


def chat_with_development_tools(messages):
    conversation = list(messages)
    proposals = []
    total_calls = 0
    for round_index in range(MAX_TOOL_ROUNDS + 1):
        result = ask("/api/chat", {
            "model": MODEL, "messages": conversation, "stream": False,
            "think": False, "tools": DEVELOPMENT_TOOLS,
            "options": {"num_predict": 512}
        })
        message = result.get("message")
        if type(message) is not dict:
            raise RuntimeError("Invalid model response")
        calls = message.get("tool_calls") or []
        if not calls:
            answer = message.get("content", "")
            if type(answer) is not str or not answer.strip():
                raise RuntimeError("Empty model response")
            return answer, proposals
        if round_index >= MAX_TOOL_ROUNDS or type(calls) is not list or len(calls) > 3:
            raise RuntimeError("Tool-call limit reached")
        if total_calls + len(calls) > MAX_TOOL_CALLS:
            raise RuntimeError("Tool-call limit reached")
        conversation.append(message)
        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name")
            args = fn.get("arguments", {})
            if type(args) is str:
                args = json.loads(args)
            if type(args) is not dict:
                raise ValueError("Invalid tool arguments")
            if name == "propose_directory":
                response = propose_directory(args)
                proposals.append(response)
            elif name == "propose_file_change":
                response = propose_file_change(args)
                proposals.append(response)
            elif name in {
                "get_workstation_status", "get_blender_status",
                "get_blender_scene", "get_blender_project"
            }:
                if args:
                    raise ValueError("Computer-awareness tools accept no arguments")
                operation = {
                    "get_workstation_status": "workstation",
                    "get_blender_status": "blender_status",
                    "get_blender_scene": "blender_scene_snapshot",
                    "get_blender_project": "blender_project_snapshot",
                }[name]
                result_data = inspect_computer(operation)
                if name == "get_blender_project":
                    result_data = project_tool_summary(result_data)
                response = {
                    "ok": True,
                    "operation": operation,
                    "result": result_data
                }
            elif name in {tool["function"]["name"] for tool in TESTAROSSA_TOOLS}:
                dispatched = testarossa_dispatch(name, args)
                if name == "testarossa_propose_blender_plan":
                    proposals.append(dispatched)
                response = {
                    "ok": True,
                    "result": dispatched
                }
            elif name in {"list_files", "read_file", "validate_python"}:
                response = development_request(name, args)
            else:
                raise ValueError("Tool is not authorized")
            encoded = json.dumps(response, ensure_ascii=False)
            if len(encoded.encode("utf-8")) > 300 * 1024:
                raise RuntimeError("Tool response too large")
            conversation.append({"role":"tool","tool_name":name,"content":encoded})
            total_calls += 1
    raise RuntimeError("Tool loop ended unexpectedly")

def read_body(handler, maximum=320000):
    n = int(handler.headers.get("Content-Length", "0"))
    if not 1 <= n <= maximum:
        raise ValueError("Invalid request size")
    raw = handler.rfile.read(n)
    def unique(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValueError("Duplicate JSON key")
            out[k] = v
        return out
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)

class App(BaseHTTPRequestHandler):
    def send(self, code, obj, kind="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def browser_session(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get("baby_approval_session")
        token = morsel.value if morsel else ""
        with STATE_LOCK:
            expiry = SESSIONS.get(token)
            if not token or not expiry or expiry < time.time():
                return None
        return token

    def valid_origin(self):
        return self.headers.get("Origin") == "http://172.30.50.10:8765"

    def do_GET(self):
        raw_path = self.path
        query = parse_qs(raw_path.partition("?")[2], keep_blank_values=True)
        self.path = raw_path.split("?", 1)[0]

        if self.path == "/api/computer/status":
            operations = query.get("operation", [])
            if len(operations) != 1 or operations[0] not in {
                "workstation", "blender_status",
                "blender_scene_snapshot", "blender_project_snapshot"
            }:
                self.send(400, {"error":"Invalid computer-awareness operation"})
                return
            operation = operations[0]
            try:
                result = inspect_computer(operation)
                self.send(200, {
                    "ok":True, "operation":operation, "result":result
                })
            except Exception:
                self.send(503, {
                    "ok":False,
                    "error":"Computer observer unavailable or response invalid"
                })
            return

        if self.path == "/favicon.ico":
            self.send_response(204); self.end_headers(); return
        if self.path == "/":
            token = secrets.token_urlsafe(32)
            with STATE_LOCK:
                SESSIONS[token] = time.time() + 43200
            with open("/opt/jarvis-baby/ui/static/index.html", "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", "baby_approval_session=" + token +
                             "; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200")
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/status":
            try:
                ask("/api/version")
                self.send(200, {"online":True,"model":MODEL})
            except Exception:
                self.send(503, {"online":False,"model":MODEL})
        else:
            self.send(404, {"error":"Not found"})

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        try:
            if route == "/api/chat":
                data = read_body(self, CHAT_REQUEST_MAX_BYTES)
                messages = data["messages"]
                if type(messages) is not list or not 1 <= len(messages) <= 10:
                    raise ValueError("Invalid messages")
                clean = [{"role":"system","content":IDENTITY}]
                for m in messages:
                    if type(m) is not dict or m.get("role") not in ("user","assistant"):
                        raise ValueError("Invalid message")
                    if type(m.get("content")) is not str or len(m["content"]) > 4000:
                        raise ValueError("Invalid message content")
                    clean.append({"role":m["role"],"content":m["content"]})
                answer, proposals = chat_with_development_tools(clean)
                self.send(200, {"response":answer,"proposals":proposals})
                return

            if route in ("/api/blender/approve", "/api/blender/reject"):
                if not self.valid_origin() or not self.browser_session():
                    self.send(403, {"error":"Browser approval session required"})
                    return
                data = read_body(self, 4096)
                if type(data) is not dict or set(data) != {"proposal_id"} or type(data["proposal_id"]) is not str:
                    raise ValueError("Invalid Blender proposal ID")
                manager = blender_approval_manager()
                try:
                    if route == "/api/blender/approve":
                        result = manager.approve(data["proposal_id"])
                        self.send(200, {"ok":True,"result":result})
                    else:
                        result = manager.reject(data["proposal_id"])
                        self.send(200, result)
                except ProposalUnavailable:
                    self.send(410, {"error":"Blender proposal expired or already used"})
                except BridgeExecutionError as exc:
                    self.send(502, {"error":"Blender execution failed: " + str(exc)[:120]})
                return

            if route in ("/api/approve", "/api/reject"):
                if not self.valid_origin() or not self.browser_session():
                    self.send(403, {"error":"Browser approval session required"})
                    return
                data = read_body(self, 4096)
                if set(data) != {"proposal_id"} or type(data["proposal_id"]) is not str:
                    raise ValueError("Invalid proposal ID")
                proposal_id = data["proposal_id"]
                with STATE_LOCK:
                    secret = PENDING_SECRETS.get(proposal_id)
                if not secret:
                    self.send(410, {"error":"Proposal expired or unavailable"})
                    return
                action = "approve_write" if route == "/api/approve" else "reject_write"
                result = broker_call({
                    "action":action, "proposal_id":proposal_id,
                    "approval_secret":secret
                })
                with STATE_LOCK:
                    PENDING_SECRETS.pop(proposal_id, None)
                if route == "/api/approve":
                    self.send(200, {"ok":True,"result":result})
                else:
                    self.send(200, {"ok":True,"rejected":True})
                return

            self.send(404, {"error":"Not found"})
        except ValueError as exc:
            # Log validation reason only; never log request contents.
            print(f"Rejected {route}: {type(exc).__name__}: {exc}", flush=True)
            self.send(400, {"error":"Invalid request"})
        except Exception:
            self.send(502, {"error":"The operation was rejected or could not be completed"})

if __name__ == "__main__":
    ThreadingHTTPServer(("172.30.50.10", 8765), App).serve_forever()
