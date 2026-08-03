import psycopg


def get_conn():
    return psycopg.connect(
        host="localhost",
        port=5432,
        dbname="vellum_memory_db",
        user="postgres",
        password="AVANI",
        options="-c search_path=public"
    )