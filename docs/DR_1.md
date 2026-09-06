## DR-001: Use versioned metadata files as the source of truth

**Context:**
On a cold start, a reader needs to determine which data files belong to the table. Data files are opaque and cannot be inspected to reconstruct or validate table state, so the table needs a separate representation of its contents.

There are two distinct pieces of state to discover: which metadata version represents the current table state, and which data files belong to that state. We need a design that allows both `append()` and `scan()` to determine the current table contents without any prior state.

**Decision:**
Use versioned metadata files as the authoritative representation of table membership.

Each metadata version contains the complete list of data files that belong to the table. An `append()` creates a new metadata version containing the contents of the previous version plus the newly appended files. Existing metadata versions are immutable. Metadata file list is not ordered.

A data file is considered part of the table only when it is referenced by the current metadata version. A data file that exists in the table directory but is not referenced by the current metadata version is ignored.

Table creation is a separate operation. `create_table()` creates the initial metadata version, representing an empty table. `append()` requires an existing table and fails if no metadata exists.

Metadata versions are named using an incrementing version number, for example `v1`, `v2`, `v3`. To discover the current version, `append()` and `scan()` list the metadata directory, parse the version numbers, and use the highest version number.
Every file matching v*.json in the metadata directory must contain a numeric version (v0.json, v1.json, etc.). An unparseable metadata filename causes MetadataFileIsCorruptedError; it must not be silently ignored.

The version naming convention and highest-version-wins discovery rule are part of the metadata format contract.

**Rejected:**
A directory-as-truth approach was rejected even though it could use the presence of data files to determine table membership. Under that model, any valid data file present in the table directory could become part of the table even if it was never successfully committed by an `append()`. This makes partially completed appends and unrelated files indistinguishable from committed table contents.

Using metadata as the source of truth provides a clear commit boundary: a data file becomes part of the table only when a metadata version references it. This also means that an incomplete append can leave an unreferenced data file without affecting what readers see.

A single metadata file updated in place was rejected because safely replacing the current table state would require an atomic publication mechanism. Versioning does not remove this requirement; it only relocates where the damage lands, from the single shared file to whichever version is currently highest. Writing a new version file is not atomic with respect to its contents: a reader that lists the metadata directory while the highest version is still being written, or after a crash has left it truncated, observes a torn file, exactly as `test_read_metadata_corrupted_json` demonstrates by writing a torn file at the highest (and only) version and showing the table becomes unreadable. `read_metadata()` has no way to fall back to the previous, valid version in that case. Atomic publication of a metadata version is still required and is deferred to a later design iteration.

Having `append()` implicitly create a table when no metadata exists was rejected because table creation and appending are separate operations with different semantics.

**Consequence:**
Both `append()` and `scan()` must list the metadata directory and determine the highest metadata version before operating on the table. This makes version discovery part of every read and append operation and gives it a cost proportional to the number of metadata versions.

Because each metadata version stores the complete file list rather than a delta of the append, metadata size grows with the whole table, not just with version count. `n` single-file appends write versions of size `1, 2, ..., n`, so the cumulative data written across all versions is `n(n+1)/2` entries, and every `scan()` or `append()` reads a list proportional to the entire table's file count rather than to what changed. This is the constraint most likely to force a redesign as tables grow, and it is written down here so it is a known, accepted limitation rather than a surprise discovered later.

The metadata version naming convention must remain stable because readers depend on it to identify the current version. Version number collisions or concurrent appends are not handled by this iteration: `append()` does not retry or resolve the conflict. It only guarantees that the loser fails loudly, via `ConcurrentModificationError`, instead of silently overwriting the winner's metadata version or leaking the underlying `FileExistsError`.

An existing table always has at least one metadata version, even when it is empty.

Data files may exist in the table directory without being referenced by the current metadata version. Such files are invisible to `scan()` by design. They may result from failed appends or other incomplete operations; cleanup of these files is deferred to a later iteration.

Metadata versions accumulate over time and will eventually require cleanup or retention.
