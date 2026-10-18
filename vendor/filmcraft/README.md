# Filmcraft native editing foundation

Clipline reuses Filmcraft's native editor and document. This directory pins a source archive and one combined patch. It does not contain a second timeline model or the evaluation's fixed-split demo process.

The pinned engine changes provide linked native razor/regular trim and committed-only playback/autosave publication. Native times, source handles, effects and history remain authoritative. Exact JSON float parsing preserves framing values through native save/reopen. Schema2 binds gameplay and an optional separate facecam role across shared, landscape and portrait sequences. Gameplay inherits native audio once; facecam adds no audio. Schema1 remains supported. General shared move/insert/reorder/speed commands remain incomplete. Identified unsupported linked timing commands reject explicitly. Do not expose this scope as a complete Clipline editor.

Document-wide validation includes inactive linked owners, complete native role/source/audio bindings and owner revisions. Save/export owners reject malformed linked documents before IO. Recovery copies retain invalid working data, while optional current-project autosave refuses to overwrite the current file with it. `clipline.saveSnapshot` checks the exact working path, native revision and complete linked-owner inventory, then validates and saves a copy in one synchronous native call. It preserves the working path and dirty state. The Clipline checkpoint gateway publishes those opaque bytes through ProjectStore generation CAS; a lost reply remains uncertain and is not retried automatically.

Accepted source-clock words enter through one native `clipline.acceptCaptions` command. It pins the explicit CreatorLibrary transcript revision, validates source/import and all three existing native caption tracks, and groups/maps cues with native clocks. Linked split/trim regenerates those cues in the same undoable edit. Native style/settings and unmanaged cues remain editable; managed text/timing changes require the canonical accepted transcript owner. Missing/interpolated timing, ambiguous audio selection and conflicting unmanaged cues reject. The parent gateway performs full source verification before and after the edit; native import fingerprints alone are not a fresh file hash.

The Session publication owner retains a validated immutable playback project. Invalid raw working data remains recoverable without reaching live playback, and staged command/group data remains private. The engine mixer and UI playback provider use that same boundary.

## Prepare source

Obtain the exact archive using the endpoint in `pin.json`. This endpoint reconstructs the tag download; the original request URL was not retained. The verified archive hash and its embedded commit establish the accepted input. Keep downloaded source and Cargo targets outside this repository. Preparation checks the complete archive and patch hashes, refuses an existing destination, preflights archive paths, applies the patch with local Git line-ending conversion disabled, and checks each declared patched file hash. It preserves failed attempts for diagnosis.

From the repository root:

```powershell
python scripts/engine/prepare_filmcraft.py --archive <downloaded-source.zip> --patch vendor/filmcraft/clipline-native-edits.patch --pin vendor/filmcraft/pin.json --destination <fresh-source-directory>
```

A `prepared-source.json` receipt certifies source preparation only. Compilation, native command tests, renderer checks and full editor acceptance are separate. The patch uses LF bytes, enforced by this directory's Git attributes, because its complete hash is part of the pin.

## Checks and remaining work

The combined frozen engine passed 82 focused native checks on Windows GNU: 21 accepted-caption, 44 linked-command, 16 publication-observer and one shared decoder case. The controller read all four final raw logs. Fresh archive preparation matched all seventeen pinned changed files. Packaged upstream test targets and full upstream CI remain unrun. The external decoder test calls the real shared decoder with the unchanged focused assertions. The linked fixture has an existing unused helper warning; observer panic output is deliberately injected and caught.

These checks do not establish rendered captions, speech accuracy, a functioning GUI on this latest source, complete Creator Kit application, unrestricted linked editing, global resource control or complete clip-to-export acceptance. The earlier v2.1 GUI passes actual linked edit/product-client/save/reopen checks after an identical-object LLD comparison isolated its GNU linker startup fault. A fresh controlled Cargo build on this latest source is a separate gate. Exact receipts and unfinished gates remain in the execution ledger.


## Source notices

Original `LICENSE-MIT`, `LICENSE-APACHE`, `NOTICE` and `ATTRIBUTION.md` are preserved here. Filmcraft code is offered under MIT OR Apache-2.0. The upstream notice separately identifies fonts, third-party material and trademarks. Clipline changes are identified by the patch and this source pin. A release must preserve the applicable source and bundled-asset notices; this source preparation step does not complete redistribution review.
