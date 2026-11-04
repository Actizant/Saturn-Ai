"""
Feature: flowstate
Description: FlowState Time Machine for Saturn.
Snapshots and restores persistent desktop workspace environments (browser tabs,
open apps, VS Code projects, and window layouts) that survive reboots.
"""

from typing import Any, Dict

FEATURE_METADATA = {
    "name": "flowstate",
    "description": (
        "FlowState Time Machine: Captures or restores the entire desktop state "
        "(open browser tabs, running apps, VS Code project folders, and workspace layouts). "
        "Allows users to snapshot their work context, reboot, and restore it later."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action to perform: 'snapshot' (save state), 'restore' (load state), 'list' (show saved states), or 'delete'."
            },
            "name": {
                "type": "STRING",
                "description": "Name of the workspace/restoration point (e.g. 'Dev Sprint', 'Shopping Research', 'Study Session')."
            }
        },
        "required": ["action"]
    },
    "version": "1.0.0",
    "active": True
}


def execute(**kwargs: Any) -> Dict[str, Any]:
    action = str(kwargs.get("action", "snapshot")).lower().strip()
    name = str(kwargs.get("name") or "Main Workspace").strip()

    from core.flowstate import get_flowstate_manager
    manager = get_flowstate_manager()

    try:
        if action in ("snapshot", "save", "create"):
            res = manager.snapshot_workspace(name)
            apps_c = res.get("apps_count", 0)
            tabs_c = res.get("chrome_tabs_count", 0) + res.get("edge_tabs_count", 0)
            return {
                "success": True,
                "action": "snapshot",
                "name": name,
                "summary": f"Workspace '{name}' snapshotted successfully with {apps_c} apps and {tabs_c} browser tabs.",
                "voice_summary": f"Workspace '{name}' has been snapshotted, sir. It will persist even if you restart your PC.",
            }

        elif action in ("restore", "load", "switch"):
            res = manager.restore_workspace(name)
            if res.get("success"):
                items = res.get("restored_items", [])
                items_str = ", ".join(items[:4])
                return {
                    "success": True,
                    "action": "restore",
                    "name": name,
                    "summary": f"Restored {len(items)} items: {items_str}.",
                    "voice_summary": f"Restored workspace '{name}' with {len(items)} applications and browser tabs, sir.",
                }
            else:
                return {
                    "success": False,
                    "error": res.get("error", "Failed to restore workspace."),
                }

        elif action in ("list", "show"):
            workspaces = manager.list_workspaces()
            if not workspaces:
                return {
                    "success": True,
                    "workspaces": [],
                    "summary": "No saved FlowState restoration points found.",
                    "voice_summary": "You have no saved workspace restoration points right now, sir.",
                }
            names = [w["name"] for w in workspaces]
            return {
                "success": True,
                "workspaces": workspaces,
                "summary": f"Saved restoration points: {', '.join(names)}",
                "voice_summary": f"You have {len(workspaces)} saved workspaces: {', '.join(names[:3])}, sir.",
            }

        elif action in ("delete", "remove"):
            ok = manager.delete_workspace(name)
            return {
                "success": ok,
                "summary": f"Deleted workspace '{name}'." if ok else f"Workspace '{name}' not found.",
                "voice_summary": f"Removed workspace '{name}', sir." if ok else f"Could not find '{name}', sir.",
            }

        return {"error": f"Unknown FlowState action '{action}'."}
    except Exception as exc:
        return {"error": f"FlowState encountered an error: {str(exc)}"}
