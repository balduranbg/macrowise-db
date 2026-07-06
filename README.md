# db-builder

Scheduled data build pipeline. Produces a compressed SQLite database from
several public data sources, published as a versioned GitHub Release.

## Building locally

```bash
pip install -r requirements.txt

cd scripts
python3 build_db.py

# Test build (small sample, no compression)
python3 build_db.py --limit 100 --no-compress
```

Some sources require an API key supplied via environment variable — see
`scripts/build_db.py` for details.

## Required GitHub secrets

Configured in repo settings; see `.github/workflows/` for what each one is used for.

## Releases

Each scheduled run creates a GitHub Release containing the built database
and a metadata file describing the build.

## License

Build scripts in this repository are MIT licensed. Underlying data retains
the license terms of its original source.
