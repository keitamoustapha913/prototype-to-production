import unittest

from scripts import cross_screen


class CrossScreenTests(unittest.TestCase):
    def test_affected_scope_follows_each_consumer_and_its_dependents(self):
        manifest = {"slices": {
            "origin": {}, "settings": {}, "projects": {},
            "settings-child": {"dependencies": ["settings"]},
            "settings-grandchild": {"dependencies": ["settings-child"]},
            "independent": {},
        }}
        plan = {"slice_id": "origin", "screens": [
            {"slice_ids": ["settings"]}, {"slice_ids": ["projects"]},
        ]}
        self.assertEqual(
            cross_screen.affected_slices(manifest, plan),
            {"origin", "settings", "projects", "settings-child", "settings-grandchild"},
        )


class CrossScreenWorkflowTests(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from scripts import project_state

        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = self.root / ".artifacts" / "prototype-to-production" / "fixture"
        self.directory.mkdir(parents=True)
        self.component = self.root / "menu.css"
        self.component.write_text("#menu {background: red}")
        self.manifest = {
            "stage": "implement", "initiative_id": "fixture", "scope": "full",
            "initiative_mode": "prototype", "next_action": "Implement menu",
            "active_approval_id": "approval-1", "approved_version": "prototype-1",
            "approvals": {"approval-1": {"visual_cases": []}},
            "slices": {
                "erp-slice": {"status": "ready", "prototype_version": "prototype-1", "owner": "builder",
                              "allowed_paths": ["menu.css"]},
                "settings-slice": {"status": "accepted"},
                "dependent-slice": {"status": "ready", "dependencies": ["settings-slice"]},
                "independent-slice": {"status": "ready"},
            },
        }
        project_state.atomic_json(self.directory / "manifest.json", self.manifest)
        (self.directory / "decisions.jsonl").write_text("")
        self.spec = {
            "change_id": "menu-layout", "summary": "Update the shared menu",
            "component_paths": ["menu.css"],
            "screens": [self.screen("erp", "erp-slice"), self.screen("settings", "settings-slice")],
        }

    def screen(self, name, slice_id):
        return {"route": "/" + name + ".html", "slice_ids": [slice_id],
                "reason": "Imports menu.css", "source_paths": [name + ".html"], "cases": [{
                    "case_id": name + "-open", "reason": "Exercise keyboard opening",
                    "expectation": "Tab then Enter opens the menu with focus retained",
                    "state": {"route": "/" + name + ".html", "theme": "light", "data": "sample menu",
                              "interaction": "opened by keyboard", "active_control": "summary", "open_menus": ["menu"]},
                    "renderCase": {"route": "/" + name + ".html", "viewport": {"width": 390, "height": 844},
                                   "ready_selector": "#menu", "setup": [
                                       {"action": "press", "key": "Tab"}, {"action": "press", "key": "Enter"}]},
                    "interaction_assertions": [{"selector": "summary", "probe": "focused", "expected": True},
                                               {"selector": "section", "probe": "visible", "expected": True}],
                }]}

    def run_action(self, action, payload):
        return cross_screen.change(self.directory, "erp-slice", action, payload)

    def blockers(self, key):
        from scripts import project_state

        return project_state.cross_screen_blockers(self.directory, project_state.manifest_at(self.directory), key)

    def test_cli_lists_registered_groups_and_current_status(self):
        import json
        from pathlib import Path
        import subprocess
        import sys

        cli = Path(__file__).resolve().parents[1] / "scripts" / "project_state.py"
        command = [sys.executable, str(cli), "cross-screen", "--project-root", str(self.root),
                   "--initiative-id", "fixture"]
        input_file = self.directory / "plan.json"
        input_file.write_text(json.dumps(self.spec))
        planned = subprocess.run([*command, "--action", "plan", "--slice-id", "erp-slice",
                                  "--input-file", str(input_file)], capture_output=True, text=True)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        listed = subprocess.run([*command, "--action", "list"], capture_output=True, text=True)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        changes = json.loads(listed.stdout)
        self.assertEqual(changes[0]["change_id"], "menu-layout")
        self.assertEqual(changes[0]["current_status"], "pending")
        self.assertEqual(changes[0]["affected_slices"], ["dependent-slice", "erp-slice", "settings-slice"])

    def test_submitted_ui_work_requires_shared_path_classification_and_matching_plan(self):
        from scripts import project_state

        report = {"slice_id": "erp-slice", "owner": "builder", "status": "submitted",
                  "summary": "Adjusted the dropdown", "implementation_paths": ["menu.css"]}
        report_file = self.directory / "report-input.json"

        def submit():
            project_state.atomic_json(report_file, report)
            project_state.worker_report(self.directory, "erp-slice", "builder", str(report_file))

        with self.assertRaisesRegex(project_state.StateError, "requires shared_ui_paths"):
            submit()
        report["shared_ui_paths"] = ["menu.css"]
        with self.assertRaisesRegex(ValueError, "pre-edit cross-screen plan"):
            submit()
        self.run_action("plan", self.spec)
        report["shared_ui_paths"] = []
        with self.assertRaisesRegex(ValueError, "known shared UI"):
            submit()
        report["shared_ui_paths"] = ["menu.css"]
        submit()
        self.assertTrue(self.blockers("erp-slice"))
        self.run_action("abandon", {"change_id": "menu-layout", "reason": "No edit needed"})
        self.assertFalse(self.blockers("erp-slice"))
        report["shared_ui_paths"] = []
        with self.assertRaisesRegex(ValueError, "known shared UI"):
            submit()

    def test_plan_requires_ownership_and_holds_only_consumers(self):
        from copy import deepcopy
        from scripts import project_state

        invalid = deepcopy(self.spec)
        invalid["component_paths"] = ["erp.html"]
        with self.assertRaisesRegex(ValueError, "within slice ownership"):
            self.run_action("plan", invalid)
        plan = self.run_action("plan", self.spec)
        self.assertEqual(plan["status"], "pending")
        self.assertTrue(self.blockers("erp-slice"))
        self.assertTrue(self.blockers("settings-slice"))
        self.assertTrue(self.blockers("dependent-slice"))
        self.assertFalse(self.blockers("independent-slice"))
        with self.assertRaisesRegex(project_state.StateError, "menu-layout is pending"):
            project_state.require_cross_screen(self.directory, project_state.manifest_at(self.directory), "dependent-slice")
        with self.assertRaisesRegex(ValueError, "every affected case"):
            self.run_action("record", {"change_id": "menu-layout", "cases": []})
        self.component.write_text("#menu {background: blue}")
        with self.assertRaisesRegex(ValueError, "original planned source"):
            self.run_action("capture", {"change_id": "menu-layout", "case_id": "erp-open", "phase": "before"})
        with self.assertRaisesRegex(ValueError, "restoration"):
            self.run_action("abandon", {"change_id": "menu-layout", "reason": "Changed direction"})

    def test_coverage_extension_retains_original_cases_and_rejects_edited_source(self):
        from copy import deepcopy

        original = self.run_action("plan", self.spec)
        expanded = deepcopy(self.spec)
        expanded["screens"].append(self.screen("projects", "independent-slice"))
        self.component.write_text("#menu {background: green}")
        with self.assertRaisesRegex(ValueError, "original scoped source"):
            self.run_action("extend", expanded)
        self.component.write_text("#menu {background: red}")
        plan = self.run_action("extend", expanded)
        self.assertEqual(plan["created_at"], original["created_at"])
        self.assertEqual(len(plan["screens"]), 3)
        self.assertTrue(self.blockers("independent-slice"))
        reduced = deepcopy(expanded)
        reduced["screens"][0]["cases"][0]["renderCase"]["viewport"]["width"] = 500
        with self.assertRaisesRegex(ValueError, "original case definitions"):
            self.run_action("extend", reduced)

    def test_real_browser_keeps_original_evidence_through_regression_and_repair(self):
        from functools import partial
        from http.server import ThreadingHTTPServer
        import shutil
        from threading import Thread
        from scripts import project_state, visual_integrity
        from fixtures.blueprint_browser_probe import ItemsHandler

        browser = shutil.which("google-chrome") or shutil.which("chromium")
        if not browser:
            self.skipTest("Chromium is unavailable")
        page = ('<link rel="stylesheet" href="/menu.css"><details id="menu"><summary>Open</summary>'
                '<section>Menu choices</section></details>')
        for name in ("erp", "settings"):
            (self.root / (name + ".html")).write_text(("<title>Settings</title>" if name == "settings" else "") + page)
        self.run_action("plan", self.spec)

        def capture(server, name, phase):
            return self.run_action("capture", {"change_id": "menu-layout", "case_id": name + "-open",
                "phase": phase, "browser": browser, "appUrl": f"http://127.0.0.1:{server.server_port}"})

        def row(name, receipt=None, interaction="pass"):
            results = {"visual": "pass", "interaction": interaction, "blueprint": "pass"}
            if receipt is None:
                results = dict.fromkeys(results, "not_checked")
            value = {"case_id": name + "-open", "checks": {
                check: {"result": result, "note": "Reviewed the captured UI and keyboard outcome"}
                for check, result in results.items()
            }}
            if receipt is None:
                return {**value, "unavailable_reason": "Settings fixture could not be loaded"}
            return {**value, "capture_id": receipt["capture_id"]}

        request = {"change_id": "menu-layout"}
        baseline = {}
        finding = {"finding_id": "settings-menu-hidden", "case_id": "settings-open",
                   "status": "open", "severity": "blocking", "dimension": "interaction",
                   "description": "The choices were hidden on Settings after keyboard activation",
                   "classification": "confirmed_defect", "classification_reason": "Keyboard visibility assertion failed",
                   "evidence": "The menu is expanded but the choices are hidden", "protected_decision_ids": []}
        with ThreadingHTTPServer(("127.0.0.1", 0), partial(ItemsHandler, directory=str(self.root))) as server:
            Thread(target=server.serve_forever, daemon=True).start()
            try:
                for name in ("erp", "settings"):
                    baseline[name] = capture(server, name, "before")
                self.component.write_text("#menu {background: blue} html:has(title) section {display:none}")
                erp = capture(server, "erp", "after")
                pending = self.run_action("record", {**request, "cases": [row("erp", erp), row("settings")]})
                self.assertEqual(pending["attempts"][-1]["cases"][1]["status"], "not_verified")
                self.assertTrue(self.blockers("dependent-slice"))
                erp = capture(server, "erp", "after")
                settings = capture(server, "settings", "after")
                attempts = {"cases": [row("erp", erp), row("settings", settings, "fail")], "findings": [finding]}
                with self.assertRaisesRegex(ValueError, "actual browser assertion"):
                    self.run_action("record", {**request, **attempts, "cases": [row("erp", erp), row("settings", settings)]})
                self.assertEqual(self.run_action("record", {**request, **attempts})["status"], "unresolved")
                self.component.write_text("#menu {background: blue}")
                erp = capture(server, "erp", "after")
                settings = capture(server, "settings", "after")
                fixed = {**finding, "status": "fixed", "repair_scope": "Retain original keyboard behavior",
                         "correction": "Removed the unintended hiding rule", "verification": "Keyboard assertion passed"}
                plan = self.run_action("record", {**request, "cases": [row("erp", erp), row("settings", settings)],
                                                   "findings": [fixed]})
                self.assertEqual(plan["status"], "verified")
                self.assertFalse(self.blockers("dependent-slice"))
                settings_file = self.root / "settings.html"
                settings_file.write_text(settings_file.read_text().replace("Menu choices", "Available choices"))
                self.assertRegex(self.blockers("dependent-slice")[0], "stale")
                self.assertFalse(self.blockers("independent-slice"))
                manifest = project_state.manifest_at(self.directory)
                self.assertEqual(cross_screen.review_changes(self.directory, manifest)[0]["current_status"], "stale")
                erp = capture(server, "erp", "after")
                settings = capture(server, "settings", "after")
                plan = self.run_action("record", {**request, "cases": [row("erp", erp), row("settings", settings)]})
            finally:
                server.shutdown()
        self.assertEqual(plan["status"], "verified")
        self.assertEqual(len(plan["attempts"]), 4)
        after = plan["attempts"][-1]["cases"][1]
        self.assertEqual(after["before"], baseline["settings"])
        self.assertTrue(after["comparisons"][0]["pixels_changed"])
        visual_integrity.resolve_evidence_path(self.directory, after["comparisons"][0]["annotated_latest"])
        self.assertFalse(self.blockers("dependent-slice"))
        manifest = project_state.manifest_at(self.directory)
        self.assertFalse(cross_screen.history_errors(self.directory, manifest))
        self.component.write_text("#menu {background: purple}")
        self.assertRegex(self.blockers("dependent-slice")[0], "stale")
        self.assertFalse(self.blockers("independent-slice"))
        self.assertEqual(cross_screen.review_changes(self.directory, manifest)[0]["current_status"], "stale")
