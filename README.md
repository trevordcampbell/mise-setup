# Trevor's macOS mise setup

Reproducible host setup for an Apple Silicon Mac. This repository is the live global mise configuration at `~/.config/mise`. Development projects continue to use their devcontainers.

`config.toml` declares nine tools, four native command-line packages, 19 desktop app casks, Poppins, MarkText, shell repositories, managed shell files, Google variable fonts, and automatic update jobs. Native packages use mise's built-in `brew` and `brew-cask` backends; a Homebrew installation or executable is not required. Native libraries are resolved automatically.

## Restore on a new Mac

1. Install Apple's Command Line Tools if Git is unavailable. Run `xcode-select --install`, finish Apple's installer, then confirm `git --version` works. This avoids bootstrap offering to install Homebrew to obtain Git.
2. Install the official mise binary:

   ```sh
   curl -fsSL https://mise.run | sh
   ```

3. Adopt this public repository and apply the complete configuration. GitHub sign-in is not needed to restore:

   ```sh
   "$HOME/.local/bin/mise" -C "$HOME" bootstrap --adopt trevordcampbell/mise-setup --yes --force-dotfiles
   exec zsh -l
   ```

`--adopt` clones the repository into `~/.config/mise`, so later commands read it as their global configuration. `--force-dotfiles` replaces existing shell startup files with this setup; review those files first when restoring onto a Mac with custom settings. An existing nonempty `~/.config/mise` that is not this repository must be reviewed and merged or moved aside before adoption. On an already adopted Mac, pull and reconcile the complete setup with `mise -C "$HOME" run setup:sync`. Local Git edits are preserved and reported, and missing native packages are installed before upgrades. You can also preview with `mise -C "$HOME" bootstrap --dry-run`; apply with `mise -C "$HOME" bootstrap --yes --skip-dirty`. Add `--update` to fast-forward the configuration checkout first.

Mise must live at `~/.local/bin/mise`, because the update jobs use that stable path. Initial native package setup may ask for administrator authentication to create `/opt/homebrew`. Tools are installed before the font hook and update jobs need them; the final bootstrap hook generates shell completions and configures a stable GitHub credential helper that survives gh upgrades.

This manifest targets macOS ARM64, not a Linux server or Intel Mac. App logins, licenses, preferences, terminal font selection, documents, OrbStack containers, and application databases still need their own restore or sign-in. App Store applications, Zoom, RODE Central, licensed vendor applications, and macOS system applications are outside this manifest. No project configurations, APFS volumes, or Time Machine settings are changed.

## Python and uv

Mise manages the current Python interpreter and `uv@latest`; both join automatic tool updates. Pyenv is not installed. `UV_PYTHON_PREFERENCE=system` makes uv prefer Python already supplied by Mise. A project that explicitly requires another Python version can still ask uv to download it when needed.

Use `uv venv` to create an environment, `uv pip install` for packages inside it, `uv sync` / `uv run` for projects already configured for uv, and `uvx` for temporary Python command-line tools. Existing development projects and devcontainers are not converted automatically. For a new environment using the current host Python explicitly, run `uv venv --python "$(mise which python)"`.

The optional terminal tools `cmatrix`, GNU `gzip`, `tree`, and `wget`, the disabled `fzf-tab` checkout, and the Supabase CLI are intentionally excluded. macOS supplies `gzip`, `gunzip`, and `curl`; the shell's `lt` and `lta` functions use eza for tree views.

