# Patreon Downloader

Brutally simple, lighting fast. One script that gets everything you paid for (embedded videos included).

## Usage

1. Install this package. Use editable mode (`-e`) in case any troubleshooting is required. Python 3.11 or higher recommended.

   ```
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install -c requirements.txt -e .
   ```

1. Make a copy of `config.json.tpl` and name it `config.json`.

   ```
   cp config.json.tpl config.json
   ```

1. Follow [instructions](https://github.com/patrickkfkan/patreon-dl/wiki/How-to-obtain-Cookie) and borrow your Patreon browser session. Fill out `cookies` and `user_agent` fields in `config.json`.

   Session expires, so you may need to repeat this step eventually.

1. Run the script. Your content will be waiting for you in `downloads/`.

   ```
   pdl
   ```

1. (Optional) If you encounter errors downloading embedded videos, the video provider may require you to login. Login to the video provider in a browser and use `--browser` to share the cookies. You will be prompted for system password. For example:

   ```
   pdl --browser chrome
   ```

## About

This project is heavily inspired by [patreon-dl](https://github.com/patrickkfkan/patreon-dl). It rewrites the core functionality for extremely fast content downloading, while simplifying and compressing everything into a single source file, which makes it easy to use and hacker friendly at the same time.

Enjoy.
