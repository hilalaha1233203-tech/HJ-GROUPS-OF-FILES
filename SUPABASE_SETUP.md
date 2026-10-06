# Supabase setup for HJ GROUPS OF FILES

The bot no longer uses MongoDB for its user database. Telegram files are still stored/forwarded through the configured Telegram DB channel; Supabase stores bot user/status records.

## 1. Create the table

Open the Supabase SQL Editor and run the SQL in `supabase_schema.sql`.

## 2. Get credentials

From the Supabase project dashboard, get:

- Project URL
- A server-side secret key

For a server-side worker, use the Supabase secret key. A legacy `service_role` key is also accepted by this bot for compatibility. Do not expose either secret in source code or commit it to GitHub.

## 3. Voroa environment variables

Keep the existing Telegram/bot variables. Remove the old MongoDB `DATABASE_URL` if it is no longer used, and add:

`SUPABASE_URL=<your Supabase project URL>`

`SUPABASE_SECRET_KEY=<your server-side Supabase secret key>`

The code also accepts `SUPABASE_SERVICE_ROLE_KEY` or `SUPABASE_KEY` as compatibility names.

## 4. Deploy

After saving the variables in Voroa, redeploy the Background Worker. The bot database layer keeps the original `Database` method interface, so the existing bot handlers and broadcast flow continue to call the same methods.


## 5. Admin auto-delete setting

The SQL also creates the `bot_settings` table. After deployment, the bot owner can run `/settings` to choose the auto-delete time for files delivered from share/start links.

Available choices: 5 minutes, 15 minutes, 30 minutes, 1 hour, 6 hours, 24 hours, or disabled.

This setting is stored in Supabase and is available after a worker restart. Only `BOT_OWNER` can change it.


## 6. Compression + website streaming backend

The Store Keeper bot now has an owner-only **Compression Center**:

- `/compression` — open the button-based compression center.
- `/compress` — reply to one storage-channel media message, or use `/compress <message_id>`.
- `/compress_bulk audio 21-1114` — queue large audio files from a storage-channel message range.
- `/compress_bulk video 1-500` — queue video compression.
- `/compress_bulk document all` — queue safe document optimisation.
- `/compression_status` — view job progress.
- `/compression_cancel <job_id>` — cancel a pending job.

The target profile **19 MB — Web Stream** is intended for the website Bot API streaming path. The heavy download/FFmpeg/upload work runs on an ephemeral GitHub-hosted runner. Render is not used for large media bytes.

The SQL schema adds:

- `compression_jobs` — durable, resumable compression queue.
- `telegram_media_index` — server-side Telegram file-id index for the website worker.

### GitHub repository secrets

Add these secrets to **HJ-GROUPS-OF-FILES** before enabling the scheduled maintenance workflow:

`API_ID`
`API_HASH`
`BOT_TOKEN`
`SUPABASE_URL`
`SUPABASE_SERVICE_ROLE_KEY`

Optional:

`DB_CHANNEL` — legacy fallback if `bot_settings.storage_channels` is empty.

The scheduled workflow runs every 5 minutes and can also be started manually. It uses FFmpeg/Ghostscript on the ephemeral runner, processes jobs independently, and refreshes the Telegram media index.

## 7. Cloudflare website streaming worker

The lightweight Cloudflare worker no longer needs the MTProto `teleproto` bundle for website media delivery. It looks up the server-side `telegram_media_index`, calls Telegram Bot API `getFile`, and proxies the returned Telegram file response to the browser.

Configure these Worker secrets/variables:

`TELEGRAM_BOT_TOKEN`
`SUPABASE_URL` — website content/access database
`SUPABASE_PUBLISHABLE_KEY` — website public key
`SUPABASE_SERVICE_ROLE_KEY` — fallback only when the media index is in the same project
`MEDIA_INDEX_SUPABASE_URL` — Store Keeper project URL when it differs from the website project
`MEDIA_INDEX_SUPABASE_SERVICE_ROLE_KEY` — Store Keeper project service-role/secret key
`HJ_WEB_BASE_URL`
`MEDIA_TICKET_SECRET`
`CORS_ALLOWED_ORIGINS`
`MEDIA_TICKET_SECRET`
`CORS_ALLOWED_ORIGINS`

For website streaming, compressed media should remain below the Telegram Bot API 20 MB download limit; the worker intentionally returns a clear 413 error for larger indexed files instead of falling back to the old CPU-heavy MTProto path.

