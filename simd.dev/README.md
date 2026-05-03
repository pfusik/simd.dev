# simd.dev — landing page

Static single-page site that describes the project and provides a small
intrinsic-search box. Designed to be served at the project's domain.

```
simd.dev/
├── index.html       # the page
├── app.js           # search + result-card logic (~150 lines)
├── styles.css       # ~200 lines, no framework
├── sync.sh          # copies dist artifacts from ../simd-tooltip/dist/
├── dist/            # copy target — gitignored
└── README.md        # this file
```

## Develop locally

```sh
( cd simd.dev && ./sync.sh )                    # populate dist/
( cd simd.dev && python3 -m http.server 8000 )  # http://localhost:8000/
```

The page eagerly loads `dist/simd-names.json` (~95 KB gzipped) so search
lights up immediately, and lazily loads `dist/simd-data.json` (~450 KB
gzipped) the first time you click a result.

## Deploy

The directory is self-contained after `sync.sh` runs. Point any static
host (GitHub Pages, Cloudflare Pages, Netlify, S3+CloudFront, …) at it.

A simple deploy recipe:

```sh
( cd simd.dev && ./sync.sh )
rsync -a --delete simd.dev/ user@host:/var/www/simd.dev/
```

For GitHub Pages, push `simd.dev/` as a separate site root or configure
Pages to serve from `/simd.dev/` in this repo.

## What runs in the page

- `dist/simd-tooltips.js` — the same library that ships under
  [`simd-tooltip/`](../simd-tooltip), loaded with `data-on="hover+?"`
  so any intrinsic name in the prose pops a tooltip when you hover.
- `app.js` — the search box wiring. Substring match (prefix-first),
  shows up to 80 results as clickable pills; click renders the full
  card inline.

## Updating

Whenever upstream changes (`scripts/build_all.sh --refresh`), re-run
`sync.sh` and re-deploy. The vendored files in `dist/` are gitignored
to avoid committing two copies of the same data.
