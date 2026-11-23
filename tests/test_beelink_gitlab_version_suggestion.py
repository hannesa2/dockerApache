import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "gitlab_version_suggestion", Path(__file__).resolve().parents[1] /
    "monitoring/suggest-beelink-gitlab-versions.py")
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)


class VersionSuggestionTest(unittest.TestCase):
    def test_patch_then_minor_and_matching_runner_patches(self):
        stops = [(19, 2), (19, 5), (19, 8), (19, 11)]
        gitlab = [(19, 5, 3), (19, 5, 8), (19, 6, 0), (19, 6, 1)]
        runners = [(19, 5, 2), (19, 5, 3), (19, 6, 0)]
        targets = PLAN.gitlab_targets((19, 5, 3), gitlab, stops)
        self.assertEqual(targets, [(19, 5, 8), (19, 6, 1)])
        self.assertEqual(PLAN.suggest_runners((19, 5, 3), targets, runners),
                         [(19, 5, 3), (19, 6, 0)])

    def test_existing_stop_and_intermediate_stops(self):
        stops = [(19, 2), (19, 5), (19, 8), (19, 11)]
        images = [(19, 5, 8), (19, 8, 4), (19, 9, 2)]
        self.assertEqual(PLAN.gitlab_targets((19, 5, 8), images, stops),
                         [(19, 8, 4), (19, 9, 2)])
        with self.assertRaisesRegex(ValueError, "required stop"):
            PLAN.gitlab_targets((19, 5, 8), [images[0], images[2]], stops)

    def test_no_update_and_no_runner_downgrade(self):
        self.assertEqual(PLAN.gitlab_targets((19, 6, 1), [(19, 6, 1)], [(19, 5)]), [])
        with self.assertRaisesRegex(ValueError, "without downgrade"):
            PLAN.suggest_runners((19, 6, 2), [(19, 6, 3)], [(19, 6, 0)])
        with self.assertRaisesRegex(ValueError, "newer"):
            PLAN.gitlab_targets((19, 6, 2), [(19, 6, 1)], [(19, 5)])

    def test_scope_and_official_stop_format_fail_closed(self):
        self.assertEqual(PLAN.required_stops(
            '---\n- major: 19\n  minor: 5\n  comments: "info"\n- major: 19\n  minor: 8\n'),
            [(19, 5), (19, 8)])
        for text in ("", "- major: 19\n", "- major: 19\n  minor: 5\n  condition: true\n",
                     "- major: 19\n  minor: 5\n- major: 19\n  minor: 5\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                PLAN.required_stops(text)
        with self.assertRaisesRegex(ValueError, "before 17.5"):
            PLAN.gitlab_targets((17, 4, 0), [(17, 5, 0)], [(17, 5)])
        with self.assertRaisesRegex(ValueError, "does not cover"):
            PLAN.gitlab_targets((20, 1, 0), [(20, 2, 0)], [(19, 11)])

    def test_registry_pagination_and_active_architectures(self):
        def image(name, arches, status="active"):
            return {"name": name, "tag_status": status,
                    "images": [{"os": "linux", "architecture": arch, "status": "active"}
                               for arch in arches]}
        pages = [
            {"results": [image("19.5.8-ce.0", ["amd64", "arm64"]),
                         image("19.5.9-ce.0", ["amd64"]),
                         image("19.5.99-rc1-ce.0", ["amd64", "arm64"])],
             "next": "https://hub.docker.com/next"},
            {"results": [image("19.6.1-ce.0", ["amd64", "arm64"]),
                         image("19.6.2-ce.0", ["amd64", "arm64"], "inactive")],
             "next": None},
        ]
        with patch.object(PLAN, "fetch", side_effect=[json.dumps(p) for p in pages]):
            self.assertEqual(PLAN.available_images(
                "gitlab-ce", "19.", r"([0-9]+\.[0-9]+\.[0-9]+)-ce\.0"),
                [(19, 5, 8), (19, 6, 1)])

    def test_registry_rejects_external_next_and_empty_results(self):
        for next_url, error in (("https://example.org/page", "pagination"), (None, "No matching")):
            with self.subTest(next_url=next_url), \
                    patch.object(PLAN, "fetch", return_value=json.dumps({
                        "results": [], "next": next_url})), \
                    self.assertRaisesRegex(ValueError, error):
                PLAN.available_images("gitlab-ce", "19.", r"([0-9.]+)-ce\.0")


if __name__ == "__main__":
    unittest.main()
