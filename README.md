# Anchor Games catalog exporter

This repository generates an `ankergames.json` catalog from Anchor Games' public sitemaps and game pages.

## Automatic updates

`.github/workflows/update-ankergames.yml` runs the exporter every day at **03:00 UTC** and **15:00 UTC**. It can also be started manually from the Actions tab. The workflow commits `ankergames.json` only when its contents change.

GitHub Actions schedules use UTC and can be delayed during periods of high platform load.

## Local usage

```bash
python export_ankergames.py --output ankergames.json
```

The generated object follows the reference `name`/`downloads` schema. Public sitemap and detail pages provide the URL, update date, display version, build, and file size. Download `uris` are not exposed by the public pages, so they are emitted as empty arrays unless a previous JSON is supplied with `--existing-json`.

After the first successful workflow run, the public catalog is available at:

```text
https://raw.githubusercontent.com/tutw/ankergames-catalogo/main/ankergames.json
```
