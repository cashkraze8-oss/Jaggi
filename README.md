# Universal Media Downloader Bot

Telethon (MTProto) + yt-dlp + MongoDB. Send a link or a song name, pick a format.

## Setup
1. Get `API_ID` / `API_HASH` from https://my.telegram.org and a **new** bot token from @BotFather.
2. Create a free MongoDB Atlas cluster and copy the connection string to `MONGO_URI`.
3. Add the bot as **admin** in every force-subscribe channel.
4. Copy `env.example` values into your environment, then:
   - Docker (recommended, includes ffmpeg + deno): `docker build -t mediabot . && docker run --env-file .env mediabot`
   - Local: install ffmpeg, `pip install -r requirements.txt`, `python bot.py`

On Render use a **Docker** service (not the Python runtime) so ffmpeg is available.

## Why Telethon?
The normal Bot API caps uploads at 50 MB. MTProto bots can upload up to ~2 GB per file.

## Size / quality pipeline
1. Best quality that fits `MAX_SOURCE_MB` is downloaded.
2. Container is remuxed to MP4 (lossless).
3. Under ~2 GB: sent untouched, never re-encoded.
4. Over ~2 GB: x264 CRF 20 re-encode (audio copied), then split with stream copy if still too big.

Re-encoding big 4K files is CPU heavy — on a small free server it can take a very long time.

## Admin commands
`/stats`, `/broadcast` (reply to a message or `/broadcast text`), `/ban <id>`, `/unban <id>`

## Notes
- DRM sites (Spotify, Netflix…) are not downloadable.
- If YouTube/Instagram show "sign in" errors, export a `cookies.txt` from a throwaway account and set `COOKIES_FILE`.
- Keep yt-dlp updated (`pip install -U yt-dlp`), sites change often.
- Only download content you have the right to download.
