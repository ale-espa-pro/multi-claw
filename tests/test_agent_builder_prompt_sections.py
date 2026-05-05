import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.agent_builder import AgentBuilder


class AgentBuilderPromptSectionTests(unittest.TestCase):
    def test_crons_section_includes_directory_name_and_readme_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cron_dir = Path(tmpdir) / "daily_summary"
            cron_dir.mkdir()
            (cron_dir / "README.md").write_text(
                "description: Genera resumen diario de tareas pendientes.\n\n# Detalles",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"CRONS_PATH": tmpdir}):
                section = AgentBuilder()._crons_section()

        self.assertIn("Agentes Cron programados:", section)
        self.assertIn("nombre: daily_summary", section)
        self.assertIn(f"ruta: {cron_dir}", section)
        self.assertIn("description: Genera resumen diario de tareas pendientes.", section)

    def test_workflows_section_marks_missing_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow_dir = Path(tmpdir) / "dashboard"
            workflow_dir.mkdir()
            (workflow_dir / "README.md").write_text("# Dashboard\nSin campo.", encoding="utf-8")

            with patch.dict(os.environ, {"WORKFLOW_PATH": tmpdir}):
                section = AgentBuilder()._worflows_section()

        self.assertIn("Workflows creados:", section)
        self.assertIn("nombre: dashboard", section)
        self.assertIn("description: sin descripcion en README.md", section)


if __name__ == "__main__":
    unittest.main()
