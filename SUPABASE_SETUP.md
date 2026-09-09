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
