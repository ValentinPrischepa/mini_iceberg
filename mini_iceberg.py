import json
from collections import Counter
from json import JSONDecodeError
from pathlib import Path


class TableAlreadyExistsError(Exception):
    pass


class TableDoesNotExistError(Exception):
    pass


class DataFileAlreadyExistsError(Exception):
    pass


class MetadataFileIsCorruptedError(Exception):
    pass


class DuplicatedInputFilesError(Exception):
    pass


class ConcurrentModificationError(Exception):
    pass


def create_table(table_name: str) -> None:
    metadata_file = Path(table_name) / "metadata/v0.json"
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with metadata_file.open("x") as f:
            json.dump({"files": []}, f)
    except FileExistsError:
        raise TableAlreadyExistsError


def get_last_metadata_version(table_name: str) -> int:
    versions = []
    for path in (Path(table_name) / "metadata").glob("v*.json"):
        try:
            versions.append(int(path.stem[1:]))
        except ValueError:
            raise MetadataFileIsCorruptedError(
                f"Invalid metadata filename: {path.name}"
            ) from None
    if not versions:
        raise TableDoesNotExistError
    return max(versions)


def read_metadata(table_name: str) -> tuple[int, list[str]]:
    last_version = get_last_metadata_version(table_name)
    metadata_file = Path(table_name) / f"metadata/v{str(last_version)}.json"
    try:
        with open(metadata_file, "r") as f:
            metadata = json.load(f)
        files = metadata["files"]
    except (JSONDecodeError, KeyError):
        raise MetadataFileIsCorruptedError
    if not isinstance(files, list) or not all(isinstance(file, str) for file in files):
        raise MetadataFileIsCorruptedError

    return last_version, files


def scan(table_name: str) -> list[str]:
    _, files = read_metadata(table_name)
    return files


def append(table_name: str, files: list[str]) -> None:
    last_version, existed_files = read_metadata(table_name)
    if not files:
        return

    duplicated_input_files = [
        file
        for file, count in Counter(files).items()
        if count > 1
    ]

    if duplicated_input_files:
        raise DuplicatedInputFilesError(f"Input data contains the following duplicated files:{duplicated_input_files}")

    overlapped_files = [
        file
        for file, count in Counter(existed_files + files).items()
        if count > 1
    ]
    if overlapped_files:
        raise DataFileAlreadyExistsError(f"The following data files already exist in {table_name} table: {overlapped_files}")
    new_metadata_file = Path(table_name) / f"metadata/v{last_version + 1}.json"
    all_files = existed_files + files
    try:
        with new_metadata_file.open("x") as f:
            metadata = {"files": all_files}
            json.dump(metadata, f)
    except FileExistsError:
        raise ConcurrentModificationError(
            f"Metadata version {last_version + 1} for table {table_name} was written concurrently"
        ) from None
