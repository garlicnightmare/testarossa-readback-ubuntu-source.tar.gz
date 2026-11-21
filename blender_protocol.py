"""Strict declarative command protocol for the isolated Testarossa Blender project."""
from __future__ import annotations
import json
import math
import re
import uuid

SCHEMA_VERSION = 1
MAX_STEPS = 64
MAX_PLAN_BYTES = 64 * 1024
SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
NAME_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,64}$")

PRIMITIVES = {"CUBE", "UV_SPHERE", "ICO_SPHERE", "CYLINDER", "CONE", "TORUS", "PLANE"}
LIGHT_TYPES = {"AREA", "POINT", "SUN", "SPOT"}
RENDER_ENGINES = {"BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH", "CYCLES"}

SCHEMAS = {
    "create_collection": {"name"},
    "add_mesh": {"name", "primitive", "collection", "location", "rotation", "scale"},
    "create_material": {"name", "base_color", "metallic", "roughness"},
    "assign_material": {"object", "material"},
    "set_transform": {"object", "location", "rotation", "scale"},
    "add_camera": {"name", "collection", "location", "rotation", "lens"},
    "add_light": {"name", "collection", "kind", "location", "color", "energy", "size"},
    "set_render": {"engine", "resolution_x", "resolution_y", "fps"},
    "build_humanoid_blockout": {"name", "collection", "height", "shoulder_width", "hip_width", "voxel_size"},
    "save_project": set(),
}

def fail(message):
    raise ValueError(message)

def exact_keys(value, expected, label):
    if type(value) is not dict or set(value) != set(expected):
        fail(f"{label}: invalid fields")

def name(value, label):
    if type(value) is not str or not NAME_RE.fullmatch(value):
        fail(f"{label}: invalid name")
    return value

def number(value, label, low, high):
    if type(value) not in (int, float):
        fail(f"{label}: expected a number")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        fail(f"{label}: outside permitted range")
    return value

def vector(value, label, low, high, length):
    if type(value) is not list or len(value) != length:
        fail(f"{label}: invalid vector")
    return [number(v, f"{label}[{i}]", low, high) for i, v in enumerate(value)]

def integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        fail(f"{label}: invalid integer")
    return value

def validate_args(operation, args):
    exact_keys(args, SCHEMAS[operation], operation)

    if operation == "create_collection":
        return {"name": name(args["name"], "collection")}

    if operation == "add_mesh":
        if type(args["primitive"]) is not str or args["primitive"] not in PRIMITIVES:
            fail("unsupported mesh primitive")
        return {
            "name": name(args["name"], "object"),
            "primitive": args["primitive"],
            "collection": name(args["collection"], "collection"),
            "location": vector(args["location"], "location", -1000, 1000, 3),
            "rotation": vector(args["rotation"], "rotation", -628.32, 628.32, 3),
            "scale": vector(args["scale"], "scale", 0.001, 100, 3),
        }

    if operation == "create_material":
        return {
            "name": name(args["name"], "material"),
            "base_color": vector(args["base_color"], "base_color", 0, 1, 4),
            "metallic": number(args["metallic"], "metallic", 0, 1),
            "roughness": number(args["roughness"], "roughness", 0, 1),
        }

    if operation == "assign_material":
        return {
            "object": name(args["object"], "object"),
            "material": name(args["material"], "material"),
        }

    if operation == "set_transform":
        return {
            "object": name(args["object"], "object"),
            "location": vector(args["location"], "location", -1000, 1000, 3),
            "rotation": vector(args["rotation"], "rotation", -628.32, 628.32, 3),
            "scale": vector(args["scale"], "scale", 0.001, 100, 3),
        }

    if operation == "add_camera":
        return {
            "name": name(args["name"], "camera"),
            "collection": name(args["collection"], "collection"),
            "location": vector(args["location"], "location", -1000, 1000, 3),
            "rotation": vector(args["rotation"], "rotation", -628.32, 628.32, 3),
            "lens": number(args["lens"], "lens", 10, 300),
        }

    if operation == "add_light":
        if type(args["kind"]) is not str or args["kind"] not in LIGHT_TYPES:
            fail("unsupported light type")
        return {
            "name": name(args["name"], "light"),
            "collection": name(args["collection"], "collection"),
            "kind": args["kind"],
            "location": vector(args["location"], "location", -1000, 1000, 3),
            "color": vector(args["color"], "color", 0, 1, 3),
            "energy": number(args["energy"], "energy", 0, 100000),
            "size": number(args["size"], "size", 0.001, 100),
        }

    if operation == "set_render":
        if type(args["engine"]) is not str or args["engine"] not in RENDER_ENGINES:
            fail("unsupported render engine")
        return {
            "engine": args["engine"],
            "resolution_x": integer(args["resolution_x"], "resolution_x", 320, 7680),
            "resolution_y": integer(args["resolution_y"], "resolution_y", 240, 4320),
            "fps": integer(args["fps"], "fps", 1, 240),
        }

    if operation == "build_humanoid_blockout":
        return {
            "name": name(args["name"], "object"),
            "collection": name(args["collection"], "collection"),
            "height": number(args["height"], "height", 1.4, 2.1),
            "shoulder_width": number(args["shoulder_width"], "shoulder_width", 0.32, 0.62),
            "hip_width": number(args["hip_width"], "hip_width", 0.28, 0.52),
            "voxel_size": number(args["voxel_size"], "voxel_size", 0.025, 0.08),
        }

    if operation == "save_project":
        return {}

    fail("operation not authorized")

