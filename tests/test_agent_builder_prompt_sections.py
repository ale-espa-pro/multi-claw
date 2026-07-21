import os
import json
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
                section = AgentBuilder()._workflows_section()

        self.assertIn("Workflows creados:", section)
        self.assertIn("nombre: dashboard", section)
        self.assertIn("description: sin descripcion en README.md", section)

    def test_agent_runtime_params_are_loaded_from_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "agent_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "_runner": {
                            "max_iterations": 11,
                        },
                        "_defaults": {
                            "model": "default-model",
                            "reasoning": {"effort": "low", "summary": "auto"},
                            "parallel_tool_calls": False,
                            "max_iterations": 2,
                        },
                        "ExecutorAgent": {
                            "tools": ["read_file"],
                            "main": True,
                            "json_response": False,
                            "model": "executor-model",
                            "reasoning": {"effort": "high"},
                        },
                        "WebSearchAgent": {
                            "tools": [{"type": "web_search"}],
                            "json_response": True,
                            "parallel_tool_calls": True,
                            "max_iterations": 5,
                        },
                    }
                ),
                encoding="utf-8",
            )

            builder = AgentBuilder()
            builder.load_agents(str(config_path))

        self.assertEqual(
            builder.get_runner_config(),
            {"max_iterations": 11},
        )
        self.assertEqual(builder.get_agent_max_iterations("WebSearchAgent"), 5)
        self.assertEqual(builder.get_tools_for_agent("WebSearchAgent"), [{"type": "web_search"}])

        executor_params = builder.get_agent_params("ExecutorAgent")
        self.assertEqual(executor_params["model"], "executor-model")
        self.assertEqual(executor_params["reasoning"], {"effort": "high", "summary": "auto"})
        self.assertFalse(executor_params["parallel_tool_calls"])
        self.assertIsNone(executor_params["text"])
        self.assertIsNone(executor_params["provider"])
        self.assertEqual(executor_params["max_output_tokens"], 16_000)

        web_params = builder.get_agent_params("WebSearchAgent")
        self.assertEqual(web_params["model"], "default-model")
        self.assertTrue(web_params["parallel_tool_calls"])
        self.assertEqual(web_params["text"], {"format": {"type": "json_object"}})


if __name__ == "__main__":
    unittest.main()
