"""Member 4: application entry point and integration of Members 1-3.
Run with: python project.py
"""
import os
import argparse
import csv
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name('.env'), override=False)
except ImportError:
    pass

import preprocessing as pp
import comparison as cmp
from psycopg2 import sql

MAX_EXPERIMENT_QUERIES = 30
MAX_DAILY_API_REQUESTS = 20
EXPERIMENT_PAUSE_SECONDS = 5
BUDGET_DIR = Path(__file__).with_name('experiment-results')


class ProjectService:
    """Backend methods called by the GUI's worker thread.

    Each operation owns and closes its connection. No connection is shared
    between the Tkinter thread and the worker thread.
    """

    @staticmethod
    def connection(settings, database, schema=None):
        conn = pp.connect(database, user=settings['user'],
                          password=settings['password'], host=settings['host'],
                          port=settings['port'])
        try:
            with conn.cursor() as cur:
                cur.execute('SET statement_timeout = 60000')
                if schema:
                    # Identifier quoting supports spaces and mixed-case schemas.
                    cur.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
            conn.commit()  # Preserve the search path across M1's rollbacks.
            return conn
        except Exception:
            conn.close()
            raise

    def databases(self, settings):
        return pp.list_databases(**settings)

    def schemas(self, settings, database):
        conn = self.connection(settings, database)
        try:
            return pp.list_schemas(conn)
        finally:
            conn.close()

    def compare(self, settings, database, schema, query, api_key, use_cache, progress):
        if not query.strip():
            raise ValueError('Enter an SQL query first.')
        conn = self.connection(settings, database, schema)
        try:
            progress('Reading schema, keys and indexes...')
            schema_info = pp.get_schema(conn, schema)
            conn.rollback()
            if not schema_info:
                raise ValueError('No tables were found in the selected schema.')
            progress('Getting the PostgreSQL plan...')
            pg_tree = pp.get_postgres_plan(conn, query, parallel=False)
            progress('Waiting for Gemini (server retries may take over a minute)...')
            ai_tree = pp.get_ai_plan(conn, query, schema_info=schema_info,
                                     api_key=api_key or None, use_cache=use_cache)
            progress('Comparing plans...')
            result = cmp.compare_plans(pg_tree, ai_tree, schema_info)
            # Credentials are deliberately excluded from the export.
            metadata = {'database': database, 'schema': schema, 'query': query,
                        'model': ai_tree.details.get('ai_model'),
                        'cache_enabled': use_cache, 'parallel': False,
                        'created_utc': datetime.now(timezone.utc).isoformat()}
            return result, metadata
        finally:
            conn.close()


def load_workload(path, query_ids=None):
    """Load and validate the SQL workload, optionally selecting query IDs."""
    with Path(path).open(encoding='utf-8') as workload_file:
        workload = json.load(workload_file)

    queries = workload.get('queries') if isinstance(workload, dict) else None
    if not isinstance(queries, list) or not queries:
        raise ValueError('Workload must contain a non-empty queries list.')
    if len(queries) > MAX_EXPERIMENT_QUERIES:
        raise ValueError(f'Workload exceeds the {MAX_EXPERIMENT_QUERIES}-query run limit.')

    seen = set()
    for query in queries:
        if not isinstance(query, dict) or not all(query.get(key) for key in ('id', 'title', 'sql')):
            raise ValueError('Each workload query must have an id, title, and SQL statement.')
        query_id = query['id']
        if query_id in seen:
            raise ValueError(f'Duplicate workload query ID: {query_id}')
        seen.add(query_id)

    if query_ids:
        unknown = set(query_ids) - seen
        if unknown:
            raise ValueError(f'Unknown query IDs: {", ".join(sorted(unknown))}')
        queries = [query for query in queries if query['id'] in set(query_ids)]

    return workload.get('dataset', 'unknown'), queries


def _budget_lock():
    BUDGET_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = BUDGET_DIR / '.api_budget.lock'
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(
            f'An experiment may already be running ({lock_path}). '
            'Remove this lock only after confirming no run is active.'
        ) from exc
    os.close(descriptor)
    return lock_path


def _write_budget(path, request_count, reservations):
    data = {
        'date_utc': datetime.now(timezone.utc).date().isoformat(),
        'api_requests': request_count,
        'daily_limit': MAX_DAILY_API_REQUESTS,
        'reservations': reservations,
    }
    temporary_path = path.with_suffix('.tmp')
    temporary_path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary_path, path)


