
import sys, os, stat
import os.path
from collections import deque

from twisted.internet import defer, reactor, task
from twisted.python.failure import Failure
from twisted.python import runtime
from twisted.application import service

from allmydata.interfaces import IDirectoryNode
from allmydata.util import log
from allmydata.util.fileutil import abspath_expanduser_unicode, precondition_abspath
from allmydata.util.assertutil import precondition
from allmydata.util.encodingutil import listdir_unicode, to_filepath, \
     unicode_from_filepath, quote_local_unicode_path, FilenameEncodingError
from allmydata.immutable.upload import FileName, Data
from allmydata import backupdb, magicpath


IN_EXCL_UNLINK = 0x04000000L

def get_inotify_module():
    try:
        if sys.platform == "win32":
            from allmydata.windows import inotify
        elif runtime.platform.supportsINotify():
            from twisted.internet import inotify
        else:
            raise NotImplementedError("filesystem notification needed for drop-upload is not supported.\n"
                                      "This currently requires Linux or Windows.")
        return inotify
    except (ImportError, AttributeError) as e:
        log.msg(e)
        if sys.platform == "win32":
            raise NotImplementedError("filesystem notification needed for drop-upload is not supported.\n"
                                      "Windows support requires at least Vista, and has only been tested on Windows 7.")
        raise


