"""Offline checks for the bounded M5 evaluation workflow."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import preprocessing as pp  # noqa: E402
import project  # noqa: E402


class FakeConnection:
    def rollback(self):
        pass

    def close(self):
        pass


class FakePlan:
    details = {'ai_model': 'test-model'}


class FakeComparison:
    def to_dict(self):
        return {
            'difference_rate': 0.125,
            'strict_difference_rate': 0.2,
            'edit_distance': 1.0,
            'pg_size': 4,
            'ai_size': 4,
            'category_counts': {'ACCESS_METHOD': 1},
        }


class EvaluationTests(unittest.TestCase):
    def test_workload_has_30_unique_queries(self):
        workload_path = Path(__file__).parents[1] / 'workload.json'
        dataset, queries = project.load_workload(workload_path)
        self.assertEqual(dataset, 'TPC-H')
        self.assertEqual(len(queries), 30)
        self.assertEqual(len({query['id'] for query in queries}), 30)

    def test_evaluation_writes_outputs_and_reserves_one_request(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            budget_dir = output_dir / 'shared-budget'
            budget_dir.mkdir()
            existing_budget = {
                'date_utc': datetime.now(timezone.utc).date().isoformat(),
                'api_requests': 14,
                'daily_limit': project.MAX_DAILY_API_REQUESTS,
            }
            (budget_dir / 'api_budget.json').write_text(json.dumps(existing_budget))
            with mock.patch.object(project, 'BUDGET_DIR', budget_dir, create=True), \
                 mock.patch.object(project.pp, 'connect', return_value=FakeConnection()), \
                 mock.patch.object(project.pp, 'get_schema', return_value={'region': {}}), \
                 mock.patch.object(project.pp, '_postgres_version', return_value='PostgreSQL test'), \
                 mock.patch.object(project.pp, 'get_postgres_plan', return_value=object()), \
                 mock.patch.object(project.pp, 'get_ai_plan', return_value=FakePlan()) as get_ai_plan, \
                 mock.patch.object(project.cmp, 'compare_plans', return_value=FakeComparison()):
                metadata = project.run_evaluation(
                    {'user': 'test', 'password': '', 'host': 'localhost', 'port': 5432},
                    'TPC-H', 'public', 'free-tier-test-key', 'test-model',
                    Path(__file__).parents[1] / 'workload.json', output_dir,
                    query_ids=['Q01'], max_queries=1, pause_seconds=0,
                    scale_factor=0.1)

            get_ai_plan.assert_called_once()
            self.assertEqual(get_ai_plan.call_args.kwargs['retries'], 0)
            self.assertEqual(get_ai_plan.call_args.kwargs['max_request_attempts'], 1)
            self.assertEqual(metadata['api_requests_reserved'], 1)
            self.assertEqual(metadata['daily_api_requests_used'], 15)
            self.assertEqual(metadata['scale_factor'], 0.1)
            self.assertEqual(metadata['postgres_version'], 'PostgreSQL test')
            self.assertEqual(metadata['parallel'], False)
            self.assertFalse(metadata['cache_enabled'])
            saved_budget = json.loads((budget_dir / 'api_budget.json').read_text())
            self.assertEqual(saved_budget['api_requests'], 15)
            self.assertEqual(saved_budget['reservations'][0]['query_id'], None)
            self.assertEqual(saved_budget['reservations'][0]['count'], 14)
            self.assertEqual(saved_budget['reservations'][1]['query_id'], 'Q01')
            result_files = list(output_dir.glob('evaluation_*.json'))
            self.assertEqual(len(result_files), 1)
            self.assertEqual(len(list(output_dir.glob('evaluation_*.csv'))), 1)
            self.assertEqual(len(list(output_dir.glob('evaluation_*.png'))), 1)
            summary = json.loads(result_files[0].read_text())['summary']
            self.assertEqual(summary['successful_queries'], 1)
            self.assertEqual(summary['mean_difference_rate'], 0.125)
            self.assertEqual(summary['mean_strict_difference_rate'], 0.2)
            self.assertEqual(summary['category_totals'], {'ACCESS_METHOD': 1})

    def test_daily_budget_blocks_run_before_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / 'custom-results'
            budget_dir = Path(directory) / 'shared-budget'
            budget = {
                'date_utc': datetime.now(timezone.utc).date().isoformat(),
                'api_requests': project.MAX_DAILY_API_REQUESTS,
            }
            budget_dir.mkdir()
            (budget_dir / 'api_budget.json').write_text(json.dumps(budget))
            with mock.patch.object(project, 'BUDGET_DIR', budget_dir, create=True), \
                  mock.patch.object(project.pp, 'connect', return_value=FakeConnection()) as connect, \
                  mock.patch.object(project.pp, 'get_schema', return_value={'region': {}}), \
                  mock.patch.object(project.pp, 'get_postgres_plan', return_value=object()), \
                  mock.patch.object(project.pp, 'get_ai_plan', return_value=FakePlan()), \
                  mock.patch.object(project.cmp, 'compare_plans', return_value=FakeComparison()):
                with self.assertRaisesRegex(RuntimeError, 'Daily free-tier request limit'):
                    project.run_evaluation(
                        {'user': 'test', 'password': '', 'host': 'localhost', 'port': 5432},
                        'TPC-H', 'public', 'free-tier-test-key', 'test-model',
                        Path(__file__).parents[1] / 'workload.json', output_dir,
                        query_ids=['Q01'], max_queries=1, pause_seconds=0,
                        scale_factor=0.1)
                connect.assert_not_called()
            self.assertFalse((budget_dir / '.api_budget.lock').exists())

    def test_interruption_keeps_query_checkpoint_and_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / 'results'
            budget_dir = Path(directory) / 'shared-budget'
            with mock.patch.object(project, 'BUDGET_DIR', budget_dir, create=True), \
                 mock.patch.object(project.pp, 'connect', return_value=FakeConnection()), \
                 mock.patch.object(project.pp, 'get_schema', return_value={'region': {}}), \
                 mock.patch.object(project.pp, 'get_postgres_plan', return_value=object()), \
                 mock.patch.object(project.pp, 'get_ai_plan', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    project.run_evaluation(
                        {'user': 'test', 'password': '', 'host': 'localhost', 'port': 5432},
                        'TPC-H', 'public', 'free-tier-test-key', 'test-model',
                        Path(__file__).parents[1] / 'workload.json', output_dir,
                        query_ids=['Q01'], max_queries=1, pause_seconds=0,
                        scale_factor=0.1)

            checkpoints = list(output_dir.glob('evaluation_*.json'))
            self.assertEqual(len(checkpoints), 1)
            checkpoint = json.loads(checkpoints[0].read_text())
            self.assertEqual(checkpoint['metadata']['status'], 'interrupted')
            self.assertEqual(checkpoint['results'][0]['status'], 'in_progress')
            reservations = json.loads((budget_dir / 'api_budget.json').read_text())['reservations']
            self.assertEqual(reservations[0]['query_id'], 'Q01')

    def test_quota_exhaustion_stops_batch_and_marks_remaining_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / 'results'
            budget_dir = Path(directory) / 'shared-budget'
            with mock.patch.object(project, 'BUDGET_DIR', budget_dir, create=True), \
                 mock.patch.object(project.pp, 'connect', return_value=FakeConnection()), \
                 mock.patch.object(project.pp, 'get_schema', return_value={'region': {}}), \
                 mock.patch.object(project.pp, '_postgres_version', return_value='PostgreSQL test'), \
                 mock.patch.object(project.pp, 'get_postgres_plan', return_value=object()), \
                 mock.patch.object(project.pp, 'get_ai_plan',
                                   side_effect=pp.AIError('429 RESOURCE_EXHAUSTED daily quota')) as get_ai_plan:
                metadata = project.run_evaluation(
                    {'user': 'test', 'password': '', 'host': 'localhost', 'port': 5432},
                    'TPC-H', 'public', 'free-tier-test-key', 'test-model',
                    Path(__file__).parents[1] / 'workload.json', output_dir,
                    query_ids=['Q01', 'Q02', 'Q03'], max_queries=3, pause_seconds=0,
                    scale_factor=0.1)

            get_ai_plan.assert_called_once()
            self.assertEqual(metadata['status'], 'quota_exhausted')
            results_file = next(output_dir.glob('evaluation_*.json'))
            results = json.loads(results_file.read_text())['results']
            self.assertEqual([item['status'] for item in results],
                             ['error', 'not_run', 'not_run'])

    def test_ask_ai_attempt_limit_disables_retry_amplification(self):
        class RateLimitError(Exception):
            code = 429

        class FakeModels:
            def __init__(self):
                self.calls = 0

            def generate_content(self, **kwargs):
                self.calls += 1
                raise RateLimitError('rate limited')

        class FakeClient:
            def __init__(self):
                self.models = FakeModels()

        client = FakeClient()
        with mock.patch('google.genai.Client', return_value=client), \
             mock.patch.object(pp.time, 'sleep') as sleep:
            with self.assertRaises(pp.AIError):
                pp.ask_ai('system', 'user', api_key='test-key', max_attempts=1)

        self.assertEqual(client.models.calls, 1)
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()