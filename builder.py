import sys, os, random, errno, stat
import vars, jwack, state
from helpers import log, log_, debug2, err, unlink, close_on_exec


def _possible_do_files(t):
    yield "%s.do" % t, t, ''
    dirname,filename = os.path.split(t)
    l = filename.split('.')
    l[0] = os.path.join(dirname, l[0])
    for i in range(1,len(l)+1):
        basename = '.'.join(l[:i])
        ext = '.'.join(l[i:])
        if ext: ext = '.' + ext
        yield (os.path.join(dirname, "default%s.do" % ext),
               os.path.join(dirname, basename), ext)


def _find_do_file(t):
    for dofile,basename,ext in _possible_do_files(t):
        debug2('%s: %s ?\n' % (t, dofile))
        if os.path.exists(dofile):
            state.add_dep(t, 'm', dofile)
            return dofile,basename,ext
        else:
            state.add_dep(t, 'c', dofile)
    return None,None,None


def _nice(t):
    return os.path.normpath(os.path.join(vars.PWD, t))


def _try_stat(filename):
    try:
        return os.stat(filename)
    except OSError, e:
        if e.errno == errno.ENOENT:
            return None
        else:
            raise


class BuildJob:
    def __init__(self, t, lock, shouldbuildfunc, donefunc):
        self.t = t
        self.tmpname = '%s.redo.tmp' % t
        self.lock = lock
        self.shouldbuildfunc = shouldbuildfunc
        self.donefunc = donefunc
        self.before_t = _try_stat(self.t)

    def start(self):
        assert(self.lock.owned)
        t = self.t
        tmpname = self.tmpname
        if not self.shouldbuildfunc(t):
            # target doesn't need to be built; skip the whole task
            return self._after2(0)
        if (os.path.exists(t) and not state.is_generated(t)
             and not os.path.exists('%s.do' % t)):
            # an existing source file that is not marked as a generated file.
            # This step is mentioned by djb in his notes.  It turns out to be
            # important to prevent infinite recursion.  For example, a rule
            # called default.c.do could be used to try to produce hello.c,
            # which is undesirable since hello.c existed already.
            state.stamp(t)
            return self._after2(0)
        state.start(t)
        (dofile, basename, ext) = _find_do_file(t)
        if not dofile:
            if os.path.exists(t):
                return self._after2(0)
            else:
                err('no rule to make %r\n' % t)
                return self._after2(1)
        state.stamp(dofile)
        unlink(tmpname)
        ffd = os.open(tmpname, os.O_CREAT|os.O_RDWR|os.O_EXCL, 0666)
        close_on_exec(ffd, True)
        self.f = os.fdopen(ffd, 'w+')
        # this will run in the dofile's directory, so use only basenames here
        argv = ['sh', '-e',
                os.path.basename(dofile),
                os.path.basename(basename),  # target name (extension removed)
                ext,  # extension (if any), including leading dot
                os.path.basename(tmpname)  # randomized output file name
                ]
        if vars.VERBOSE: argv[1] += 'v'
        if vars.XTRACE: argv[1] += 'x'
        if vars.VERBOSE or vars.XTRACE: log_('\n')
        log('%s\n' % _nice(t))
        self.argv = argv
        jwack.start_job(t, self._do_subproc, self._after)

    def _do_subproc(self):
        td = os.environ.get('REDO_PWD', '')
        dn = os.path.dirname(self.t)
        os.environ['REDO_PWD'] = os.path.join(td, dn)
        os.environ['REDO_TARGET'] = os.path.basename(self.t)
        os.environ['REDO_DEPTH'] = vars.DEPTH + '  '
        if dn:
            os.chdir(dn)
        os.dup2(self.f.fileno(), 1)
        os.close(self.f.fileno())
        close_on_exec(1, False)
        os.execvp(self.argv[0], self.argv)
        assert(0)
        # returns only if there's an exception

    def _after(self, t, rv):
        try:
            rv = self._after1(t, rv)
        finally:
            self._after2(rv)

    def _after1(self, t, rv):
        f = self.f
        tmpname = self.tmpname
        before_t = self.before_t
        after_t = _try_stat(t)
        before_tmp = os.fstat(f.fileno())
        after_tmp = _try_stat(tmpname)
        after_where = os.lseek(f.fileno(), 0, os.SEEK_CUR)
        if after_t != before_t and not stat.S_ISDIR(after_t.st_mode):
            err('%r modified %r directly!\n' % (self.argv[2], t))
            err('...you should update $3 (a temp file) instead of $1.\n')
            rv = 206
        elif after_tmp and before_tmp != after_tmp and before_tmp.st_size > 0:
            err('%r wrote to stdout *and* replaced $3.\n' % self.argv[2])
            err('...you should write status messages to stderr, not stdout.\n')
            rv = 207
        elif after_where > 0 and after_tmp and after_tmp.st_size != after_where:
            err('%r wrote differing data to stdout and $3.\n' % self.argv[2])
            err('...you should write status messages to stderr, not stdout.\n')
            rv = 208
        if rv==0:
            if os.path.exists(tmpname) and os.stat(tmpname).st_size:
                # there's a race condition here, but if the tmpfile disappears
                # at *this* point you deserve to get an error, because you're
                # doing something totally scary.
                os.rename(tmpname, t)
            else:
                unlink(tmpname)
            state.built(t)
            state.stamp(t)
        else:
            unlink(tmpname)
            state.unstamp(t)
        f.close()
        if rv != 0:
            err('%s: exit code %d\n' % (_nice(t),rv))
        else:
            if vars.VERBOSE or vars.XTRACE:
                log('%s (done)\n\n' % _nice(t))
        return rv

    def _after2(self, rv):
        try:
            self.donefunc(self.t, rv)
            assert(self.lock.owned)
        finally:
            self.lock.unlock()


def main(targets, shouldbuildfunc):
    retcode = [0]  # a list so that it can be reassigned from done()
    if vars.SHUFFLE:
        random.shuffle(targets)

    locked = []

    def done(t, rv):
        if rv:
            retcode[0] = 1

    for i in range(len(targets)):
        t = targets[i]

    # In the first cycle, we just build as much as we can without worrying
    # about any lock contention.  If someone else has it locked, we move on.
    for t in targets:
        jwack.get_token(t)
        if retcode[0] and not vars.KEEP_GOING:
            break
        if not state.is_sane():
            retcode[0] = 205
            break
        lock = state.Lock(t)
        lock.trylock()
        if not lock.owned:
            if vars.DEBUG_LOCKS:
                log('%s (locked...)\n' % _nice(t))
            locked.append(t)
        else:
            BuildJob(t, lock, shouldbuildfunc, done).start()

    # Now we've built all the "easy" ones.  Go back and just wait on the
    # remaining ones one by one.  This is technically non-optimal; we could
    # use select.select() to wait on more than one at a time.  But it should
    # be rare enough that it doesn't matter, and the logic is easier this way.
    while locked or jwack.running():
        jwack.wait_all()
        # at this point, we don't have any children holding any tokens, so
        # it's okay to block below.
        if retcode[0] and not vars.KEEP_GOING:
            break
        if locked:
            if not state.is_sane():
                retcode[0] = 205
                break
            t = locked.pop(0)
            lock = state.Lock(t)
            lock.waitlock()
            assert(lock.owned)
            if vars.DEBUG_LOCKS:
                log('%s (...unlocked!)\n' % _nice(t))
            if state.stamped(t) == None:
                err('%s: failed in another thread\n' % _nice(t))
                retcode[0] = 2
                lock.unlock()
            else:
                BuildJob(t, lock, shouldbuildfunc, done).start()
    return retcode[0]