class MagicFolder(service.MultiService):
    name = 'magic-folder'

    def __init__(self, client, upload_dircap, collective_dircap, local_dir, dbfile, inotify=None,
                 pending_delay=1.0):
        precondition_abspath(local_dir)

        service.MultiService.__init__(self)
        self._stopped = False
        self._remote_scan_delay = 3 # XXX
        self._local_dir = abspath_expanduser_unicode(local_dir)
        self._upload_lazy_tail = defer.succeed(None)
        self._upload_pending = set()
        self._download_scan_batch = {}
        self._download_lazy_tail = defer.succeed(None)
        self._download_pending = set()
        self._collective_dirnode = None
        self._client = client
        self._stats_provider = client.stats_provider
        self._convergence = client.convergence
        self._local_path = to_filepath(self._local_dir)
        self._dbfile = dbfile

        self._download_deque = deque()
        self._upload_deque = deque()
        self.is_ready = False

        self._inotify = inotify or get_inotify_module()

        if not self._local_path.exists():
            raise AssertionError("The '[magic_folder] local.directory' parameter was %s "
                                 "but there is no directory at that location."
                                 % quote_local_unicode_path(local_dir))
        if not self._local_path.isdir():
            raise AssertionError("The '[magic_folder] local.directory' parameter was %s "
                                 "but the thing at that location is not a directory."
                                 % quote_local_unicode_path(local_dir))

        # TODO: allow a path rather than a cap URI.
        self._upload_dirnode = self._client.create_node_from_uri(upload_dircap)
        self._collective_dirnode = self._client.create_node_from_uri(collective_dircap)

        if not IDirectoryNode.providedBy(self._upload_dirnode):
            raise AssertionError("The URI in 'private/magic_folder_dircap' does not refer to a directory.")
        if self._upload_dirnode.is_unknown() or self._upload_dirnode.is_readonly():
            raise AssertionError("The URI in 'private/magic_folder_dircap' is not a writecap to a directory.")

        if not IDirectoryNode.providedBy(self._collective_dirnode):
            raise AssertionError("The URI in 'private/collective_dircap' does not refer to a directory.")
        if self._collective_dirnode.is_unknown() or not self._collective_dirnode.is_readonly():
            raise AssertionError("The URI in 'private/collective_dircap' is not a readonly cap to a directory.")

        self._processed_callback = lambda ign: None
        self._download_callback = lambda ign: None
        self._upload_ignore_count = 0
        self._download_ignore_count = 0

        self._notifier = self._inotify.INotify()

        if hasattr(self._notifier, 'set_pending_delay'):
            self._notifier.set_pending_delay(pending_delay)

        # We don't watch for IN_CREATE, because that would cause us to read and upload a
        # possibly-incomplete file before the application has closed it. There should always
        # be an IN_CLOSE_WRITE after an IN_CREATE (I think).
        # TODO: what about IN_MOVE_SELF, IN_MOVED_FROM, or IN_UNMOUNT?
        #
        self.mask = ( self._inotify.IN_CLOSE_WRITE
                    | self._inotify.IN_MOVED_TO
                    | self._inotify.IN_MOVED_FROM
                    | self._inotify.IN_DELETE
                    | self._inotify.IN_ONLYDIR
                    | IN_EXCL_UNLINK
                    )
        self._notifier.watch(self._local_path, mask=self.mask, callbacks=[self._notify],
                             recursive=True)

    def _should_download(self, relpath_u, remote_version):
        """
        _should_download returns a bool indicating whether or not a remote object should be downloaded.
        We check the remote metadata version against our magic-folder db version number;
        latest version wins.
        """
        v = self._db.get_local_file_version(relpath_u)
        return (v is None or v < remote_version)

    def _get_collective_latest_file(self, filename):
        """_get_collective_latest_file takes a file path pointing to a file managed by
        magic-folder and returns a deferred that fires with the two tuple containing a
        file node and metadata for the latest version of the file located in the
        magic-folder collective directory.
        """
        upload_readonly_dircap = self._upload_dirnode.get_readonly_uri()
        collective_dirmap_d = self._collective_dirnode.list()
        def do_filter(result):
            print result
            others = [x for x in result.keys() if result[x][0].get_readonly_uri() != upload_readonly_dircap]
            return result, others
        collective_dirmap_d.addCallback(do_filter)
        def scan_collective(result):
            list_of_deferreds = []
            collective_dirmap, others_list = result
            for dir_name in result:
                # XXX make sure it's a directory
                d = defer.succeed(None)
                d.addCallback(lambda x, dir_name=dir_name: collective_dirmap[dir_name][0].get_child_and_metadata(filename))
                list_of_deferreds.append(d)
            deferList = defer.DeferredList(list_of_deferreds)
            return deferList
        collective_dirmap_d.addCallback(scan_collective)
        def highest_version(deferredList):
            max_version = 0
            metadata = None
            node = None
            for success, result in deferredList:
                if success:
                    if result[1]['version'] > max_version:
                        node, metadata = result
                        max_version = result[1]['version']
            return node, metadata
        collective_dirmap_d.addCallback(highest_version)
        return collective_dirmap_d

    def _scan_remote(self, nickname, dirnode):
        listing_d = dirnode.list()
        self._download_scan_batch = {}
        def scan_listing(listing_map):
            for name in listing_map.keys():
                file_node, metadata = listing_map[name]
                if self._download_scan_batch.has_key(name):
                    self._download_scan_batch[name] += [(name, file_node, metadata)]
                else:
                    self._download_scan_batch[name] = [(name, file_node, metadata)]
        listing_d.addCallback(scan_listing)
        return listing_d

    def _scan_remote_collective(self):
        if self._collective_dirnode is None:
            return
        upload_readonly_dircap = self._upload_dirnode.get_readonly_uri()
        collective_dirmap_d = self._collective_dirnode.list()
        def do_filter(result):
            others = [x for x in result.keys() if result[x][0].get_readonly_uri() != upload_readonly_dircap]
            return result, others
        collective_dirmap_d.addCallback(do_filter)
        def scan_collective(result):
            d = defer.succeed(None)
            collective_dirmap, others_list = result
            for dir_name in others_list:
                d.addCallback(lambda x, dir_name=dir_name: self._scan_remote(dir_name, collective_dirmap[dir_name][0]))
                # XXX todo add errback
            return d
        collective_dirmap_d.addCallback(scan_collective)
        collective_dirmap_d.addCallback(self._filter_scan_batch)
        collective_dirmap_d.addCallback(self._add_batch_to_download_queue)
        return collective_dirmap_d

    def _add_batch_to_download_queue(self, result):
        self._download_deque.extend(result)
        self._download_pending.update(map(lambda x: x[0], result))

    def _filter_scan_batch(self, result):
        extension = []
        for name in self._download_scan_batch.keys():
            if name in self._download_pending:
                continue
            name, file_node, metadata = max(self._download_scan_batch[name], key=lambda x: x[2]['version'])
            if self._should_download(name, metadata['version']):
                extension += [(name, file_node, metadata)]
        return extension

    def _download_file(self, name, file_node):
        d = file_node.download_best_version()
        def succeeded(res):
            d.addCallback(lambda result: self._write_downloaded_file(name, result))
            self._stats_provider.count('magic_folder.objects_downloaded', 1)
            return None
        def failed(f):
            self._log("download failed")
            self._stats_provider.count('magic_folder.objects_download_failed', 1)
            return f
        def remove_from_pending(result):
            self._download_pending = self._download_pending.difference(set([name]))
        d.addCallbacks(succeeded, failed)
        d.addBoth(self._do_download_callback)
        d.addBoth(remove_from_pending)
        return d

    def _write_downloaded_file(self, name, file_contents):
        print "_write_downloaded_file: no-op."

    def _db_file_is_uploaded(self, childpath):
        """_db_file_is_uploaded returns true if the file was previously uploaded
        """
        assert self._db != None
        r = self._db.check_file(childpath)
        return r.was_uploaded()

    def _scan(self, localpath):
        if not os.path.isdir(localpath):
            raise AssertionError("Programmer error: _scan() must be passed a directory path.")
        quoted_path = quote_local_unicode_path(localpath)
        try:
            children = listdir_unicode(localpath)
        except EnvironmentError:
            raise(Exception("WARNING: magic folder: permission denied on directory %s" % (quoted_path,)))
        except FilenameEncodingError:
            raise(Exception("WARNING: magic folder: could not list directory %s due to a filename encoding error" % (quoted_path,)))

        d = defer.succeed(None)
        for child in children:
            assert isinstance(child, unicode), child
            childpath = os.path.join(localpath, child)

            def _process_child(ign, childpath=childpath):
                # note: symlinks to directories are both islink() and isdir()
                isdir = os.path.isdir(childpath)
                isfile = os.path.isfile(childpath)
                islink = os.path.islink(childpath)

                if islink:
                    self.warn("WARNING: cannot backup symlink %s" % quote_local_unicode_path(childpath))
                    return None
                elif isdir:
                    # process directories unconditionally
                    self._append_to_upload_deque(childpath)

                    # recurse on the child directory
                    return self._scan(childpath)
                elif isfile:
                    file_version = self._db.get_local_file_version(childpath)
                    if file_version is None:
                        # XXX upload if we didn't record our version in magicfolder db?
                        self._append_to_upload_deque(childpath)
                        return None
                    else:
                        d2 = self._get_collective_latest_file(childpath)
                        def _got_latest_file((file_node, metadata)):
                            collective_version = metadata['version']
                            if collective_version is None:
                                return None
                            if file_version > collective_version:
                                self._append_to_upload_deque(childpath)
                            elif file_version < collective_version:
                                # if a collective version of the file is newer than ours
                                # we must download it and unlink the old file from our upload dirnode
                                self._append_to_download_deque(childpath)
                                # XXX where should we save the returned deferred?
                                return self._upload_dirnode.delete(childpath, must_be_file=True)
                            else:
                                # XXX same version. do nothing.
                                pass
                        d2.addCallback(_got_latest_file)
                        return d2
                else:
                    self.warn("WARNING: cannot backup special file %s" % quote_local_unicode_path(childpath))
                    return None
            d.addCallback(_process_child)
            d.addErrback(log.err)

        return d

    def startService(self):
        self._db = backupdb.get_backupdb(self._dbfile, create_version=(backupdb.SCHEMA_v3, 3))
        if self._db is None:
            return Failure(Exception('ERROR: Unable to load magic folder db.'))

        service.MultiService.startService(self)
        d = self._notifier.startReading()
        self._stats_provider.count('magic_folder.dirs_monitored', 1)
        return d

    def ready(self):
        """ready is used to signal us to start
        processing the upload and download items...
        """
        self.is_ready = True
        self._scan(self._local_dir)
        self._scan_remote_collective()
        self._turn_upload_deque()
        self._turn_download_deque()

    def _turn_download_deque(self):
        if self._stopped:
            return
        try:
            file_path, file_node, metadata = self._download_deque.pop()
        except IndexError:
            self._log("magic folder upload deque is now empty")
            self._download_lazy_tail = defer.succeed(None)
            self._download_lazy_tail.addCallback(lambda ign: task.deferLater(reactor, self._remote_scan_delay, self._scan_remote_collective))
            self._download_lazy_tail.addCallback(lambda ign: task.deferLater(reactor, 0, self._turn_download_deque))
            return
        self._download_lazy_tail.addCallback(lambda ign: task.deferLater(reactor, 0, self._download_file, file_path, file_node))
        self._download_lazy_tail.addCallback(lambda ign: task.deferLater(reactor, self._remote_scan_delay, self._turn_download_deque))

    def _append_to_download_deque(self, path):
        if path in self._download_scan_batch.keys():
            return
        self._download_deque.append(path)
        self._download_pending.add(path)
        self._stats_provider.count('magic_folder.download_objects_queued', 1)
        if self.is_ready:
            reactor.callLater(0, self._turn_download_deque)

    def _append_to_upload_deque(self, path):
        if path in self._upload_pending:
            return
        self._upload_deque.append(path)
        self._upload_pending.add(path)
        self._stats_provider.count('magic_folder.objects_queued', 1)
        if self.is_ready:
            reactor.callLater(0, self._turn_upload_deque)

    def _turn_upload_deque(self):
        try:
            path = self._upload_deque.pop()
        except IndexError:
            self._log("magic folder upload deque is now empty")
            self._upload_lazy_tail = defer.succeed(None)
            return
        self._upload_lazy_tail.addCallback(lambda ign: task.deferLater(reactor, 0, self._process, path))
        self._upload_lazy_tail.addCallback(lambda ign: self._turn_upload_deque())

    def _notify(self, opaque, path, events_mask):
        self._log("inotify event %r, %r, %r\n" % (opaque, path, ', '.join(self._inotify.humanReadableMask(events_mask))))
        path_u = unicode_from_filepath(path)
        self._append_to_upload_deque(path_u)

    def _process(self, path_u):
        precondition(isinstance(path_u, unicode), path_u)
        d = defer.succeed(None)

        def _add_file(encoded_name_u, version):
            uploadable = FileName(path_u, self._convergence)
            return self._upload_dirnode.add_file(encoded_name_u, uploadable, metadata={"version":version}, overwrite=True)

        def _add_dir(encoded_name_u):
            self._notifier.watch(to_filepath(path_u), mask=self.mask, callbacks=[self._notify], recursive=True)
            uploadable = Data("", self._convergence)
            encoded_name_u += u"@_"
            upload_d = self._upload_dirnode.add_file(encoded_name_u, uploadable, metadata={"version":0}, overwrite=True)
            def _succeeded(ign):
                self._log("created subdirectory %r" % (path_u,))
                self._stats_provider.count('magic_folder.directories_created', 1)
            def _failed(f):
                self._log("failed to create subdirectory %r" % (path_u,))
                return f
            upload_d.addCallbacks(_succeeded, _failed)
            upload_d.addCallback(lambda ign: self._scan(path_u))
            return upload_d

        def _maybe_upload(val):
            self._upload_pending.remove(path_u)  # FIXME make _upload_pending hold relative paths
            relpath_u = os.path.relpath(path_u, self._local_dir)
            encoded_name_u = magicpath.path2magic(relpath_u)

            def get_metadata(result):
                try:
                    metadata_d = self._upload_dirnode.get_metadata_for(encoded_name_u)
                except KeyError:
                    return Failure()
                return metadata_d

            if not os.path.exists(path_u):
                self._log("drop-upload: notified object %r disappeared "
                          "(this is normal for temporary objects)" % (path_u,))
                self._stats_provider.count('magic_folder.objects_disappeared', 1)
                d2 = defer.succeed(None)
                if self._db.check_file_db_exists(relpath_u):
                    d2.addCallback(get_metadata)
                    def set_deleted(metadata):
                        current_version = self._db.get_local_file_version(relpath_u) + 1
                        metadata['version'] = current_version
                        metadata['deleted'] = True
                        emptyUploadable = Data("", self._convergence)
                        return self._upload_dirnode.add_file(encoded_name_u, emptyUploadable, overwrite=True, metadata=metadata)
                    d2.addCallback(set_deleted)
                d2.addCallback(lambda x: Exception("file does not exist"))
                return d2
            elif os.path.islink(path_u):
                raise Exception("symlink not being processed")
            if os.path.isdir(path_u):
                return _add_dir(encoded_name_u)
            elif os.path.isfile(path_u):
                version = self._db.get_local_file_version(relpath_u)
                if version is None:
                    version = 0
                else:
                    version += 1
                print "NEW VERSION %d for %r" % (version, relpath_u)
                d2 = _add_file(encoded_name_u, version)
                def add_db_entry(filenode):
                    filecap = filenode.get_uri()
                    s = os.stat(path_u)
                    size = s[stat.ST_SIZE]
                    ctime = s[stat.ST_CTIME]
                    mtime = s[stat.ST_MTIME]
                    self._db.did_upload_file(filecap, relpath_u, version, mtime, ctime, size)
                    self._stats_provider.count('magic_folder.files_uploaded', 1)
                d2.addCallback(add_db_entry)
                return d2
            else:
                raise Exception("non-directory/non-regular file not being processed")

        d.addCallback(_maybe_upload)

        def _succeeded(res):
            self._stats_provider.count('magic_folder.objects_queued', -1)
            self._stats_provider.count('magic_folder.objects_succeeded', 1)
            return res
        def _failed(f):
            self._stats_provider.count('magic_folder.objects_queued', -1)
            self._stats_provider.count('magic_folder.objects_failed', 1)
            self._log("%r while processing %r" % (f, path_u))
            return f
        d.addCallbacks(_succeeded, _failed)
        d.addBoth(self._do_processed_callback)
        return d

    def _do_download_callback(self, res):
        if self._download_ignore_count == 0:
            self._download_callback(res)
        else:
            self._download_ignore_count -= 1
        return None  # intentionally suppress failures, which have already been logged

    def _do_processed_callback(self, res):
        if self._upload_ignore_count == 0:
            self._processed_callback(res)
        else:
            self._upload_ignore_count -= 1
        return None  # intentionally suppress failures, which have already been logged

    def set_download_callback(self, callback, ignore_count=0):
        """
        set_download_callback sets a function that will be called after a
        remote filesystem notification has been processed (successfully or unsuccessfully).
        """
        self._download_callback = callback
        self._download_ignore_count = ignore_count

    def set_processed_callback(self, callback, ignore_count=0):
        """
        set_processed_callback sets a function that will be called after a
        local filesystem notification has been processed (successfully or unsuccessfully).
        """
        self._processed_callback = callback
        self._upload_ignore_count = ignore_count

    def finish(self, for_tests=False):
        self._stopped = True
        self._notifier.stopReading()
        self._stats_provider.count('magic_folder.dirs_monitored', -1)

        if for_tests and hasattr(self._notifier, 'wait_until_stopped'):
            d = self._notifier.wait_until_stopped()
        else:
            d = defer.succeed(None)

        d.addCallback(lambda x: self._download_lazy_tail)
        return d

    def remove_service(self):
        return service.MultiService.disownServiceParent(self)

    def _log(self, msg):
        self._client.log("drop-upload: " + msg)
        #print "_log %s" % (msg,)
        #open("events", "ab+").write(msg)