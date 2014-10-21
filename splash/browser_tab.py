# -*- coding: utf-8 -*-
from __future__ import absolute_import
import os
import base64
import copy
import json
import pprint
import weakref
import functools
from collections import namedtuple
from PyQt4.QtWebKit import QWebPage, QWebSettings, QWebView
from PyQt4.QtCore import (Qt, QUrl, QBuffer, QSize, QTimer, QObject,
                          pyqtSlot, QByteArray)
from PyQt4.QtGui import QPainter, QImage
from PyQt4.QtNetwork import QNetworkRequest
from twisted.internet import defer
from twisted.python import log
from splash import defaults
from splash.qtutils import qurl2ascii, OPERATION_QT_CONSTANTS
from splash.har.utils import without_private

from .qwebpage import SplashQWebPage


class BrowserTab(object):
    """
    An object for controlling a single browser tab (QWebView).

    It is created by splash.pool.Pool. Pool attaches to tab's deferred
    and waits until either a callback or an errback is called, then destroys
    a BrowserTab.
    """

    def __init__(self, network_manager, splash_proxy_factory, verbosity, render_options):
        """ Create a new browser tab. """
        self.deferred = defer.Deferred()
        self.network_manager = network_manager
        self.verbosity = verbosity
        self._uid = render_options.get_uid()
        self._closing = False
        self._default_headers = None
        self._active_timers = set()
        self._cancel_on_redirect_timers = weakref.WeakKeyDictionary()  # timer: callback
        self._js_console = None
        self._history = []

        self._init_webpage(verbosity, network_manager, splash_proxy_factory, render_options)
        self._setup_logging(verbosity)

    def _init_webpage(self, verbosity, network_manager, splash_proxy_factory, render_options):
        """ Create and initialize QWebPage and QWebView """
        self.web_page = SplashQWebPage(verbosity)
        self.web_page.setNetworkAccessManager(network_manager)
        self.web_page.splash_proxy_factory = splash_proxy_factory
        self.web_page.render_options = render_options

        self._set_default_webpage_options(self.web_page)
        self._listen_to_urlchanges()

        self.web_view = QWebView()
        self.web_view.setPage(self.web_page)
        self.web_view.setAttribute(Qt.WA_DeleteOnClose, True)

    def _set_default_webpage_options(self, web_page):
        """
        Set QWebPage options.
        TODO: allow to customize them.
        """
        settings = web_page.settings()
        settings.setAttribute(QWebSettings.JavascriptEnabled, True)
        settings.setAttribute(QWebSettings.PluginsEnabled, False)
        settings.setAttribute(QWebSettings.PrivateBrowsingEnabled, True)
        settings.setAttribute(QWebSettings.LocalStorageEnabled, True)
        settings.setAttribute(QWebSettings.LocalContentCanAccessRemoteUrls, True)
        web_page.mainFrame().setScrollBarPolicy(Qt.Vertical, Qt.ScrollBarAlwaysOff)
        web_page.mainFrame().setScrollBarPolicy(Qt.Horizontal, Qt.ScrollBarAlwaysOff)

    def _setup_logging(self, verbosity):
        """ Setup logging of various events """
        self.logger = _BrowserTabLogger(
            uid=self._uid,
            web_page=self.web_page,
            verbosity=verbosity,
        )
        self.logger.enable()

    def _listen_to_urlchanges(self):
        self.web_page.mainFrame().urlChanged.connect(self._on_url_changed)

    def return_result(self, result):
        """ Return a result to the Pool. """
        if self.result_already_returned():
            self.logger.log("error: result is already returned", min_level=1)

        self.deferred.callback(result)
        # self.deferred = None

    def return_error(self, error=None):
        """ Return an error to the Pool. """
        if self.result_already_returned():
            self.logger.log("error: result is already returned", min_level=1)
        self.deferred.errback(error)
        # self.deferred = None

    def result_already_returned(self):
        """ Return True if an error or a result is already returned to Pool """
        return self.deferred.called

    def set_default_headers(self, headers):
        """ Set default HTTP headers """
        self._default_headers = headers

    def set_images_enabled(self, enabled):
        self.web_page.settings().setAttribute(QWebSettings.AutoLoadImages, enabled)

    def set_viewport(self, size):
        """
        Set viewport size.
        If size is "full" viewport size is detected automatically.
        If can also be "<width>x<height>".
        """
        if size == 'full':
            size = self.web_page.mainFrame().contentsSize()
            if size.isEmpty():
                self.logger.log("contentsSize method doesn't work %s", min_level=1)
                size = defaults.VIEWPORT_FALLBACK

        if not isinstance(size, QSize):
            w, h = map(int, size.split('x'))
            size = QSize(w, h)

        self.web_page.setViewportSize(size)
        w, h = int(size.width()), int(size.height())
        self.logger.log("viewport size is set to %sx%s" % (w, h), min_level=2)

    def go(self, url, callback, errback, baseurl=None, http_method='GET', body=None):
        """
        Go to an URL. This is similar to entering an URL in
        address tab and pressing Enter.
        """

        # TODO / FIXME: cancel previous goto request.
        # We must call errback from previous goto because multiple
        # webpages per browser tab are not supported.
        # Does it happen automatically?

        self.store_har_timing("_onStarted")

        if baseurl:
            # If baseurl is used, we download the page manually,
            # then set its contents to the QWebPage and let it
            # download related resources and render the result.
            if http_method != 'GET':
                raise NotImplementedError()

            request = self._create_request(url)
            request.setOriginatingObject(self.web_page.mainFrame())

            # TODO / FIXME: add support for multiple replies
            # or discard/cancel previous replies
            self._reply = self.network_manager.get(request)

            cb = functools.partial(
                self._on_baseurl_request_finished,
                callback=callback,
                errback=errback,
                baseurl=baseurl,
                url=url,
            )
            self._reply.finished.connect(cb)
        else:
            cb = functools.partial(
                self._on_goto_load_finished,
                callback=callback,
                errback=errback,
            )
            self.web_page.loadFinished.connect(cb)
            self._load_url_to_mainframe(url, http_method, body)

    def stop_loading(self):
        """
        Stop loading of the current page and all pending page
        refresh/redirect requests.
        """
        self.web_view.pageAction(QWebPage.StopScheduledPageRefresh)
        self.web_view.stop()

    def _close(self):
        """ Destroy this tab """
        self._closing = True
        self.web_view.pageAction(QWebPage.StopScheduledPageRefresh)
        self.web_view.stop()
        self.web_view.close()
        self.web_page.deleteLater()
        self.web_view.deleteLater()

    def _on_baseurl_request_finished(self, callback, errback, baseurl, url):
        """
        This method is called when ``baseurl`` is used and a
        reply for the first request is received.
        """
        self.logger.log("baseurl_request_finished", min_level=2)

        cb = functools.partial(
            self._on_goto_load_finished,
            callback=callback,
            errback=errback,
        )
        self.web_page.loadFinished.connect(cb)

        baseurl = QUrl(baseurl.decode('utf8'))
        mimeType = self._reply.header(QNetworkRequest.ContentTypeHeader).toString()
        data = self._reply.readAll()
        self.web_page.mainFrame().setContent(data, mimeType, baseurl)
        if self._reply.error():
            self.logger.log("Error loading %s: %s" % (url, self._reply.errorString()), min_level=1)
        self._reply.close()
        self._reply.deleteLater()

    def _load_url_to_mainframe(self, url, http_method, body=None):
        request = self._create_request(url)
        meth = OPERATION_QT_CONSTANTS[http_method]
        if body is None:  # PyQT doesn't support body=None
            self.web_page.mainFrame().load(request, meth)
        else:
            self.web_page.mainFrame().load(request, meth, body)

    def _create_request(self, url):
        request = QNetworkRequest()
        request.setUrl(QUrl(url.decode('utf8')))
        self._set_request_headers(request, self._default_headers)
        return request

    def _on_goto_load_finished(self, ok, callback, errback):
        """
        This method is called when a QWebPage finishes loading its contents.
        """
        if self._closing:
            self.logger.log("loadFinished is ignored because BrowserTab is closing", min_level=2)
            return

        page_ok = ok and self.web_page.errorInfo is None
        maybe_redirect = not ok and self.web_page.errorInfo is None
        error_loading = ok and self.web_page.errorInfo is not None

        if maybe_redirect:
            self.logger.log("Redirect or other non-fatal error detected", min_level=2)
            # XXX: It assumes loadFinished will be called again because
            # redirect happens. If redirect is detected improperly,
            # loadFinished won't be called again, and Splash will return
            # the result only after a timeout.
            #
            # FIXME: This can happen if server returned incorrect
            # Content-Type header; there is no an additional loadFinished
            # signal in this case.
            return

        if page_ok:  # or maybe_redirect:
            self.logger.log("loadFinished: ok", min_level=2)
            callback()
        elif error_loading:
            self.logger.log("loadFinished: %s" % (str(self.web_page.errorInfo)), min_level=1)
            # XXX: maybe return a meaningful error page instead of generic
            # error message?
            errback()
            # errback(RenderError())
        else:
            self.logger.log("loadFinished: unknown error", min_level=1)
            errback()
            # errback(RenderError())

    def _set_request_headers(self, request, headers):
        """ Set HTTP headers for the request. """
        if isinstance(headers, dict):
            headers = headers.items()

        for name, value in headers or []:
            request.setRawHeader(name, value)
            if name.lower() == 'user-agent':
                self.web_page.custom_user_agent = value

    def wait(self, time_ms, callback, onredirect=None):
        """
        Wait for time_ms, then run callback.

        If onredirect is True then timer is cancelled if redirect happens.
        If onredirect is callable then timer is cancelled and this callable
        is called in case of redirect.
        """

        timer = QTimer()
        timer.setSingleShot(True)
        timer_callback = functools.partial(self._on_wait_timeout,
            timer=timer,
            callback=callback,
        )
        timer.timeout.connect(timer_callback)

        self.logger.log("waiting %sms; timer %s" % (time_ms, id(timer)), min_level=2)

        timer.start(time_ms)
        self._active_timers.add(timer)
        if onredirect:
            self._cancel_on_redirect_timers[timer] = onredirect

    def _on_wait_timeout(self, timer, callback):
        self.logger.log("wait timeout for %s" % id(timer), min_level=2)
        self._active_timers.remove(timer)
        self._cancel_on_redirect_timers.pop(timer, None)
        callback()

    def _cancel_timer(self, timer, errback=None):
        self.logger.log("cancelling timer %s" % id(timer), min_level=2)
        self._cancel_on_redirect_timers.pop(timer, None)
        self._active_timers.remove(timer)
        timer.stop()
        if callable(errback):
            errback()

    def _on_url_changed(self, url):
        # log history
        url = unicode(url.toString())
        cause_ev = self.web_page.har_log._prev_entry(url, -1)
        if cause_ev:
            self._history.append(without_private(cause_ev.data))

        # cancel all timers that should be cancelled on redirect
        for timer, onredirect in list(self._cancel_on_redirect_timers.items()):
            self._cancel_timer(timer, onredirect)

    def inject_js(self, filename):
        """
        Load JS library from file ``filename`` to the current frame.
        """

        # TODO: shouldn't it keep injected scripts after redirects/reloads?
        with open(filename, 'rb') as f:
            script = f.read().decode('utf-8')
            return self.runjs(script)

    def inject_js_libs(self, folder):
        """
        Load all JS libraries from ``folder`` folder to the current frame.
        """
        # TODO: shouldn't it keep injected scripts after redirects/reloads?
        for jsfile in os.listdir(folder):
            if jsfile.endswith('.js'):
                filename = os.path.join(folder, jsfile)
                self.inject_js(filename)

    def runjs(self, js_source):
        """
        Run JS code in page context and return the result.
        Only string results are supported.
        """
        frame = self.web_page.mainFrame()
        res = frame.evaluateJavaScript(js_source)
        return unicode(res.toString())

    def store_har_timing(self, name):
        self.web_page.har_log.store_timing(name)

    def _jsconsole_enable(self):
        # TODO: add public interface or make console available by default
        if self._js_console is not None:
            return
        self._js_console = _JavascriptConsole()
        frame = self.web_page.mainFrame()
        frame.addToJavaScriptWindowObject('console', self._js_console)

    def _jsconsole_messages(self):
        # TODO: add public interface or make console available by default
        if self._js_console is None:
            return []
        return self._js_console.messages[:]

    def html(self):
        """ Return HTML of the current main frame """
        self.logger.log("getting HTML", min_level=2)
        frame = self.web_page.mainFrame()
        result = bytes(frame.toHtml().toUtf8())
        self.store_har_timing("_onHtmlRendered")
        return result

    def png(self, width=None, height=None, b64=False):
        """ Return screenshot in PNG format """
        self.logger.log("getting PNG", min_level=2)

        image = QImage(self.web_page.viewportSize(), QImage.Format_ARGB32)
        painter = QPainter(image)
        self.web_page.mainFrame().render(painter)
        painter.end()
        self.store_har_timing("_onScreenshotPrepared")

        if width:
            image = image.scaledToWidth(width, Qt.SmoothTransformation)
        if height:
            image = image.copy(0, 0, width, height)
        b = QBuffer()
        image.save(b, "png")
        result = bytes(b.data())
        if b64:
            result = base64.b64encode(result)
        self.store_har_timing("_onPngRendered")
        return result

    def iframes_info(self, children=True, html=True):
        """ Return information about all iframes """
        self.logger.log("getting iframes", min_level=3)
        frame = self.web_page.mainFrame()
        result = self._frame_to_dict(frame, children, html)
        self.store_har_timing("_onIframesRendered")
        return result

    def har(self):
        """ Return HAR information """
        self.logger.log("getting HAR", min_level=3)
        return self.web_page.har_log.todict()

    def history(self):
        """ Return history of 'main' HTTP requests """
        self.logger.log("getting history", min_level=3)

        hist = copy.deepcopy(self._history)
        for entry in hist:
            if entry is not None:
                del entry['request']['queryString']
        return hist

    def _frame_to_dict(self, frame, children=True, html=True):
        g = frame.geometry()
        res = {
            "url": unicode(frame.url().toString()),
            "requestedUrl": unicode(frame.requestedUrl().toString()),
            "geometry": (g.x(), g.y(), g.width(), g.height()),
            "title": unicode(frame.title())
        }
        if html:
            res["html"] = unicode(frame.toHtml())

        if children:
            res["childFrames"] = [
                self._frame_to_dict(f, True, html)
                for f in frame.childFrames()
            ]
            res["frameName"] = unicode(frame.frameName())

        return res


