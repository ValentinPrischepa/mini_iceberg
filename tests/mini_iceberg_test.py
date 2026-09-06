import json
from pathlib import Path
from typing import TypedDict

from mini_iceberg import create_table, TableAlreadyExistsError, get_last_metadata_version, scan, append, \
    DataFileAlreadyExistsError, TableDoesNotExistError, MetadataFileIsCorruptedError, read_metadata, \
    DuplicatedInputFilesError, ConcurrentModificationError
import pytest


class TableMetadata(TypedDict):
    files: list[str]


TABLE_NAME = "table_1"

metadata_file_v0: TableMetadata = {"files": []}
metadata_file_v1: TableMetadata = {"files": ["file1", "file2"]}
metadata_file_v2: TableMetadata = {"files": ["file1", "file2", "file3", "file4"]}
duplicated_files = ["file5", "file5", "file5"]


@pytest.fixture
def table_path(tmp_path: Path) -> Path:
    return tmp_path / TABLE_NAME


# Catches regressions where create_table() fails to create the initial v0 metadata file for a brand-new table.
def test_create_table_positive(table_path: Path) -> None:
    create_table(str(table_path))
    assert (table_path / "metadata" / "v0.json").exists()


# Catches regressions where create_table() crashes (e.g. mkdir without exist_ok=True) if the
# metadata directory already exists but has no version file yet.
def test_create_table_when_metadata_directory_already_exists(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    create_table(str(table_path))
    assert (metadata_dir / "v0.json").exists()


# Prevents create_table() from silently overwriting an existing table's v0 metadata and losing its state.
def test_create_table_table_already_exists(table_path: Path) -> None:
    metadata_file = table_path / "metadata" / "v0.json"
    metadata_file.parent.mkdir(parents=True)
    metadata_file.touch()
    with pytest.raises(TableAlreadyExistsError):
        create_table(str(table_path))


# Regression test: create_table() must write valid JSON (i.e. {"files": []}) rather than an
# empty file, otherwise scan() raises json.JSONDecodeError on a freshly created table instead
# of returning [] as DR-1 requires ("An existing table always has at least one metadata version").
def test_scan_after_create_table_returns_empty_list(table_path: Path) -> None:
    create_table(str(table_path))
    assert scan(str(table_path)) == []


# Catches off-by-one/wrong-max bugs when picking the highest version number out of several metadata files.
def test_get_last_metadata_version(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    for i in range(0, 6):
        (metadata_dir / f"v{i}.json").touch()
    actual_version = get_last_metadata_version(str(table_path))
    assert actual_version == 5


# Catches regressions where version discovery sorts version numbers lexicographically (where
# "v10" < "v9" as strings) instead of numerically, per DR-1's "highest-version-wins" contract.
def test_get_last_metadata_version_uses_numeric_not_lexicographic_order(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    for i in (1, 2, 9, 10, 11):
        (metadata_dir / f"v{i}.json").touch()
    actual_version = get_last_metadata_version(str(table_path))
    assert actual_version == 11


# Directly catches get_last_metadata_version() failing to raise TableDoesNotExistError when no
# metadata versions exist, instead of relying only on scan()/append()'s indirect coverage.
def test_get_last_metadata_version_table_does_not_exist(table_path: Path) -> None:
    with pytest.raises(TableDoesNotExistError):
        get_last_metadata_version(str(table_path))


# Catches regressions where an unparseable metadata filename (e.g. "v1.2.json", which still
# matches the "v*.json" glob) is silently ignored instead of raising MetadataFileIsCorruptedError,
# per DR-1's "every v*.json file must contain a numeric version" contract.
def test_get_last_metadata_version_corrupted_file_invalid_name(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    for i in range(0, 6):
        (metadata_dir / f"v{i}.json").touch()
    (metadata_dir / f"v1.2.json").touch()
    with pytest.raises(MetadataFileIsCorruptedError):
        get_last_metadata_version(str(table_path))


# Catches regressions where a torn/corrupted metadata file (invalid JSON, e.g. from a partial or
# interrupted write) propagates a raw json.JSONDecodeError instead of the typed
# MetadataFileIsCorruptedError that callers are expected to handle.
def test_read_metadata_corrupted_json(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    with open(Path(metadata_dir / "v0.json"), "w") as f:
        f.write("""""table_name": "table_1", "files": [""")
    with pytest.raises(MetadataFileIsCorruptedError):
        read_metadata(str(table_path))


# Regression test: valid JSON that is missing the "files" key used to raise a raw KeyError
# instead of MetadataFileIsCorruptedError, because the key lookup happened outside the
# try/except that only guarded against JSON parsing failures.
def test_read_metadata_missing_files_key(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    with (metadata_dir / "v0.json").open("w") as f:
        json.dump({}, f)
    with pytest.raises(MetadataFileIsCorruptedError):
        read_metadata(str(table_path))


# Catches regressions where read_metadata() returns a version number that doesn't match the
# files it read (e.g. an off-by-one against the highest version), since both scan() and append()
# rely on getting back a consistent (version, files) pair from a single directory listing.
def test_read_metadata_positive(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    with (metadata_dir / "v2.json").open("w") as f:
        json.dump(metadata_file_v2, f)

    with (metadata_dir / "v0.json").open("w") as f:
        json.dump(metadata_file_v0, f)

    with (metadata_dir / "v1.json").open("w") as f:
        json.dump(metadata_file_v1, f)

    version, files = read_metadata(str(table_path))
    assert version == 2
    assert files == metadata_file_v2["files"]


# Catches regressions where scan() picks the wrong metadata file (e.g. by write/creation order
# on disk) instead of resolving the highest version number among all metadata files present.
def test_scan(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    with (metadata_dir / "v2.json").open("w") as f:
        json.dump(metadata_file_v2, f)

    with (metadata_dir / "v0.json").open("w") as f:
        json.dump(metadata_file_v0, f)

    with (metadata_dir / "v1.json").open("w") as f:
        json.dump(metadata_file_v1, f)

    actual = scan(str(table_path))
    assert actual == metadata_file_v2["files"]


# Directly catches scan() failing to raise TableDoesNotExistError on a missing table, instead of
# relying only on append()'s indirect exercise of the same scan() code path.
def test_scan_table_does_not_exist(table_path: Path) -> None:
    with pytest.raises(TableDoesNotExistError):
        scan(str(table_path))


# Prevents scan() from treating stray/unreferenced data files sitting in the table directory as
# table contents; DR-1 explicitly rejects "directory-as-truth" in favor of metadata-as-truth, so
# only files listed in the current metadata version should ever be returned.
def test_scan_ignores_data_files_not_referenced_by_metadata(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    with (metadata_dir / "v1.json").open("w") as f:
        json.dump(metadata_file_v1, f)
    (table_path / "file_not_in_metadata").touch()
    assert scan(str(table_path)) == metadata_file_v1["files"]


# Catches regressions where append() fails to merge newly appended files with an existing
# (empty) table's metadata on its first append.
def test_append_positive_first_append(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    with (metadata_dir / "v0.json").open("w") as f:
        json.dump(metadata_file_v0, f)
    files = metadata_file_v1["files"]
    append(str(table_path), list(files))
    with open(metadata_dir / "v1.json", "r") as f:
        actual_files = json.load(f)["files"]
    assert files == actual_files


# Prevents append() from mutating the caller-supplied `files` list in place (e.g. via
# list.extend()), which would leak the table's existing files back into the caller's own list
# object as an unexpected side effect.
def test_append_does_not_mutate_input_files_list(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    with (metadata_dir / "v1.json").open("w") as f:
        json.dump(metadata_file_v1, f)
    files = ["file3", "file4"]
    original_files = list(files)
    append(str(table_path), files)
    assert files == original_files


# Catches regressions where append() drops previously committed files when a later append adds
# more files, instead of carrying forward the full accumulated file list.
def test_append_positive_second_append(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    with (metadata_dir / "v1.json").open("w") as f:
        json.dump(metadata_file_v1, f)
    files = ["file3", "file4"]
    append(str(table_path), files)
    with open(metadata_dir / "v2.json", "r") as f:
        actual_files = json.load(f)["files"]
    assert set(actual_files) == set(metadata_file_v2["files"])


# Prevents append() from mutating older metadata version files in place; DR-1 guarantees that
# existing metadata versions are immutable, so v0 must be unchanged after v1 is written.
def test_append_does_not_modify_previous_metadata_version(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    with (metadata_dir / "v0.json").open("w") as f:
        json.dump(metadata_file_v0, f)
    append(str(table_path), ["file1", "file2"])
    with open(metadata_dir / "v0.json", "r") as f:
        assert json.load(f) == metadata_file_v0


# Prevents append() from silently discarding a concurrently-written metadata version, and from
# leaking the raw builtin FileExistsError. Opening the target version file with "x" (exclusive
# create) must raise instead of overwriting it with "w", and the collision must surface as the
# typed ConcurrentModificationError so callers can distinguish a version race from an unrelated
# filesystem error.
def test_append_raises_on_concurrent_version_collision(table_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    with (metadata_dir / "v0.json").open("w") as f:
        json.dump(metadata_file_v0, f)
    with (metadata_dir / "v1.json").open("w") as f:
        json.dump(metadata_file_v1, f)
    monkeypatch.setattr("mini_iceberg.get_last_metadata_version", lambda table_name: 0)
    with pytest.raises(ConcurrentModificationError):
        append(str(table_path), ["file3", "file4"])
    with (metadata_dir / "v1.json").open("r") as f:
        assert json.load(f) == metadata_file_v1


# Prevents append() from creating a spurious new metadata version or corrupting existing
# metadata when called with an empty file list on a table that already exists.
def test_append_empty_file_list(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    with (metadata_dir / "v1.json").open("w") as f:
        json.dump(metadata_file_v1, f)
    append(str(table_path), [])
    with open(metadata_dir / "v1.json", "r") as f:
        actual_files = json.load(f)["files"]
    assert actual_files == metadata_file_v1["files"]
    assert not (metadata_dir / "v2.json").exists()


# Regression test: append() must check table existence even when files is empty, instead of
# returning early (silent no-op) before ever verifying the table exists.
def test_append_empty_file_list_table_does_not_exist(table_path: Path) -> None:
    with pytest.raises(TableDoesNotExistError):
        append(str(table_path), [])


# Prevents append() from silently accepting a data file that already belongs to the table,
# which would duplicate/collide entries in the metadata file list.
def test_append_overlapped_files(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    with (metadata_dir / "v2.json").open("w") as f:
        json.dump(metadata_file_v2, f)
    files = metadata_file_v2["files"]
    with pytest.raises(DataFileAlreadyExistsError):
        append(str(table_path), list(files))


# Prevents append() from silently accepting duplicated input files within a single batch, even
# when none of them overlap with files already committed to the table.
def test_append_duplicated_input_files(table_path: Path) -> None:
    metadata_dir = table_path / "metadata"
    metadata_dir.mkdir(parents=True)
    with (metadata_dir / "v2.json").open("w") as f:
        json.dump(metadata_file_v2, f)
    with pytest.raises(DuplicatedInputFilesError):
        append(str(table_path), list(duplicated_files))


# Prevents append() from implicitly creating a table (or raising the wrong error) when no
# metadata exists yet; DR-1 requires table creation and append to remain separate operations.
def test_append_table_does_not_exist(table_path: Path) -> None:
    with pytest.raises(TableDoesNotExistError):
        files = metadata_file_v2["files"]
        append(str(table_path), list(files))
