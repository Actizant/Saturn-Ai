"""
core/creators_catalog.py
========================
The single data source behind Saturn's Creators page.

The page shows every capability Saturn can point at - the portable MCP
servers it forged, the lightweight features it forged, and the MCP servers
the user configured by hand - each with the same shape so one card renderer
handles all three tiers.

This module owns exactly one job: turn the scattered truth (forge manifests
on disk, live ``MCPServerInstance`` objects, the dynamic registry, the user's
mcp_servers.json, and CapabilityHealth's rolling telemetry) into a list of
uniform capability dicts. It never touches Qt and never mutates anything.

Design contract it satisfies (creators_handoff.md section 3), per capability:

    name, tier ("feature" | "mcp"), description, long_description,
    goal (the original request text a forge was asked to fulfil),
    origin ("forged" | "manual"), forged_at, first_forged_at, reforged,
    version_history[], audit[] (recent redacted calls), declared_needs[],
    risk ("LOW"|"MEDIUM"|"HIGH"), permissions_enforced,
    forged_at, version, folder, source_path,
    tools[] {name, description, requires[], permission{...}, params[...]},
    resources[] {uri, description, mime}, prompts[] {name, description, args[]},
    health ("ok"|"warn"|"err"), health_note, last_used_at, heal_count,
    calls_24h[24] {ok, failed}, calls_14d[14] {ok, failed}, avg_latency_ms,
    last_errors[5] {at, tool, message, healed}

Anything we cannot know (a manual server's line count, a hand-configured
server's source) is left empty rather than faked - the page degrades those
sections instead of inventing data.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("CreatorsCatalog")

# Internal forge tiers -> the two words the page uses.
_TIER_MAP = {"portable": "mcp", "lightweight": "feature", "mcp": "mcp", "feature": "feature"}


def _read_manifest(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug(f"[Catalog] could not read {path}: {exc}")
    return None


def _params_from_schema(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten a JSON-Schema ``properties`` block into the parameter rows the
    detail panel expands under each tool."""
    out: List[Dict[str, Any]] = []
    if not isinstance(schema, dict):
        return out
    required = set(schema.get("required") or [])
    for pname, pdef in (schema.get("properties") or {}).items():
        pdef = pdef or {}
        ptype = pdef.get("type")
        if isinstance(ptype, list):
            ptype = "/".join(ptype)
        out.append({
            "name": pname,
            "type": (ptype or "string"),
            "required": pname in required,
            "description": pdef.get("description", ""),
            "default": pdef.get("default"),
        })
    return out


