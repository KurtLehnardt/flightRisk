# E2E Test Identifiers

This directory contains identifier (id) registries for E2E tests, organized by owning task to enable parallel development.

## Structure

Each task owns one or more YAML files:
- `<task-id>.yaml`: Registry of identifier metadata for that task

Schema (per file):
```yaml
ids:
  <screen>.<element>[.<id>]:
    android: true|false
    ios: true|false
    source: "path/to/file.kt" or "path/to/file.swift"
    description: "optional description"
```

## Merging

The `check_ids.py` script merges all files in this directory into a unified registry and validates:
- Both apps declare every `mission.*` identifier (phone-only mission journeys)
- Identifier names follow the convention `<screen>.<element>[.<id>]`
- Source files exist

No identifier may be missing from the unified registry or used by flows without being declared.

## Wave 0

W0-2 (this task) creates the directory structure and validates the merge process.
Wave 1 and later tasks each own one file, ensuring parallel work without file conflicts.
