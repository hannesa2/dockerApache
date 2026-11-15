# Source with the absolute repository path as the first argument (bash or zsh).
if [ "$#" -ne 1 ] || [ ! -f "$1/private/check-git.py" ]; then
  echo "ERROR: source git-notice.sh with the absolute repository path" >&2
  return 1
fi
PRIVATE_CONFIG_GIT_CHECK="$1/private/check-git.py"

git() {
  if ! command python3 "$PRIVATE_CONFIG_GIT_CHECK" "$@"; then
    printf '%s\n' "WARNING: Private configuration status could not be checked." >&2
    printf '%s\n' "WARNING: Reminder from $PRIVATE_CONFIG_GIT_CHECK; remove the git-notice.sh source line from ~/.bashrc or ~/.zshrc and restart the shell (or run unset -f git)." >&2
  fi
  command git "$@"
}