class _JavascriptConsole(QObject):
    def __init__(self, parent=None):
        self.messages = []
        super(_JavascriptConsole, self).__init__(parent)

    @pyqtSlot(str)
    def log(self, message):
        self.messages.append(unicode(message))


class _BrowserTabLogger(object):
    """ This class logs various events that happen with QWebPage """
    def __init__(self, uid, web_page, verbosity):
        self.uid = uid
        self.web_page = web_page
        self.verbosity = verbosity

    def enable(self):
        # setup logging
        if self.verbosity >= 4:
            self.web_page.loadStarted.connect(self.on_load_started)
            self.web_page.mainFrame().loadFinished.connect(self.on_frame_load_finished)
            self.web_page.mainFrame().loadStarted.connect(self.on_frame_load_started)
            self.web_page.mainFrame().contentsSizeChanged.connect(self.on_contents_size_changed)
            # TODO: on_repaint

        if self.verbosity >= 3:
            self.web_page.mainFrame().javaScriptWindowObjectCleared.connect(self.on_javascript_window_object_cleared)
            self.web_page.mainFrame().initialLayoutCompleted.connect(self.on_initial_layout_completed)
            self.web_page.mainFrame().urlChanged.connect(self.on_url_changed)

    def on_load_started(self):
        self.log("loadStarted")

    def on_frame_load_finished(self, ok):
        self.log("mainFrame().LoadFinished %s" % ok)

    def on_frame_load_started(self):
        self.log("mainFrame().loadStarted")

    def on_contents_size_changed(self):
        self.log("mainFrame().contentsSizeChanged")

    def on_javascript_window_object_cleared(self):
        self.log("mainFrame().javaScriptWindowObjectCleared")

    def on_initial_layout_completed(self):
        self.log("mainFrame().initialLayoutCompleted")

    def on_url_changed(self, url):
        self.log("mainFrame().urlChanged %s" % qurl2ascii(url))

    def log(self, message, min_level=None):
        if min_level is not None and self.verbosity < min_level:
            return

        if isinstance(message, unicode):
            message = message.encode('unicode-escape').decode('ascii')

        message = "[%s] %s" % (self.uid, message)
        log.msg(message, system='render')