def _tools_from_manifest(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for t in tools or []:
        rows.append({
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            # Declared needs ride through to the detail panel's permission
            # selects; older manifests simply carry nothing.
            "requires": [str(r) for r in (t.get("requires") or [])],
            "permission_default": t.get("permission_default", ""),
            "params": _params_from_schema(t.get("inputSchema") or {}),
        })
    return rows


def _count_source_lines(folder: Optional[str]) -> int:
    """How many lines of Python Saturn actually wrote for this capability.
    A cheap proxy for authorship the header KPI adds up."""
    if not folder:
        return 0
    total = 0
    try:
        base = Path(folder)
        for py in base.rglob("*.py"):
            try:
                total += sum(1 for _ in py.open(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
    except Exception:
        return 0
    return total


class CreatorsCatalog:
    """Reads every capability source and hands the page a uniform list."""

    @classmethod
    def build(cls) -> List[Dict[str, Any]]:
        caps: List[Dict[str, Any]] = []
        caps += cls._forged_mcp_servers()
        caps += cls._forged_features()
        caps += cls._manual_mcp_servers(existing={c["name"] for c in caps})
        for cap in caps:
            cls._attach_telemetry(cap)
        # Newest forged first; manual servers (no forged_at) fall to the back.
        caps.sort(key=lambda c: c.get("forged_at") or 0.0, reverse=True)
        return caps

    @staticmethod
    def get(name: str, tier: str) -> Optional[Dict[str, Any]]:
        for cap in CreatorsCatalog.build():
            if cap["name"] == name and cap["tier"] == tier:
                return cap
        return None

    # ── Sources ───────────────────────────────────────────────────────────────

    @classmethod
    def _forged_mcp_servers(cls) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            from core.mcp_forge import MCPForge
            forged = MCPForge.list_forged()
        except Exception as exc:
            logger.debug(f"[Catalog] MCPForge unavailable: {exc}")
            forged = []
        live = cls._live_servers()
        for m in forged:
            name = m.get("name") or ""
            if not name:
                continue
            folder = m.get("_dir")
            server = live.get(name)
            tools = (_tools_from_manifest(server.tools) if server
                     else _tools_from_manifest(m.get("tools", [])))
            resources = [
                {"uri": r.get("uri", ""), "description": r.get("description", ""),
                 "mime": r.get("mimeType", "")}
                for r in (getattr(server, "resources", []) if server else [])
            ]
            prompts = [
                {"name": p.get("name", ""), "description": p.get("description", ""),
                 "args": p.get("arguments", []) or []}
                for p in (getattr(server, "prompts", []) if server else [])
            ]
            source_path = str(Path(folder) / "server.py") if folder else None
            # Prefer the live instance's config; fall back to a reconstructed
            # one so Copy-config and Unregister work even for an offline forge.
            cfg = dict(getattr(server, "config", {}) or {}) if server else {}
            if not cfg and source_path:
                try:
                    from core.mcp_forge import _mcp_server_python
                    cfg = {"command": _mcp_server_python(), "args": [source_path]}
                except Exception:
                    cfg = {}
            out.append({
                "name": name,
                "tier": "mcp",
                "config": cfg,
                "description": m.get("description", ""),
                "long_description": m.get("description", ""),
                "goal": m.get("goal", ""),
                "origin": "forged",
                "forged_at": m.get("created_at") or 0.0,
                "forge_task_id": None,
                "version": m.get("version", "1.0.0"),
                "folder": folder,
                "source_path": source_path,
                "tools": tools,
                "resources": resources,
                "prompts": prompts,
                "tool_count": len(tools),
                "connected": bool(server and getattr(server, "is_connected", False)),
                "disabled": bool(cfg.get("disabled", False)),
                "can_reforge": True,
                "can_copy_config": True,
                "source_lines": _count_source_lines(folder),
                "lazy": bool(getattr(server, "lazy", True)) if server else True,
                "_manifest": m,
            })
        return out

    @classmethod
    def _forged_features(cls) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            from core.dynamic_registry import DynamicToolRegistry
            skills = DynamicToolRegistry.list_skills()
            features_dir = DynamicToolRegistry.get_skills_directory()
        except Exception as exc:
            logger.debug(f"[Catalog] dynamic registry unavailable: {exc}")
            return out
        for s in skills:
            folder = s.get("path") or ""
            manifest = _read_manifest(Path(folder) / "manifest.json") if folder else None
            if not manifest:
                continue
            # Only Saturn-authored lightweight features; hand-written native
            # verbs are not the Creators page's story.
            author = str(manifest.get("author", "")).lower()
            if "saturn" not in author and "self-evolution" not in author:
                continue
            desc = s.get("description", "") or manifest.get("description", "")
            source_path = None
            for cand in ("skill.py", f"{s.get('name')}.py"):
                p = Path(folder) / cand
                if p.exists():
                    source_path = str(p)
                    break
            out.append({
                "name": s.get("name", ""),
                "tier": "feature",
                "config": {},
                "description": desc,
                "long_description": desc,
                "goal": manifest.get("goal", ""),
                "origin": "forged",
                "forged_at": manifest.get("created_at") or s.get("created_at") or 0.0,
                "forge_task_id": None,
                "version": s.get("version", "1.0.0"),
                "folder": folder,
                "source_path": source_path,
                # A lightweight feature is a single verb, not a tool table.
                "tools": [{"name": s.get("name", ""), "description": desc, "params": []}],
                "resources": [],
                "prompts": [],
                "tool_count": 1,
                "connected": bool(s.get("active", True)),
                "can_reforge": True,
                "can_copy_config": False,   # runs inside Saturn only
                "source_lines": _count_source_lines(folder),
                "lazy": False,   # in-process: no server to wake
                "_manifest": manifest,
            })
        return out

    @classmethod
    def _manual_mcp_servers(cls, existing: set) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        configs: Dict[str, Any] = {}
        for path in cls._config_paths():
            data = _read_manifest(path) or {}
            configs.update(data.get("mcpServers", {}) or {})
        live = cls._live_servers()
        for name, cfg in configs.items():
            if not name or name in existing:
                continue
            server = live.get(name)
            tools = _tools_from_manifest(server.tools) if server else []
            resources = [
                {"uri": r.get("uri", ""), "description": r.get("description", ""),
                 "mime": r.get("mimeType", "")}
                for r in (getattr(server, "resources", []) if server else [])
            ]
            prompts = [
                {"name": p.get("name", ""), "description": p.get("description", ""),
                 "args": p.get("arguments", []) or []}
                for p in (getattr(server, "prompts", []) if server else [])
            ]
            out.append({
                "name": name,
                "tier": "mcp",
                "config": dict(cfg or {}),
                "description": cfg.get("description", ""),
                "long_description": cfg.get("description", "Configured manually."),
                "goal": "",
                "origin": "manual",
                "forged_at": 0.0,
                "forge_task_id": None,
                "version": "",
                "folder": None,
                "source_path": None,
                "tools": tools,
                "resources": resources,
                "prompts": prompts,
                "tool_count": len(tools),
                "connected": bool(server and getattr(server, "is_connected", False)),
                "disabled": bool(cfg.get("disabled", False)),
                "can_reforge": False,   # no source to re-forge from
                "can_copy_config": True,
                "source_lines": 0,
            })
        return out

    # ── Telemetry attach ──────────────────────────────────────────────────────

    @staticmethod
    def _attach_telemetry(cap: Dict[str, Any]) -> None:
        try:
            from core.capability_health import CapabilityHealth
            r = CapabilityHealth.rollup(cap["name"], cap["tier"])
        except Exception:
            r = {}
        cap["health"] = r.get("health", "ok")
        cap["health_note"] = _health_note(cap, r)
        cap["calls_24h"] = r.get("calls_24h", [{"ok": 0, "failed": 0} for _ in range(24)])
        cap["calls_14d"] = r.get("calls_14d", [{"ok": 0, "failed": 0} for _ in range(14)])
        cap["calls_14d_total"] = r.get("calls_14d_total", 0)
        cap["success_pct"] = r.get("success_pct", 100.0)
        cap["avg_latency_ms"] = r.get("avg_latency_ms", 0.0)
        cap["last_used_at"] = r.get("last_used_at", 0.0)
        cap["last_errors"] = r.get("last_errors", [])
        cap["heal_count"] = r.get("heal_count", 0)
        CreatorsCatalog._attach_trust(cap)

    # ── Trust & provenance attach ─────────────────────────────────────────────

    @staticmethod
    def _attach_trust(cap: Dict[str, Any]) -> None:
        """Permissions, risk, version history and audit rows for one cap.

        The design's detail panel shows a per-tool Always allow / Ask / Block
        select, a risk badge, a NETWORK/FILES/COMMANDS grid, version history
        and the recent-call table. Everything the panel needs is derived here
        from data that actually exists (the manifest, the override store, the
        audit file, ``.bak`` siblings on disk); what cannot be known is left
        empty - the page degrades the section instead of inventing rows.
        """
        try:
            from core import permissions
        except Exception:
            return
        name = cap["name"]
        is_forged = cap.get("origin") == "forged"
        union: List[str] = []
        for t in cap.get("tools", []):
            if is_forged:
                eff = permissions.effective(name, t["name"],
                                            requires=t.get("requires") or [],
                                            tool_name=t["name"],
                                            description=t.get("description", ""))
            else:
                # Hand-configured servers are the user's own choice and are
                # not gated; the page still shows what is known about them.
                eff = {"level": permissions.ALLOW, "level_label": "Not gated",
                       "default": permissions.ALLOW,
                       "declared_requires": list(t.get("requires") or []),
                       "risk": "LOW", "overridden": False,
                       "sandbox_facts": []}
            t["permission"] = eff
            for n in eff["declared_requires"]:
                if n not in union:
                    union.append(n)
        cap["declared_needs"] = sorted(union)
        cap["risk"] = max(
            (t["permission"]["risk"] for t in cap.get("tools", [])
             if isinstance(t.get("permission"), dict)),
            key=["LOW", "MEDIUM", "HIGH"].index, default="LOW")
        cap["permissions_enforced"] = is_forged

        m = cap.pop("_manifest", None) or {}
        cap["first_forged_at"] = m.get("first_forged_at") or cap.get("forged_at") or 0.0
        cap["reforged"] = bool(m.get("reforged"))
        history = []
        ver = cap.get("version") or m.get("version") or ""
        if ver:
            history.append({"version": ver, "at": cap.get("forged_at") or 0.0,
                            "state": "current"})
        prev = m.get("previous_version")
        if prev:
            history.append({"version": prev, "at": cap.get("first_forged_at", 0.0),
                            "state": "reforged from"})
        folder = cap.get("folder")
        if folder:
            # A surviving server.py.bak is physical proof an earlier build was
            # kept and could be rolled back to - show it as history, not a guess.
            for bak, what in (("server.py.bak", "rollback snapshot"),
                              ("manifest.json.bak", "rollback snapshot")):
                if (Path(folder) / bak).exists():
                    history.append({"version": "previous build",
                                    "at": 0.0, "state": what})
                    break
        cap["version_history"] = history

        try:
            from core.audit_log import AuditLog
            prefix = f"mcp_{name}_"
            entries = AuditLog.recent(limit=400)
            rows = []
            for e in reversed(entries):
                tool = str(e.get("tool", ""))
                if cap["tier"] == "mcp" and tool.startswith(prefix):
                    rows.append({**e, "tool": tool[len(prefix):]})
                elif cap["tier"] == "feature" and tool == name:
                    rows.append(e)
                if len(rows) >= 12:
                    break
            cap["audit"] = rows
        except Exception:
            cap["audit"] = []

    # ── Safety state ──────────────────────────────────────────────────────────

    @staticmethod
    def safety_state(caps: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """The numbers behind the page's safety bar, all read from truth.

        Kill-switch position, how many forged servers are live versus sleeping
        (lazy, woken on first call) and the running caps. Built from the same
        live manager the engine consults, so the banner cannot drift from what
        a call would actually hit. ``caps`` is an optional already-built list
        to avoid a second catalog build for the common case.
        """
        state = {"forged_tools_enabled": True, "running": 0, "forged_servers": 0,
                 "lazy_sleeping": 0, "max_running": 4}
        try:
            from core.mcp_manager import forged_tools_enabled, forged_max_running
            state["forged_tools_enabled"] = bool(forged_tools_enabled())
            state["max_running"] = int(forged_max_running())
        except Exception:
            pass
        live = CreatorsCatalog._live_servers()
        # A forged capability can be known from the live manager (a running
        # or woken server) or from the built caps (a forge on disk that has
        # never been woken yet). Union both so the counts match the shelf.
        forged_names = set()
        for sname, server in live.items():
            if getattr(server, "is_forged", False):
                forged_names.add(sname)
        for cap in (caps if caps is not None else []):
            if cap.get("tier") == "mcp" and cap.get("origin") == "forged":
                forged_names.add(cap["name"])
        state["forged_servers"] = len(forged_names)
        for sname in forged_names:
            server = live.get(sname)
            if server is None:
                continue
            if getattr(server, "is_connected", False):
                state["running"] += 1
            elif getattr(server, "lazy", False):
                state["lazy_sleeping"] += 1
        return state

    @staticmethod
    def set_permission(cap_name: str, tool_name: str, level: Optional[str]) -> bool:
        """Persist one Always allow / Ask / Block choice from the page.

        The catalog is otherwise read-only; this is the single deliberate
        write seam so the UI never hand-edits the store file. ``level=None``
        clears the override back to Saturn's classified default.
        """
        try:
            from core import permissions
            return permissions.PermissionsStore.set_level(cap_name, tool_name, level)
        except Exception:
            return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _live_servers() -> Dict[str, Any]:
        try:
            from core.mcp_manager import UniversalMCPManager
            return dict(UniversalMCPManager.get_instance()._servers)
        except Exception:
            return {}

    @staticmethod
    def _config_paths() -> List[Path]:
        paths: List[Path] = []
        try:
            from core.mcp_manager import MCP_CONFIG_FILE
            paths.append(MCP_CONFIG_FILE)
        except Exception:
            pass
        paths.append(Path("config/mcp_servers.json").resolve())
        return paths


def _health_note(cap: Dict[str, Any], rollup: Dict[str, Any]) -> str:
    """A one-line human note under the health dot, matching the design's tone."""
    if cap.get("origin") == "manual" and not cap.get("connected", False):
        return "Configured but not connected right now."
    health = rollup.get("health", "ok")
    total = rollup.get("calls_14d_total", 0)
    if health == "err":
        return "Failing repeatedly - Saturn will offer to re-forge it."
    if health == "warn":
        return "Some recent calls failed; watching closely."
    if total == 0:
        return "Idle since launch - no calls recorded yet."
    return f"{cap.get('tool_count', 0)} verb(s) healthy over {total} call(s)."


def summary(caps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Header band + KPI numbers, derived entirely from the built list.
    The page never recomputes these so the counts and the cards agree."""
    n_feature = sum(1 for c in caps if c["tier"] == "feature")
    n_mcp = sum(1 for c in caps if c["tier"] == "mcp")
    n_tools = sum(c.get("tool_count", 0) for c in caps)
    now = time.time()
    month_ago = now - 30 * 86400
    forged_30d = sum(1 for c in caps if c.get("forged_at", 0) >= month_ago)
    self_healed = sum(1 for c in caps if c.get("heal_count", 0) > 0)
    calls_14d = sum(c.get("calls_14d_total", 0) for c in caps)
    lines_written = sum(c.get("source_lines", 0) for c in caps)
    return {
        "features": n_feature,
        "mcp_servers": n_mcp,
        "tools_live": n_tools,
        "forged_30d": forged_30d,
        "self_healed": self_healed,
        "calls_14d": calls_14d,
        "lines_written": lines_written,
        "total": len(caps),
    }
