"""A thin, practical wrapper around curses terminal capabilities."""

# standard modules
import collections
import contextlib
import platform
import warnings
import termios
import codecs
import curses
import locale
import select
import struct
import fcntl
import time
import tty
import sys
import os

# local imports
import sequences
import keyboard

__all__ = ['Terminal']

try:
    from io import UnsupportedOperation as IOUnsupportedOperation
except ImportError:
    class IOUnsupportedOperation(Exception):
        """A dummy exception to take the place of Python 3's
        ``io.UnsupportedOperation`` in Python 2.5"""

if ('3', '0', '0') <= platform.python_version_tuple() < ('3', '2', '2+'):
    # Good till 3.2.10
    # Python 3.x < 3.2.3 has a bug in which tparm() erroneously takes a string.
    raise ImportError('Blessings needs Python 3.2.3 or greater for Python 3 '
                      'support due to http://bugs.python.org/issue10570.')


class Terminal(object):
    """An abstraction around terminal capabilities

    Unlike curses, this doesn't require clearing the screen before doing
    anything, and it's friendlier to use. It keeps the endless calls to
    ``tigetstr()`` and ``tparm()`` out of your code, and it acts intelligently
    when somebody pipes your output to a non-terminal.

    Instance attributes:

      ``stream``
        The stream the terminal outputs to. It's convenient to pass the stream
        around with the terminal; it's almost always needed when the terminal
        is and saves sticking lots of extra args on client functions in
        practice.
    """
    def __init__(self, kind=None, stream=None, force_styling=False):
        """Initialize the terminal.

        If ``stream`` is not a tty, I will default to returning an empty
        Unicode string for all capability values, so things like piping your
        output to a file won't strew escape sequences all over the place. The
        ``ls`` command sets a precedent for this: it defaults to columnar
        output when being sent to a tty and one-item-per-line when not.

        :arg kind: A terminal string as taken by ``setupterm()``. Defaults to
            the value of the ``TERM`` environment variable.
        :arg stream: A file-like object representing the terminal. Defaults to
            the original value of stdout, like ``curses.initscr()`` does.
        :arg force_styling: Whether to force the emission of capabilities, even
            if we don't seem to be in a terminal. This comes in handy if users
            are trying to pipe your output through something like ``less -r``,
            which supports terminal codes just fine but doesn't appear itself
            to be a terminal. Just expose a command-line option, and set
            ``force_styling`` based on it. Terminal initialization sequences
            will be sent to ``stream`` if it has a file descriptor and to
            ``sys.__stdout__`` otherwise. (``setupterm()`` demands to send them
            somewhere, and stdout is probably where the output is ultimately
            headed. If not, stderr is probably bound to the same terminal.)

            If you want to force styling to not happen, pass
            ``force_styling=None``.

        """
        global _CUR_TERM
        if stream is None:
            stream = sys.__stdout__
            self.stream_kb = sys.__stdin__.fileno()

        try:
            stream_descriptor = (stream.fileno() if hasattr(stream, 'fileno')
                                 and callable(stream.fileno)
                                 else None)
        except IOUnsupportedOperation:
            stream_descriptor = None

        self._is_a_tty = (stream_descriptor is not None and
                          os.isatty(stream_descriptor))
        self._does_styling = ((self.is_a_tty or force_styling) and
                              force_styling is not None)

        # keyboard input only valid when stream is sys.stdout

        # The desciptor to direct terminal initialization sequences to.
        # sys.__stdout__ seems to always have a descriptor of 1, even if output
        # is redirected.
        self._init_descriptor = (sys.__stdout__.fileno()
                                 if stream_descriptor is None
                                 else stream_descriptor)
        self._kind = kind or os.environ.get('TERM', 'unknown')
        if self.does_styling:
            # Make things like tigetstr() work. Explicit args make setupterm()
            # work even when -s is passed to nosetests. Lean toward sending
            # init sequences to the stream if it has a file descriptor, and
            # send them to stdout as a fallback, since they have to go
            # somewhere.
            try:
                curses.setupterm(self._kind, self._init_descriptor)
            except curses.error:
                warnings.warn('Failed to setupterm(kind=%s)' % (self._kind,))
                self._kind = None
                self._does_styling = False
            else:
                if _CUR_TERM is None or self._kind == _CUR_TERM:
                    _CUR_TERM = self._kind
                else:
                    warnings.warn(
                        'A terminal of kind "%s" has been requested; due to an'
                        ' internal python curses bug,  terminal capabilities'
                        ' for a terminal of kind "%s" will continue to be'
                        ' returned for the remainder of this process. see:'
                        ' https://github.com/erikrose/blessings/issues/33' % (
                            self._kind, _CUR_TERM,))

        if self.does_styling:
            sequences.init_sequence_patterns(self)

            # build database of int code <=> KEY_NAME
            self._keycodes = keyboard.get_keyboard_codes()

            # store attributes as: self.KEY_NAME = code
            for key_code, key_name in self._keycodes.items():
                setattr(self, key_name, key_code)

            # build database of sequence <=> KEY_NAME
            self._keymap = keyboard.get_keyboard_sequences(self)

        self._keyboard_buf = []
        locale.setlocale(locale.LC_ALL, '')
        self._encoding = locale.getpreferredencoding()
        self._keyboard_decoder = codecs.getincrementaldecoder(self._encoding)()

        self.stream = stream

    # Sugary names for commonly-used capabilities, intended to help avoid trips
    # to the terminfo man page and comments in your code:
    _sugar = dict(
        # Don't use "on" or "bright" as an underscore-separated chunk in any of
        # these (e.g. on_cology or rock_on) so we don't interfere with
        # __getattr__.
        save='sc',
        restore='rc',

        clear_eol='el',
        clear_bol='el1',
        clear_eos='ed',
        # 'clear' clears the whole screen.
        position='cup',  # deprecated
        enter_fullscreen='smcup',
        exit_fullscreen='rmcup',
        move='cup',
        move_x='hpa',
        move_y='vpa',
        move_left='cub1',
        move_right='cuf1',
        move_up='cuu1',
        move_down='cud1',

        hide_cursor='civis',
        normal_cursor='cnorm',

        reset_colors='op',  # oc doesn't work on my OS X terminal.

        normal='sgr0',
        reverse='rev',
        # 'bold' is just 'bold'. Similarly...
        # blink
        # dim
        # flash
        italic='sitm',
        no_italic='ritm',
        shadow='sshm',
        no_shadow='rshm',
        standout='smso',
        no_standout='rmso',
        subscript='ssubm',
        no_subscript='rsubm',
        superscript='ssupm',
        no_superscript='rsupm',
        underline='smul',
        no_underline='rmul')

    def __getattr__(self, attr):
        """Return a terminal capability, like bold.

        For example, you can say ``term.bold`` to get the string that turns on
        bold formatting and ``term.normal`` to get the string that turns it off
        again. Or you can take a shortcut: ``term.bold('hi')`` bolds its
        argument and sets everything to normal afterward. You can even combine
        things: ``term.bold_underline_red_on_bright_green('yowzers!')``.

        For a parametrized capability like ``cup``, pass the parameters too:
        ``some_term.cup(line, column)``.

        ``man terminfo`` for a complete list of capabilities.

        Return values are always Unicode.

        """
        resolution = (self._resolve_formatter(attr) if self.does_styling
                      else NullCallableString())
        setattr(self, attr, resolution)  # Cache capability codes.
        return resolution

    @property
    def does_styling(self):
        """Whether attempt to emit capabilities

        This is influenced by the ``is_a_tty`` property and by the
        ``force_styling`` argument to the constructor. You can examine
        this value to decide whether to draw progress bars or other frippery.

        """
        return self._does_styling

    @property
    def is_a_tty(self):
        """Whether my ``stream`` appears to be associated with a terminal"""
        return self._is_a_tty

    @property
    def height(self):
        """T.height -> int

        The height of the terminal in characters.

        If an alternative ``stream`` is chosen, the size of that stream
        is returned if it is a connected to a terminal such as a pty.
        Otherwise, the size of the controlling terminal is returned.

        If neither of these streams are terminals, such as when stdout is piped
        to less(1), the values of the environment variable LINES and COLS are
        returned.

        None may be returned if no suitable size is discovered.
        """
        return self._height_and_width()[1]

    @property
    def width(self):
        """T.width -> int

        The width of the terminal in characters.

        None may be returned if no suitable size is discovered.
        """
        return self._height_and_width()[0]

    @staticmethod
    def _winsize(fd):
        """T._winsize -> WINSZ(ws_row, ws_col, ws_xpixel, ws_ypixel)

        The tty connected by file desriptor fd is queried for its window size,
        and returned as a collections.namedtuple instance WINSZ.

        May raise exception IOError.
        """
        data = fcntl.ioctl(fd, termios.TIOCGWINSZ, WINSZ._BUF)
        return WINSZ(*struct.unpack(WINSZ._FMT, data))

    def _height_and_width(self):
        """Return a tuple of (terminal height, terminal width).
        """
        # TODO(jquast): hey kids, even if stdout is redirected to a file,
        # we can still query sys.__stdin__.fileno() for our terminal size.
        # -- of course, if both are redirected, we have no use for this fd.
        for descriptor in self._init_descriptor, sys.__stdout__:
            try:
                winsize = self._winsize(descriptor)
                return winsize.ws_row, winsize.ws_col
            except IOError:
                pass

        lines, cols = None, None
        if os.environ.get('LINES', None) is not None:
            lines = int(os.environ['LINES'])
        if os.environ.get('COLUMNS', None) is not None:
            cols = int(os.environ['COLUMNS'])
        return lines, cols

    @contextlib.contextmanager
    def location(self, x=None, y=None):
        """Return a context manager for temporarily moving the cursor.

        Move the cursor to a certain position on entry, let you print stuff
        there, then return the cursor to its original position::

            term = Terminal()
            with term.location(2, 5):
                print 'Hello, world!'
                for x in xrange(10):
                    print 'I can do it %i times!' % x

        Specify ``x`` to move to a certain column, ``y`` to move to a certain
        row, both, or neither. If you specify neither, only the saving and
        restoration of cursor position will happen. This can be useful if you
        simply want to restore your place after doing some manual cursor
        movement.

        """
        # Save position and move to the requested column, row, or both:
        self.stream.write(self.save)
        if x is not None and y is not None:
            self.stream.write(self.move(y, x))
        elif x is not None:
            self.stream.write(self.move_x(x))
        elif y is not None:
            self.stream.write(self.move_y(y))
        try:
            yield
        finally:
            # Restore original cursor position:
            self.stream.write(self.restore)

    @contextlib.contextmanager
    def fullscreen(self):
        """Return a context manager that enters fullscreen mode while inside it
        and restores normal mode on leaving."""
        self.stream.write(self.enter_fullscreen)
        try:
            yield
        finally:
            self.stream.write(self.exit_fullscreen)

    @contextlib.contextmanager
    def hidden_cursor(self):
        """Return a context manager that hides the cursor while inside it and
        makes it visible on leaving."""
        self.stream.write(self.hide_cursor)
        try:
            yield
        finally:
            self.stream.write(self.normal_cursor)

    @property
    def color(self):
        """Return a capability that sets the foreground color.

        The capability is unparametrized until called and passed a number
        (0-15), at which point it returns another string which represents a
        specific color change. This second string can further be called to
        color a piece of text and set everything back to normal afterward.

        :arg num: The number, 0-15, of the color

        """
        return (ParametrizingString(self._foreground_color, self.normal)
                if self.does_styling else NullCallableString())

    @property
    def on_color(self):
        """Return a capability that sets the background color.

        See ``color()``.

        """
        return (ParametrizingString(self._background_color, self.normal)
                if self.does_styling else NullCallableString())

    @property
    def number_of_colors(self):
        """Return the number of colors the terminal supports.

        Common values are 0, 8, 16, 88, and 256.

        Though the underlying capability returns -1 when there is no color
        support, we return 0. This lets you test more Pythonically::

            if term.number_of_colors:
                ...

        We also return 0 if the terminal won't tell us how many colors it
        supports, which I think is rare.

        """
        # This is actually the only remotely useful numeric capability. We
        # don't name it after the underlying capability, because we deviate
        # slightly from its behavior, and we might someday wish to give direct
        # access to it.
        # Returns -1 if no color support, -2 if no such capability.
        colors = self.does_styling and curses.tigetnum('colors') or -1
        #  self.__dict__['colors'] = ret  # Cache it. It's not changing.
                                          # (Doesn't work.)
        return max(0, colors)

    def _resolve_formatter(self, attr):
        """Resolve a sugary or plain capability name, color, or compound
        formatting function name into a callable capability.

        Return a ``ParametrizingString`` or a ``FormattingString``.

        """
        if attr in COLORS:
            return self._resolve_color(attr)
        elif attr in COMPOUNDABLES:
            # Bold, underline, or something that takes no parameters
            return self._formatting_string(self._resolve_capability(attr))
        else:
            formatters = split_into_formatters(attr)
            if all(f in COMPOUNDABLES for f in formatters):
                # It's a compound formatter, like "bold_green_on_red". Future
                # optimization: combine all formatting into a single escape
                # sequence.
                return self._formatting_string(
                    u''.join(self._resolve_formatter(s) for s in formatters))
            else:
                return ParametrizingString(self._resolve_capability(attr))

    def _resolve_capability(self, atom):
        """Return a terminal code for a capname or a sugary name, or an empty
        Unicode.

        The return value is always Unicode, because otherwise it is clumsy
        (especially in Python 3) to concatenate with real (Unicode) strings.

        """
        code = curses.tigetstr(self._sugar.get(atom, atom))
        if code:
            # Decode sequences as latin1, as they are always 8-bit bytes.
            return code.decode('latin1')
        return u''

    def _resolve_color(self, color):
        """Resolve a color like red or on_bright_green into a callable
        capability."""
        # TODO: Does curses automatically exchange red and blue and cyan and
        # yellow when a terminal supports setf/setb rather than setaf/setab?
        # I'll be blasted if I can find any documentation. The following
        # assumes it does.
        color_cap = (self._background_color if 'on_' in color else
                     self._foreground_color)
        # curses constants go up to only 7, so add an offset to get at the
        # bright colors at 8-15:
        offset = 8 if 'bright_' in color else 0
        base_color = color.rsplit('_', 1)[-1]
        if self.number_of_colors == 0:
            return NullCallableString()
        return self._formatting_string(
            color_cap(getattr(curses, 'COLOR_' + base_color.upper()) + offset))

    @property
    def _foreground_color(self):
        return self.setaf or self.setf

    @property
    def _background_color(self):
        return self.setab or self.setb

    def _formatting_string(self, formatting):
        """Return a new ``FormattingString`` which implicitly receives my
        notion of "normal"."""
        return FormattingString(formatting, self.normal)

    def ljust(self, text, width=None, fillchar=u' '):
        """T.ljust(text, [width], [fillchar]) -> string

        Return string ``text``, left-justified by printable length ``width``.
        Padding is done using the specified fill character (default is a
        space).  Default width is the attached terminal's width. ``text`` is
        escape-sequence safe."""
        if width is None:
            width = self.width
        return sequences.Sequence(text, self).ljust(width, fillchar)

    def rjust(self, text, width=None, fillchar=u' '):
        """T.rjust(text, [width], [fillchar]) -> string

        Return string ``text``, right-justified by printable length ``width``.
        Padding is done using the specified fill character (default is a space)
        Default width is the attached terminal's width. ``text`` is
        escape-sequence safe."""
        if width is None:
            width = self.width
        return sequences.Sequence(text, self).rjust(width, fillchar)

    def center(self, text, width=None, fillchar=u' '):
        """T.center(text, [width], [fillchar]) -> string

        Return string ``text``, centered by printable length ``width``.
        Padding is done using the specified fill character (default is a
        space).  Default width is the attached terminal's width. ``text`` is
        escape-sequence safe."""
        if width is None:
            width = self.width
        return sequences.Sequence(text, self).center(width, fillchar)

    def length(self, text):
        """T.length(text) -> int

        Return printable length of string ``text``, which may contain (some
        kinds) of sequences. Strings containing sequences such as 'clear',
        which repositions the cursor will not give accurate results.
        """
        return sequences.Sequence(text, self).length()

    def wrap(self, text, width=None, **kwargs):
        """T.wrap(text, [width=None, indent=u'', ...]) -> unicode

        Wrap paragraphs containing escape sequences, ``text``, to the full
        width of Terminal instance T, unless width is specified, wrapped by
        the virtual printable length, irregardless of the video attribute
        sequences it may contain.

        Returns a list of strings that may contain escape sequences. See
        textwrap.TextWrapper class for available additional kwargs to
        customize wrapping behavior.

        Note that the keyword argument ``break_long_words`` may not be set,
        it is not sequence-safe.
        """

        _blw = 'break_long_words'
        assert (_blw not in kwargs or not kwargs[_blw]), (
            "keyword argument, '{}' is not sequence-safe".format(_blw))

        width = width is None and self.width or width
        lines = []
        for line in text.splitlines():
            lines.extend(
                (_linewrap for _linewrap in sequences.SequenceTextWrapper(
                    width=width, term=self, **kwargs).wrap(text))
                if line.strip() else (u'',))

        return lines

    def kbhit(self, timeout=0):
        """T.kbhit([timeout=0]) -> bool

        Returns True if a keypress has been detected on keyboard.

        When ``timeout`` is 0, this call is non-blocking(default), or blocking
        indefinitely until keypress when ``None``, and blocking until keypress
        or time elapsed when ``timeout`` is non-zero.

        If input is not a terminal, False is always returned.
        """
        if self.keyboard_fd is None:
            return False

        check_r, check_w, check_x = [self.stream_kb], [], []
        ready_r, ready_w, ready_x = select.select(
            check_r, check_w, check_x, timeout)

        return check_r == ready_r

    @contextlib.contextmanager
    def cbreak(self):
        """Return a context manager that enters 'cbreak' mode: disabling line
        buffering of keyboard input, making characters typed by the user
        immediately available to the program.  Also referred to as 'rare'
        mode, this is the opposite of 'cooked' mode, the default for most
        shells.

        In 'cbreak' mode, echo of input is also disabled: the application must
        explicitly print any input received, if they so wish.

        More information can be found in the manual page for curses.h,
           http://www.openbsd.org/cgi-bin/man.cgi?query=cbreak

        The python manual for curses,
           http://docs.python.org/2/library/curses.html

        Note also that setcbreak sets VMIN = 1 and VTIME = 0,
           http://www.unixwiz.net/techtips/termios-vmin-vtime.html
        """
        assert self.is_a_tty, u'stream is not a a tty.'
        if self.stream_kb is not None:
            # save current terminal mode,
            save_mode = termios.tcgetattr(self.stream_kb)
            tty.setcbreak(self.stream_kb, termios.TCSANOW)
            try:
                yield
            finally:
                # restore prior mode,
                termios.tcsetattr(self.stream_kb, termios.TCSAFLUSH, save_mode)
        else:
            yield

    def inkey(self, timeout=None, esc_delay=0.35):
        """T.inkey(timeout=None, esc_delay=0.35) -> Keypress()

        Receive next keystroke from keyboard (stdin), blocking until a
        keypress is received or ``timeout`` elapsed, if specified.

        When used without the context manager ``cbreak``, stdin remains
        line-buffered, and this function will block until return is pressed.

        The value returned is an instance of ``Keystroke``, with properties
        ``is_sequence``, and, when True, non-None values for ``code`` and
        ``name``. The value of ``code`` may be compared against attributes
        of this terminal beginning with KEY, such as KEY_ESCAPE.

        To distinguish between KEY_ESCAPE, and sequences beginning with
        escape, the ``esc_delay`` specifies the amount of time after receiving
        the escape character ('\x1b') to seek for application keys.

        """
        # TODO(jquast): "meta sends escape", where alt+1 would send '\x1b1',
        #               what do we do with that? Surely, something useful.
        #               comparator to term.KEY_meta('x') ?
        # TODO(jquast): Ctrl characters, KEY_CTRL_[A-Z], and the rest;
        #               KEY_CTRL_\, KEY_CTRL_{, etc. are not legitimate
        #               attributes. comparator to term.KEY_ctrl('z') ?
        def _timeleft(stime, timeout):
            """_timeleft(stime, timeout) -> float

            Returns time-relative time remaining before ``timeout`` after time
            elapsed since ``stime``.
            """
            if timeout is not None:
                if timeout is 0:
                    return 0
                return max(0, timeout - (time.time() - stime))

        def _decode_next():
            """Read and decode next byte from stdin."""
            byte = os.read(self.stream_kb, 1)
            return self._keyboard_decoder.decode(byte, final=False)

        def _resolve(text):
            return keyboard.resolve_sequence(text=text,
                                             mapper=self._keymap,
                                             codes=self._keycodes)

        stime = time.time()

        # re-buffer previously received keystrokes,
        ucs = u''
        while self._keyboard_buf:
            ucs += self._keyboard_buf.pop()

        # receive all immediately available bytes
        while self.kbhit():
            ucs += _decode_next()

        # decode keystroke, if any
        ks = _resolve(ucs)

        # so long as the most immediately received or buffered keystroke is
        # incomplete, (which may be a multibyte encoding), block until until
        # one is received.
        while not ks and self.kbhit(_timeleft(stime, timeout)):
            ucs += _decode_next()
            ks = _resolve(ucs)

        # handle escape key (KEY_ESCAPE) vs. escape sequence (which begins
        # with KEY_ESCAPE, \x1b[, \x1bO, or \x1b?), up to esc_delay when
        # received. This is not optimal, but causes least delay when
        # (currently unhandled, and rare) "meta sends escape" is used,
        # or when an unsupported sequence is sent.

        if ks.code is self.KEY_ESCAPE:
            esctime = time.time()
            while (ks.code is self.KEY_ESCAPE and
                   # XXX specially handle [?O sequence,
                   # which may soon become [?O{A,B,C,D}, as there
                   # is also an [?O in some terminal types
                   (len(ucs) <= 1 or ucs[1] in u'[?O') and
                   self.kbhit(_timeleft(esctime, esc_delay))):
                ucs += _decode_next()
                ks = _resolve(ucs)

        for remaining in ucs[len(ks):]:
            self._keyboard_buf.insert(0, remaining)
        return ks


