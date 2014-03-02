import sqlalchemy.engine, sqlalchemy.event
import traceback, time, collections, sys, mako.template, os, Queue

class QueryStats(object):
    """ Statistics about a query

    You should not create these objects, but your application may choose
    inspect them in the filter functions you pass to :func:`sqltap.collect`
    and :func:`sqltap.purge`.
    
    :attr text: The text of the query
    :attr stack: The stack trace when this query was issued. Formatted as
        returned by py:func:`traceback.extract_stack`
    :attr duration: Duration of the query in seconds.
    :attr user_context: The value returned by the user_context_fn set 
        with :func:`sqltap.start`.
    """
    def __init__(self, text, stack, duration, user_context):
        self.text = text
        self.stack = stack
        self.duration = duration
        self.user_context = user_context

class ProfilingSession(object):
    """ A ProfilingSession captures queries run on an Engine and metadata about them.

    The profiling session hooks into SQLAlchmey and captures query text,
    timing information, and backtraces of where those queries came from.

    You may have multiple profiling sessions active at the same time on
    the same or different Engines. If multiple profiling sessions are
    active on the same engine, queries on that engine will be collected
    by both sessions.

    You may pass a context function to the session's constructor which
    will be executed at each query invocation and its result stored with
    that query. This is useful for associating queries with specific
    requests in a web framework, or specific threads in a process.

    By default, a session collects all of :class:`QueryStats` objects in
    an internal queue whose contents you can retrieve by calling
    :func:`ProfilingSession.collect`. If you want to collect the query
    results continually, you may do so by passing your own collection
    function to the session's constructor.

    You may start, stop, and restart a profiling session as much as you
    like. Calling start on an already started session or stop on an
    already stopped session will raise an AssertionError.

    You may use a profiling session object like a context manager. This
    has the effect of only profiling queries issued while executing
    within the context.

    Example usage::
        
        profiler = ProfilingSession()
        with profiler:
            for number in Session.query(Numbers).filter(Numbers.value <= 3):
                print number

    You may also use a profiling session object like a decorator. This
    has the effect of only profiling queries issued within the decorated
    function.

    Example usage::
        
        profiler = ProfilingSession()

        @profiler
        def holy_hand_grenade():
            for number in Session.query(Numbers).filter(Numbers.value <= 3):
                print number
    """

    def __init__(self, engine=sqlalchemy.engine.Engine, user_context_fn=None, collect_fn=None):
        """ Create a new :class:`ProfilingSession` object

        :param engine: The sqlalchemy engine on which you want to
            profile queries. The default is sqlalchemy.engine.Engine
            which will profile queries across all engines.

        :param user_context_fn: A function which returns a value to be stored
            with the query statistics. The function takes the same parameters 
            passed to the after_execute event in sqlalchemy: 
            (conn, clause, multiparams, params, results)

        :param collect_fn: A function which accepts a :class:`QueryStats`
            argument. If specified, the :class:`ProfilingSession` will not
            save queries in an internal queue and will instead pass them
            to this function immediately.
        """
        self.started = False
        self.engine = engine
        self.user_context_fn = user_context_fn

        if collect_fn:
            # the user said they want to do their own collecting
            self.collector = None
            self.collect_fn = collect_fn
        else:
            # we're doing the collecting, make an unbounded thread-safe queue
            self.collector = Queue.Queue(0)
            self.collect_fn = self.collector.put

    def _before_exec(self, conn, clause, multiparams, params):
        """ SQLAlchemy event hook """
        conn._sqltap_query_start_time = time.time()

    def _after_exec(self, conn, clause, multiparams, params, results):
        """ SQLAlchemy event hook """
        # calculate the query time
        duration = time.time() - conn._sqltap_query_start_time

        # get the user's context
        context = (None if not self.user_context_fn else
                   self.user_context_fn(conn, clause, multiparams, params, results))

        # add the querystats to the collector
        self.collect_fn(QueryStats(clause, traceback.extract_stack()[:-1], duration, context))

    def collect(self):
        """ Return all queries collected by this profiling session so far.
        Throws an exception if you passed a `collect_fn` argument to the
        session's constructor.
        """
        if not self.collector:
            raise AssertionError("Can't call collect when you've registered your own collect_fn!")

        queries = []
        try:
            while True:
                queries.append(self.collector.get(block=False))
        except Queue.Empty:
            pass

        return queries

    def start(self):
        """ Start profiling

        :raises AssertionError: If calling this function when the session 
            is already started.
        """
        if self.started == True:
            raise AssertionError("Profiling session is already started!")

        self.started = True
        sqlalchemy.event.listen(self.engine, "before_execute", self._before_exec)
        sqlalchemy.event.listen(self.engine, "after_execute", self._after_exec)

    def stop(self):
        """ Stop profiling

        :raises AssertionError: If calling this function when the session 
            is already stopped.
        """
        if self.started == False:
            raise AssertionError("Profiling session is already stopped")

        self.started = False
        sqlalchemy.event.remove(self.engine, "before_execute", self._before_exec)
        sqlalchemy.event.remove(self.engine, "after_execute", self._after_exec)

    def __enter__(self, *args, **kwargs):
        """ context manager """
        self.start()

    def __exit__(self, *args, **kwargs):
        """ context manager """
        self.stop()

    def __call__(self, fn):
        """ decorator """
        def decorated(*args, **kwargs):
            with self:
                return fn(*args, **kwargs)
        return decorated

