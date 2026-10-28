# Contributing to Blynky

Thanks for helping! Bug reports, fixes and improvements are all welcome.

## Before you start

- **Bugs:** open an issue with what you expected, what happened, and the
  relevant log lines. Say whether you run Blynky under Docker or `uv`, and
  which camera models and sync modules you have, since Blink hardware behaves
  in surprisingly different ways.
- **Features and larger changes:** open an issue first to discuss them, so
  nobody spends time on something that will not be merged.
- **Security problems:** do not open an issue. See [SECURITY.md](SECURITY.md).

> [!IMPORTANT]
> Never post real camera stills, clips, descriptions, ntfy topics, Blink
> account details or the contents of `.blink_creds.json` in an issue or pull
> request. Remove camera names and locations from logs too.

## Development setup

Running without Docker needs Python 3.10+, [uv](https://docs.astral.sh/uv/)
and `ffmpeg`. You also need blinkpy checked out next to this repo, at the
commit pinned in the `Dockerfile`:

```bash
git clone https://github.com/gudlyf/blinkpy.git ../blinkpy
git -C ../blinkpy checkout "$(sed -n 's/^ARG BLINKPY_REF=//p' Dockerfile)"

cp .env.example .env
uv run run.py
```

To try the Docker build, run `docker compose up -d --build`.

## Making a change

1. Fork the repo and create a branch from `main`.
2. Keep the change focused. One fix or feature per pull request is easiest to
   review.
3. Run the linter. It covers formatting rules and docstrings:

   ```bash
   uv run ruff check .
   ```

4. Test it against real cameras if you can, and say in the pull request what
   you tried. Blink's API is unofficial and is not mocked here, so a
   description of what you ran is the test plan.
5. Update the documentation when behaviour or settings change. Each new
   environment variable needs a line in `.env.example`, the configuration
   table in `README.md` and, if it needs explaining, `docs/guide.md`.

### Things to keep in mind

- **Don't break existing archives.** People have months of captures in
  `DATA_DIR`. The sidecar JSON, the folder layout and `cameras.json` must keep
  reading what older versions wrote.
- **Go easy on Blink.** Don't poll more often than once a minute, and don't
  wake battery cameras unless the user asked for it.
- **blinkpy changes go on the fork's `blynky` branch.** If a fix belongs in
  blinkpy, open a pull request against the `blynky` branch of
  [gudlyf/blinkpy](https://github.com/gudlyf/blinkpy), so that `uv run` and
  the Docker image keep using the same code. Better yet, also send it
  upstream to [fronzbot/blinkpy](https://github.com/fronzbot/blinkpy).
- **Match the surrounding code.** Comments explain *why*, not *what*. The
  front end is plain JavaScript and CSS with no build step, and should stay
  that way.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/), for example
`fix(monitor): …`, `feat(ui): …` or `docs: …`. Pull requests are squashed when
merged, so the pull request title is what ends up on `main`. Write it the same
way.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