def derivative_colors(colors):
    """Return the names of valid color variants, given the base colors."""
    return set([('on_' + c) for c in colors] +
               [('bright_' + c) for c in colors] +
               [('on_bright_' + c) for c in colors])


COLORS = set(['black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan',
              'white'])
COLORS.update(derivative_colors(COLORS))
COMPOUNDABLES = (COLORS |
                 set(['bold', 'underline', 'reverse', 'blink', 'dim', 'italic',
                      'shadow', 'standout', 'subscript', 'superscript']))


class ParametrizingString(unicode):
    """A Unicode string which can be called to parametrize it as a terminal
    capability"""

    def __new__(cls, formatting, normal=None):
        """Instantiate.

        :arg normal: If non-None, indicates that, once parametrized, this can
            be used as a ``FormattingString``. The value is used as the
            "normal" capability.

        """
        new = unicode.__new__(cls, formatting)
        new._normal = normal
        return new

    def __call__(self, *args):
        try:
            # Re-encode the cap, because tparm() takes a bytestring in Python
            # 3. However, appear to be a plain Unicode string otherwise so
            # concats work.
            parametrized = curses.tparm(
                self.encode('latin1'), *args).decode('latin1')
            return (parametrized if self._normal is None else
                    FormattingString(parametrized, self._normal))
        except TypeError:
            # If the first non-int (i.e. incorrect) arg was a string, suggest
            # something intelligent:
            if len(args) == 1 and isinstance(args[0], basestring):
                raise TypeError(
                    'A native or nonexistent capability template received '
                    '%r when it was expecting ints. You probably misspelled a '
                    'formatting call like bright_red_on_white(...).' % args)
            else:
                # Somebody passed a non-string; I don't feel confident
                # guessing what they were trying to do.
                raise


