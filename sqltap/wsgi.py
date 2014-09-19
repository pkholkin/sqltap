from __future__ import absolute_import

try:
    import urllib.parse as urlparse
except ImportError:
    import urlparse
from . import sqltap
try:
    import queue
except ImportError:
    import Queue as queue

class SQLTapMiddleware(object):
    """ SQLTap dashboard middleware for WSGI applications.

    For example, if you are using Flask::

        app.wsgi_app = SQLTapMiddleware(app.wsgi_app)

    And then you can use SQLTap dashboard from ``/__sqltap__`` page (this
    path prefix can be set by ``path`` parameter).

    :param app: A WSGI application object to be wrap.
    :param dir_path: A path to the folder with reports.
    :param path: A path prefix for access. Default is `'/__sqltap__'`
    """

    def __init__(self, app, dir_path, path='/__sqltap__'):
        self.app = app
        self.path = path.rstrip('/')
        self.dir_path = dir_path
        self.on = True
        self.stats = []

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path == self.path or path == self.path + '/':
            return self.render(environ, start_response)

        def user_context_fn(*args):
            return "%s %s" % (environ['REQUEST_METHOD'], environ['PATH_INFO'])

        self.p = sqltap.ProfilingSession(user_context_fn=user_context_fn)

        self.p.start()
        res = self.app(environ, start_response)
        self.p.stop()

        stats = self.p.collect()
        total_time = 0.0

        for s in stats:
            total_time += s.duration

        prof_filename = os.path.join(self.dir_path,
                    '%06dms.%s.%s.%d.html' % (
                total_time * 1000.0,
                environ['REQUEST_METHOD'],
                environ.get('PATH_INFO').strip('/').replace('/', '.') or 'root',
                time.time()
            ))

        self.stats = stats
        sqltap.report(stats, prof_filename, "report.mako")
        return res

    def start(self):
        if not self.on:
            self.on = True
            self.profiler.start()

    def stop(self):
        if self.on:
            self.on = False
            self.profiler.stop()

    def render(self, environ, start_response):
        verb = environ.get('REQUEST_METHOD', 'GET').strip().upper()
        if verb not in ('GET', 'POST'):
            start_response('405 Method Not Allowed', [
                ('Allow', 'GET, POST'),
                ('Content-Type', 'text/plain')
            ])
            return ['405 Method Not Allowed']

        # handle on/off switch
        if verb == 'POST':
            try:
                clen = int(environ.get('CONTENT_LENGTH', '0'))
            except ValueError:
                clen = 0
            body = urlparse.parse_qs(environ['wsgi.input'].read(clen))
            clear = body.get('clear', None)
            if clear:
              del self.stats[:]
              return self.render_response(start_response)

            turn = body.get('turn', ' ')[0].strip().lower()
            if turn not in ('on', 'off'):
                start_response('400 Bad Request',
                               [('Content-Type', 'text/plain')])
                return ['400 Bad Request: parameter "turn=(on|off)" required']
            if turn == 'on':
                self.start()
            else:
                self.stop()

        return self.render_response(start_response)

    def render_response(self, start_response):
        start_response('200 OK', [('Content-Type', 'text/html')])
        html = sqltap.report(self.stats, middleware=self, template="wsgi.mako")
        return [html.encode('utf-8')]
