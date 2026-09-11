# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-09-11

### Added

- **Right-to-left page flipping** for EPUBs that declare
  `<spine page-progression-direction="rtl">` in their OPF — manga, and
  Arabic or Hebrew books. The reMarkable has no right-to-left reading
  mode, so the plugin builds the PDF so the device behaves as if it
  did: the page order is reversed, the cover is duplicated at both
  ends of the file (thumbnail at page 1, opening page at the end), and
  `lastOpenedPage` is set so the book opens on its cover. The PDF is
  also flagged `/ViewerPreferences << /Direction /R2L >>` with an
  `/OpenAction` on the cover for desktop PDF readers.
- New **Page flip direction** setting: follow the EPUB (default,
  `auto`), or force `rtl`/`ltr` for books whose OPF omits the
  attribute.

### Fixed

- PDF page counts are now read from the PDF's page tree (via
  calibre's own podofo, then a pure-Python page-tree reader) instead
  of falling back to counting raw `/Type /Page` byte matches, which
  overcounted whenever calibre's PDF pipeline left orphaned page
  objects behind. The old count was only cosmetic before, but it
  broke right-to-left books: an overcounted `lastOpenedPage` fell
  outside the valid page range, so the reMarkable app discarded it
  and opened the book on page 1 instead of its cover.

## [1.0.0] - 2026-04-25

First stable release.

A Calibre plugin that sends books from your library to your reMarkable
tablet via the desktop app, converts EPUBs to reMarkable-tuned PDFs,
and syncs reading positions back to Calibre.

### Added

- **Send to reMarkable** — selected books land in the desktop app's
  local store and sync to your tablet on next app launch.
- **Export as PDF…** — save the same reMarkable-tuned PDF anywhere on
  disk, no device required (useful for previewing or as a fallback
  upload path).
- **EPUB → PDF conversion** tuned per device (reMarkable 2, Paper Pro,
  Paper Pro Move): configurable font family, size, line height, and
  per-edge margins, plus a configurable page footer.
- **Full-bleed cover** inserted as the first page (top-aligned, padded
  with the cover's dominant edge color).
- **Reading position sync** — pull progress, current page, and
  last-read timestamp back into Calibre custom columns.
- **Smart document naming**: `Series-Number Title - Author`.

### Requirements

- Calibre 5.0+ (tested on Calibre 9.7).
- The [reMarkable desktop app](https://remarkable.com/desktop)
  installed and signed in. **A free reMarkable Connect account is
  enough — no paid subscription required.**
- After sending books, **quit and restart the desktop app** so it picks
  up the new documents and uploads them to your tablet.

### Notes

- Developed and tested on **macOS**. Windows and Linux code paths exist
  but are currently untested — feedback welcome.
- See the [README](README.md) for the full feature list, settings
  reference, and reading-position sync setup.

[1.1.0]: https://github.com/chris-kuo/calibre-remarkable/releases/tag/v1.1.0
[1.0.0]: https://github.com/mremond/calibre-remarkable/releases/tag/v1.0.0
