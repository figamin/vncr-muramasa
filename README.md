# muramasa

A Discord bot for a Japanese visual novel reading community. It is not maintained, and issues and pull requests are not answered.

## What it does

- **VN of the Month**: a pool of titles, monthly picks and rendered banners.
- **Immersion tracking**: logs reading, listening and watching time, with goals, statistics, charts and profile cards. A botsu import exists in the code, but its command is switched off.
- **kizuna**: logs the reading sessions the kizuna texthooker posts through its webhook.
- **Joining**: an in-server application with a Kotoba quiz and moderator review.
- **Moderation**: a word filter that reposts cleaned messages, media flagging by member vote, and bulk message purges.
- **Server tools**: highlights, sticky messages, role menus, bump reminders and error logging.
- **Backups**: scheduled database backups to a channel, and a full backup and restore of a server's structure.
- **Web console**: a Flask admin page for the bot's settings and data.
- **sakura**: a smaller bot for a second server, started from the same process (`sub-bots/anime-bot/`).

## Running it

You need Docker, or Python 3.11 with `pip install -r requirements.txt`.

1. Create a bot in the Discord developer portal and turn on the Server Members, Message Content and Presence intents.
2. Put its token in `token.json`: `{"token": "your-bot-token"}`.
3. Create `config.json` containing `{}`. The `/manage_*` commands and the web console fill it in.
4. Copy `.env.example` to `.env` and fill it in. The web console does not start without a password.
5. Run `docker compose up -d --build`, or `python main.py`.

The compose file also mounts the hikaru bot's data from `../hikaru/` and sakura's token and settings from `sub-bots/anime-bot/`; remove those lines if you don't run them. Data is kept in `data/`.

The web console listens on port 5000. Under Docker it is published on `CONSOLE_BIND_IP`; run directly, it listens on `WEB_CONSOLE_HOST`. Both default to 127.0.0.1. It asks for the login set in `.env` and serves plain HTTP, so keep it on a private address.

No images are included. `/motivation` posts a random file from `assets/motivation/`, the welcome message picks a random `assets/other/banner*.webp`, and the join flow's entry message uses `assets/other/hikaru_banner` (webp, png, jpg or gif). Each works without them; add your own to use them.

Some messages link to vnclub.org pages, which are offline.

## License

[GNU Affero General Public License v3.0](LICENSE). Third-party parts and their licenses are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
