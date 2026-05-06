import os, stat

home = os.path.expanduser('~')
bin_dir = os.path.join(home, '.slurm_tracker', 'bin')
os.makedirs(bin_dir, exist_ok=True)

path_dirs = [d for d in os.environ.get('PATH', '').split(':')
             if d and d != bin_dir]
real_sbatch = next(
    (os.path.join(d, 'sbatch') for d in path_dirs
     if os.path.isfile(os.path.join(d, 'sbatch'))
     and os.access(os.path.join(d, 'sbatch'), os.X_OK)),
    'sbatch'
)

wrapper_template = r"""#!/bin/bash
export SLURM_TRACKER_MAX_MB="${SLURM_TRACKER_MAX_MB:-10}"
stage=""
case "$PWD" in
    "$HOME"|"$HOME/"|/|"") ;;
    *)
        mkdir -p "$HOME/.slurm_tracker/staging" 2>/dev/null
        stage=$(mktemp -d "$HOME/.slurm_tracker/staging/XXXXXX" 2>/dev/null) || stage=""
        if [ -n "$stage" ]; then
            cp --reflink=always -a "$PWD"/. "$stage"/ 2>/dev/null \
                || cp -al "$PWD"/. "$stage"/ 2>/dev/null \
                || { rm -rf "$stage"; stage=""; }
        fi
        ;;
esac
_output=$(__REAL_SBATCH__ "$@")
_rc=$?
echo "$_output"
_job_id=$(echo "$_output" | grep -oE '[0-9]+' | tail -n 1)
if [ -n "$_job_id" ]; then
    ~/.slurm_tracker/capture.py "$_job_id" "$stage" "$PWD" "$@" >/dev/null 2>&1 &
elif [ -n "$stage" ]; then
    rm -rf "$stage"
fi
exit $_rc
"""

wrapper = wrapper_template.replace('__REAL_SBATCH__', real_sbatch)

wrapper_path = os.path.join(bin_dir, 'sbatch')
with open(wrapper_path, 'w') as f:
    f.write(wrapper)
os.chmod(wrapper_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP
                       | stat.S_IROTH | stat.S_IXOTH)

path_block = (
    '\n# --- SLURM DASH INTERCEPTOR ---\n'
    'export PATH="$HOME/.slurm_tracker/bin:$PATH"\n'
    '# --- END SLURM DASH INTERCEPTOR ---\n'
)
for rc in ['~/.bashrc', '~/.zshrc']:
    rc_path = os.path.expanduser(rc)
    if os.path.isfile(rc_path):
        content = open(rc_path).read()
        if 'SLURM DASH INTERCEPTOR' not in content:
            with open(rc_path, 'a') as f:
                f.write(path_block)

print(f'Wrapper: {wrapper_path}')
print(f'Real sbatch: {real_sbatch}')
