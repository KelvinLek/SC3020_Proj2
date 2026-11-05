"""Member 4: application entry point and integration of Members 1-3.
Run with: python project.py
"""
import os
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


def main():
    # Load this project's .env even when launched from a different directory.
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).with_name('.env'), override=False)
    except ImportError:
        pass
    from interface import QueryPlanApp
    app = QueryPlanApp(ProjectService(),
                       os.environ.get('GEMINI_MODEL') or pp.DEFAULT_AI_MODEL)
    app.mainloop()


if __name__ == '__main__':
    main()
