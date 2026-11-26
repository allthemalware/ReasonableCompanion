# ReasonableCompanion

Installs every Reason sound pack your account can access, using the same live
catalog as Reason Companion. Python 3.9+, no dependencies. Windows, macOS, and
Linux (Wine/Bottles).

## Usage

```bash
python3 download_packs.py --dry-run   # list what would be installed
python3 download_packs.py             # install
```

By default it uses Companion's signed-in session (read-only; it is never
renewed, so Companion stays signed in). That session expires within minutes
when Companion is idle, so for long runs sign in separately:

```bash
python3 download_packs.py --login
```

On Linux, point it at the prefix:

```bash
python3 download_packs.py --wine-prefix /path/to/prefix [--wine-user NAME]
```

## Options

| Option | Description |
| --- | --- |
| `--dry-run` | List packs; write nothing. |
| `-o, --output DIR` | Install somewhere other than Companion's Packs folder. |
| `--wine-prefix PATH`, `--wine-user NAME` | Wine/Bottles prefix and user. |
| `--start ID`, `--end ID` | Limit to a pack ID range. |
| `--login`, `--username NAME --password-stdin` | Sign in with your account. |
| `--no-sync` | Don't register installs with your account. |
| `-v, --verbose` | Show progress and warnings. |

## Notes

- Installed packs are skipped and never modified; re-running resumes.
- Packs are unpacked beside the Packs folder and moved in when complete.
- On macOS with python.org Python, run *Install Certificates* once.

## Tests

```bash
python3 -m unittest discover -s tests
```

## License

[MIT](LICENSE)
