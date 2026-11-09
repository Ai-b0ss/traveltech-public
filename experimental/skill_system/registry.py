"""Load trusted versioned procedures; check dependencies before any inference."""

import hashlib, json
from pathlib import Path
from provider_transport import Failure, canonical

ROOT = Path(__file__).resolve().parents[2]
OPERATIONS = {
    "understand",
    "validate_constraints",
    "archive_search",
    "live_search",
    "compare",
    "select_evidence",
    "prepare_preview",
    "inspect",
    "clarify",
    "undo",
    "preference",
}


class Registry:
    def __init__(self, path=None):
        self.root = Path(path or ROOT / "skills")
        self.skills = {}
        for p in sorted(self.root.glob("*/manifest.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            if set(d) != {
                "id",
                "version",
                "description",
                "operations",
                "requires",
                "depends_on",
                "resources",
                "completion",
            }:
                raise Failure("SKILL_MANIFEST_FIELDS")
            if (
                d["id"] != p.parent.name
                or d["id"] in self.skills
                or not set(d["operations"]) <= OPERATIONS
            ):
                raise Failure("SKILL_MANIFEST_INVALID")
            body = (p.parent / "SKILL.md").read_text(encoding="utf-8")
            for ref in d["resources"]:
                rp = (p.parent / ref).resolve()
                if not rp.is_relative_to(p.parent.resolve()) or not rp.is_file():
                    raise Failure("SKILL_RESOURCE_INVALID")
            d["instructions"] = body
            d["resource_texts"] = {
                ref: (p.parent / ref).read_text(encoding="utf-8") for ref in d["resources"]
            }
            d["hash"] = hashlib.sha256(canonical(d).encode()).hexdigest()
            self.skills[d["id"]] = d
        if not self.skills:
            raise Failure("SKILL_REGISTRY_EMPTY")
        for key in self.skills:
            self.resolve([key])

    def resolve(self, names):
        out = []
        active = set()
        seen = set()

        def visit(name):
            if name not in self.skills:
                raise Failure("SKILL_DEPENDENCY_MISSING")
            if name in active:
                raise Failure("SKILL_DEPENDENCY_CYCLE")
            if name in seen:
                return
            active.add(name)
            for dep in self.skills[name]["depends_on"]:
                visit(dep)
            active.remove(name)
            seen.add(name)
            out.append(self.skills[name])

        for n in names:
            visit(n)
        return out

    def context(self, names, state):
        packs = self.resolve(names)
        required = set().union(*(set(p["requires"]) for p in packs))
        if required - set(state):
            raise Failure("SKILL_CONTEXT_MISSING")
        # Critical constraints are never removed by a probabilistic selector.
        return {
            "state": {k: state[k] for k in sorted(required)},
            "skills": [
                {
                    "id": p["id"],
                    "version": p["version"],
                    "hash": p["hash"],
                    "instructions": p["instructions"],
                    "resources": p["resource_texts"],
                }
                for p in packs
            ],
        }

    def index(self):
        return [
            {
                "id": d["id"],
                "description": d["description"],
                "version": d["version"],
                "hash": d["hash"],
            }
            for d in self.skills.values()
        ]
