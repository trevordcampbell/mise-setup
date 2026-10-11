typeset -U path fpath

# Formula packages are managed directly by mise in this prefix.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
fpath=(/opt/homebrew/share/zsh/site-functions $fpath)
export MANPATH="/opt/homebrew/share/man:${MANPATH:-}"
export INFOPATH="/opt/homebrew/share/info:${INFOPATH:-}"
eval "$("$HOME/.local/bin/mise" activate zsh --shims)"

# Added by OrbStack: command-line tools and integration
source ~/.orbstack/shell/init.zsh 2>/dev/null || :
