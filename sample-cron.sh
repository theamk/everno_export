#!/bin/bash
# Run from within cron. No output unless there are errors.

CODE_DIR=$(dirname $(readlink -f "$0"))
DATA_DIR=~/media/misc/evernote
(
    # Exit on errors
    set -e
    if ! flock -n 8; then
        echo Fatal: cannot obtain a lock
        exit 1
    fi

    cd $DATA_DIR

    MSG=`$CODE_DIR/everno_export.py -a ~/.local/evernote_account.info --data-dir $DATA_DIR -q`

    if [[ "$MSG" == "" ]]; then
        MSG="(unknown reason)"
    fi

    git add --all .

    if git status -s | grep -q .; then
        git commit -q -m "auto: $MSG"
    fi

) 8>>/tmp/theamk-cron-evernote.lock