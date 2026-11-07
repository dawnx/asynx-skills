from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

from asxlib import batches, cli, state, tasks
from asxlib.constants import DEFAULT_MODEL
from asxlib.errors import AsxError
from test_audit_regressions import FakeClient


class ResultClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.queried = []
        self.asset_attempts = []
        self.asset_count = 1
        self.failing_index = None

    def task(self, task_id):
        self.queried.append(task_id)
        result, request_id = super().task(task_id)
        result["result"]["assets"] = [
            {"index": index} for index in range(self.asset_count)
        ]
        return result, request_id

    def asset(self, task_id, index, url, *, metrics):
        self.asset_attempts.append((task_id, index))
        if index == self.failing_index:
            raise AsxError("download interrupted", code="network_error")
        return super().asset(task_id, index, url, metrics=metrics)


class TaskContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("ASYNX_")
        }
        environment.update({
            "ASYNX_CONFIG_PATH": str(self.root / "config.json"),
            "ASYNX_STATE_PATH": str(self.root / "state.db"),
            "ASYNX_CACHE_PATH": str(self.root / "cache.json"),
            "ASYNX_UPDATE_CHECK": "off",
        })
        env = patch.dict(os.environ, environment, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = ResultClient()

    def test_advanced_options_survive_batch_append_without_inventing_quality(self):
        created = batches.create_batch(
            self.client, operation="generate", prompt="first", model_selector=None,
            image_size=None, aspect_ratio=None, quality=None, count=1, output_format="svg",
            references=[], mask=None, total=1, name=None, output_dir=str(self.root),
            options={"size": "1536x1024", "svg": {"model": "test-svg", "reasoning": {"effort": "medium"}}},
        )
        batches.add_to_batch(
            self.client, created["id"], total=1, prompt="second", references=None,
            mask=None, count=None, image_size=None, aspect_ratio=None, quality=None,
            output_format=None, options={"background": "transparent"},
        )
        with state.connect_db() as connection:
            first = json.loads(connection.execute("SELECT template_json FROM batches WHERE id = ?", (created["id"],)).fetchone()[0])["input"]
            second = json.loads(connection.execute("SELECT body_json FROM batch_items WHERE batch_id = ? AND sequence = 2", (created["id"],)).fetchone()[0])["input"]
        self.assertNotIn("quality", second)
        self.assertEqual(second["size"], first["size"])
        self.assertEqual(second["svg"], first["svg"])
        self.assertEqual(second["background"], "transparent")
        self.assertNotIn("image_size", second)
        self.assertNotIn("aspect_ratio", second)
        # A ratio override replaces an inherited exact size without inventing a tier.
        batches.add_to_batch(
            self.client, created["id"], total=1, prompt="third", references=None,
            mask=None, count=None, image_size=None, aspect_ratio="16:9", quality=None,
            output_format=None, options={"svg": {"reasoning": {"effort": "high"}}},
        )
        with state.connect_db() as connection:
            third = json.loads(connection.execute(
                "SELECT body_json FROM batch_items WHERE batch_id = ? AND sequence = 3",
                (created["id"],),
            ).fetchone()[0])["input"]
        self.assertNotIn("size", third)
        self.assertNotIn("image_size", third)
        self.assertEqual(third["aspect_ratio"], "16:9")
        self.assertEqual(third["svg"], {"model": "test-svg", "reasoning": {"effort": "high"}})
        self.assertEqual(first["svg"], {"model": "test-svg", "reasoning": {"effort": "medium"}})
        # Persisted batch settings do not adopt a new default on continuation.
        with patch("asxlib.images.DEFAULT_SVG_AUTHOR_MODEL", "later-default"):
            batches.add_to_batch(
                self.client, created["id"], total=1, prompt="continued", references=None,
                mask=None, count=None, image_size=None, aspect_ratio=None, quality=None,
                output_format=None,
            )
        with state.connect_db() as connection:
            fourth = json.loads(connection.execute(
                "SELECT body_json FROM batch_items WHERE batch_id = ? AND sequence = 4",
                (created["id"],),
            ).fetchone()[0])["input"]
        self.assertEqual(fourth["svg"], first["svg"])

    def test_parameter_file_cannot_replace_transport_or_task_identity(self):
        from asxlib.image_options import options_from_args
        path = self.root / "params.json"
        path.write_text('{"base_url":"https://other.example"}')
        args = cli.parser().parse_args(["generate", "--prompt", "cat", "--params-file", str(path)])
        with self.assertRaises(AsxError):
            options_from_args(args)
        path.write_text('{"background":"opaque","output_compression":0}')
        args = cli.parser().parse_args(["generate", "--prompt", "cat", "--params-file", str(path), "--background", "transparent"])
        self.assertEqual(options_from_args(args), {"background": "transparent", "output_compression": 0})
        path.write_text(json.dumps({"background": "transparent", "svg": {
            "model": "json-model", "reasoning": {"effort": "medium"},
        }}))
        args = cli.parser().parse_args([
            "generate", "--prompt", "cat", "--params-file", str(path), "--svg-model", "cli-model",
        ])
        self.assertEqual(options_from_args(args), {"background": "transparent", "svg": {
            "model": "cli-model", "reasoning": {"effort": "medium"},
        }})
        args = cli.parser().parse_args([
            "generate", "--prompt", "cat", "--params-file", str(path), "--svg-reasoning-effort", "high",
        ])
        self.assertEqual(options_from_args(args)["svg"], {"model": "json-model", "reasoning": {"effort": "high"}})
        # An effort-only SVG command uses the built-in author without asking the caller.
        result, code = self.execute([
            "generate", "--prompt", "icon", "--output-format", "svg",
            "--svg-reasoning-effort", "high", "--detach", "--output-dir", str(self.root),
        ])
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(self.client.submitted[-1][0]["input"]["svg"], {
            "model": "gpt-6.1-sol", "reasoning": {"effort": "high"},
        })
        for invalid in (
            {"model": ""},
            {"model": None},
            {"model": "model", "reasoning": "high"},
            {"model": "model", "reasoning": {"effort": 1}},
            {"model": "model", "reasoning": {"effort": "high", "secret": "value"}},
            {"model": "model", "endpoint": "https://other.example"},
        ):
            path.write_text(json.dumps({"svg": invalid}))
            args = cli.parser().parse_args(["generate", "--prompt", "cat", "--params-file", str(path)])
            with self.subTest(svg=invalid), self.assertRaises(AsxError):
                options_from_args(args)

    def test_batch_format_change_drops_inherited_options_but_preserves_explicit_values(self):
        # Avoid sending stale format settings while retaining this request's intent.
        for source_format, options in (
            ("gif", {"frame_count": 9, "frame_duration_ms": 200}),
            ("svg", {"svg": {"model": "test-svg"}}),
            ("webp", {"output_compression": 75}),
        ):
            with self.subTest(source_format=source_format):
                created = batches.create_batch(
                    self.client, operation="generate", prompt="first",
                    model_selector="gpt-image-2.6-sunburst", image_size=None,
                    aspect_ratio=None, quality=None, count=1, output_format=source_format,
                    references=[], mask=None, total=1, name=None, output_dir=str(self.root),
                    options=options,
                )
                for explicit in ({}, options):
                    batches.add_to_batch(
                        self.client, created["id"], total=1, prompt="next", references=None,
                        mask=None, count=None, image_size=None, aspect_ratio=None, quality=None,
                        output_format="png", options=explicit,
                    )
                with state.connect_db() as connection:
                    bodies = [json.loads(row[0])["input"] for row in connection.execute(
                        "SELECT body_json FROM batch_items WHERE batch_id = ? AND sequence > 1 ORDER BY sequence",
                        (created["id"],),
                    )]
                    template = json.loads(connection.execute(
                        "SELECT template_json FROM batches WHERE id = ?", (created["id"],),
                    ).fetchone()[0])["input"]
                self.assertEqual(bodies[0]["output_format"], "png")
                for key, value in options.items():
                    self.assertNotIn(key, bodies[0])
                    self.assertEqual(bodies[1][key], value)
                    self.assertEqual(template[key], value)
        self.assertEqual(self.client.submitted, [])

    def test_batch_replacing_original_requires_matching_overlay_before_adding_items(self):
        # Same-size replacement must not silently reuse another image's annotations.
        from PIL import Image
        original = self.root / "original.png"
        replacement = self.root / "replacement.png"
        layer = self.root / "layer.png"
        Image.new("RGB", (4, 4), "red").save(original)
        Image.new("RGB", (4, 4), "blue").save(replacement)
        overlay = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
        overlay.putpixel((0, 0), (255, 0, 0, 255))
        overlay.save(layer)
        for kind in ("markup", "mask"):
            with self.subTest(kind=kind):
                created = batches.create_batch(
                    self.client, operation="edit", prompt="first", model_selector=None,
                    image_size=None, aspect_ratio=None, quality=None, count=1, output_format="png",
                    references=[str(original)], mask=str(layer) if kind == "mask" else None,
                    total=1, name=None, output_dir=str(self.root),
                    options={"markup": str(layer)} if kind == "markup" else None,
                )
                arguments = {"total": 1, "prompt": "next", "references": [str(replacement)],
                             "mask": None, "count": None, "image_size": None, "aspect_ratio": None,
                             "quality": None, "output_format": None}
                with self.assertRaises(AsxError) as raised:
                    batches.add_to_batch(self.client, created["id"], **arguments)
                self.assertEqual(raised.exception.code, f"batch_{kind}_required")
                self.assertEqual(batches.batch_status(created["id"])["batch"]["total"], 1)
                # Keeping the original still permits a prompt-only continuation.
                batches.add_to_batch(self.client, created["id"], **{**arguments, "references": None})
                explicit = {"mask": str(layer)} if kind == "mask" else {"options": {"markup": str(layer)}}
                batches.add_to_batch(self.client, created["id"], **{**arguments, **explicit})
                self.assertEqual(batches.batch_status(created["id"])["batch"]["total"], 3)
        self.assertEqual(self.client.submitted, [])

    def test_batch_format_change_does_not_silently_switch_default_model(self):
        created = batches.create_batch(
            self.client, operation="generate", prompt="first", model_selector=None,
            image_size=None, aspect_ratio=None, quality=None, count=1, output_format="png",
            references=[], mask=None, total=1, name=None, output_dir=str(self.root),
        )
        for output_format in ("svg", "gif"):
            with self.subTest(output_format=output_format), self.assertRaises(AsxError) as raised:
                batches.add_to_batch(
                    self.client, created["id"], total=1, prompt=None, references=None,
                    mask=None, count=None, image_size=None, aspect_ratio=None, quality=None,
                    output_format=output_format,
                )
            self.assertEqual(raised.exception.code, "batch_model_format_conflict")
        snapshot = batches.batch_status(created["id"])["batch"]
        self.assertEqual(snapshot["model"], DEFAULT_MODEL)
        self.assertEqual(snapshot["total"], 1)
        self.assertEqual(self.client.submitted, [])
        # Prompt-only append keeps omitted dimensions; explicit size removes inherited fields.
        for options in ({}, {"size": "1000x1000"}):
            batches.add_to_batch(
                self.client, created["id"], total=1, prompt="next", references=None,
                mask=None, count=None, image_size=None, aspect_ratio=None, quality=None,
                output_format=None, options=options,
            )
        with state.connect_db() as connection:
            inputs = [json.loads(row[0])["input"] for row in connection.execute(
                "SELECT body_json FROM batch_items WHERE batch_id = ? AND sequence > 1 ORDER BY sequence",
                (created["id"],),
            )]
        for item in inputs:
            self.assertNotIn("image_size", item)
            self.assertNotIn("aspect_ratio", item)
        self.assertNotIn("size", inputs[0])
        self.assertEqual(inputs[1]["size"], "1000x1000")

    def intent(self, name, *, remote_id=None, status="intent"):
        with state.connect_db() as connection:
            row = state.create_task_intent(
                connection,
                operation="generate",
                model=DEFAULT_MODEL,
                idempotency_key=f"asx-contract-{name}",
                request={
                    "task_type": "image.generate", "model": DEFAULT_MODEL,
                    "input": {"prompt": name, "aspect_ratio": "1:1"},
                },
                output_dir=str(self.root / name),
                base_url=self.client.base_url,
                local_id=f"local_{name}",
                status=status,
            )
            if remote_id:
                row = state.bind_remote_task(
                    connection, row["local_id"], remote_id,
                    status=status,
                    remote={"id": remote_id, "status": status},
                )
            return row

    def execute(self, arguments):
        with patch.object(cli, "_client", return_value=self.client):
            return cli.execute(cli.parser().parse_args(arguments))

    def test_known_rejection_cannot_be_recovered_into_new_generation(self):
        # Known admission rejections must not become repeated paid generation attempts.
        for code, status in (("group_model_unavailable", 503), ("unsupported_image_size", 422)):
            error = AsxError("rejected", code=code, http_status=status)
            with self.subTest(code=code), patch.object(self.client, "submit", side_effect=error) as submit:
                with self.assertRaises(AsxError) as caught:
                    self.execute(["generate", "--prompt", "cat", "--detach", "--output-dir", str(self.root)])
                local_id = caught.exception.details["local_id"]
                self.assertNotIn("next_action_args", caught.exception.details)
                self.assertEqual(tasks.local_task_status(local_id)["task"]["status"], "failed")
                tasks.recover_tasks(self.client, local_id)
                self.assertEqual(submit.call_count, 1)

    def test_unknown_503_keeps_idempotent_recovery_available(self):
        error = AsxError("unavailable", code="http_503", http_status=503)
        with patch.object(self.client, "submit", side_effect=error), self.assertRaises(AsxError) as caught:
            self.execute(["generate", "--prompt", "cat", "--detach", "--output-dir", str(self.root)])
        local_id = caught.exception.details["local_id"]
        self.assertEqual(caught.exception.details["next_action_args"], ["task", "recover", local_id])
        self.assertEqual(tasks.local_task_status(local_id)["task"]["status"], "submitting")

    def test_refresh_by_local_or_remote_id_downloads_without_submission(self):
        row = self.intent("bound", remote_id="task_bound", status="queued")
        for identifier in (row["local_id"], "task_bound"):
            with self.subTest(identifier=identifier):
                result, code = self.execute(["task", "status", identifier, "--refresh"])
                self.assertEqual(code, 0)
                self.assertTrue(result["ok"])
                task = result["task"]
                self.assertEqual(task["local_id"], row["local_id"])
                self.assertEqual(task["task_id"], "task_bound")
                self.assertEqual(task["status"], "succeeded")
                self.assertEqual(len(task["files"]), 1)
                self.assertEqual(task["images"][0]["width"], 4)
                self.assertTrue(Path(task["files"][0]).is_file())
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.client.queried, ["task_bound", "task_bound"])
        self.assertEqual(self.client.asset_attempts, [("task_bound", 0)])

    def test_local_snapshot_needs_no_client_and_does_not_refresh(self):
        row = self.intent("snapshot", remote_id="task_snapshot", status="queued")
        with patch.object(cli, "_client", side_effect=AssertionError("must remain offline")):
            result, code = cli.execute(cli.parser().parse_args([
                "task", "status", row["local_id"],
            ]))
        self.assertEqual(code, 0)
        self.assertEqual(result["task"]["status"], "queued")

    def test_refresh_unsubmitted_intent_only_offers_scoped_recovery(self):
        row = self.intent("unsubmitted")
        result, code = self.execute(["task", "status", row["local_id"], "--refresh"])
        self.assertEqual(code, 0)
        self.assertEqual(result["task"]["status"], "intent")
        self.assertEqual(
            result["task"]["next_action_args"], ["task", "recover", row["local_id"]],
        )
        self.assertEqual(self.client.queried, [])
        self.assertEqual(self.client.submitted, [])

    def test_unknown_local_refresh_does_not_query_remote_or_submit(self):
        self.intent("unrelated")
        with self.assertRaises(AsxError) as caught:
            self.execute(["task", "status", "local_missing", "--refresh"])
        self.assertEqual(caught.exception.code, "task_not_found")
        self.assertEqual(self.client.queried, [])
        self.assertEqual(self.client.submitted, [])

    def test_untracked_remote_refresh_downloads_without_submission(self):
        with patch.object(tasks, "DEFAULT_OUTPUT_DIR", str(self.root / "untracked")):
            result, code = self.execute(["task", "status", "task_untracked", "--refresh"])
        self.assertEqual(code, 0)
        self.assertEqual(result["task"]["task_id"], "task_untracked")
        self.assertEqual(len(result["task"]["files"]), 1)
        self.assertTrue(Path(result["task"]["files"][0]).is_file())
        self.assertEqual(self.client.submitted, [])

    def test_refresh_rejects_unknown_remote_status(self):
        self.client.statuses["task_unknown_status"] = "unexpected_state"
        with self.assertRaises(AsxError) as caught:
            self.execute(["task", "status", "task_unknown_status", "--refresh"])
        self.assertEqual(caught.exception.code, "invalid_task_status")
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.client.asset_attempts, [])

    def test_scoped_recovery_reuses_key_and_leaves_other_intents_untouched(self):
        chosen = self.intent("chosen")
        other = self.intent("other")
        result, code = self.execute(["task", "recover", chosen["local_id"], "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["local_id"], chosen["local_id"])
        self.assertEqual(self.client.submitted, [(chosen["request"], chosen["idempotency_key"])])
        unchanged = tasks.local_task_status(other["local_id"])["task"]
        self.assertEqual(unchanged["status"], "intent")
        self.assertNotIn("task_id", unchanged)

    def test_scoped_recovery_accepts_remote_id_without_resubmitting(self):
        row = self.intent("existing", remote_id="task_existing", status="queued")
        self.intent("pending")
        result = tasks.recover_tasks(self.client, "task_existing")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["local_id"], row["local_id"])
        self.assertEqual(self.client.submitted, [])

    def test_scoped_recovery_preserves_paused_batch_without_submitting(self):
        batch_id = batches.create_batch(
            self.client, operation="generate", prompt="cat", model_selector=None,
            image_size=None, aspect_ratio=None, quality=None, count=1,
            output_format=None, references=[], mask=None, total=1, name=None,
            output_dir=str(self.root / "paused_batch"),
        )["id"]
        self.client.reject_submit = True
        batches.poll_batch(self.client, batch_id)
        local_id = tasks.local_tasks_payload()["tasks"][0]["local_id"]
        batches.set_batch_paused(batch_id, True)
        self.client.reject_submit = False
        result, code = self.execute(["task", "recover", local_id])
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["tasks"]), 1)
        task = result["tasks"][0]
        self.assertEqual(task["local_id"], local_id)
        self.assertTrue(task["paused"])
        self.assertIn("batch_paused", [warning["code"] for warning in task["warnings"]])
        self.assertEqual(task["next_action_args"], ["batch", "status", batch_id, "--refresh"])
        self.assertEqual(batches.batch_status(batch_id)["batch"]["status"], "paused")
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.client.queried, [])
        self.assertNotIn("task_id", tasks.local_task_status(local_id)["task"])

    def test_missing_recovery_id_never_falls_back_to_all_tasks(self):
        self.intent("pending")
        for identifier in ("local_missing", "task_missing"):
            with self.subTest(identifier=identifier):
                with self.assertRaises(AsxError) as caught:
                    self.execute(["task", "recover", identifier])
                self.assertEqual(caught.exception.code, "task_not_found")
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.client.queried, [])

    def test_recovery_does_not_resubmit_failed_or_canceled_records(self):
        for status in ("failed", "timeout", "canceled"):
            for bound in (False, True):
                with self.subTest(status=status, bound=bound):
                    name = f"{status}_{bound}"
                    row = self.intent(
                        name, status=status, remote_id=f"task_{name}" if bound else None,
                    )
                    result, code = self.execute(["task", "recover", row["local_id"]])
                    self.assertEqual(code, 4)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["tasks"][0]["status"], status)
                    self.assertNotIn("next_action_args", result["tasks"][0])
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.client.queried, [])

    def test_partial_download_recovers_only_missing_asset_without_regeneration(self):
        row = self.intent("partial", remote_id="task_partial", status="queued")
        self.client.asset_count = 2
        self.client.failing_index = 1
        result, code = self.execute(["task", "status", row["local_id"], "--refresh"])
        self.assertEqual(code, 4)
        self.assertFalse(result["ok"])
        task = result["task"]
        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(task["download_status"], "failed")
        self.assertEqual(task["error"]["phase"], "download")
        self.assertEqual(task["next_action_args"], ["wait", row["local_id"]])
        self.assertEqual(len(task["files"]), 1)
        self.assertEqual(len(task["images"]), 1)
        first_path = Path(task["files"][0])
        first_contents = first_path.read_bytes()
        self.client.failing_index = None
        completed, code = self.execute(task["next_action_args"])
        self.assertEqual(code, 0)
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["download_status"], "downloaded")
        self.assertEqual(len(completed["files"]), 2)
        self.assertEqual(len(completed["images"]), 2)
        self.assertEqual(first_path.read_bytes(), first_contents)
        self.assertTrue(all(Path(path).is_file() for path in completed["files"]))
        self.assertNotIn("next_action_args", completed)
        self.assertEqual(self.client.asset_attempts, [
            ("task_partial", 0), ("task_partial", 1), ("task_partial", 1),
        ])
        self.assertEqual(self.client.submitted, [])

    def test_wait_on_unsubmitted_intent_offers_recovery_without_submitting(self):
        row = self.intent("awaiting_submit")
        with self.assertRaises(AsxError) as caught:
            self.execute(["wait", row["local_id"]])
        self.assertEqual(caught.exception.code, "task_not_submitted")
        self.assertEqual(
            caught.exception.details["next_action_args"], ["task", "recover", row["local_id"]],
        )
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.client.queried, [])

    def test_legacy_recovery_without_identifier_retains_explicit_limit(self):
        parsed = cli.parser().parse_args(["task", "recover", "--limit", "3"])
        self.assertIsNone(parsed.task_id)
        self.assertEqual(parsed.limit, 3)


if __name__ == "__main__":
    unittest.main()
