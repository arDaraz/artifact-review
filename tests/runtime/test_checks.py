#!/usr/bin/env python3
"""Artifact check and guidance-staleness contract tests."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "artifact-review" / "scripts"
AREV = SCRIPTS / "arev.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import checks


def artifact(body, title="Review"):
    return textwrap.dedent(f"""\
        <!doctype html>
        <html lang="en"><head><meta charset="utf-8">
        <title>{title}</title></head><body><main>
        {body}
        </main></body></html>
        """)


class MermaidAnalysisTests(unittest.TestCase):
    def test_reads_type_labels_and_fan_out(self):
        result = checks.analyse_mermaid(textwrap.dedent("""\
            flowchart LR
              a["active"] --> v["verifying"]
              a --> b["blocked"]
              a --> s["suspended"]
              a --> f["failed"]
              a --> x["cancelled"]
              v -->|"on retry"| a
            """))
        self.assertEqual(result["declared"], "flowchart")
        self.assertEqual(result["max_fan_out"], 5)
        self.assertEqual(result["fan_out_node"], "active")
        self.assertIn("verifying", result["labels"])

    def test_edge_label_is_not_read_as_an_arrow(self):
        result = checks.analyse_mermaid(
            'flowchart LR\n  a["one"] -->|"safe re-entry"| b["two"]\n')
        self.assertEqual(result["edges"], 1)
        self.assertEqual(result["max_fan_out"], 1)

    def test_state_diagram_start_marker_survives(self):
        result = checks.analyse_mermaid(
            "stateDiagram-v2\n  [*] --> idle\n  idle --> busy\n")
        self.assertEqual(result["declared"], "statediagram-v2")
        self.assertEqual(result["edges"], 2)

    def test_unknown_first_token_is_not_a_type(self):
        self.assertEqual(checks.analyse_mermaid("nonsense\n a --> b")["declared"], "")


class DiagramMarkupTests(unittest.TestCase):
    def check(self, body, **kwargs):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "a.html")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(artifact(body))
            return checks.run_checks(path, discover=False, **kwargs)

    def kinds(self, report):
        return {item["kind"] for item in report["findings"]}

    def test_pre_mermaid_with_an_id_passes(self):
        report = self.check(
            '<pre class="mermaid" id="flow">flowchart LR\n a["one"] --> b["two"]\n</pre>')
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["ok"])
        self.assertEqual(report["stats"]["diagrams"], 1)

    def test_div_mermaid_is_an_error(self):
        report = self.check(
            '<div class="mermaid" id="flow">flowchart LR\n a --> b\n</div>')
        self.assertIn("diagram-not-pre", self.kinds(report))
        self.assertFalse(report["ok"])

    def test_missing_and_duplicate_ids_are_errors(self):
        report = self.check(
            '<pre class="mermaid">flowchart LR\n a --> b</pre>'
            '<pre class="mermaid" id="x">flowchart LR\n a --> b</pre>'
            '<pre class="mermaid" id="x">flowchart LR\n a --> b</pre>')
        self.assertIn("diagram-missing-id", self.kinds(report))
        self.assertIn("diagram-duplicate-id", self.kinds(report))

    def test_cdn_mermaid_loader_is_an_error(self):
        report = self.check(
            '<pre class="mermaid" id="d">flowchart LR\n a --> b</pre>'
            '<script src="https://cdn.example/mermaid.min.js"></script>')
        self.assertIn("mermaid-cdn-script", self.kinds(report))

    def test_inline_static_mermaid_import_is_an_error(self):
        report = self.check(
            '<pre class="mermaid" id="d">flowchart LR\n a --> b</pre>'
            '<script type="module">import mermaid from '
            '"https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";'
            '</script>')
        self.assertIn("mermaid-cdn-script", self.kinds(report))

    def test_dynamic_mermaid_import_is_only_a_warning(self):
        # A file: guard keeps this copy off the review server, so a page
        # opened from disk still draws its diagrams.
        report = self.check(
            '<pre class="mermaid" id="d">flowchart LR\n a --> b</pre>'
            '<script type="module">if (location.protocol === "file:") {'
            ' const { default: mermaid } = await import('
            '"https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs");'
            ' mermaid.initialize({ startOnLoad: true }); }</script>')
        self.assertIn("mermaid-dynamic-import", self.kinds(report))
        self.assertTrue(report["ok"])

    def test_inline_import_of_another_host_is_an_external_script(self):
        report = self.check(
            '<script type="module">import "https://cdn.example/chart.js";'
            '</script>')
        self.assertIn("external-script", self.kinds(report))

    def test_unrenderable_source_is_an_error(self):
        report = self.check('<pre class="mermaid" id="d">a --> b</pre>')
        self.assertIn("diagram-unknown-type", self.kinds(report))

    def test_hand_built_box_diagram_is_flagged(self):
        report = self.check(
            "<div><span>Browser</span> -> <span>API</span> -> "
            "<span>Store</span> -> <span>Agent</span></div>")
        self.assertIn("hand-built-diagram", self.kinds(report))

    def test_arrows_inside_code_do_not_count_as_a_drawing(self):
        report = self.check(
            "<p>Run <code>a -> b</code> and <code>c -> d</code> and "
            "<code>e -> f</code>.</p>")
        self.assertNotIn("hand-built-diagram", self.kinds(report))

    def test_state_machine_drawn_as_a_flowchart_is_flagged(self):
        report = self.check(
            '<h2>Job state</h2>'
            '<pre class="mermaid" id="job">flowchart LR\n'
            '  a["active"] --> b["blocked"]\n</pre>'
            '<p class="cap">Every transition in the job state machine.</p>')
        self.assertIn("diagram-type-mismatch", self.kinds(report))

    def test_state_diagram_type_is_accepted(self):
        report = self.check(
            '<h2>Job state</h2>'
            '<pre class="mermaid" id="job">stateDiagram-v2\n'
            '  active --> blocked\n</pre>'
            '<p class="cap">Every transition in the job state machine.</p>')
        self.assertNotIn("diagram-type-mismatch", self.kinds(report))

    def test_node_label_wording_does_not_trigger_the_type_check(self):
        report = self.check(
            '<h2>Isolation</h2>'
            '<pre class="mermaid" id="iso">flowchart LR\n'
            '  a["worker"] --> b["control socket, state DB, config, other runs"]\n'
            '</pre><p class="cap">Each door has its own capability.</p>')
        self.assertNotIn("diagram-type-mismatch", self.kinds(report))
        self.assertIn("diagram-long-label", self.kinds(report))

    def test_wide_fan_out_is_flagged(self):
        edges = "\n".join(f'  hub --> n{n}["node {n}"]' for n in range(6))
        report = self.check(
            f'<pre class="mermaid" id="d">flowchart LR\n{edges}\n</pre>')
        self.assertIn("diagram-fan-out", self.kinds(report))

    def test_hardcoded_mermaid_theme_is_an_error(self):
        report = self.check(
            '<pre class="mermaid" id="d">'
            "%%{init: {'theme': 'forest'}}%%\n"
            'flowchart LR\n  a --> b\n</pre>')
        finding = next(f for f in report["findings"]
                       if f["kind"] == "diagram-hardcoded-theme")
        self.assertEqual(finding["severity"], "error")

    def test_non_theme_init_directive_is_a_warning(self):
        report = self.check(
            '<pre class="mermaid" id="d">'
            "%%{init: {'flowchart': {'curve': 'linear'}}}%%\n"
            'flowchart LR\n  a --> b\n</pre>')
        finding = next(f for f in report["findings"]
                       if f["kind"] == "diagram-hardcoded-theme")
        self.assertEqual(finding["severity"], "warn")

    def test_plain_diagram_has_no_theme_finding(self):
        report = self.check(
            '<pre class="mermaid" id="d">flowchart LR\n  a --> b\n</pre>')
        self.assertNotIn("diagram-hardcoded-theme", self.kinds(report))


class SourceCoverageTests(unittest.TestCase):
    SPEC = textwrap.dedent("""\
        # Platform design

        ## 1. Overview
        The service owns its data.

        ## 2. Job state
        Jobs move between named states.

        ## 3. Endpoint state
        Endpoints appear and vanish.

        ## 4. Competitive scope
        Nothing here is drawn.
        """)

    def run_against(self, body, ignore=()):
        folder = tempfile.mkdtemp()
        spec = os.path.join(folder, "spec.md")
        with open(spec, "w", encoding="utf-8") as handle:
            handle.write(self.SPEC)
        page = os.path.join(folder, "a.html")
        with open(page, "w", encoding="utf-8") as handle:
            handle.write(artifact(body))
        return checks.run_checks(page, source_paths=[spec], ignore=ignore)

    def gap(self, report, kind):
        for item in report["findings"]:
            if item["kind"] == kind:
                return item
        return None

    def test_missing_sections_are_named_with_the_words_that_are_absent(self):
        report = self.run_against("<h1>Overview</h1><p>The service owns data.</p>")
        gap = self.gap(report, "section-not-covered")
        self.assertIsNotNone(gap)
        self.assertIn("4. Competitive scope", gap["sections"])
        missing = {entry["heading"]: entry["missing"] for entry in gap["detail"]}
        self.assertIn("competitive", missing["4. Competitive scope"])

    def test_a_covered_source_reports_no_coverage_gap(self):
        report = self.run_against(
            "<h1>Overview</h1><p>The service owns data.</p>"
            '<h2>Job state</h2><pre class="mermaid" id="job">stateDiagram-v2\n'
            "  active --> blocked\n</pre>"
            '<h2>Endpoint state</h2><pre class="mermaid" id="ep">stateDiagram-v2\n'
            "  present --> missing\n</pre>"
            "<h2>Competitive scope</h2><p>Compared with other tools.</p>")
        self.assertIsNone(self.gap(report, "section-not-covered"))
        self.assertIsNone(self.gap(report, "section-not-diagrammed"))
        self.assertTrue(report["ok"])

    def test_one_diagram_does_not_cover_every_state_section(self):
        report = self.run_against(
            "<h1>Overview</h1><p>The service owns data.</p>"
            '<h2>Job state</h2><pre class="mermaid" id="job">stateDiagram-v2\n'
            "  active --> blocked\n</pre>"
            "<h2>Endpoint state</h2><p>Endpoints appear and vanish.</p>"
            "<h2>Competitive scope</h2><p>Compared with other tools.</p>")
        gap = self.gap(report, "section-not-diagrammed")
        self.assertIsNotNone(gap)
        self.assertIn("3. Endpoint state", gap["sections"])
        self.assertNotIn("2. Job state", gap["sections"])

    def test_a_caption_citing_the_section_number_counts_as_coverage(self):
        report = self.run_against(
            "<h1>Overview</h1><p>The service owns data.</p>"
            "<h2>Job state</h2><p>Jobs move between named states.</p>"
            "<h2>How things move</h2>"
            '<pre class="mermaid" id="one">stateDiagram-v2\n'
            "  a --> b\n</pre>"
            '<p class="cap">Every transition in sections 2 and 3.</p>'
            "<h2>Endpoint state</h2><p>Endpoints appear and vanish.</p>"
            "<h2>Competitive scope</h2><p>Compared with other tools.</p>")
        self.assertIsNone(self.gap(report, "section-not-diagrammed"))

    def test_ignore_excludes_a_section_on_purpose(self):
        report = self.run_against(
            "<h1>Overview</h1><p>The service owns data.</p>"
            '<h2>Job state</h2><pre class="mermaid" id="job">stateDiagram-v2\n'
            "  active --> blocked\n</pre>"
            '<h2>Endpoint state</h2><pre class="mermaid" id="ep">stateDiagram-v2\n'
            "  present --> missing\n</pre>",
            ignore=("Competitive scope",))
        self.assertIsNone(self.gap(report, "section-not-covered"))

    def test_a_title_only_top_heading_does_not_become_the_section_list(self):
        source = checks.read_source(self._write(self.SPEC))
        self.assertEqual(source["section_level"], 2)
        self.assertEqual(len(source["sections"]), 4)

    def test_repeated_top_level_headings_are_the_sections(self):
        source = checks.read_source(self._write(
            "# One\ntext\n\n# Two\ntext\n"))
        self.assertEqual(source["section_level"], 1)
        self.assertEqual(len(source["sections"]), 2)

    def test_headings_inside_fenced_code_are_ignored(self):
        source = checks.read_source(self._write(
            "# Title\n\n## Real\n\n```\n## Not a heading\n```\n\n## Also real\n"))
        self.assertEqual([s["text"] for s in source["sections"]],
                         ["Real", "Also real"])

    def _write(self, text):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        return handle.name


class SourceDiscoveryTests(unittest.TestCase):
    def test_the_opening_text_names_the_source(self):
        folder = tempfile.mkdtemp()
        spec = os.path.join(folder, "spec.md")
        other = os.path.join(folder, "NOTES.md")
        for path in (spec, other):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# Title\n\n## One\ntext\n")
        page = os.path.join(folder, "a.html")
        with open(page, "w", encoding="utf-8") as handle:
            handle.write(artifact(
                "<h1>Explainer</h1><p>What <code>spec.md</code> proposes.</p>"
                + "<p>filler text.</p>" * 200
                + "<p>See <code>NOTES.md</code> for the rest.</p>"))
        report = checks.run_checks(page)
        found = [os.path.basename(item["path"]) for item in report["sources"]]
        self.assertEqual(found, ["spec.md"])

    def test_a_named_source_wins_over_discovery(self):
        folder = tempfile.mkdtemp()
        spec = os.path.join(folder, "spec.md")
        with open(spec, "w", encoding="utf-8") as handle:
            handle.write("# Title\n\n## One\ntext\n")
        page = os.path.join(folder, "a.html")
        with open(page, "w", encoding="utf-8") as handle:
            handle.write(artifact("<h1>No paths named here</h1>"))
        report = checks.run_checks(page, source_paths=[spec])
        self.assertEqual(len(report["sources"]), 1)
        self.assertFalse(report["discovered"])


class CheckCommandTests(unittest.TestCase):
    def run_arev(self, *arguments):
        return subprocess.run(
            [sys.executable, str(AREV), *arguments],
            capture_output=True, text=True)

    def test_a_clean_artifact_exits_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            page = os.path.join(folder, "a.html")
            with open(page, "w", encoding="utf-8") as handle:
                handle.write(artifact(
                    '<pre class="mermaid" id="d">flowchart LR\n'
                    ' a["one"] --> b["two"]\n</pre>'))
            result = self.run_arev("check", page)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_an_error_exits_non_zero_and_warn_only_does_not(self):
        with tempfile.TemporaryDirectory() as folder:
            page = os.path.join(folder, "a.html")
            with open(page, "w", encoding="utf-8") as handle:
                handle.write(artifact(
                    '<div class="mermaid" id="d">flowchart LR\n a --> b</div>'))
            failed = self.run_arev("check", page)
            relaxed = self.run_arev("check", page, "--warn-only")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("FAIL", failed.stdout)
        self.assertEqual(relaxed.returncode, 0)

    def test_json_output_is_machine_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            page = os.path.join(folder, "a.html")
            with open(page, "w", encoding="utf-8") as handle:
                handle.write(artifact('<pre class="mermaid">flowchart LR\n'
                                      ' a["one"] --> b["two"]\n</pre>'))
            result = self.run_arev("check", page, "--json", "--warn-only")
        report = json.loads(result.stdout)
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"], 1)

    def test_a_missing_file_is_reported(self):
        result = self.run_arev("check", "/nonexistent/page.html")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no such file", result.stderr)


class GuidanceVersionTests(unittest.TestCase):
    def test_brief_and_doctor_agree_on_the_version(self):
        brief = subprocess.run(
            [sys.executable, str(AREV), "brief"],
            capture_output=True, text=True, check=True)
        line = [row for row in brief.stdout.splitlines()
                if row.startswith("GUIDANCE ")]
        self.assertEqual(len(line), 1)
        stamped = line[0].split()[1]
        doctor = subprocess.run(
            [sys.executable, str(AREV), "doctor"],
            capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(doctor.stdout)["guidance"], stamped)

    def test_editing_a_playbook_changes_the_version(self):
        import arev

        before = arev._guidance_version()
        playbook = os.path.join(arev.REFERENCE_DIR, "design.md")
        with open(playbook, "rb") as handle:
            original = handle.read()
        try:
            with open(playbook, "ab") as handle:
                handle.write(b"\n- a new rule\n")
            self.assertNotEqual(arev._guidance_version(), before)
        finally:
            with open(playbook, "wb") as handle:
                handle.write(original)
        self.assertEqual(arev._guidance_version(), before)

    def test_a_session_started_on_older_guidance_is_reported_stale(self):
        import arev

        live = arev._guidance_version()
        self.assertIsNone(arev._guidance_note({"guidance": live}))
        note = arev._guidance_note({"guidance": "0000deadbeef"})
        self.assertIn("GUIDANCE STALE", note)
        self.assertIn("0000deadbeef", note)
        self.assertIn(live, note)
        self.assertIn("arev brief", note)
        self.assertIn("unknown", arev._guidance_note({}))

    def test_the_stale_note_stays_out_of_the_event_envelope(self):
        """The poll event is a fixed public shape, so the note goes to stderr."""
        import arev

        self.assertEqual(arev.event_envelope("idle"),
                         {"schema": arev.EVENT_SCHEMA, "type": "idle"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
