"""File-loading convenience only; normalization lives in processing.loader.

The former dictionary loader has been retired. Use load_store_from_files()
or load_store(parsed_clients, parsed_reference) and then build_dossier().
"""
import json
from pathlib import Path

from processing.loader import load_store


def load_store_from_files(clients_path, reference_path=None):
    clients_path = Path(clients_path)
    with clients_path.open(encoding="utf-8") as file:
        clients = json.load(file)
    reference = None
    if reference_path is not None:
        with Path(reference_path).open(encoding="utf-8") as file:
            reference = json.load(file)
    return load_store(clients, reference, source=clients_path.name)