def validate_plan(plan):
    if type(plan) is not dict:
        fail("plan must be an object")
    exact_keys(plan, {"schema_version", "plan_id", "project_slug", "steps"}, "plan")

    if type(plan["schema_version"]) is not int or plan["schema_version"] != SCHEMA_VERSION:
        fail("unsupported schema version")

    try:
        if type(plan["plan_id"]) is not str or str(uuid.UUID(plan["plan_id"])) != plan["plan_id"]:
            fail("invalid canonical plan UUID")
    except (ValueError, AttributeError):
        fail("invalid plan UUID")

    slug = plan["project_slug"]
    if type(slug) is not str or not SLUG_RE.fullmatch(slug):
        fail("invalid project slug")

    steps = plan["steps"]
    if type(steps) is not list or not 1 <= len(steps) <= MAX_STEPS:
        fail("invalid step count")

    try:
        encoded = json.dumps(plan, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        fail("plan is not valid finite JSON")
    if len(encoded) > MAX_PLAN_BYTES:
        fail("plan exceeds size limit")

    collections, objects, materials = set(), set(), set()
    saved = False
    render_configured = False
    normalized = []
    step_ids = set()

    for index, step in enumerate(steps):
        exact_keys(step, {"step_id", "operation", "args"}, f"step {index}")
        if type(step["step_id"]) is not str or not re.fullmatch(r"s[0-9]{1,3}", step["step_id"]):
            fail(f"step {index}: invalid step ID")
        if step["step_id"] in step_ids:
            fail(f"step {index}: duplicate step ID")
        step_ids.add(step["step_id"])

        operation = step["operation"]
        if type(operation) is not str or operation not in SCHEMAS:
            fail(f"step {index}: operation not authorized")
        if saved:
            fail("save_project must be final")

        args = validate_args(operation, step["args"])

        if operation == "create_collection":
            if args["name"] in collections:
                fail("duplicate collection")
            collections.add(args["name"])

        elif operation in {"add_mesh", "add_camera", "add_light"}:
            if args["name"] in objects:
                fail("duplicate object")
            if args["collection"] not in collections:
                fail("unknown collection dependency")
            objects.add(args["name"])

        elif operation == "build_humanoid_blockout":
            if args["collection"] not in collections:
                fail("unknown collection dependency")
            if args["name"] in objects:
                fail("duplicate object")
            objects.add(args["name"])

        elif operation == "create_material":
            if args["name"] in materials:
                fail("duplicate material")
            materials.add(args["name"])

        elif operation == "assign_material":
            if args["object"] not in objects or args["material"] not in materials:
                fail("unknown object or material dependency")

        elif operation == "set_transform":
            if args["object"] not in objects:
                fail("unknown object dependency")

        elif operation == "set_render":
            if render_configured:
                fail("render settings may only be configured once")
            render_configured = True

        elif operation == "save_project":
            if index != len(steps) - 1:
                fail("save_project must be final")
            saved = True

        normalized.append({"step_id": step["step_id"], "operation": operation, "args": args})

    if not saved:
        fail("plan must end with save_project")

    return {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan["plan_id"],
        "project_slug": slug,
        "steps": normalized,
    }

def summarize_plan(plan):
    valid = validate_plan(plan)
    return {
        "project_slug": valid["project_slug"],
        "step_count": len(valid["steps"]),
        "operations": valid["steps"],
        "execution_model": "typed_allowlist_only",
        "arbitrary_python": False,
        "current_blender_scene_access": False,
    }