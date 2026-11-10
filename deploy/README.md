# Deploying to a small VM (about 1 GB RAM)

Written for a Google Cloud free-tier `e2-micro` running Debian 12 or Ubuntu 22.04+, but any Debian/Ubuntu VM works.
**Status: the scripts were written and syntax-checked, and backup/restore was tested locally. `install.sh`, the
systemd units and Caddy have NOT been run on a real VM yet.** Expect to fix small things on the first run.

## What you need first

- A VM with a public IP. Check Google's current free-tier terms (machine type, regions, disk size, network egress)
  before relying on "free": they change.
- A DNS name pointing at the VM's IP (any provider). Caddy gets a free HTTPS certificate for it automatically,
  which needs ports 80 and 443 open. Without HTTPS, logins travel in clear text.
- Open ports 80 and 443 in the cloud firewall (on Google Cloud: allow `tcp:80,443`, e.g. via the `http-server` and
  `https-server` network tags). Do **not** open port 8000: the API listens on 127.0.0.1 only.
- Accounts and keys: OpenAI, Groq, Qdrant Cloud (`QDRANT_URL`, `QDRANT_API_KEY`). Free tiers may use your prompts, and
  uploaded text goes to OpenAI and Groq. Use sample data only.

## Steps

On your PC (Node 18+ installed):

```bash
bash deploy/make-bundle.sh                       # builds the UI, writes ../erag-bundle-<date>.tgz
scp ../erag-bundle-*.tgz you@VM_IP:~
```

On the VM:

```bash
tar xzf erag-bundle-*.tgz
sudo bash erag/deploy/install.sh --domain rag.example.com
sudo nano /opt/erag/.env                         # add the keys (OpenAI, Groq, Qdrant, Langfuse); JWT_SECRET is generated
                                                 # and set tracing.environment to "vm" in /opt/erag/config/config.yaml
sudo systemctl restart erag
cd /opt/erag && sudo -u erag .venv/bin/python -m scripts.create_admin --username admin --role admin
curl -s http://127.0.0.1:8000/health
```

Then open `https://rag.example.com`, sign in, upload a document.

To update later: build a new bundle on your PC, copy it over, extract, and run `install.sh` again with the same
arguments. It replaces code and dependencies and never touches `/opt/erag/.env` or `/opt/erag/data`.

## What the installer sets up

| Piece | Details |
|---|---|
| Swap | 2 GB swapfile, `vm.swappiness=10` (parsing a PDF can spike memory) |
| App user | `erag`, no login shell, owns `/opt/erag` |
| API | `erag.service`: one uvicorn worker on 127.0.0.1:8000, memory limit 800 MB (soft 650 MB), filesystem mostly read-only |
| HTTPS | Caddy from the distro package: automatic certificate, security headers, streaming not buffered, 25 MB upload cap |
| Secrets | `/opt/erag/.env`, mode 600, owned by `erag` |
| Backups | `erag-backup.timer` runs `erag-backup` nightly at about 03:15 and keeps the newest 7 |

## Backups

`erag-backup` saves the SQLite database (a consistent copy) plus `data/raw`, `data/assets` and `data/captions` to
`/var/backups/erag`. It does **not** save Qdrant vectors; those live in Qdrant Cloud and are rebuilt from the raw
files with `scripts.reindex` (below).

- **Encrypt them:** install `age`, create a key pair on your PC (`age-keygen`), keep the private key off the VM, and put
  `AGE_RECIPIENT=age1...` (the public key) in `/etc/erag-backup.env`. Without it the archive is plain.
- **Copy off the VM:** a backup on the same disk is lost if the VM is deleted. Set `GCS_BUCKET=name` in the same file
  (needs `gsutil` and permission), or copy `/var/backups/erag` somewhere yourself.
- **Test a backup now:** `sudo -u erag erag-backup`, then restore into a scratch folder:
  `APP_DIR=/tmp/restore-test NO_SERVICE=1 erag-restore /var/backups/erag/erag-<stamp>.tgz`.
- **Restore for real:** `sudo AGE_IDENTITY=/path/to/key.txt erag-restore <file>`. The current data folder is moved
  aside, not deleted.
- **Rebuild Qdrant** after a restore, or when the collection was lost or recreated:
  `cd /opt/erag && sudo -u erag .venv/bin/python -m scripts.reindex --all` (add `--dry-run` first to see the list).
  Every chunk is embedded again, so this costs OpenAI embedding calls; captions come from `data/captions`.

## Failed and interrupted ingestions

- Ingestion runs inside the API process. If the service restarts mid-ingestion, the next start marks those
  documents `failed` ("Interrupted by a server restart") so the same file can be uploaded again.
- Retry every document whose newest version failed:
  `cd /opt/erag && sudo -u erag .venv/bin/python -m scripts.reindex --failed`. Exit code 1 if any still fail.
- After changing the caption prompt or chunking, `scripts.reindex --all` applies it to existing documents; only
  changed chunks are embedded again.

## Day-to-day

```bash
sudo systemctl status erag caddy            # is it running
sudo journalctl -u erag -f                  # API logs
sudo journalctl -u caddy -f                 # proxy / certificate logs
free -m                                     # memory; watch it while ingesting a large PDF
sudo systemctl list-timers erag-backup.timer
```

## Things to verify on the first real run

- Memory: ingest a real PDF and watch `free -m`. If Presidio plus parsing is too heavy, set `pii.mode: off` in
  `config/config.yaml` (and restart), or use a bigger VM.
- Caddy gets a certificate (needs the DNS name to resolve to the VM and ports 80/443 open).
- Streaming answers arrive word by word through Caddy, and source images load (the page's content-security-policy
  only allows images from the same origin).
- fastembed caches its BM25 files under `/opt/erag/data/cache` (set through `FASTEMBED_CACHE_PATH`; confirm the first
  start finds them after a restart instead of downloading again).
