# HJ GROUPS OF FILES — Maintenance Status

Date: 2026-10-08

## Role

HJ GROUPS OF FILES is an independent maintenance utility in the HJ GROUPS ecosystem.

Final architecture rule:
- It must never be called by the HJ website or production streaming Worker at request time.
- Its Telegram indexing/compression capability may be used only as explicitly controlled maintenance tooling.
- GitHub Actions may run heavy compression/splitting as ephemeral maintenance work.
- No always-on runtime is required for the website or streaming architecture.

## This phase

Production behavior changed in this repository: NO.

The ~950-episode catalog was bulk-migrated/compressed/uploaded: NO.

Original Telegram media deleted: NO.

Telegram session regenerated: NO.

Website/streaming runtime dependency introduced: NO.

## Verified boundary change

The HJ Web Supabase project now has a dedicated `public.streaming_media_sources` mapping table owned by the website/streaming architecture.

The corrected HJ-Telegram-Streaming review branch reads its mapping from HJ Web Supabase instead of the HJ GROUPS OF FILES `telegram_media_index` table.

The new mapping table is currently empty (verified SQL row count: 0), so no existing Files-repo mappings were modified or copied.

## Maintenance rules

- Do not bulk-map the 950 episodes.
- Do not fabricate Telegram `file_id` values.
- Do not replace/delete original Telegram media as part of this migration.
- Do not make the Files repo a website runtime dependency.
- Future compression/splitting must run through controlled GitHub Actions workflows.
- Large-media handling target is separately-created <=19 MB derivatives/chunks uploaded to Telegram; the original source remains untouched.
- Any future mapping must be based on an authoritative Telegram result and recorded deliberately.

## NOT VERIFIED / future work

- The existing maintenance workflows have not been reworked in this phase.
- The existing compression runner still requires a separate safety review before any use on production originals.
- No bulk 950-item operation is authorized or completed.
- Final lazy mapping/verification workflow still needs to be implemented and tested on a tiny controlled media set.
- Website runtime independence from this repository must be regression-tested after the web/streaming migration is completed.

## Status

PARTIALLY COMPLETED — Files remains an independent maintenance utility. No production content operation was performed in this phase.


## 2026-10-08 phase update

Verified code changes:
- The former 5-minute scheduled maintenance trigger was removed from `.github/workflows/telegram-maintenance.yml`.
- Automatic Telegram media-index refresh was removed from that workflow.
- A manual, single-message lazy mapper was added at `scripts/streaming_lazy_mapper.py`.
- Manual workflow `.github/workflows/streaming-lazy-mapping.yml` accepts exactly one content kind and one Telegram message ID.
- The mapper verifies the authoritative Telegram `file_id` through Pyrogram and writes only the selected <=20 MB single-part mapping to HJ Web Supabase.
- It does not edit/replace the original Telegram message and does not delete Telegram media.
- Required web-database secrets are referenced only as GitHub Actions secrets; no secret values were committed.

NOT VERIFIED:
- The manual mapper has not been run with real GitHub Actions secrets in this phase.
- The preservation-safe >20 MB split/upload workflow and streaming Worker reassembly are not implemented yet.
- The legacy compression runner still contains in-place `edit_message_media` behavior and must not be used for production originals until replaced by the preservation-safe maintenance path.

No 950-item bulk operation was performed.
