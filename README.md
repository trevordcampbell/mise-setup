# Trevor's macOS mise setup

Reproducible host setup for an Apple Silicon Mac. This repository is the live global mise configuration at `~/.config/mise`. Development projects continue to use their devcontainers.

`config.toml` declares the global tools, four native command-line packages, 22 desktop app casks, Poppins, MarkText, shell repositories, managed shell files, Google variable fonts, and automatic update jobs. Native packages use mise's built-in `brew` and `brew-cask` backends; a Homebrew installation or executable is not required. Native libraries are resolved automatically. Claude Code is the global `claude` tool from Mise’s registry; it does not depend on a global npm installation. Its background updater is disabled in the Mise environment so Mise handles updates. The terminal command `codex` comes from the global Mise registry tool `codex@latest` and follows the same 24-hour release-age policy. Run `mise which codex` to locate the standalone CLI. The desktop app maintains its own bundled binary. Claude Desktop and T3 Code retain their built-in app updaters and can be adopted without replacing their installed bundles. Blender is installed and upgraded through Mise’s native cask backend. Its release downloads use Blender’s official mirror service and retain the cask’s checksum verification.

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

`--adopt` clones the repository into `~/.config/mise`, so later commands read it as their global configuration. `--force-dotfiles` replaces existing shell startup files with this setup; review those files first when restoring onto a Mac with custom settings. An existing nonempty `~/.config/mise` that is not this repository must be reviewed and merged or moved aside before adoption. On an already adopted Mac, pull and reconcile the complete setup with `mise run setup:sync`. Local Git edits are preserved and reported, and missing native packages are installed before upgrades. You can also preview with `mise -C "$HOME" bootstrap --dry-run`; apply with `mise -C "$HOME" bootstrap --yes --skip-dirty`. Add `--update` to fast-forward the configuration checkout first.

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
mise run maintenance:dependencies
mise run shell:status
mise run shell:update
mise run fonts:check
mise run fonts:update
```

The tasks are global: run `mise run maintenance:update` from any directory. The task switches to your home directory before launching the updater, and the updater strips inherited Mise overrides from its child environment so host maintenance uses your global setup. A project can override a task with the same name; `mise -C "$HOME" run maintenance:update` remains available to explicitly select the host task.

Interactive maintenance uses one progress line. The summary leads with failures and security advisories, gives one reason and next step for a pending app, and shows installed changes separately from changes observed between runs. Routine checks stay in the detailed report. Redirected output and scheduled jobs use a compact plain-text summary; terminal color honors `NO_COLOR`. Native app downloads show received/total MiB, percentage when known, and transfer speed by observing Mise’s resumable download cache. Progress is also saved to the detailed log every five seconds and streamed with `--verbose`. Existing inactive partial downloads are not displayed as live transfers.

An active app download that stops writing for 120 seconds fails that app’s stage and lets other stages continue. App update commands have a 30-minute total limit; native app previews have a two-minute limit. Slow downloads that keep advancing can continue within the total limit. On timeout or Ctrl-C, both quiet and verbose modes stop the command’s process group, including child processes. Mise retains partial downloads for a later retry; the updater does not delete them or bypass native checksum checks. The detailed log identifies the command and progress preceding an error.

The updater compares installed state before and after each stage: Mise and global tool versions, app versions/builds, all linked native package/dependency versions, repository commits, deployed font contents, and shell completion contents. It groups changed font styles by family and shows shortened commit/build hashes in the terminal. Full values and both state snapshots stay in the JSON report. Source-revision bookkeeping alone does not count as a font update. Bootstrap and maintenance install Zsh completions for Mise and generate Codex completions from the active host CLI. The default summary shows completion changes only when the contents change.

Failed stages retain any observed partial changes. Missing or unreadable state is reported as unverified, never as unchanged. Most running apps receive a native dry-run check: confirmed current apps are not deferred, confirmed outdated apps are pending until closed, and ambiguous versions are reported as unverified. Native checks use the same platform metadata and ownership rules as Mise installation. No apps are quit automatically. Repeated confirmed deferrals retain their first deferred timestamp and consecutive run count. Snapshots describe observed changes during the stage; a vendor or another updater acting concurrently can also change installed state. Clean repository updates fast-forward; dirty repositories are explicitly listed as preserved.

Cursor, Google Chrome, and ChatGPT have independent official stable-release checks. Cursor compares the installed marketing version on its stable channel with the official macOS update endpoint; source commits and artifact hashes are not treated as comparable build numbers. Chrome compares its complete four-part marketing version with Google Version History’s active, fully rolled-out (`fraction=1`) macOS stable releases, excluding partial rollouts. ChatGPT uses the official architecture-specific Sparkle appcast, filters unsupported macOS versions and channels, and compares both the marketing version and Sparkle build number. The terminal highlights pending or inconclusive comparisons. JSON retains all three comparisons with installed and available versions, source URLs, channel, and check time.

Current or newer apps are preserved without invoking native replacement. Confirmed outdated apps still use Mise’s native ownership, download, checksum, and running-app guards. Running apps wait until closed. If the vendor is ahead of the native cask, maintenance still reports the available version and explains that the app updater or a later run is needed; it does not force installation. Failed feeds, unexpected bundle identities, non-stable Cursor channels, or unsupported version formats remain explicitly unverified. Native receipt versions are historical installation records, not proof of an auto-updated app’s present version. These checks use current vendor stable availability, rather than applying the tool/MarkText 24-hour release-age policy to native apps.

OrbStack uses the installed app bundle’s `orbctl update --check`. Its exit status reports current (0) or update available (3) for the app’s configured update channel. The report saves the live bundle version and build with the check result. A check failure leaves the app in place and appears under **Check coverage**. When the app is idle, maintenance can apply a confirmed update through Mise and then asks OrbStack again. An open app waits for a later run.

Changes to this checkout’s updater scripts take effect on the next task or scheduled run; no new shell, app restart, or LaunchAgent reload is needed. Use `mise run maintenance:update` normally. On another already bootstrapped Mac, `mise run setup:sync` pulls and applies the saved setup. An app restart is only relevant when an app itself needs to finish installing an update.

Preview mode applies no updates. It uses Mise's structured `outdated` output for global tools, the font helper's structured plan, and native package-manager dry-run plans. Available versions are separate from installed changes. Native plan formats that cannot be interpreted are flagged, with their full output kept in the log. MarkText retains its verified release resolver. Full command logs and JSON state reports are always saved locally.

```sh
# Show every stage and underlying command output live, while also saving the log:
mise run maintenance:update --verbose