def start(engine=sqlalchemy.engine.Engine, user_context_fn=None, collect_fn=None):
    """ Create a new :class:`ProfilingSession` and call start on it.

    This is a convenience method. See :class:`ProfilingSession`'s
    constructor for documentation on the arguments.

    :return: A new :class:`ProfilingSession`
    """
    session = ProfilingSession(engine, user_context_fn, collect_fn)
    session.start()
    return session

def report(statistics, filename=None):
    """ Generate an HTML report of query statistics.
    
    :param statistics: An iterable of :class:`.QueryStats` objects over
        which to prepare a report. This is typically a list returned by
        a call to :func:`collect`.

    :param filename: If present, additionally write the html report out 
        to a file at the specified path.

    :return: The generated HTML report.
    """

    class QueryGroup:
        def __init__(self):
            self.queries = []
            self.stacks = collections.defaultdict(int)
            self.max = 0
            self.min = sys.maxint
            self.sum = 0
            self.mean = 0

        def add(self, q):
            self.queries.append(q)
            self.stacks[q.stack] += 1

            self.max = max(self.max, q.duration)
            self.min = min(self.min, q.duration)
            self.sum = self.sum + q.duration
            self.mean = self.sum / len(self.queries)

    query_groups = collections.defaultdict(QueryGroup)
    all_group = QueryGroup()

    for qstats in statistics:
        qstats.stack = ''.join(traceback.format_list(qstats.stack))

        group = query_groups[str(qstats.text)]
        group.add(qstats)
        all_group.add(qstats)

    html = mako.template.Template(
        filename = os.path.join(os.path.dirname(__file__), 
                                "templates", "report.mako")
    ).render(
        query_groups = query_groups,
        all_group = all_group,
        name = "SQLTap Profiling Report"
    )

    if filename:
        with open(filename, 'w') as f:
            f.write(html)
        
    return html

def _hotfix_dispatch_remove():
    """ The fix for this bug is in sqlalchemy 0.9.4, until then, we'll
    monkey patch SQLalchemy so that it works """
    import sqlalchemy

    if sqlalchemy.__version__ >= "0.9.4":
        return

    from sqlalchemy.event.attr import _DispatchDescriptor
    from sqlalchemy.event import registry

    def remove(self, event_key):
        target = event_key.dispatch_target
        stack = [target]
        while stack:
            cls = stack.pop(0)
            stack.extend(cls.__subclasses__())
            if cls in self._clslevel:
                self._clslevel[cls].remove(event_key._listen_fn)
        registry._removed_from_collection(event_key, self)

    _DispatchDescriptor.remove = remove

_hotfix_dispatch_remove()