def _evaluation_summary(results):
    successful = [item for item in results if item['status'] == 'ok']
    core_rates = [item['comparison']['difference_rate'] for item in successful]
    strict_rates = [item['comparison']['strict_difference_rate'] for item in successful]
    category_totals = {}
    for item in successful:
        for category, count in item['comparison'].get('category_counts', {}).items():
            category_totals[category] = category_totals.get(category, 0) + count

    def rate_stats(rates):
        if not rates:
            return {'mean': None, 'median': None, 'min': None, 'max': None}
        return {
            'mean': round(statistics.mean(rates), 4),
            'median': round(statistics.median(rates), 4),
            'min': round(min(rates), 4),
            'max': round(max(rates), 4),
        }

    core_stats = rate_stats(core_rates)
    strict_stats = rate_stats(strict_rates)
    return {
        'successful_queries': len(successful),
        'failed_queries': sum(item['status'] == 'error' for item in results),
        'not_run_queries': sum(item['status'] == 'not_run' for item in results),
        'mean_difference_rate': core_stats['mean'],
        'mean_strict_difference_rate': strict_stats['mean'],
        'core_difference_rate_stats': core_stats,
        'strict_difference_rate_stats': strict_stats,
        'category_totals': dict(sorted(category_totals.items())),
    }


def _is_quota_exhausted(error):
    message = str(error).upper()
    return 'RESOURCE_EXHAUSTED' in message or 'QUOTA EXCEEDED' in message or '429' in message


def _plot_results(results, output_path):
    successful = [item for item in results if item['status'] == 'ok']
    if not successful:
        return False

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    labels = [item['query_id'] for item in successful]
    core_rates = [item['comparison']['difference_rate'] for item in successful]
    strict_rates = [item['comparison']['strict_difference_rate'] for item in successful]
    category_totals = {}
    for item in successful:
        for category, count in item['comparison'].get('category_counts', {}).items():
            category_totals[category] = category_totals.get(category, 0) + count

    figure, (rate_axis, category_axis) = plt.subplots(
        2, 1, figsize=(max(12, len(labels) * 0.45), 9),
        gridspec_kw={'height_ratios': [2, 1]})
    positions = list(range(len(labels)))
    rate_axis.bar([position - 0.2 for position in positions], core_rates,
                  width=0.4, label='Core difference rate')
    rate_axis.bar([position + 0.2 for position in positions], strict_rates,
                  width=0.4, label='Full-tree difference rate')
    rate_axis.set_xticks(positions, labels, rotation=60, ha='right')
    rate_axis.set_ylim(0, 1)
    rate_axis.set_ylabel('Difference rate')
    rate_axis.legend()
    rate_axis.grid(axis='y', alpha=0.25)

    categories = sorted(category_totals)
    if categories:
        category_axis.bar(categories, [category_totals[name] for name in categories])
        category_axis.tick_params(axis='x', labelrotation=35)
        category_axis.set_ylabel('Reported differences')
        category_axis.grid(axis='y', alpha=0.25)
    else:
        category_axis.text(0.5, 0.5, 'No differences reported', ha='center', va='center')
        category_axis.set_axis_off()

    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)
    return True


