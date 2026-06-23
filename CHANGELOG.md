# Changelog

## v0.1.1 - 2026-06-23

### Docs
- Added a dashboard showcase section to the README with a promotional screenshot of the web console.
- Listed the new README showcase asset in the documented project structure.

## v0.1.0 - 2026-06-02

### Added
- Added persistent per-card remarks for both eSIM and physical SIM entries, with a new `/api/esim/remarks/{sub_id}` API for save and clear operations.
- Added a remark editor overlay in the dashboard so card remarks can be viewed and updated directly from the eSIM list.

### Changed
- Extended `/api/esim/latest` subscription payloads to include local remark data alongside the synced SIM snapshot.
- Expanded automated coverage for remark persistence, API authorization, and physical-SIM remark handling.
