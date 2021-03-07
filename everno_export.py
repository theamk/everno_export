#!/usr/bin/env python
"""
Evernote-Export -- export whole Evernote account into a human-readable
directory structure.

Uses sync protocol for efficient transfers -- in case nothing is changes, only a
single request is made.

To run program you need to create an 'account file', with a single line:
   authToken='OAUTH-TOKEN'
then run program with -a pointing to this file.
To get a developer token, go there:
https://www.evernote.com/api/DeveloperToken.action

Unless specified otherwise, program will create metadata cache in the account
file directory, and write data to subdir 'data' under location of metadata
cache.

"""

import binascii
import collections
import errno
import md5
import optparse
import os
import pprint
import re
import sys
import time
import yaml

sys.path.append(
    os.path.join(os.path.dirname(os.path.realpath(__file__)),
                 'evernote-sdk-python/lib'))
import thrift.protocol.TBinaryProtocol as TBinaryProtocol
import thrift.transport.THttpClient as THttpClient
import evernote.edam.userstore.UserStore as UserStore
import evernote.edam.userstore.constants as UserStoreConstants
import evernote.edam.notestore.NoteStore as NoteStore
import evernote.edam.type.ttypes as Types

def yaml_dump(obj):
    # Convert object to yaml string.
    return yaml.dump(obj,
                     default_flow_style=False,
                     Dumper=yaml.CSafeDumper)