def run_evaluation(settings, database, schema, api_key, model, workload_path,
                   output_dir, query_ids=None, max_queries=MAX_DAILY_API_REQUESTS,
                   pause_seconds=EXPERIMENT_PAUSE_SECONDS, scale_factor=None):
    """Run a bounded, serial evaluation and save machine-readable results and charts."""
    if not 1 <= max_queries <= MAX_EXPERIMENT_QUERIES:
        raise ValueError(f'max_queries must be between 1 and {MAX_EXPERIMENT_QUERIES}.')
    if not api_key:
        raise ValueError('Set GEMINI_API_KEY locally before running the evaluation.')

    dataset, queries = load_workload(workload_path, query_ids)
    if dataset == 'TPC-H' and (scale_factor is None or scale_factor <= 0):
        raise ValueError('Provide the positive TPC-H database scale factor for reproducible results.')
    queries = queries[:max_queries]
    if not queries:
        raise ValueError('No workload queries were selected.')

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _budget_lock()
    budget_path = BUDGET_DIR / 'api_budget.json'
    results = []
    request_count = 0
    requests_this_run = 0
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    json_path = output_dir / f'evaluation_{run_id}.json'
    csv_path = output_dir / f'evaluation_{run_id}.csv'
    chart_path = output_dir / f'evaluation_{run_id}.png'
    reservations = []

    try:
        if budget_path.exists():
            budget = json.loads(budget_path.read_text(encoding='utf-8'))
            if budget.get('date_utc') == datetime.now(timezone.utc).date().isoformat():
                request_count = int(budget.get('api_requests', 0))
                reservations = list(budget.get('reservations', []))
                if request_count and not reservations:
                    reservations.append({
                        'run_id': None, 'query_id': None, 'count': request_count,
                        'reserved_utc': None, 'legacy_unattributed': True,
                    })
        if request_count + len(queries) > MAX_DAILY_API_REQUESTS:
            raise RuntimeError(
                f'Daily free-tier request limit is {MAX_DAILY_API_REQUESTS}; '
                f'{request_count} already reserved today.'
            )

        metadata = {
            'run_id': run_id, 'created_utc': datetime.now(timezone.utc).isoformat(),
            'dataset': dataset, 'database': database, 'schema': schema, 'model': model,
            'scale_factor': scale_factor,
            'postgres_version': None, 'index_profile': {},
            'billing_mode': 'free tier only; billing must be disabled by the account owner',
            'max_api_requests_per_run': MAX_EXPERIMENT_QUERIES,
            'max_api_requests_per_utc_day': MAX_DAILY_API_REQUESTS,
            'api_requests_reserved': requests_this_run,
            'daily_api_requests_used': request_count,
            'concurrency': 1, 'queries_requested': len(queries),
            'parallel': False, 'cache_enabled': False,
            'queries_succeeded': 0, 'queries_failed': 0, 'queries_not_run': 0,
            'status': 'running',
        }

        def write_checkpoint():
            metadata['api_requests_reserved'] = requests_this_run
            metadata['daily_api_requests_used'] = request_count
            metadata['queries_succeeded'] = sum(item['status'] == 'ok' for item in results)
            metadata['queries_failed'] = sum(item['status'] == 'error' for item in results)
            metadata['queries_not_run'] = sum(item['status'] == 'not_run' for item in results)
            temporary_path = json_path.with_suffix('.tmp')
            temporary_path.write_text(json.dumps(
                {'metadata': metadata, 'summary': _evaluation_summary(results),
                 'results': results}, indent=2, ensure_ascii=False
            ) + '\n', encoding='utf-8')
            os.replace(temporary_path, json_path)

        write_checkpoint()
        conn = pp.connect(database, user=settings['user'], password=settings['password'],
                          host=settings['host'], port=settings['port'])
        try:
            metadata['postgres_version'] = pp._postgres_version(conn)
            schema_info = pp.get_schema(conn, schema)
            metadata['index_profile'] = {
                table: sorted(index['name'] for index in info.get('indexes', []))
                for table, info in schema_info.items()
            }
            write_checkpoint()
            conn.rollback()
            if not schema_info:
                raise ValueError(f'No tables were found in schema {schema!r}.')

            for index, query in enumerate(queries):
                item = {
                    'query_id': query['id'], 'title': query['title'],
                    'families': query.get('families', []), 'sql': query['sql'],
                    'model': model, 'status': 'pending',
                }
                results.append(item)
                write_checkpoint()
                quota_exhausted = False
                try:
                    pg_tree = pp.get_postgres_plan(conn, query['sql'], parallel=False)
                    request_count += 1
                    requests_this_run += 1
                    item['status'] = 'in_progress'
                    reservation = {
                        'run_id': run_id, 'query_id': query['id'],
                        'reserved_utc': datetime.now(timezone.utc).isoformat(),
                    }
                    reservations.append(reservation)
                    _write_budget(budget_path, request_count, reservations)
                    write_checkpoint()
                    ai_tree = pp.get_ai_plan(
                        conn, query['sql'], schema_info=schema_info, model=model,
                        api_key=api_key, retries=0, use_cache=False,
                        max_request_attempts=1)
                    result = cmp.compare_plans(pg_tree, ai_tree, schema_info)
                    item['model'] = ai_tree.details.get('ai_model', model)
                    item['status'] = 'ok'
                    item['comparison'] = result.to_dict()
                except Exception as exc:
                    item['status'] = 'error'
                    item['error'] = f'{type(exc).__name__}: {exc}'
                    quota_exhausted = _is_quota_exhausted(exc)
                write_checkpoint()
                print(f"{query['id']}: {item['status']}" +
                      (f" - {item['error']}" if 'error' in item else ''))
                if quota_exhausted:
                    metadata['status'] = 'quota_exhausted'
                    metadata['stop_reason'] = 'Gemini free-tier request quota exhausted.'
                    for remaining_query in queries[index + 1:]:
                        results.append({
                            'query_id': remaining_query['id'],
                            'title': remaining_query['title'],
                            'families': remaining_query.get('families', []),
                            'sql': remaining_query['sql'], 'model': model,
                            'status': 'not_run',
                            'error': 'Not attempted after Gemini quota exhaustion.',
                        })
                    write_checkpoint()
                    break
                if index + 1 < len(queries) and pause_seconds > 0:
                    time.sleep(pause_seconds)
        finally:
            conn.close()
        if metadata['status'] == 'running':
            metadata['status'] = 'complete_with_errors' if metadata['queries_failed'] else 'complete'
        write_checkpoint()
    except KeyboardInterrupt:
        if 'write_checkpoint' in locals():
            metadata['status'] = 'interrupted'
            metadata['interrupted_utc'] = datetime.now(timezone.utc).isoformat()
            write_checkpoint()
        raise
    except Exception as exc:
        if 'write_checkpoint' in locals():
            metadata['status'] = 'failed'
            metadata['error'] = f'{type(exc).__name__}: {exc}'
            write_checkpoint()
        raise
    finally:
        lock_path.unlink(missing_ok=True)

    fields = ['query_id', 'title', 'families', 'model', 'status', 'difference_rate',
              'strict_difference_rate', 'edit_distance', 'pg_size', 'ai_size',
              'category_counts', 'error']
    with csv_path.open('w', newline='', encoding='utf-8') as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fields)
        writer.writeheader()
        for item in results:
            comparison = item.get('comparison', {})
            writer.writerow({
                'query_id': item['query_id'], 'title': item['title'],
                'families': '; '.join(item['families']), 'model': item['model'],
                'status': item['status'],
                'difference_rate': comparison.get('difference_rate', ''),
                'strict_difference_rate': comparison.get('strict_difference_rate', ''),
                'edit_distance': comparison.get('edit_distance', ''),
                'pg_size': comparison.get('pg_size', ''),
                'ai_size': comparison.get('ai_size', ''),
                'category_counts': json.dumps(comparison.get('category_counts', {})),
                'error': item.get('error', ''),
            })

    chart_created = _plot_results(results, chart_path)
    print(f"Results: {json_path}\nCSV: {csv_path}")
    print(f"Chart: {chart_path}" if chart_created else 'Chart skipped: no successful comparisons.')
    print(f"Requests reserved this run: {requests_this_run}; today: "
          f"{request_count}/{MAX_DAILY_API_REQUESTS}")
    return metadata


