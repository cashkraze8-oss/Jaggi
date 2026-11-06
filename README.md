# Number Info Telegram Bot

Telegram bot starter with MongoDB persistence, mandatory channel membership, `/admin` controls, user statistics, broadcast, and configurable reward settings.

## Important API note
The supplied CLI script calls a phone-number lookup endpoint that can return personal subscriber information. This Telegram bot intentionally does **not** call that endpoint or display subscriber identity/address/other private records. The Number Info button only validates international phone-number format. Do not connect an API that exposes private subscriber data.

## Deploy on Render
1. Push these files to a private GitHub repository.
2. In Render, create a **Background Worker** from that repository.
3. Render can use `render.yaml`; add the environment variables:
   - `BOT_TOKEN`: Telegram bot token from BotFather
   - `ADMIN_ID`: your numeric Telegram user ID
   - `MONGODB_URI`: MongoDB Atlas connection string
   - `DB_NAME`: optional, defaults to `number_info_bot`
4. Create a MongoDB Atlas database and allow Render's outbound IPs (or temporarily use the provider's documented network access option).
5. Add the bot as an administrator to each force-join channel, with permission to view members.
6. Deploy.

## Admin commands
- `/admin` opens the owner-only admin panel.
- Force join > Add channel: send `@channelusername | https://t.me/channelusername`
- The bot must be administrator in each channel for membership checks to work.
- Broadcast: send any Telegram message after selecting Broadcast; the bot copies it to registered users.
- Bot settings: configure signup bonus, referral reward, minimum redeem, and toggle referral/redeem.

## Notes
- Rewards start at zero and both reward systems start disabled.
- Referral attribution and actual redemption/payment processing are not enabled in this starter; the panel stores the settings for a later safe implementation.
- Active users are users who have interacted with the bot within the last 24 hours or 7 days.
- Never commit `.env` or credentials to GitHub.