class EvernoteSyncer(object):
    # Changes to fetch per request. Set to low lumber like 8 for debug.
    FETCH_CHANGES_COUNT = 128

    # Max length of filename (excluding extensions, elipssis)
    MAX_NAME_LEN = 64

    def __init__(self, metaFilename=None, create_ok=True, quiet=False):
        """Init object"""

        # Metadata here includes all the data except
        #   actual contents of notes and attachements.
        # For easier serialization, it is stored as dicts.
        self.metaFilename = metaFilename
        self.meta = None
        self.meta_updated = False

        self.create_ok = create_ok
        self.quiet = quiet

        # Notestore cache. Initialized on first access.
        self._noteStore = None

        # NOTE: all _wf variables are reset at the beginning of writeFiles.
        # Filenames written so far -- so we can delete all other files later.
        # (this includes directories, too)
        self._wf_names = None
        # Unique prefixes. used to prevent accidental file name duplication.
        self._wf_prefixes = None
        # Output directory
        self._wf_outdir = None
        # Notebook guid->name mappings
        self._wf_notebook_names = None
        # Pretend flag
        self._wf_pretend = None

        self._loadMeta()

    def connect(self, auth_file):
        """Setup account info.
        @returns True if meta was updated.
        """
        vars = dict(
            evernoteHost = 'www.evernote.com',
            authToken = None)
        execfile(auth_file, vars)

        evernoteHost = vars['evernoteHost']
        self.authToken = vars['authToken']
        assert self.authToken, 'Auth file does not defile authToken'

        token_md5 = md5.new("***".join(
                [self.authToken, evernoteHost])
                            ).hexdigest()
        result = False

        old_info = self.meta['userInfo']
        if self.meta['tokenMD5'] != token_md5:
            # token changed. Invalidate some fields, so they are fetched later.
            self.message('Lookes like token has changed. Will re-fetch data')
            self.meta.update(noteStoreUrl=None,
                             user=None,
                             userInfo=None,
                             tokenMD5=token_md5)
            self.meta_updated = True

        if (self.meta['noteStoreUrl'] is None or self.meta['userInfo'] is None):
            self.message('Getting user info')
            userStoreUri = "https://" + evernoteHost + "/edam/user"
            userStore = UserStore.Client(
                TBinaryProtocol.TBinaryProtocol(
                    THttpClient.THttpClient(userStoreUri)))

            # Check version
            versionOK = userStore.checkVersion(
                "EverDirExport",
                UserStoreConstants.EDAM_VERSION_MAJOR,
                UserStoreConstants.EDAM_VERSION_MINOR)
            assert versionOK, 'EDAM version is too old'

            # Get note store URL
            self.meta['noteStoreUrl'] = \
                userStore.getNoteStoreUrl(self.authToken)
            self.meta_updated = True

            # Get user info
            user_raw = userStore.getUser(self.authToken)
            if 0:
                # We do not record whole user struct, becase we do not
                # care enough.
                user = dict(user_raw.__dict__.items())
                for fld in ['accounting', 'attributes']:
                    if user[fld] is not None:
                        user[fld] = dict(user[fld].__dict__.items())
                self._fixTimestamp(user, ['created', 'updated', 'deleted'])
                if user['accounting']:
                    self._fixTimestamp(user['accounting'], ['uploadLimitEnd'])
                self.meta['user'] = user

            # Record subset of 'user' which will not ever change
            userInfo = dict(
                id=user_raw.id,
                evernoteHost=evernoteHost,
                username=user_raw.username,
                shardId=user_raw.shardId)

            if (old_info is not None) and (old_info != userInfo):
                raise Exception('Fatal: meta cache %r seems to contain info '
                                'for a different user:\n'
                                '=== EXPECTED ===\n%s=== FOUND ===\n%s=== * ===' % (
                        self.metaFilename, yaml_dump(userInfo), yaml_dump(old_info)))

            self.meta['userInfo'] = userInfo
            result = True

        self._noteStore = NoteStore.Client(
            TBinaryProtocol.TBinaryProtocol(
                THttpClient.THttpClient(self.meta['noteStoreUrl'])))

        return result

    def noteStore(self):
        assert self._noteStore, 'connect() not called'
        return self._noteStore

    def _loadMeta(self):
        self.meta = dict(
            maxUSN = 0,
            noteStoreUrl = None,
            user = None,
            userInfo = None,
            tokenMD5 = None,

            notebooks = dict(),
            notes = dict(),
            tags = dict(),
            searches = dict(),
            resources = dict(),
            )
        self.meta_updated = True

        assert self.metaFilename
        if os.path.exists(self.metaFilename):
            self.meta = yaml.safe_load(
                open(self.metaFilename, 'r'))
            self.meta_updated = False
        else:
            if not self.create_ok:
                raise Exception('Fatal: --create-ok not given, '
                                'and no existing metafile found at %r' %
                                self.metaFilename)
            self.warn('Metafile %r not found, will make new'
                      % self.metaFilename)

    def saveMeta(self, force=False, pretend=False):
        if not (self.meta_updated or force):
            return

        self.message('Writing metadata cache')

        temp = self.metaFilename + '.new'
        with open(temp, 'w') as f:
            f.write(yaml_dump(self.meta))
        os.rename(temp, self.metaFilename)
        self.meta_updated = False

    def message(self, msg):
        if not self.quiet:
            print >>sys.stderr, '. ' + msg

    def warn(self, msg):
        print >>sys.stderr, 'WARN ' + msg

    def fetchLatestMeta(self):
        """Poll the server for metadata changes, fetch them.
        @returns update summary string, or empty string if none
        """

        initial = self.meta['maxUSN'] == 0

        stats = collections.defaultdict(int)

        # Check if no changes
        if not initial:
            syncstate = self.noteStore().getSyncState(self.authToken)
            if self.meta['maxUSN'] == syncstate.updateCount:
                self.message('Database is up to date (%d)' % syncstate.updateCount)
                # no changes
                return ''
        else:
            self.message('Doing initial fetch')
            stats['initial'] = 1

        # get and process updates
        changes = list()
        while True:
            # fetch chunk
            data = self.noteStore().getSyncChunk(
                self.authToken, self.meta['maxUSN'],
                self.FETCH_CHANGES_COUNT, initial)
            item_counts = dict(
                (k, len(v)) for (k, v) in data.__dict__.iteritems()
                if isinstance(v, list))
            changes.append(dict(
                    inputUSN = self.meta['maxUSN'],
                    highUSN = data.chunkHighUSN,
                    currentTime = data.currentTime,
                    updateCount = data.updateCount,
                    items = item_counts))

            # log chunk
            self.message('Fetched usn=%r/%r items=%r' % (
                    self.meta['maxUSN'], data.updateCount, item_counts))

            # update sequence number as needed
            if not data.chunkHighUSN:
                # Done.
                assert len(item_counts) == 0
                break
            self.meta['maxUSN'] = data.chunkHighUSN
            self.meta_updated = True

            assert len(item_counts) != 0
            stats['update-cycles'] += 1

            new_resources = []
            expunged_items = []

            # process note updates
            for note_obj in (data.notes or []):
                note = dict(note_obj.__dict__)
                note['attributes'] = dict(note_obj.attributes.__dict__)
                assert note['content'] is None
                del note['content']
                note['contentHash'] = binascii.hexlify(note_obj.contentHash)

                self._fixTimestamp(note, ['created', 'updated', 'deleted'])

                attr = note['attributes']
                if attr:
                    self._fixTimestamp(attr, ['subjectDate', 'shareDate'])
                    if attr['applicationData']:
                        attr['applicationData'] = dict(attr['applicationData'].__dict__)


                if note_obj.resources:
                    new_resources += note_obj.resources
                    note['resourceGuids'] = [
                        r.guid for r in note_obj.resources]
                else:
                    note['resourceGuids'] = []
                del note['resources']

                old = self.meta['notes'].get(note_obj.guid)
                if old:
                    stats['notes-updated'] += 1
                else:
                    stats['notes-created'] += 1
                self.meta['notes'][note_obj.guid] = note

            for guid in (data.expungedNotes or []):
                expunged_items.append(('notes', guid))
                stats['notes-expunged'] += 1
                for r_guid in self.meta['notes'][guid]['resources']:
                    stats['resource-expunged'] += 1
                    expunged_items.append(('resources', r_guid))

            # process resources updates
            for res_obj in (data.resources or []) + new_resources:
                res = dict(res_obj.__dict__)
                for fld in ['attributes', 'data', 'recognition']:
                    if res[fld] is not None:
                        res[fld] = res[fld].__dict__

                attr = res['attributes']
                if attr:
                    self._fixTimestamp(attr, ['timestamp'])
                    if attr['applicationData']:
                        attr['applicationData'] = dict(attr['applicationData'].__dict__)

                for rec in (res['data'], res['recognition']):
                    if rec is None: continue
                    assert rec['body'] is None
                    del rec['body']
                    rec['bodyHash'] = binascii.hexlify(rec['bodyHash'])

                stats['resources'] += 1
                self.meta['resources'][res_obj.guid] = res
            # no explicit resource expungements

            # process updates on other fields
            for fld in ['notebooks', 'tags', 'searches']:
                new = getattr(data, fld)
                for entry in (new or []):
                    stats[fld] += 1
                    self.meta[fld][entry.guid] = e = entry.__dict__
                    if fld == 'notebooks':
                        self._fixTimestamp(e, ['serviceCreated',
                                               'serviceUpdated'])
                        if e['publishing']:
                            e['publishing'] = dict(e['publishing'].__dict__)
                        if e['sharedNotebooks']:
                            # TODO: serialize better
                            e['sharedNotebooksGuids'] = [
                                sn.notebookGuid for sn in e['sharedNotebooks']]
                            del e['sharedNotebooks']

                gone = getattr(data, 'expunged' + fld.title())
                for guid in (gone or []):
                    stats[fld + '-expunged'] += 1
                    expunged_items.append((fld, guid))
                    # TODO: deleting notebook removes all of its notes

            # process deletions
            for fld, guid in expunged_items:
                old = self.meta[fld].pop(guid, None)
                if old is None:
                    self.warn('Could not remove %s %r' % (fld, guid))

        # fill up tagNames member
        for note in self.meta['notes'].values():
            if note['tagGuids']:
                note['tagNames'] = [
                    self.meta['tags'].get(guid, {'name':'unknown-tag'})['name']
                    for guid in note['tagGuids']]
            else:
                note['tagNames'] = None

        return ' '.join('%s=%s' % (k, v)
                        for (k, v) in sorted(stats.items()))

    def _fixTimestamp(self, rec, fields):
        if rec is None:
            return

        for fld in fields:
            if rec[fld] is None:
                rec.pop(fld + '_str', None)
            else:
                val_s = time.strftime('%F %T %Z',
                                      time.localtime(rec[fld] / 1000.0))
                rec[fld + '_str'] = val_s

    def _makeFilePrefix(self, base, name):
        """Escapes 'name', then joins base to make a prefix.
        Uses _wf_prefixes to make sure returned values always unique
        """
        p = re.sub("[\x00-\x1F/\\\\]+", "_", name).strip()
        p = re.sub(r"^[\.]+", "", p).strip('. _')
        if len(p) > self.MAX_NAME_LEN:
            p = p[:self.MAX_NAME_LEN] + "..."
        elif p == '':
            p = 'untitled'
        if p != name and 0:
            self.message('Mangling name: %r->%r' % (name, p))


        prefix = os.path.join(base, p)
        res = prefix
        num = 0
        while res in self._wf_prefixes:
            num += 1
            res = "%s(%d)" % (prefix, num)

        self._wf_prefixes[res] = name

        return res

    def _writeFile(self, name, contents, force=False,
                   expect_size=None, expect_md5=None):
        """Write 'contents' to given filename. Does not touch the file
        if it already exists and has correct contents.
        Records file in _wf_names.
        """
        assert name not in self._wf_names, 'Duplicate name: %r' % name
        assert name.startswith(self._wf_outdir)
        self._wf_names[name] = True

        assert contents is not None

        errors = list()
        if (expect_size is not None) and (len(contents) != expect_size):
            errors.append('size is %d, expected %d' % (len(contents), expect_size))

        if expect_md5 is not None:
            real_md5 = md5.new(contents).hexdigest()
            if expect_md5 != real_md5:
                errors.append('md5 is %r, expected %r' % (real_md5, expect_md5))

        if errors:
            self.warn('Bad data for %r: %s' % (name, '; '.join(errors)))

        if not force:
            try:
                st = os.stat(name)
                if st.st_size == len(contents):
                    with open(name, 'rb') as f:
                        if f.read() == contents:
                            return
                self.message('Updating file: %r' % name)
            except OSError:
                # File not found
                self.message('Creating file: %r' % name)

        if self._wf_pretend:
            return
        with open(name, 'wb') as f:
            f.write(contents)

    def _mkdir(self, path):
        """Make dir if not exists, record it."""
        assert path not in self._wf_names
        self._wf_names[path] = True
        if self._wf_pretend:
            return
        try:
            os.mkdir(path)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise


    def _checkSizeMD5(self, name, size, md5hex):
        """Return True if file named 'name' exists, and has
        given size, md5
        """
        # Check if need to write contents
        if not os.path.exists(name):
            return False

        if os.stat(name).st_size != size:
            return False

        if md5hex is None:
            return True

        hasher = md5.new()
        # TODO: skip verification if csum matches
        with open(name, 'rb') as f:
            hasher.update(f.read())
        return hasher.hexdigest() == md5hex

    def _writeNotebook(self, nbook):
        basedir = self._makeFilePrefix(self._wf_outdir, nbook['name'])
        self._mkdir(basedir)
        self._wf_notebook_names[nbook['guid']] = basedir

        # makeFilePrefix will never return names which start from _,
        # so this will will not be overridden.
        self._writeFile(basedir + '/_notebook.meta', yaml_dump(nbook))

    def _writeNote(self, note):
        # Generate file name
        note_path = self._wf_notebook_names[note['notebookGuid']]
        if not note['active']:
            # Note in trash. Store it there.
            assert note_path.startswith(self._wf_outdir)
            # we will never have auto-generated folder names which start from _
            note_path = os.path.join(
                self._wf_outdir, '_trash',
                note_path[len(self._wf_outdir):].lstrip('/'))

            # Create parent directories as needed
            # (and add them to _wf_names, so we do not try to delete them)
            cur = note_path
            to_mkdir = list()
            while (cur.startswith(self._wf_outdir + '/') and len(cur) > 2):
                if cur not in self._wf_names:
                    to_mkdir.append(cur)
                cur = os.path.dirname(cur)
            for cur in reversed(to_mkdir):
                self._mkdir(cur)

        basename = self._makeFilePrefix(note_path, note['title'])

        resources = [ self.meta['resources'][g] for g in sorted(note['resourceGuids']) ]

        # Write contents if need to.
        if not (self._checkSizeMD5(basename + ".enml",
                                   note.get('override-contentLength', note['contentLength']),
                                   note['contentHash'])
                and os.path.exists(basename + ".stxt")):
            self.message('Fetching content for %r' % basename)

            content = self.noteStore().getNoteContent(self.authToken, note['guid'])

            if len(content) != note['contentLength']:
                # damn buggy everNote -- the metadata which does not describe data
                # we will add an override field to prevent further complains.
                # note that any metadata update will re-create the object,
                # and thus drop the override.
                note['override-contentLength'] = len(content)
                self.meta_updated = True

            self._writeFile(basename + ".enml", content,
                            force=True,
                            expect_size=note['contentLength'],
                            expect_md5=note['contentHash'])

            # Also write text version for reference
            txt_content = self.noteStore().getNoteSearchText(
                self.authToken, note['guid'], noteOnly=True,
               tokenizeForIndexing=False)
            self._writeFile(basename + '.stxt', txt_content)

            # TODO: add export to pseudo-.txt files. Should be unambigious
            # conversion which looks like text as long as styles are not present.
            # (this will be like .stxt, but better)x
        else:
            # content up to date. Do not delete.
            self._wf_names[basename + ".enml"] = True
            self._wf_names[basename + ".stxt"] = True

        # TODO: save note Application Data

        # write metadata
        self._writeFile(basename + '.meta', yaml_dump(note))


        # Write attachements.
        for res_num, res in enumerate(resources):
            ext = '.' + res['mime'].split('/')[-1]
            if ext.endswith('meta'): ext += '.dat'
            if ext.endswith('recog') and res['recognition']:
                ext += '.dat'

            res_name = '%s.data%02d' % (basename, res_num + 1)
            res2 = dict(_payload = os.path.basename(res_name + ext),
                        **res)
            self._writeFile(res_name + '.meta', yaml_dump(res2))

            # Write body
            if not self._checkSizeMD5(
                res_name + ext, res['data']['size'], res['data']['bodyHash']):

                self.message('Fetching resource %r' % (res_name + ext))
                data = self.noteStore().getResourceData(self.authToken, res['guid'])

                self._writeFile(res_name + ext, data,
                                expect_size=res['data']['size'],
                                expect_md5=res['data']['bodyHash'])
                body_changed = True
            else:
                self._wf_names[res_name + ext] = True
                body_changed = False

            # Write recognition, if any
            # Apparently bodyHash could be lies for recoginition, so we only check size
            # (and force re-fetch when main body changes)
            recog = res['recognition']
            if not recog:
                pass # no recognition
            elif body_changed or not self._checkSizeMD5(
                res_name + '.txt', recog['size'], None): #recog['bodyHash']):

                self.message('Fetching recognition %r' % (res_name + '.txt'))
                data = self.noteStore().getResourceRecognition(self.authToken, res['guid'])
                #rec = self.noteStore().getResource(self.authToken, res['guid'],
                #                                 withData=False, withRecognition=True,
                #                                 withAttributes=False, withAlternateData=False)
                #assert binascii.hexlify(rec.recognition.bodyHash) == recog['bodyHash']
                #data = rec.recognition.body

                self._writeFile(res_name + '.txt', data,
                                expect_size=recog['size'],
                                expect_md5=recog['bodyHash'])
            else:
                self._wf_names[res_name + '.txt'] = True

            # TODO: export application data
            # TODO: export alternate contents


    def _cleanupOutputDir(self):
        """Walk output dir, remove all non-dot files which are not in _wf_names."""
        to_remove_f = list()
        to_remove_d = list()
        new = set(self._wf_names.keys())

        for dirpath, dirnames, filenames in os.walk(self._wf_outdir):
            # Ignore dot-dirs
            for dirname in list(dirnames):
                if dirname.startswith('.'):
                    dirnames.remove(dirname)
                    continue
                absname = os.path.join(dirpath, dirname)
                if absname in new:
                    new.remove(absname)
                else:
                    to_remove_d.append(absname)

            # iterate subfiles, subdirs
            for relname in filenames:
                if relname.startswith('.'):
                    continue
                absname = os.path.join(dirpath, relname)
                if absname in new:
                    new.remove(absname)
                else:
                    to_remove_f.append(absname)

        # Make sure all expectd files are there, otherwise there could be some
        # strange iteration error.
        if len(new) and not self._wf_pretend:
            raise Exception('Error: some files not found. Not removing anything. List:\n  ' +
                            '\n  '.join(sorted(new)))

        for fn in to_remove_f:
            self.message('Removing file: %r' % fn)
            if not self._wf_pretend:
                try:
                    os.unlink(fn)
                except (IOError, OSError) as e:
                    self.warn('Failed to remove file %r: %s' % (fn, e))

        to_remove_d.sort(reverse=True)
        for fn in to_remove_d:
            self.message('Removing dir: %r' % fn)
            if not self._wf_pretend:
                try:
                    os.rmdir(fn)
                except (IOError, OSError) as e:
                    self.warn('Failed to remove dir %r: %s' % (fn, e))

    def writeAllData(self, dname, pretend=False, clean=True):
        self._wf_names = dict()
        self._wf_prefixes = dict()
        self._wf_outdir = dname.rstrip('/')
        self._wf_notebook_names = dict()
        self._wf_pretend = pretend

        userinfo_name = os.path.join(self._wf_outdir, '_userInfo.meta')
        userinfo_data = yaml_dump(self.meta['userInfo'])

        if not os.path.exists(userinfo_name):
            if not self.create_ok:
                raise Exception('Fatal: --create-ok not given, '
                                'and no old data with userinfo found in %r' %
                                self._wf_outdir)
            # create dir -- it must not exist (otherwise we can wipe
            # a wrong dir later)
            self.message('Creating output dir %r' % self._wf_outdir)
            if not self._wf_pretend and not os.getenv('RECREATE_USERINFO'):
                os.mkdir(self._wf_outdir)
        else:
            # A userinfo file, if exists, with identical contents.
            old_info = open(userinfo_name, 'r').read(4096)
            if old_info != userinfo_data:
                raise Exception('Fatal: output dir %r seems to contain info '
                                'for a different user:\n'
                                '=== EXPECTED ===\n%s=== FOUND ===\n%s=== * ===' % (
                        self._wf_outdir, userinfo_data, old_info))

        self._writeFile(userinfo_name, userinfo_data)
        self._writeFile(os.path.join(self._wf_outdir, '_tags.meta'),
                        yaml_dump(self.meta['tags']))
        self._writeFile(os.path.join(self._wf_outdir, '_searches.meta'),
                        yaml_dump(self.meta['searches']))

        for _, notebook in sorted(self.meta['notebooks'].items()):
            self._writeNotebook(notebook)

        # process by 'created' time -- this preserves the name.
        notelist = list(self.meta['notes'].values())
        notelist.sort(key = lambda n: (n['created'], n['guid']))

        for note in notelist:
            self._writeNote(note)

        if clean:
            self._cleanupOutputDir()

        self._wf_outdir = None
        self._wf_names = None

