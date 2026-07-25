import tempfile
import unittest
from pathlib import Path

from backend import db
from backend.config import settings
from backend.events import EventBus, THINKING


class ChatRunPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._old_data_dir = settings.data_dir
        self._temp = tempfile.TemporaryDirectory()
        settings.data_dir = Path(self._temp.name)
        db.init_db()

    def tearDown(self):
        settings.data_dir = self._old_data_dir
        self._temp.cleanup()

    def test_failed_run_and_trace_survive_conversation_reload(self):
        conv_id = db.create_conversation("failure case")
        user_id = db.add_message(conv_id, "user", "question")
        assistant_id = db.add_message(
            conv_id, "assistant", "", status="running",
        )
        run_id = db.create_turn_run(
            conv_id,
            user_id,
            assistant_id,
            route_requested="mix",
            request={"question": "question", "route": "mix", "deep": True},
        )
        trace = [{
            "type": "thinking",
            "data": {"text": "retrieving", "status": "failed"},
            "ts": 1.0,
        }]
        error = {"code": "ProviderError", "message": "safe", "stage": "retrieving"}
        db.update_message(
            assistant_id,
            trace=trace,
            status="failed",
            error=error,
            run_id=run_id,
        )
        db.update_turn_run(
            run_id,
            status="failed",
            stage="retrieving",
            error_code="ProviderError",
            error_message="safe",
            trace=trace,
            finished=True,
        )

        conversation = db.get_conversation(conv_id)
        assistant = conversation["messages"][-1]
        run = db.get_turn_run(run_id)
        self.assertEqual(assistant["status"], "failed")
        self.assertEqual(assistant["error"]["stage"], "retrieving")
        self.assertEqual(assistant["trace"], trace)
        self.assertEqual(run["status"], "failed")
        self.assertTrue(run["finished_at"])
        self.assertTrue(run["request"]["deep"])

    def test_retry_run_reuses_user_message_and_links_attempts(self):
        conv_id = db.create_conversation("retry case")
        user_id = db.add_message(conv_id, "user", "question")
        first_assistant = db.add_message(conv_id, "assistant", "", status="failed")
        first_run = db.create_turn_run(
            conv_id, user_id, first_assistant, route_requested="mix",
        )
        retry_assistant = db.add_message(conv_id, "assistant", "", status="running")
        retry_run = db.create_turn_run(
            conv_id,
            user_id,
            retry_assistant,
            route_requested="mix",
            retry_of_run_id=first_run,
        )
        loaded = db.get_turn_run(retry_run)
        self.assertEqual(loaded["user_message_id"], user_id)
        self.assertEqual(loaded["retry_of_run_id"], first_run)

    def test_event_bus_can_persist_each_trace_event(self):
        emitted = []
        bus = EventBus("run-test", on_emit=lambda event: emitted.append(event.to_dict()))
        bus.emit(THINKING, {"text": "step"})
        self.assertEqual(emitted[0]["task_id"], "run-test")
        self.assertEqual(emitted[0]["data"]["text"], "step")


if __name__ == "__main__":
    unittest.main()
