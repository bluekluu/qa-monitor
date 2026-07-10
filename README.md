# QA Monitor

Static QA report site for Kevin's recurring software QA checks.

Live site:

```text
https://bluekluu.github.io/qa-monitor/
```

Daily runs are handled by GitHub Actions in `.github/workflows/daily-qa.yml`.

Required repository secret:

- `LCC_LIVE_URL` — private Longevity Command Center dashboard URL.

Optional repository secret:

- `QA_BUGBOT_TOKEN` — fine-grained PAT for filing/updating issues in monitored repos when the default `GITHUB_TOKEN` cannot access them.
