# Repository Guidance — `agy-hud`

## Design System & UI Language

- **Default / Primary UI Spec**: `frostedPills` (R4「冰白微胶囊」) — see [docs/ui-spec-r4-frosted-pills.md](docs/ui-spec-r4-frosted-pills.md).
- Any new HUD feature, metric, status indicator, or layout adjustment MUST follow the R4 visual hierarchy, token palette, pill-wrapping rules (`_wrap_frosted_pill`), and placement rules defined in [docs/ui-spec-r4-frosted-pills.md](docs/ui-spec-r4-frosted-pills.md).

## Architecture & Constraints

- **Dual Quota Pool Model**: Strictly two pools — `Gemini` (shared by all Gemini models; legacy `argon` keys merge into `Gemini`) and `3P` (third-party models). Do not introduce a separate Argon pool or re-enable `AI Pro` plan-tier fallback text in `frostedPills`.
- **Zero External Dependencies**: Python standard library only (`hud.py` + `test_hud.py`).
- **Backward Compatibility**: All existing theme presets (`default`, `appleVibrancy`, `mintlifyGlass`, `appleSystemBlue`, `quartzTeal`) and existing unit tests must continue to pass without breaking or relaxing assertions.

## Verification & Local Install

- Run full unit tests after any code change:
  ```bash
  python3 -m unittest test_hud.py -v
  ```
- Deploy locally without overwriting user `settings.json` statusline wiring:
  ```bash
  ./install.sh --skip-settings
  ```
- Never run `git push` unless explicitly requested by the user.
