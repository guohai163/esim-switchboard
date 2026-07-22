# Changelog

## v0.1.2 - 2026-06-25

### Fixed
- Fixed dual-active SIM parsing so `/api/esim/latest` preserves both the active physical SIM and the active eSIM when the phone reports them together.
- Fixed the dashboard and switch-confirm overlays to show active eSIM and active physical SIM separately instead of collapsing them into a single "current active" card.

### Docs
- Clarified the README to explain that the UI now separates active eSIM and active physical SIM details in the status area and switch confirmation flow.

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
