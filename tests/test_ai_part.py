
import os
import sys
import json

from getpass import getpass

# Add project root to Python path
sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

import preprocessing as pp

password = getpass("Enter PostgreSQL password: ")

def main():
    # Connect to PostgreSQL
    conn = pp.connect(
        dbname="TPC-H",
        user="postgres",
        password=password,
        host="localhost",
        port=5432
    )

    try:
        print("Connected to PostgreSQL!")

        # Retrieve database schema
        schema_info = pp.get_schema(conn)

        print("\nDatabase schema retrieved successfully.")

        # Sample SQL query
        query = """
        SELECT c_name, o_orderdate
        FROM customer
        JOIN orders ON c_custkey = o_custkey
        WHERE o_totalprice > 100000
        LIMIT 10;
        """

        print("\nSQL Query:")
        print(query)

        # Generate AI predicted query plan
        print("\nGenerating AI predicted query plan...")

        try:
            plan = pp.get_ai_plan(conn, query, schema_info=schema_info)
        except pp.AIError as e:
            print("\nAI error:", e)
            return

        print("\n========== AI PREDICTED PLAN ==========")

        print("--- PostgreSQL")
        print(pp.get_postgres_plan(conn, query, parallel=False).pretty())
        print("--- AI")
        print(plan.pretty())

        print("\n========== TEST COMPLETED ==========")

    finally:
        conn.close()
        print("\nPostgreSQL connection closed.")


if __name__ == "__main__":
    main()
