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
                                               {"selector": "section", "probe": "visible", "expected": True},
                                               {"selector": "summary", "probe": "accessibility",
                                                "expected": {"name": "Open", "role": "DisclosureTriangle",
                                                             "expanded": True, "disabled": False}}],
                }]}

    def run_action(self, action, payload):
        return cross_screen.change(self.directory, "erp-slice", action, payload)

    def blockers(self, key):
        from scripts import project_state

        return project_state.cross_screen_blockers(self.directory, project_state.manifest_at(self.directory), key)

    def test_missing_discovery_original_holds_only_affected_work_and_cannot_be_relabelled(self):
        import json
        from pathlib import Path
        import subprocess
        import sys
        from scripts import project_state, visual_baseline

        page = self.root / "erp.html"
        page.write_text("original application")
        manifest = project_state.manifest_at(self.directory)
        manifest["slices"]["erp-slice"]["allowed_paths"].append("erp.html")
        manifest["slices"]["dependent-slice"]["dependencies"] = ["erp-slice"]
        manifest["discovery_audits"] = {
            "a": {"source_hashes_at_discovery": {"erp.html": project_state.digest_of(page)}},
        }
        project_state.atomic_json(self.directory / "manifest.json", manifest)
        spec = {**self.spec["screens"][0]["cases"][0], "baseline_id": "erp-menu",
                "baseline_feature_ids": ["known-menu"], "source_paths": ["erp.html"]}
        gap = visual_baseline.record(self.directory, "gap", {
            **spec, "essential": True, "unavailable_reason": "Original fixture is unavailable",
        })
        self.assertIsNone(gap["original"])
        self.assertIn("erp-menu", self.blockers("erp-slice")[0])
        self.assertIn("erp-menu", self.blockers("dependent-slice")[0])
        self.assertFalse(self.blockers("independent-slice"))
        self.assertFalse(self.blockers("settings-slice"))
        page.write_text("rewritten application")
        with self.assertRaisesRegex(ValueError, "Restore the recorded original"):
            visual_baseline.record(self.directory, "capture", spec)
        with self.assertRaisesRegex(ValueError, "source bytes from discovery"):
            visual_baseline.record(self.directory, "capture", {**spec, "baseline_id": "substitute"})
        late_gap = visual_baseline.record(self.directory, "gap", {
            **spec, "baseline_id": "another-state", "essential": False, "unavailable_reason": "Original route changed",
        })
        self.assertEqual(late_gap["sources"], gap["sources"])
        manifest = project_state.manifest_at(self.directory)
        self.assertFalse(visual_baseline.catalog(self.directory, manifest, "erp-slice")[0]["available"])
        self.assertEqual(visual_baseline.catalog(self.directory, manifest, "dependent-slice")[0]["slice_ids"],
                         ["erp-slice"])
        self.assertFalse(visual_baseline.catalog(self.directory, manifest, "independent-slice"))
        cli = Path(__file__).resolve().parents[1] / "scripts" / "project_state.py"
        listed = subprocess.run([sys.executable, str(cli), "visual-baseline",
                                 "--project-root", str(self.root), "--initiative-id", "fixture",
                                 "--action", "list", "--slice-id", "erp-slice"],
                                capture_output=True, text=True)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        by_id = {record["definition"]["baseline_id"]: record for record in json.loads(listed.stdout)}
        self.assertEqual(by_id["erp-menu"]["gap"]["reason"], "Original fixture is unavailable")
        manifest["visual_baselines"]["erp-menu"]["gap"]["essential"] = False
        self.assertIn("decision journal", visual_baseline.history_errors(self.directory, manifest)[0])

    def test_blueprint_reference_preserves_protected_decisions_for_a_later_session(self):
        from scripts import project_state, visual_baseline

        (self.root / "erp.html").write_text("original application")
        manifest = project_state.manifest_at(self.directory)
        manifest["approvals"] = {}
        manifest.pop("active_approval_id")
        project_state.atomic_json(self.directory / "manifest.json", manifest)
        spec = {**self.spec["screens"][0]["cases"][0], "baseline_id": "erp-menu",
                "baseline_feature_ids": ["old-menu"], "source_paths": ["erp.html"]}
        visual_baseline.record(self.directory, "gap", {
            **spec, "essential": True, "unavailable_reason": "Original fixture is unavailable",
        })
        manifest = project_state.manifest_at(self.directory)
        protected = [{"decision_id": "keep-keyboard", "requirement": "Keep keyboard focus on the menu trigger"}]
        feature = {"feature_id": "approved-menu", "baseline_ids": ["old-menu"], "classification": "KEEP",
                   "protected_visual_decisions": protected}
        path = "blueprints/reviewed.json"
        project_state.atomic_json(self.directory / path, {"features": [feature]})
        digest = project_state.digest_of(self.directory / path)
        manifest.update(initiative_mode="blueprint", active_approval_id="approved",
                        approvals={"approved": {"approved_version": "reviewed"}},
                        blueprints={"reviewed": {"path": path, "sha256": digest}})
        manifest["slices"]["erp-slice"]["feature_ids"] = ["approved-menu"]
        self.assertEqual(visual_baseline.link_actual(
            self.directory, manifest, "erp-slice", spec, spec["renderCase"]), "erp-menu")
        result = visual_baseline.catalog(self.directory, manifest, "erp-slice")[0]["approved"]["approved"]
        self.assertEqual(result["path"], path)
        self.assertEqual(result["sha256"], digest)
        self.assertEqual(result["features"][0]["protected_visual_decisions"], protected)
        self.assertTrue(visual_baseline.holds(self.directory, manifest))

    def test_approved_screenshot_is_served_only_with_the_locked_digest(self):
        from io import BytesIO
        from PIL import Image
        from scripts import project_state, visual_baseline

        image = BytesIO()
        Image.new("RGB", (4, 4)).save(image, format="PNG")
        relative = "references/prototype-1/menu.png"
        path = self.directory / relative
        project_state.atomic_bytes(path, image.getvalue())
        approval = self.manifest["approvals"]["approval-1"]
        approval.update(visual_cases=[{"case_id": "menu", "reference_file": relative}],
                        artifact_hashes={relative: project_state.digest_of(path)})
        self.assertEqual(visual_baseline.approved_image(
            self.directory, self.manifest, "approval-1", "menu"), image.getvalue())
        path.write_bytes(image.getvalue() + b"changed")
        with self.assertRaisesRegex(ValueError, "altered"):
            visual_baseline.approved_image(self.directory, self.manifest, "approval-1", "menu")

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
        from scripts import project_state, visual_baseline, visual_integrity
        from fixtures.blueprint_browser_probe import ItemsHandler

        browser = shutil.which("google-chrome") or shutil.which("chromium")
        if not browser:
            self.skipTest("Chromium is unavailable")
        original_css = ":root {--menu-gap: 12px} #menu {background: red}"
        self.component.write_text(original_css)
        page = ('<link rel="stylesheet" href="/menu.css"><details id="menu"><summary>Open</summary>'
                '<section>Menu choices</section></details>')
        for name in ("erp", "settings"):
            (self.root / (name + ".html")).write_text(("<title>Settings</title>" if name == "settings" else "") + page)
        manifest = project_state.manifest_at(self.directory)
        locked_hashes = cross_screen.source_hashes(self.directory, ["menu.css", "erp.html"])
        manifest["discovery_audits"] = {"a": {"source_hashes_at_discovery": locked_hashes}}
        manifest["visual_baselines_required"] = True
        manifest["final_visual_quality_required"] = True
        project_state.atomic_json(self.directory / "manifest.json", manifest)

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
            }, "accessibility": {"result": interaction if receipt else "not_checked",
                                 "note": "Tab and Enter exercise the menu and verify the focused summary"}}
            if receipt is None:
                return {**value, "unavailable_reason": "Settings fixture could not be loaded"}
            return {**value, "capture_id": receipt["capture_id"]}

        request = {"change_id": "menu-layout"}
        baseline = {}
        finding = {"finding_id": "settings-menu-hidden", "case_id": "settings-open",
                   "status": "open", "severity": "major", "dimension": "interaction",
                   "description": "The choices were hidden on Settings after keyboard activation",
                   "classification": "confirmed_defect", "classification_reason": "Keyboard visibility assertion failed",
                   "evidence": "The menu is expanded but the choices are hidden", "protected_decision_ids": []}
        with ThreadingHTTPServer(("127.0.0.1", 0), partial(ItemsHandler, directory=str(self.root))) as server:
            Thread(target=server.serve_forever, daemon=True).start()
            try:
                discovery = self.spec["screens"][0]["cases"][0]
                catalog_spec = {**discovery, "baseline_id": "erp-original", "baseline_feature_ids": ["known-menu"],
                                "source_paths": ["erp.html", "menu.css"],
                                "renderCase": {**discovery["renderCase"], "design_tokens": ["--menu-gap"]},
                                "appUrl": f"http://127.0.0.1:{server.server_port}", "browser": browser}
                historical = project_state.manifest_at(self.directory)
                historical["discovery_audits"]["a"]["source_hashes_at_discovery"]["menu.css"] = "different"
                project_state.atomic_json(self.directory / "manifest.json", historical)
                with self.assertRaisesRegex(ValueError, "original source for a served dependency"):
                    visual_baseline.record(self.directory, "capture", {**catalog_spec, "source_paths": ["erp.html"]})
                project_state.atomic_json(self.directory / "manifest.json", manifest)
                original = visual_baseline.record(self.directory, "capture", catalog_spec)["original"]["case"]
                browser_evidence = original["browser_evidence"]
                self.assertEqual(browser_evidence["design_tokens"]["root"]["--menu-gap"], "12px")
                self.assertEqual(browser_evidence["structure"]["descendants"][0]["tag"], "summary")
                self.assertEqual(browser_evidence["state"]["expandedControls"][0]["id"], "menu")
                self.assertGreater(browser_evidence["geometry"]["component"]["bounds"]["width"], 0)
                with self.assertRaisesRegex(ValueError, "immutable original"):
                    visual_baseline.record(self.directory, "capture", catalog_spec)
                registered = self.run_action("plan", self.spec)
                self.assertIn("erp-open", registered["baselines"])
                self.assertNotIn("settings-open", registered["baselines"])
                with self.assertRaisesRegex(ValueError, "settings-open"):
                    cross_screen.require_ready(self.directory, project_state.manifest_at(self.directory),
                                               "erp-slice", request)
                baseline["settings"] = capture(server, "settings", "before")
                self.component.write_text("#menu {background: blue}")
                premature = [row(name, capture(server, name, "after")) for name in ("erp", "settings")]
                with self.assertRaisesRegex(ValueError, "readiness check before editing"):
                    self.run_action("record", {**request, "cases": premature})
                with self.assertRaisesRegex(ValueError, "before modifying scoped source"):
                    self.run_action("ready", request)
                self.component.write_text(original_css)
                preflight = self.run_action("ready", request)
                self.assertTrue(preflight["ready"])
                saved = project_state.manifest_at(self.directory)["cross_screen_changes"]["menu-layout"]
                self.assertEqual(saved["pre_edit_check"], preflight["checked_at"])
                self.component.write_text("#menu {background: blue}")
                with self.assertRaisesRegex(ValueError, "fresh after capture"):
                    self.run_action("record", {**request, "cases": premature})
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
                failed = self.run_action("record", {**request, **attempts})
                self.assertEqual(failed["status"], "unresolved")
                self.component.write_text("#menu {background: blue}")
                erp = capture(server, "erp", "after")
                settings = capture(server, "settings", "after")
                fixed = {**finding, "status": "fixed", "repair_scope": "Retain original keyboard behavior",
                         "correction": "Removed the unintended hiding rule", "verification": "Keyboard assertion passed"}
                plan = self.run_action("record", {**request, "cases": [row("erp", erp), row("settings", settings)],
                                                   "findings": [fixed]})
                self.assertEqual(plan["status"], "verified")
                repaired = plan["attempts"][-1]["findings"][0]["comparison"]
                self.assertEqual(repaired["before"], failed["attempts"][-1]["cases"][1]["after"]["closeup_screenshot"])
                self.assertTrue(repaired["pixels_changed"])
                visual_integrity.resolve_evidence_path(self.directory, repaired["annotated_latest"])
                self.assertFalse(self.blockers("dependent-slice"))
                settings_file = self.root / "settings.html"
                settings_file.write_text(settings_file.read_text().replace("Menu choices", "Available choices"))
                self.assertRegex(self.blockers("dependent-slice")[0], "stale")
                self.assertFalse(self.blockers("independent-slice"))
                manifest = project_state.manifest_at(self.directory)
                self.assertEqual(cross_screen.review_changes(self.directory, manifest)[0]["current_status"], "stale")
                erp = capture(server, "erp", "after")
                settings = capture(server, "settings", "after")
                keyboard = self.run_action("capture", {**request, "case_id": "settings-open",
                    "phase": "accessibility", "browser": browser, "appUrl": f"http://127.0.0.1:{server.server_port}",
                    "keyboard_steps": [{"action": "press", "key": "Home"}],
                    "keyboard_assertions": [{"selector": "summary", "probe": "focused", "expected": True},
                                            {"selector": "summary", "probe": "accessibility",
                                             "expected": {"name": "Open", "role": "DisclosureTriangle",
                                                          "expanded": True, "disabled": False}}]})
                self.assertEqual(keyboard["browser_evidence"]["accessibility_targets"]["summary"]["name"], "Open")
                refreshed = row("settings", settings)
                refreshed["accessibility"]["capture_id"] = keyboard["capture_id"]
                plan = self.run_action("record", {**request, "cases": [row("erp", erp), refreshed]})
                self.assertEqual(plan["attempts"][-1]["cases"][1]["accessibility_capture"], keyboard)
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
        restored = visual_baseline.catalog(self.directory, manifest, "erp-slice")[0]
        self.assertEqual(restored["original"]["case"], original)
        self.assertEqual(len(restored["actual"]), 5)
        self.assertIn("approval-1", restored["approved"])
        self.assertNotEqual(restored["actual"][-1]["case"]["full_screenshot"], original["full_screenshot"])
        self.component.write_text("#menu {background: purple}")
        self.assertRegex(self.blockers("dependent-slice")[0], "stale")
        self.assertFalse(self.blockers("independent-slice"))
        self.assertEqual(cross_screen.review_changes(self.directory, manifest)[0]["current_status"], "stale")