def main():
    parser = optparse.OptionParser(usage='%prog [-a account-file] [opts]',
                                   description=__doc__)
    parser.format_description = lambda _: parser.description.lstrip()

    parser.add_option('-n', '--pretend', action='store_true',
                      help='Do not actually write the files')
    parser.add_option('-a', '--account-file', metavar='NAME',
                      help='Location of the account config file')
    parser.add_option('--meta-file', metavar='NAME',
                      help='Set metadata file name')
    parser.add_option('-D', '--data-dir', metavar='DIR',
                      help='Set data directory. WARNING: contents would be '
                      'completely replaced')
    parser.add_option('-C', '--create-ok', action='store_true',
                      help='Create output dir, meta file as needed')
    parser.add_option('-q', '--quiet',  action='store_true',
                      help='Only print errors to stderr')

    # debug options below
    parser.add_option('-M', '--no-meta-update', action='store_true',
                      help='Do not fetch latest metadata; just make sure '
                      'the output dir is consistent')
    parser.add_option('-W', '--no-write', action='store_true',
                      help='Do not touch output dir, just update metadata')
    parser.add_option('--no-clean', action='store_true',
                      help='Do not remove old files from output dir')
    parser.add_option('--no-meta-write', action='store_true',
                      help='Fetch the metafile, but do not update the '
                      'cache file')
    parser.add_option('--dump-meta',  action='store_true',
                      help='Print metadata as YAML to stdout')


    opts, args = parser.parse_args()
    if len(args):
        parser.error('No args accepted')

    if opts.account_file is None:
        parser.error('Account file must be specified')

    if opts.meta_file is None:
        opts.meta_file = os.path.join(
            os.path.dirname(os.path.abspath(opts.account_file)),
            'evernote_meta.yaml')

    updates = ''

    syncer = EvernoteSyncer(metaFilename=opts.meta_file,
                            create_ok=opts.create_ok,
                            quiet=opts.quiet)
    if syncer.connect(opts.account_file):
        updates += '[system]'

    if not opts.no_meta_update:
        updates += syncer.fetchLatestMeta()

    if not opts.no_meta_write:
        syncer.saveMeta(pretend=opts.pretend)
        #yaml.safe_dump(updates, stream=sys.stdout)

    if updates:
        print updates

    if opts.dump_meta:
        print yaml_dump(syncer.meta)

    # TODO: only if updates? but this will leave bad state if previous
    # export failed.
    if opts.data_dir is None:
        opts.data_dir = os.path.join(
            os.path.dirname(opts.meta_file),
            'data')

    if not opts.no_write:
        syncer.writeAllData(opts.data_dir,
                            pretend=opts.pretend,
                            clean=not opts.no_clean)

    if not opts.no_meta_write:
        syncer.saveMeta(pretend=opts.pretend)

    #pprint.pprint(syncer.meta)
    #yaml.dump(syncer.meta, stream=sys.stdout)




if __name__ == '__main__':
    main()
