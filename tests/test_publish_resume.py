from __future__ import annotations

import unittest
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts import publish_resume

from scripts.publish_resume import candidate_for_attempt, find_unit_artifact


class PublishResumeTest(unittest.TestCase):
    def test_partial_rerun_preserves_the_plan_producer_candidate(self):
        self.assertEqual(candidate_for_attempt('317-1', '317', '2'), '317-1')
        self.assertEqual(candidate_for_attempt('317-2', '317', '2'), '317-2')

    def test_candidate_cannot_cross_runs_or_come_from_the_future(self):
        for candidate, run, attempt in [
            ('318-1', '317', '2'), ('317-3', '317', '2'),
            ('0317-1', '317', '2'), ('317-0', '317', '2'),
            ('317-1', '317', '02'), ('local-dry-run', '317', '2'),
        ]:
            with self.subTest(candidate=candidate, run=run, attempt=attempt):
                with self.assertRaises(ValueError):
                    candidate_for_attempt(candidate, run, attempt)

    def test_finds_only_the_exact_unit_checkpoint_across_pages(self):
        name = 'unit-evidence-arm64-parent-cinder-base-317-1'
        artifact = {'id': 17, 'name': name, 'expired': False}
        self.assertEqual(find_unit_artifact([
            {'artifacts': [{'id': 18, 'name': name + '0', 'expired': False}]},
            {'artifacts': [artifact]},
        ], name), '17')
        self.assertIsNone(find_unit_artifact([{'artifacts': []}], name))

    def test_ambiguous_expired_and_malformed_checkpoints_fail_closed(self):
        name = 'unit-evidence-arm64-parent-cinder-base-317-1'
        valid = {'id': 17, 'name': name, 'expired': False}
        for artifacts in [[valid, valid], [{**valid, 'expired': True}],
                          [{**valid, 'id': False}], [{**valid, 'id': 0}]]:
            with self.subTest(artifacts=artifacts):
                with self.assertRaises(ValueError):
                    find_unit_artifact([{'artifacts': artifacts}], name)

    def test_cli_avoids_api_for_a_new_plan_and_pins_checkpoint_id_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            arguments = ['--candidate-id', '317-1', '--run-id', '317',
                         '--run-attempt', '1', '--repository', 'supergate-hub/kolla-container-images',
                         '--unit-id', 'arm64-parent-cinder-base', '--github-output', str(output)]
            with patch.object(publish_resume.subprocess, 'run') as run:
                self.assertEqual(publish_resume.main(arguments), 0)
                run.assert_not_called()
            self.assertEqual(output.read_text(), 'found=false\n')
            output.unlink()
            arguments[5] = '2'
            response = [{'artifacts': [{'id': 17, 'name': 'unit-evidence-arm64-parent-cinder-base-317-1',
                                        'expired': False}]}]
            with patch.object(publish_resume.subprocess, 'run',
                              return_value=subprocess.CompletedProcess([], 0, json.dumps(response), '')) as run:
                self.assertEqual(publish_resume.main(arguments), 0)
                self.assertIn('/repos/supergate-hub/kolla-container-images/actions/runs/317/artifacts?per_page=100', run.call_args.args[0])
            self.assertEqual(output.read_text(), 'found=true\nartifact_id=17\n')

    def test_checkpoint_lookup_recovers_without_emitting_partial_api_results(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            arguments = ['--candidate-id', '317-1', '--run-id', '317',
                         '--run-attempt', '2', '--repository', 'supergate-hub/kolla-container-images',
                         '--unit-id', 'arm64-parent-cinder-base', '--github-output', str(output)]
            failed = subprocess.CalledProcessError(1, ['gh'], output='[{"partial":', stderr='HTTP 502')
            success = subprocess.CompletedProcess([], 0, '[{"artifacts":[]}]', '')
            with patch.object(publish_resume.subprocess, 'run', side_effect=[failed, success]) as run, \
                 patch('time.sleep') as sleep:
                self.assertEqual(publish_resume.main(arguments), 0)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0], run.call_args_list[1])
            sleep.assert_called_once_with(5)
            self.assertEqual(output.read_text(), 'found=false\n')
