# Image catalog preview

This directory contains the static catalog presentation and its current GHCR
snapshot. The page renders every OpenStack release, Kolla version, base OS, and
profile from generated catalog data; it does not contain a hand-maintained image
list. `assets/` contains only the browser application and styling; all catalog
values come from `catalog.json`.

Regenerate the snapshot from the aggregate matrix and public GHCR manifests:

```bash
python3 scripts/generate-image-catalog.py --output docs/catalog/catalog.json
```

For local viewing, serve the directory rather than opening `index.html`
directly:

```bash
python3 -m http.server 8173 --directory docs/catalog
```

`catalog-data.js` is generated from the exact same data as `catalog.json`. It
only provides a `file://` preview fallback; the deployed site reads the static
catalog snapshot.

The Pages workflow stages these files with a newly generated snapshot and is
the only writer of the orphan `gh-pages` branch. Do not edit that branch by
hand.