class FormattingString(unicode):
    """A Unicode string which can be called upon a piece of text to wrap it in
    formatting"""

    def __new__(cls, formatting, normal):
        new = unicode.__new__(cls, formatting)
        new._normal = normal
        return new

    def __call__(self, text):
        """Return a new string that is ``text`` formatted with my contents.

        At the beginning of the string, I prepend the formatting that is my
        contents. At the end, I append the "normal" sequence to set everything
        back to defaults. The return value is always a Unicode.

        """
        return self + text + self._normal


class NullCallableString(unicode):
    """A dummy callable Unicode to stand in for ``FormattingString`` and
    ``ParametrizingString``

    We use this when there is no tty and thus all capabilities should be blank.

    """
    def __new__(cls):
        new = unicode.__new__(cls, u'')
        return new

    def __call__(self, *args):
        """Return a Unicode or whatever you passed in as the first arg
        (hopefully a string of some kind).

        When called with an int as the first arg, return an empty Unicode. An
        int is a good hint that I am a ``ParametrizingString``, as there are
        only about half a dozen string-returning capabilities on OS X's
        terminfo man page which take any param that's not an int, and those are
        seldom if ever used on modern terminal emulators. (Most have to do with
        programming function keys. Blessings' story for supporting
        non-string-returning caps is undeveloped.) And any parametrized
        capability in a situation where all capabilities themselves are taken
        to be blank are, of course, themselves blank.

        When called with a non-int as the first arg (no no args at all), return
        the first arg. I am acting as a ``FormattingString``.

        """
        if len(args) != 1 or isinstance(args[0], int):
            # I am acting as a ParametrizingString.

            # tparm can take not only ints but also (at least) strings as its
            # second...nth args. But we don't support callably parametrizing
            # caps that take non-ints yet, so we can cheap out here. TODO: Go
            # through enough of the motions in the capability resolvers to
            # determine which of 2 special-purpose classes,
            # NullParametrizableString or NullFormattingString, to return, and
            # retire this one.
            # As a NullCallableString, even when provided with a parameter,
            # such as t.color(5), we must also still be callable, fe:
            # >>> t.color(5)('shmoo')
            # is actually simplified result of NullCallable()(), so
            # turtles all the way down: we return another instance.
            return NullCallableString()
        return args[0]  # Should we force even strs in Python 2.x to be
                        # unicodes? No. How would I know what encoding to use
                        # to convert it?

