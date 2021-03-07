everno_export
=============

Export all Evernotes to local directory tree for backup purposes, optionally auto-check them into git tree

PROJECT STATUS:
--------------

This was working back in 2012-2013, might have bitrotted by now, but I am posting this in case this is useful
for someone.

SETUP
-----

Install libray:
```
git clone https://github.com/evernote/evernote-sdk-python.git
(cd evernote-sdk-python && git checkout 80d78768f7227c61d9)
```

Get a developer token from https://www.evernote.com/api/DeveloperToken.action then save it 
to an account file, `~/.local/evernote_account.info`
```
authToken='OAUTH-TOKEN'
```

Create a data dir and `git init` it.

finally, add the included .sh file to cron.
