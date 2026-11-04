"""
core/skills_engine.py
=====================
Saturn - Universal Community & Markdown Skills Engine.

Compatible with Antigravity, Claude Code, and Codex Skill specifications.
Enables Saturn to load, discover, install, and execute any markdown-based
SKILL.md definition from the internet, workspace, or community repositories.

Directory Structure:
  skills/
    <skill_name>/
      SKILL.md       (YAML frontmatter + step-by-step instructions)
      scripts/       (optional helper scripts: py, ps1, sh, js)
      resources/     (optional templates, schemas, assets)
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.user_paths import get_user_data_dir

logger = logging.getLogger("SkillsEngine")

WORKSPACE_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
APPDATA_SKILLS_DIR = get_user_data_dir() / "community_skills"


class SkillDefinition:
    """Represents a discovered markdown skill."""

    def __init__(
        self,
        name: str,
        description: str,
        instructions: str,
        path: Path,
        metadata: Dict[str, Any],
        has_scripts: bool = False
    ):
        self.name = name
        self.description = description
        self.instructions = instructions
        self.path = path
        self.metadata = metadata
        self.has_scripts = has_scripts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "path": str(self.path),
            "metadata": self.metadata,
            "has_scripts": self.has_scripts
        }


class UniversalSkillsEngine:
    """Discovers, parses, and activates markdown and internet skills for Saturn."""

    _instance: Optional[UniversalSkillsEngine] = None

    def __init__(self):
        WORKSPACE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        APPDATA_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        self._skills: Dict[str, SkillDefinition] = {}
        self.reload_skills()

    @classmethod
    def get_instance(cls) -> UniversalSkillsEngine:
        if cls._instance is None:
            cls._instance = UniversalSkillsEngine()
        return cls._instance

    def reload_skills(self) -> int:
        """Scans all search paths for SKILL.md definitions."""
        self._skills.clear()
        search_dirs = [WORKSPACE_SKILLS_DIR, APPDATA_SKILLS_DIR]

        for base_dir in search_dirs:
            if not base_dir.exists():
                continue
            for item in base_dir.iterdir():
                skill_file: Optional[Path] = None
                if item.is_dir():
                    candidate = item / "SKILL.md"
                    if candidate.exists():
                        skill_file = candidate
                elif item.is_file() and item.suffix.lower() == ".md":
                    skill_file = item

                if skill_file:
                    skill = self._parse_skill_file(skill_file)
                    if skill:
                        self._skills[skill.name.lower()] = skill

        logger.info(f"[SkillsEngine] Discovered {len(self._skills)} skills across workspace and user directories.")
        return len(self._skills)

    def _parse_skill_file(self, file_path: Path) -> Optional[SkillDefinition]:
        """Parses YAML frontmatter and Markdown body from SKILL.md."""
        try:
            raw = file_path.read_text(encoding="utf-8", errors="replace")
            # Parse YAML frontmatter between --- and ---
            match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, flags=re.DOTALL)
            name = file_path.parent.name if file_path.name == "SKILL.md" else file_path.stem
            description = ""
            metadata: Dict[str, Any] = {}
            body = raw

            if match:
                yaml_block = match.group(1)
                body = match.group(2).strip()
                for line in yaml_block.splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        k = k.strip().lower()
                        v = v.strip().strip('"').strip("'")
                        metadata[k] = v
                name = metadata.get("name", name)
                description = metadata.get("description", "")
            else:
                # Fallback: extract first heading
                for line in raw.splitlines():
                    if line.startswith("#"):
                        description = line.lstrip("#").strip()
                        break

            has_scripts = (file_path.parent / "scripts").exists() and any((file_path.parent / "scripts").iterdir())

            return SkillDefinition(
                name=name,
                description=description,
                instructions=body,
                path=file_path,
                metadata=metadata,
                has_scripts=has_scripts
            )
        except Exception as e:
            logger.error(f"[SkillsEngine] Failed to parse {file_path}: {e}")
            return None

    def list_skills(self) -> List[Dict[str, Any]]:
        """Returns summary metadata of all discovered skills."""
        return [skill.to_dict() for skill in self._skills.values()]

    def get_skill(self, name: str) -> Optional[SkillDefinition]:
        """Looks up a skill by exact name or normalized snake_case."""
        clean = name.strip().lower().replace("-", "_")
        for key, s in self._skills.items():
            if key == clean or key.replace("-", "_") == clean or s.name.lower() == clean:
                return s
        return None

    def search_skills(self, query: str) -> List[Dict[str, Any]]:
        """Finds skills matching keyword query in name, description, or tags."""
        q = query.lower()
        results = []
        for s in self._skills.values():
            if q in s.name.lower() or q in s.description.lower() or q in s.instructions.lower()[:500]:
                results.append(s.to_dict())
        return results

    def install_skill(self, skill_name: str, content: str) -> Dict[str, Any]:
        """Installs a new skill into workspace skills/ directory."""
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", skill_name).strip("_").lower()
        target_dir = WORKSPACE_SKILLS_DIR / slug
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / "SKILL.md"
        target_file.write_text(content, encoding="utf-8")
        self.reload_skills()
        return {
            "success": True,
            "skill_name": slug,
            "path": str(target_file),
            "total_skills": len(self._skills)
        }

    def execute_skill(self, name: str, user_query: str = "") -> Dict[str, Any]:
        """
        Retrieves the skill instructions formatted for high-performance agent execution.
        """
        skill = self.get_skill(name)
        if not skill:
            return {
                "success": False,
                "error": f"Skill '{name}' not found. Available skills: {list(self._skills.keys())}"
            }

        return {
            "success": True,
            "skill_name": skill.name,
            "description": skill.description,
            "instructions": skill.instructions,
            "metadata": skill.metadata,
            "context_prompt": (
                f"### [ACTIVATED SKILL: {skill.name.upper()}]\n"
                f"Description: {skill.description}\n\n"
                f"#### Core Directives & Protocol:\n"
                f"{skill.instructions}\n\n"
                f"User Intent: {user_query or 'Execute skill protocol'}"
            )
        }