WINSZ = collections.namedtuple('WINSZ', (
    'ws_row',     # /* rows, in characters */
    'ws_col',     # /* columns, in characters */
    'ws_xpixel',  # /* horizontal size, pixels */
    'ws_ypixel',  # /* vertical size, pixels */
))
#: format of termios structure
WINSZ._FMT = 'hhhh'
#: buffer of termios structure appropriate for ioctl argument
WINSZ._BUF = '\x00' * struct.calcsize(WINSZ._FMT)


def split_into_formatters(compound):
    """Split a possibly compound format string into segments.

    >>> split_into_formatters('bold_underline_bright_blue_on_red')
    ['bold', 'underline', 'bright_blue', 'on_red']

    """
    merged_segs = []
    # These occur only as prefixes, so they can always be merged:
    mergeable_prefixes = ['on', 'bright', 'on_bright']
    for s in compound.split('_'):
        if merged_segs and merged_segs[-1] in mergeable_prefixes:
            merged_segs[-1] += '_' + s
        else:
            merged_segs.append(s)
    return merged_segs

# From libcurses/doc/ncurses-intro.html (ESR, Thomas Dickey, et. al):
#
#   "After the call to setupterm(), the global variable cur_term is set to
#    point to the current structure of terminal capabilities. By calling
#    setupterm() for each terminal, and saving and restoring cur_term, it
#    is possible for a program to use two or more terminals at once."
#
# However, if you study Python's ./Modules/_cursesmodule.c, you'll find:
#
#   if (!initialised_setupterm && setupterm(termstr,fd,&err) == ERR) {
#
# Python - perhaps wrongly - will not allow a re-initialisation of new
# terminals through setupterm(), so the value of cur_term cannot be changed
# once set: subsequent calls to setupterm() have no effect.
#
# Therefore, the ``kind`` of each Terminal() is, in essence, a singleton.
# This global variable reflects that, and a warning is emitted if somebody
# expects otherwise.

_CUR_TERM = None