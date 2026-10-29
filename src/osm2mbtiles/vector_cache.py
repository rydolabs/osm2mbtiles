"""One-time optional PostGIS attribute cache build for repeated vector tiles."""
import argparse
import psycopg2
from .vector import prepare_cache


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=('prepare',))
    args = ap.parse_args()
    with psycopg2.connect() as db:
        prepare_cache(db)


if __name__ == '__main__':
    main()