def main():
    parser = argparse.ArgumentParser(description='Compare PostgreSQL and AI query plans.')
    parser.add_argument('--evaluate', action='store_true', help='run the bounded M5 workload')
    parser.add_argument('--confirm-free-tier', action='store_true',
                        help='confirm the Gemini key has billing disabled')
    parser.add_argument('--dbname', default='TPC-H')
    parser.add_argument('--schema', default='public')
    parser.add_argument('--scale-factor', type=float,
                        help='TPC-H data scale factor used to build the selected database')
    parser.add_argument('--workload', type=Path, default=Path(__file__).with_name('workload.json'))
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).with_name('experiment-results'))
    parser.add_argument('--max-queries', type=int, default=MAX_DAILY_API_REQUESTS)
    parser.add_argument('--query-ids', nargs='*')
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).with_name('.env'), override=False)
    except ImportError:
        pass

    if args.evaluate:
        if not args.confirm_free_tier:
            parser.error('evaluation requires --confirm-free-tier; use a Gemini key with billing disabled')
        settings = {
            'user': os.environ.get('PGUSER', 'postgres'),
            'password': os.environ.get('PGPASSWORD', ''),
            'host': os.environ.get('PGHOST', 'localhost'),
            'port': int(os.environ.get('PGPORT', '5432')),
        }
        model = os.environ.get('GEMINI_MODEL') or pp.DEFAULT_AI_MODEL
        run_evaluation(settings, args.dbname, args.schema,
                       os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'),
                       model, args.workload, args.output_dir, args.query_ids,
                       args.max_queries, scale_factor=args.scale_factor)
        return

    if any((args.confirm_free_tier, args.query_ids, args.max_queries != MAX_EXPERIMENT_QUERIES,
            args.scale_factor is not None,
            args.workload != Path(__file__).with_name('workload.json'),
            args.output_dir != Path(__file__).with_name('experiment-results'))):
        parser.error('M5 evaluation options require --evaluate')
    from interface import QueryPlanApp
    app = QueryPlanApp(ProjectService(),
                       os.environ.get('GEMINI_MODEL') or pp.DEFAULT_AI_MODEL)
    app.mainloop()


if __name__ == '__main__':
    main()
