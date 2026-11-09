"""A replaceable model boundary, tested only with typed form commands."""

from typing import Protocol


class IntentExecutor(Protocol):
    def propose(self, command: dict) -> dict: ...


class ScriptedTestExecutor:
    name = "SCRIPTED_TEST_EXECUTOR_NO_LLM"

    def propose(self, command):
        if not isinstance(command, dict) or set(command) - {
            "action",
            "sid",
            "name",
            "budget",
            "revision",
            "preview_id",
        }:
            raise ValueError("INTENT_FIELDS_NOT_ALLOWED")
        if command.get("action") not in {
            "start",
            "read",
            "budget",
            "undo",
            "commit",
            "cancel_preview",
        }:
            raise ValueError("INTENT_ACTION_NOT_ALLOWED")
        # No natural-language parsing, provider text, or system instructions are accepted.
        return dict(command)


def dispatch(app, command, executor=None):
    intent = (executor or ScriptedTestExecutor()).propose(command)
    # Independent schema gate applies to a future real model's proposed tool call too.
    intent = ScriptedTestExecutor().propose(intent)
    return app.execute(**intent)
