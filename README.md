# Trevor's macOS mise setup

Reproducible host setup for an Apple Silicon Mac. This repository is the live global mise configuration at `~/.config/mise`. Development projects continue to use their devcontainers.

`config.toml` declares nine tools, five native command-line packages, 21 desktop app casks, Poppins, MarkText, shell repositories, managed shell files, Google variable fonts, and automatic update jobs. Native packages use mise's built-in `brew` and `brew-cask` backends; a Homebrew installation or executable is not required. Native libraries are resolved automatically.

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

`--adopt` clones the repository into `~/.config/mise`, so later commands read it as their global configuration. `--force-dotfiles` replaces existing shell startup files with this setup; review those files first when restoring onto a Mac with custom settings. An existing nonempty `~/.config/mise` that is not this repository must be reviewed and merged or moved aside before adoption. On an already adopted Mac, preview with `mise -C "$HOME" bootstrap --dry-run`; apply with `mise -C "$HOME" bootstrap --yes --skip-dirty`. Add `--update` to fast-forward the configuration checkout first.

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

MarkText uses its official ARM64 release artifact and SHA-256 digest. Its updater advances the three release fields in `config.toml` when an eligible release is available. This is a local configuration change until committed; Git updates preserve it by skipping the dirty checkout.

Poppins installs all 18 static styles. Open Sans, Rubik, and Space Grotesk use all variable TTF styles supplied upstream, including italics where available. The font helper downloads from immutable official `google/fonts` revisions, verifies Git blob hashes, keeps OFL licenses locally, and avoids full repository clones. Font sources, caches, receipts, and update logs are regenerated on each machine.

```sh
mise run maintenance:check
mise run maintenance:update
mise run maintenance:status
mise run shell:status
mise run shell:update
mise run fonts:check
mise run fonts:update
```

Manual maintenance shows each stage as it starts and finishes, elapsed time, and a concise summary. Completed stages mean the command succeeded; they do not imply a version changed. Open apps are listed as deferred. Terminal output uses color when supported and honors `NO_COLOR`; scheduled jobs use plain text. Full logs and JSON state reports are always saved locally.

```sh
# Show the underlying command output live, while also saving the log:
mise -C "$HOME" run maintenance:update --verbose

# Emit a JSON report on stdout (suitable for redirecting or piping):
mise -C "$HOME" run maintenance:update --json
mise -C "$HOME" run maintenance:check --json
mise -C "$HOME" run maintenance:status --json

# The status command displays the most recent update without running updates:
mise -C "$HOME" run maintenance:status
```

`--verbose` and `--json` belong after the task name. They can be combined: the JSON report goes to stdout and command output goes to stderr. Preview mode also accepts `--verbose`, which exposes the underlying update plans. JSON stdout contains no progress messages or log-path trailer; the log path is included in the report. `last-run.json` and `last-check.json` live in `~/.local/state/mise/maintenance/`; command output is kept in `~/Library/Logs/mise-maintenance.log`.

## Save configuration changes

The global config, scripts, and tests are ordinary files in this checkout. The six shell startup/prompt files are symlinks into `dotfiles/`. Editing `~/.zshrc` or `~/.p10k.zsh` therefore edits the tracked source. The `dotfiles` shell alias runs Git in `~/.config/mise`:

```sh
dotfiles status
dotfiles diff
dotfiles add config.toml dotfiles/.zshrc dotfiles/.p10k.zsh
dotfiles commit -m "Update host setup"
dotfiles push
```

Changes are not automatically committed or pushed. Review and push changes you want available on other machines, including additions made with `mise use -g`. Use `mise -C "$HOME" bootstrap --adopt trevordcampbell/mise-setup --update --yes` on another already adopted Mac to pull and apply saved changes.

Tokens, passwords, SSH keys, shell histories, recordings, and app data are not stored here. Keep machine-specific private shell settings in `~/.config/zsh/local.zsh`, which the Zsh startup file sources if present. GitHub authentication stays in GitHub CLI's local credential storage. Sign in with `mise exec -- gh auth login --hostname github.com --git-protocol https --web` when you want to push changes; reading and restoring this public repository works without authentication. This checkout uses GitHub's noreply commit identity to avoid exposing a personal email address. The previous public dotfiles repository remains a historical snapshot; this repository is now the active configuration.

## Verification

```sh
mise exec -- python "$HOME/.config/mise/tests/test-maintenance.py"
mise exec -- python "$HOME/.config/mise/tests/test-fonts.py"
mise -C "$HOME" doctor
mise -C "$HOME" bootstrap --dry-run
```

The tests cover idle-app deferral, official artifact verification, preview behavior, rollback, atomic config writes, and verified font updates. A bootstrap preview confirms declaration order and resource state without installing packages, starting services, or running hooks.