Reference: [uv Python discovery and preferences](https://docs.astral.sh/uv/concepts/python-versions/).

Official references: [install mise](https://mise.jdx.dev/installing-mise.html), [bootstrap](https://mise.jdx.dev/bootstrap.html), and [adopt a global configuration repository](https://mise.jdx.dev/bootstrap/from-repository.html).

## Automatic updates

The maintenance LaunchAgent runs at login/load and daily at 03:00. It updates mise, global tools, rustup, native packages, idle desktop apps, fonts, and clean shell repositories, including this configuration checkout. Running applications wait for a later run. Repository updates only fast-forward and skip local edits. Mise, tool, and MarkText releases observe a 24-hour minimum age; rustup uses its official stable self-updater. Oh My Zsh's separate updater is disabled.

MarkText resolves the latest eligible stable ARM64 release from its official GitHub releases, enforcing the 24-hour release age, exact download path, and SHA-256 digest. The bootstrap final hook runs the same resolver, including when MarkText is missing on a new Mac. Mise applies its verified artifact from an isolated generated config in `~/.local/state/mise/maintenance/marktext/`, and codesign plus the installed version are checked after installation. `release.json` records verified success. No release fields or generated state are written into the Git checkout. API errors or missing digests fail visibly; they never fall back to an unverified download.

Poppins installs all 18 static styles. Open Sans, Rubik, and Space Grotesk use all variable TTF styles supplied upstream, including italics where available. The font helper downloads from immutable official `google/fonts` revisions, verifies Git blob hashes, keeps OFL licenses locally, and avoids full repository clones. Font sources, caches, receipts, and update logs are regenerated on each machine.

```sh
mise run maintenance:check
mise run maintenance:update
mise run maintenance:status
mise run setup:sync
mise run maintenance:health
mise run maintenance:history
mise run shell:status
mise run shell:update
mise run fonts:check
mise run fonts:update
```

Manual maintenance uses one animated progress line in a terminal, followed by a summary of actual installed changes, deferred apps, skipped items, and errors. Unchanged checks do not scroll past. Redirected output and scheduled jobs use a compact plain-text summary; terminal color honors `NO_COLOR`.

The updater compares installed state before and after each stage: Mise and global tool versions, app versions/builds, all linked native package/dependency versions, repository commits, deployed font contents, and shell completion contents. It groups changed font styles by family and shows shortened commit/build hashes in the terminal. Full values and both state snapshots stay in the JSON report. Source-revision bookkeeping alone does not count as a font update. Shell completion regeneration is reported separately, even when its content stays identical.

Failed stages retain any observed partial changes. Missing or unreadable state is reported as unverified, never as unchanged. Running apps first receive a native dry-run check: confirmed current apps are not deferred, confirmed outdated apps are pending until closed, and ambiguous versions are reported as unverified. Native checks use the same platform metadata and ownership rules as Mise installation. No apps are quit automatically. Repeated confirmed deferrals retain their first deferred timestamp and consecutive run count. Snapshots describe observed changes during the stage; a vendor or another updater acting concurrently can also change installed state. Clean repository updates fast-forward; dirty repositories are explicitly listed as preserved.

Preview mode applies no updates. It uses Mise's structured `outdated` output for global tools, the font helper's structured plan, and native package-manager dry-run plans. Available versions are separate from installed changes. Native plan formats that cannot be interpreted are flagged, with their full output kept in the log. MarkText retains its verified release resolver. Full command logs and JSON state reports are always saved locally.

```sh
# Show every stage and underlying command output live, while also saving the log:
mise -C "$HOME" run maintenance:update --verbose

# Emit a JSON report on stdout (suitable for redirecting or piping):
mise -C "$HOME" run maintenance:update --json
mise -C "$HOME" run maintenance:check --json
mise -C "$HOME" run maintenance:status --json

# The status command displays the most recent update without running updates:
mise -C "$HOME" run maintenance:status
```

`--verbose` and `--json` belong after the task name. They can be combined: the JSON report goes to stdout and command output goes to stderr. `changes` lists observed installed changes, `planned_changes` lists available updates, and each stage retains its `before`/`after` snapshots and any verification errors. Reports include `schema_version` of `3`; older reports are displayed without inventing change details. Preview mode also accepts `--verbose`, which exposes the underlying update plans. JSON stdout contains no progress messages or log-path trailer; the log path is included in the report. `last-run.json`, `last-check.json`, `last-manual.json`, and `last-scheduled.json` live in `~/.local/state/mise/maintenance/`; command output is kept in `~/Library/Logs/mise-maintenance.log`.


Maintenance pulls clean configuration/shell checkouts first, then invokes bootstrap in a fresh process to read the updated manifest. Bootstrap reconciles tools, repositories, dotfiles, hooks, and services; guarded package stages apply missing declarations and upgrade existing ones, letting Mise resolve dependencies. Dirty checkouts are preserved with a visible note, never reset or automatically committed. Removing a manifest entry does not automatically delete its application data or uninstall its package.

`setup:sync` performs the same complete reconciliation and update as `maintenance:update`. Nightly runs avoid reloading their own LaunchAgent mid-update; run `setup:sync` after changing the agent's schedule or arguments. The health command checks the installed Mise configuration, the running tool watcher, the loaded nightly agent and its scheduled argument, the last service exit codes, Mise doctor warnings, and a successful scheduled report no older than 36 hours. It exits nonzero when attention is needed. A manual update never overwrites the scheduled report. A sleeping/offline Mac can legitimately report stale maintenance; running a manual update does not pretend to repair that evidence.

The most recent 60 reports (updates and previews) are retained under `history/`. `maintenance:history --limit 20` shows origins and counts, and `--json` provides full reports. Installed changes during a run remain separate from `since_previous_run`: observed changes between the preceding update's final inventory and this run's initial inventory. The latter can come from Mise's background watcher or app self-updaters; no actor is inferred. Unreadable stages are excluded from that comparison and retain explicit inventory errors. Reports from before this feature provide no baseline, so the first new run makes no between-run claims.

The command log and both launchd output logs rotate at the start of maintenance after exceeding 5 MB, keeping one previous copy each. Launchd logs use copy-and-truncate rotation so their open file descriptors remain valid. Limits are applied between runs; an unusually verbose single run can exceed the threshold until the next invocation.

```sh
mise -C "$HOME" run maintenance:health --json
mise -C "$HOME" run maintenance:history --limit 20
mise -C "$HOME" run maintenance:history --json
```

## Save configuration changes

The global config, scripts, and tests are ordinary files in this checkout. The six shell startup/prompt files are symlinks into `dotfiles/`. Editing `~/.zshrc` or `~/.p10k.zsh` therefore edits the tracked source. The `dotfiles` shell alias runs Git in `~/.config/mise`:

```sh
dotfiles status
dotfiles diff
dotfiles add config.toml dotfiles/.zshrc dotfiles/.p10k.zsh
dotfiles commit -m "Update host setup"
dotfiles push
```

Changes are not automatically committed or pushed. Review and push changes you want available on other machines, including additions made with `mise use -g`. Use `mise -C "$HOME" run setup:sync` on another already adopted Mac to pull and apply saved changes.

Tokens, passwords, SSH keys, shell histories, recordings, and app data are not stored here. Keep machine-specific private shell settings in `~/.config/zsh/local.zsh`, which the Zsh startup file sources if present. GitHub authentication stays in GitHub CLI's local credential storage. Sign in with `mise exec -- gh auth login --hostname github.com --git-protocol https --web` when you want to push changes; reading and restoring this public repository works without authentication. This checkout uses GitHub's noreply commit identity to avoid exposing a personal email address. The previous public dotfiles repository remains a historical snapshot; this repository is now the active configuration.

## Verification

```sh
mise exec -- python "$HOME/.config/mise/tests/test_maintenance.py"
mise exec -- python "$HOME/.config/mise/tests/test_fonts.py"
mise -C "$HOME" doctor
mise -C "$HOME" bootstrap --dry-run
```

GitHub Actions runs portable tests on Linux/Python 3.11 and macOS/Python 3.15 on each push and pull request, plus TOML/restore-source validation, Mise task graph checks, Python compilation, and shell syntax checks. Actions are pinned to verified commits with read-only repository permissions. Tests use temporary configs and app bundles and run against an empty home directory; CI never applies the desktop manifest. Tests cover fresh MarkText installation and official artifact verification, config synchronization ordering, separate scheduled health, bounded history and log rotation, between-run changes, native app deferrals, previews, interrupts, and verified font updates. A bootstrap preview confirms declaration order and resource state without installing packages, starting services, or running hooks.
