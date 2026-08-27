---
status: accepted
date: 2026-08-26
---

# Use immutable Qlib data bundles derived from data snapshots

PostgreSQL and DataSnapshot remain the point-in-time facts, while Qlib reads an immutable QlibDataBundle generated from one DataSnapshot, exporter version, and pyqlib version. We chose Qlib's native file provider instead of a direct PostgreSQL provider or per-run DataFrames because native bundles maximize compatibility with Qlib expressions, Dataset/DataHandler, recorders, and later Alpha/model workflows; the accepted cost is rebuildable local disk duplication.

## Consequences

- A QlibDataBundle contains market and instrument facts, not labels, research results, models, or portfolio state.
- Bundles are built lazily, validated before atomic publication, shared by runs, and safe to delete when no active run uses them.
- pyqlib or exporter schema changes produce a different bundle identity rather than reusing an assumed-compatible binary format.
- ResearchUniverse and LabelWindow belong to ResearchExperiment/ResearchRun and never alter the shared bundle.