# Emit a JSON report on stdout (suitable for redirecting or piping):
mise run maintenance:update --json
mise run maintenance:check --json
mise run maintenance:status --json

# The status command displays the most recent update without running updates:
mise run maintenance:status
```

`--verbose` and `--json` belong after the task name. They can be combined: the JSON report goes to stdout and command output goes to stderr. `changes` lists observed installed changes, `planned_changes` lists available updates, and each stage retains its `before`/`after` snapshots and any verification errors. Reports include `schema_version` of `4`; older reports are displayed without inventing change details. Preview mode also accepts `--verbose`, which exposes the underlying update plans. JSON stdout contains no progress messages or log-path trailer; the log path is included in the report. `last-run.json`, `last-check.json`, `last-manual.json`, and `last-scheduled.json` live in `~/.local/state/mise/maintenance/`; command output is kept in `~/Library/Logs/mise-maintenance.log`.


Maintenance pulls clean configuration/shell checkouts first, then invokes bootstrap in a fresh process to read the updated manifest. Bootstrap reconciles tools, repositories, dotfiles, hooks, and services; guarded package stages apply missing declarations and upgrade existing ones, letting Mise resolve dependencies. Dirty checkouts are preserved with a visible note, never reset or automatically committed. Removing a manifest entry does not automatically delete its application data or uninstall its package.

`setup:sync` performs the same complete reconciliation and update as `maintenance:update`. Nightly runs avoid reloading their own LaunchAgent mid-update; run `setup:sync` after changing the agent's schedule or arguments. The health command checks the installed Mise configuration, the running tool watcher, the loaded nightly agent and its scheduled argument, the last service exit codes, Mise doctor warnings, and a successful scheduled report no older than 36 hours. It exits nonzero when attention is needed. A manual update never overwrites the scheduled report. A sleeping/offline Mac can legitimately report stale maintenance; running a manual update does not pretend to repair that evidence.

The most recent 60 reports (updates and previews) are retained under `history/`. `maintenance:history --limit 20` shows origins and counts, and `--json` provides full reports. Installed changes during a run remain separate from `since_previous_run`: observed changes between the preceding update's final inventory and this run's initial inventory. The latter can come from Mise's background watcher or app self-updaters; no actor is inferred. Unreadable stages are excluded from that comparison and retain explicit inventory errors. Reports from before this feature provide no baseline, so the first new run makes no between-run claims.

The command log and both launchd output logs rotate at the start of maintenance after exceeding 5 MB, keeping one previous copy each. Launchd logs use copy-and-truncate rotation so their open file descriptors remain valid. Limits are applied between runs; an unusually verbose single run can exceed the threshold until the next invocation.

```sh
mise run maintenance:health --json
mise run maintenance:history --limit 20
mise run maintenance:history --json
```

## Inspect the host setup

Manual updates, nightly updates, and previews run `mise audit --json` from the home directory after the global tool step. The audit checks the active host tool versions against OSV. Each finding shows the tool, installed version, advisory ID, description, and any fixed versions listed by OSV. A listed fix may still be outside the eligible release range. Tools without an advisory source appear under **Check coverage**. Coverage is limited to Mise tools and excludes desktop casks and native formulae.

When Mise lists Rust as unchecked, maintenance also checks the Rust project’s published repository advisories for the compiler, standard library, and Cargo. It reads the active `rustc -Vv` and `cargo -Vv` releases and commits. For Cargo’s 0.x advisory ranges it reads the crate version from the upstream manifest at the installed Cargo commit. The JSON report includes source URLs, versions, commits, and the number of advisories evaluated under `results.audit.rust_checks`. Matches describe affected versions. Platform-specific conditions appear in the linked advisory. This check covers those two repositories. Project dependencies, rustup, and optional components have separate advisory coverage. [Rust project advisories](https://github.com/rust-lang/rust/security/advisories) · [Cargo advisories](https://github.com/rust-lang/cargo/security/advisories)

Findings are printed even when the update itself succeeds. An unavailable or unreadable audit fails the audit step and the overall command, and other stages continue. Reports store the audit under `results.audit`. Use `mise run maintenance:status` to see the most recent update or `mise run maintenance:history` for advisory counts from past runs. Service health reports the last update’s audit separately. The existing 24-hour update policy still applies. To query OSV directly, run `mise -C "$HOME" audit`. [Mise audit reference](https://mise.jdx.dev/cli/audit.html)

Maintenance also runs a separate dependency scan after the tool-version audit. It discovers every version in `mise ls --global --current --json` and scans its installation directory inside the user’s Mise install store. Adding a tool to the global configuration automatically includes it on the next run. External links, devcontainer workspaces, desktop apps and the native package prefix are outside this scan. OSV-Scanner itself is installed and updated by Mise.

Google’s OSV-Scanner reads installed package metadata, binary inventories, lockfiles and software bills of materials. A Cargo receipt for a locked crates.io install also allows maintenance to verify the exact published crate archive’s SHA-256 and read its packaged `Cargo.lock`. That lock can include optional, build or platform-specific dependencies. For binaries that retain Cargo registry paths, maintenance can recover additional names and versions. Those paths give partial evidence. Embedded Rust inventories take precedence for the same binary. OSV receives package identities for advisory lookup. The scan uses static data with dependency resolution and call analysis disabled. [Supported OSV-Scanner artifacts](https://google.github.io/osv-scanner/supported-languages-and-lockfiles/)

Run `mise run maintenance:dependencies` for the latest saved scan. The overview ranks tools by the highest published numeric advisory score and counts matches with a listed dependency fix. Unscored matches remain visible. The coverage section identifies tools with partial or missing inventories. The report uses your local time and adapts to terminal width. After two successful scans with comparable tool coverage, it also shows how many matches appeared or disappeared.

```sh
mise run maintenance:dependencies --tool python           # Findings for one installed tool
mise run maintenance:dependencies --tool zpl --verbose    # Evidence paths and advisory links
mise run maintenance:dependencies --verbose              # Every finding grouped by tool
mise run maintenance:dependencies --json                 # Full saved inventory and findings
```

`--tool` accepts a Mise ID, display name or unique prefix. Add `--json` to export the selected tool’s record. A match counts one advisory group for a package version in one owning tool. Multiple copies and aliases count once. The advisory score describes the dependency. Exposure depends on how the tool uses it. Maintenance picks up fixes through eligible releases of the owning tool. Preview results include `results.dependency-audit`, and scanner failures appear alongside independent checks.

Run `mise -C "$HOME" bootstrap status --missing` to see which declared resources differ from the machine. The exit status is nonzero when there is drift. Use `mise run maintenance:check` to preview available version updates.

## Save configuration changes

The global config, scripts, and tests are ordinary files in this checkout. The six shell startup/prompt files are symlinks into `dotfiles/`. Editing `~/.zshrc` or `~/.p10k.zsh` therefore edits the tracked source. The `dotfiles` shell alias runs Git in `~/.config/mise`:

```sh
dotfiles status
dotfiles diff
dotfiles add config.toml dotfiles/.zshrc dotfiles/.p10k.zsh
dotfiles commit -m "Update host setup"
dotfiles push
```

Changes are not automatically committed or pushed. Review and push changes you want available on other machines, including additions made with `mise use -g`. Use `mise run setup:sync` on another already adopted Mac to pull and apply saved changes.

Tokens, passwords, SSH keys, shell histories, recordings, and app data are not stored here. Keep machine-specific private shell settings in `~/.config/zsh/local.zsh`, which the Zsh startup file sources if present. GitHub authentication stays in GitHub CLI's local credential storage. Sign in with `mise exec -- gh auth login --hostname github.com --git-protocol https --web` when you want to push changes; reading and restoring this public repository works without authentication. This checkout uses GitHub's noreply commit identity to avoid exposing a personal email address. The previous public dotfiles repository remains a historical snapshot; this repository is now the active configuration.

## Verification

```sh
mise exec -- python "$HOME/.config/mise/tests/test_maintenance.py"
mise exec -- python "$HOME/.config/mise/tests/test_fonts.py"
mise -C "$HOME" doctor
mise -C "$HOME" bootstrap --dry-run
```

GitHub Actions runs portable tests on Linux/Python 3.11 with Mise 2026.11.12 and macOS/Python 3.15 with the latest Mise release on each push and pull request, plus TOML/restore-source validation, Mise task graph checks, Python compilation, and shell syntax checks. Actions are pinned to verified commits with read-only repository permissions. Tests use temporary configs and app bundles and run against an empty home directory; CI never applies the desktop manifest. Tests cover fresh MarkText installation and official artifact verification, config synchronization ordering, separate scheduled health, bounded history and log rotation, between-run changes, native app deferrals, previews, interrupts, and verified font updates. A bootstrap preview confirms declaration order and resource state without installing packages, starting services, or running hooks.
